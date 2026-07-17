from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from app.config import Settings


class AirtableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectMatch:
    record_id: str
    title: str


class AirtableClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.airtable_token}",
                "Content-Type": "application/json",
            }
        )

    def _url(self, base_id: str, table_id: str) -> str:
        return f"https://api.airtable.com/v0/{quote(base_id)}/{quote(table_id, safe='')}"

    def _record_url(self, base_id: str, table_id: str, record_id: str) -> str:
        return f"{self._url(base_id, table_id)}/{quote(record_id, safe='')}"

    def _meta_table_url(self, base_id: str, table_id: str) -> str:
        return f"https://api.airtable.com/v0/meta/bases/{quote(base_id)}/tables/{quote(table_id, safe='')}"

    def _meta_tables_url(self, base_id: str) -> str:
        return f"https://api.airtable.com/v0/meta/bases/{quote(base_id)}/tables"

    def _request(
        self,
        method: str,
        base_id: str,
        table_id: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: dict | None = None,
    ) -> dict:
        response = self.session.request(
            method,
            self._url(base_id, table_id),
            params=params,
            json=json_body,
            timeout=30,
        )
        if response.status_code >= 400:
            raise AirtableError(f"Airtable {response.status_code}: {response.text[:500]}")
        return response.json()

    def create_record(self, base_id: str, table_id: str, fields: dict) -> dict:
        return self._request(
            "POST",
            base_id,
            table_id,
            params=[("returnFieldsByFieldId", "true")],
            json_body={"fields": fields, "typecast": True},
        )

    def list_records(
        self,
        base_id: str,
        table_id: str,
        *,
        params: list[tuple[str, str]] | None = None,
    ) -> list[dict]:
        records: list[dict] = []
        offset: str | None = None
        while True:
            request_params = list(params or [])
            if offset:
                request_params.append(("offset", offset))
            payload = self._request("GET", base_id, table_id, params=request_params)
            records.extend(payload.get("records") or [])
            offset = payload.get("offset")
            if not offset:
                return records

    def update_record(self, base_id: str, table_id: str, record_id: str, fields: dict) -> dict:
        response = self.session.patch(
            self._record_url(base_id, table_id, record_id),
            params=[("returnFieldsByFieldId", "true")],
            json={"fields": fields, "typecast": True},
            timeout=30,
        )
        if response.status_code >= 400:
            raise AirtableError(f"Airtable {response.status_code}: {response.text[:500]}")
        return response.json()

    def fetch_record(self, base_id: str, table_id: str, record_id: str) -> dict:
        response = self.session.get(
            self._record_url(base_id, table_id, record_id),
            timeout=30,
        )
        if response.status_code >= 400:
            raise AirtableError(f"Airtable {response.status_code}: {response.text[:500]}")
        return response.json()

    def list_tables_metadata(self, base_id: str) -> list[dict]:
        response = self.session.get(self._meta_tables_url(base_id), timeout=30)
        if response.status_code >= 400:
            raise AirtableError(f"Airtable metadata {response.status_code}: {response.text[:500]}")
        return response.json().get("tables") or []

    def find_table_metadata(self, base_id: str, *, table_id: str = "", table_name: str = "") -> dict | None:
        for table in self.list_tables_metadata(base_id):
            if table_id and table.get("id") == table_id:
                return table
            if table_name and table.get("name") == table_name:
                return table
        return None

    def find_voice_record_by_external_id(self, external_id: str) -> dict | None:
        if not external_id.strip() or not self.settings.voice_field_external_id_query_name:
            return None
        escaped = external_id.replace("\\", "\\\\").replace("'", "\\'")
        response = self.session.get(
            self._url(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id),
            params=[
                ("pageSize", "1"),
                ("returnFieldsByFieldId", "true"),
                ("filterByFormula", f"{{{self.settings.voice_field_external_id_query_name}}} = '{escaped}'"),
            ],
            timeout=30,
        )
        if response.status_code == 422 and _is_unknown_field_text(response.text):
            return None
        if response.status_code >= 400:
            raise AirtableError(f"Airtable {response.status_code}: {response.text[:500]}")
        records = response.json().get("records") or []
        return records[0] if records else None

    def list_projects(self) -> list[ProjectMatch]:
        projects: list[ProjectMatch] = []
        offset: str | None = None
        while True:
            params = [
                ("pageSize", "100"),
                ("returnFieldsByFieldId", "true"),
                ("fields[]", self.settings.projects_field_title),
            ]
            if offset:
                params.append(("offset", offset))
            payload = self._request(
                "GET",
                self.settings.projects_base_id,
                self.settings.projects_table_id,
                params=params,
            )
            for record in payload.get("records", []):
                title = record.get("fields", {}).get(self.settings.projects_field_title)
                if isinstance(title, str) and title.strip():
                    projects.append(ProjectMatch(record_id=record["id"], title=title.strip()))
            offset = payload.get("offset")
            if not offset:
                return projects

    def find_project(self, project_title: str) -> ProjectMatch | None:
        wanted = project_title.strip().casefold()
        if not wanted:
            return None
        projects = self.list_projects()
        for project in projects:
            if project.title.casefold() == wanted:
                return project
        for project in projects:
            title = project.title.casefold()
            if wanted in title or title in wanted:
                return project
        return None

    def create_voice_inbox_record(
        self,
        structured: dict,
        raw_text: str,
        message_type: str,
        project: ProjectMatch | None,
        *,
        external_id: str | None = None,
        google_drive_url: str | None = None,
        source: str | None = None,
        processing_error: str | None = None,
    ) -> dict:
        fields = self._voice_fields(
            structured,
            raw_text,
            message_type,
            project,
            include_optional=True,
            external_id=external_id,
            google_drive_url=google_drive_url,
            source=source,
            processing_error=processing_error,
        )
        try:
            return self.create_record(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id, fields)
        except AirtableError as exc:
            minimal = self._voice_fields(
                structured,
                raw_text,
                message_type,
                project,
                include_optional=not _is_unknown_field_error(exc),
                external_id=None if _is_unknown_field_error(exc) else external_id,
                google_drive_url=None if _is_unknown_field_error(exc) else google_drive_url,
                source=None if _is_unknown_field_error(exc) else source,
                processing_error=None if _is_unknown_field_error(exc) else processing_error,
            )
            return self.create_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                minimal,
            )

    def create_mobile_inbox_record(
        self,
        *,
        title: str,
        raw_text: str,
        message_type: str,
        notes: str,
        external_id: str | None = None,
        google_drive_url: str | None = None,
        source: str | None = None,
        processing_error: str | None = None,
        processing_status: str = "New",
    ) -> dict:
        fields: dict = {}
        self._set(fields, self.settings.voice_field_title, title)
        self._set(fields, self.settings.voice_field_raw_text, raw_text)
        self._set(fields, self.settings.voice_field_type, message_type)
        self._set(fields, self.settings.voice_field_processing_status, processing_status)
        self._set(fields, self.settings.voice_field_notes, notes)
        self._set_voice_metadata(fields, external_id, google_drive_url, source, processing_error)
        try:
            return self.create_record(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id, fields)
        except AirtableError as exc:
            if not _is_unknown_field_error(exc):
                raise
            fields.pop(self.settings.voice_field_notes, None)
            self._drop_voice_metadata(fields)
            return self.create_record(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id, fields)

    def upload_voice_attachment(
        self,
        *,
        record_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict:
        if not self.settings.voice_field_attachments:
            raise AirtableError("VOICE_FIELD_ATTACHMENTS is not configured")

        upload_base_url = self.settings.airtable_upload_base_url.rstrip("/")
        url = (
            f"{upload_base_url}/"
            f"{quote(self.settings.voice_inbox_base_id, safe='')}/"
            f"{quote(record_id, safe='')}/"
            f"{quote(self.settings.voice_field_attachments, safe='')}/uploadAttachment"
        )
        response = self.session.post(
            url,
            json={
                "contentType": content_type,
                "filename": filename,
                "file": base64.b64encode(content).decode("ascii"),
            },
            timeout=60,
        )
        if response.status_code >= 400:
            raise AirtableError(f"Airtable upload {response.status_code}: {response.text[:500]}")
        return response.json()

    def mark_mobile_upload_failed(self, record_id: str, notes: str) -> dict:
        fields: dict = {}
        self._set(fields, self.settings.voice_field_processing_status, "Needs Review")
        self._set(fields, self.settings.voice_field_notes, notes)
        self._set(fields, self.settings.voice_field_processing_error, notes)
        try:
            return self.update_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                record_id,
                fields,
            )
        except AirtableError as exc:
            if not self.settings.voice_field_notes or not _is_unknown_field_error(exc):
                raise
            fields.pop(self.settings.voice_field_notes, None)
            self._drop_voice_metadata(fields)
            return self.update_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                record_id,
                fields,
            )

    def update_voice_inbox_metadata(
        self,
        record_id: str,
        *,
        external_id: str | None = None,
        google_drive_url: str | None = None,
        source: str | None = None,
        processing_error: str | None = None,
        processing_status: str | None = None,
    ) -> dict:
        fields: dict = {}
        self._set(fields, self.settings.voice_field_processing_status, processing_status)
        self._set_voice_metadata(fields, external_id, google_drive_url, source, processing_error)
        try:
            return self.update_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                record_id,
                fields,
            )
        except AirtableError as exc:
            if not _is_unknown_field_error(exc):
                raise
            self._drop_voice_metadata(fields)
            return self.update_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                record_id,
                fields,
            )

    def list_voice_records_for_processing(self, *, batch_size: int, stale_processing_seconds: int) -> list[dict]:
        status_field = self.settings.voice_field_processing_status_query_name or self.settings.voice_field_processing_status
        formula = (
            f"OR("
            f"{{{status_field}}} = 'New',"
            f"AND("
            f"{{{status_field}}} = 'Processing',"
            f"IS_BEFORE(LAST_MODIFIED_TIME({{{status_field}}}), "
            f"DATEADD(NOW(), -{max(1, stale_processing_seconds)}, 'seconds'))"
            f")"
            f")"
        )
        return self.list_records(
            self.settings.voice_inbox_base_id,
            self.settings.voice_inbox_table_id,
            params=[
                ("pageSize", str(max(1, min(batch_size, 100)))),
                ("filterByFormula", formula),
                ("sort[0][field]", status_field),
                ("sort[0][direction]", "asc"),
            ],
        )

    def list_voice_correction_candidates(self, *, page_size: int = 50) -> list[dict]:
        train_field = self.settings.voice_field_train_on_correction
        applied_field = self.settings.voice_field_training_applied
        formula = f"AND({{{train_field}}} = TRUE(), NOT({{{applied_field}}} = TRUE()))"
        return self.list_records(
            self.settings.voice_inbox_base_id,
            self.settings.voice_inbox_table_id,
            params=[("pageSize", str(max(1, min(page_size, 100)))), ("filterByFormula", formula)],
        )

    def fetch_voice_record(self, record_id: str) -> dict:
        return self.fetch_record(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id, record_id)

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        return self.update_record(
            self.settings.voice_inbox_base_id,
            self.settings.voice_inbox_table_id,
            record_id,
            fields,
        )

    def rules_table_id(self) -> str | None:
        if self.settings.voice_processor_rules_table_id:
            return self.settings.voice_processor_rules_table_id
        table = self.find_table_metadata(
            self.settings.voice_inbox_base_id,
            table_name=self.settings.voice_processor_rules_table_name,
        )
        if not table:
            return None
        table_id = table.get("id")
        return str(table_id) if table_id else None

    def list_processing_rules(self, *, active_only: bool = True, page_size: int = 100) -> list[dict]:
        table_id = self.rules_table_id()
        if not table_id:
            return []
        params: list[tuple[str, str]] = [("pageSize", str(max(1, min(page_size, 100))))]
        if active_only:
            params.append(("filterByFormula", "{Активно} = TRUE()"))
        return self.list_records(self.settings.voice_inbox_base_id, table_id, params=params)

    def create_processing_rule(self, fields: dict[str, Any]) -> dict:
        table_id = self.rules_table_id()
        if not table_id:
            raise AirtableError("Airtable processing rules table was not found")
        return self.create_record(self.settings.voice_inbox_base_id, table_id, fields)

    def update_processing_rule_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        table_id = self.rules_table_id()
        if not table_id:
            raise AirtableError("Airtable processing rules table was not found")
        return self.update_record(self.settings.voice_inbox_base_id, table_id, record_id, fields)

    def create_project_item(
        self,
        structured: dict,
        raw_text: str,
        message_type: str,
        project: ProjectMatch,
    ) -> dict:
        fields: dict = {}
        self._set(fields, self.settings.items_field_title, structured.get("title") or _first_line(raw_text))
        self._set(fields, self.settings.items_field_project, [project.record_id])
        self._set(fields, self.settings.items_field_type, structured.get("type") or message_type)
        self._set(fields, self.settings.items_field_status, "Inbox")
        self._set(fields, self.settings.items_field_priority, structured.get("priority"))
        self._set(fields, self.settings.items_field_text, structured.get("clean_text") or raw_text)
        self._set(fields, self.settings.items_field_next_action, structured.get("next_action"))
        self._set(fields, self.settings.items_field_source, "Telegram Voice Inbox")
        self._set(fields, self.settings.items_field_date, date.today().isoformat())
        try:
            return self.create_record(self.settings.projects_base_id, self.settings.items_table_id, fields)
        except AirtableError:
            minimal: dict = {}
            self._set(minimal, self.settings.items_field_title, structured.get("title") or _first_line(raw_text))
            self._set(minimal, self.settings.items_field_project, [project.record_id])
            self._set(minimal, self.settings.items_field_text, structured.get("clean_text") or raw_text)
            self._set(minimal, self.settings.items_field_source, "Telegram Voice Inbox")
            return self.create_record(self.settings.projects_base_id, self.settings.items_table_id, minimal)

    def ensure_voice_inbox_metadata_fields(self) -> dict[str, str]:
        wanted = {
            self.settings.voice_field_external_id_query_name: "singleLineText",
            "Google Drive": "url",
            "Источник": "singleLineText",
            "Ошибка обработки": "multilineText",
        }
        table = self.find_table_metadata(self.settings.voice_inbox_base_id, table_id=self.settings.voice_inbox_table_id)
        if not table:
            raise AirtableError("Voice Inbox table was not found in Airtable metadata")

        existing = {field.get("name"): field.get("id") for field in table.get("fields", [])}
        created: dict[str, str] = {}
        for name, field_type in wanted.items():
            if not name or name in existing:
                continue
            create_response = self.session.post(
                f"{self._meta_table_url(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id)}/fields",
                json={"name": name, "type": field_type},
                timeout=30,
            )
            if create_response.status_code >= 400:
                raise AirtableError(f"Airtable metadata {create_response.status_code}: {create_response.text[:500]}")
            payload = create_response.json()
            created[name] = payload.get("id") or ""
        return created

    def ensure_voice_processor_schema(self) -> dict[str, Any]:
        created_fields = self._ensure_fields(
            self.settings.voice_inbox_base_id,
            self.settings.voice_inbox_table_id,
            [
                {"name": self.settings.voice_field_ai_result_json, "type": "multilineText"},
                {
                    "name": self.settings.voice_field_ai_confidence,
                    "type": "number",
                    "options": {"precision": 2},
                },
                {"name": self.settings.voice_field_processor_version, "type": "singleLineText"},
                {"name": self.settings.voice_field_train_on_correction, "type": "checkbox"},
                {"name": self.settings.voice_field_correction_comment, "type": "multilineText"},
                {"name": self.settings.voice_field_training_applied, "type": "checkbox"},
            ],
        )
        table_id, created_table = self._ensure_processing_rules_table()
        return {
            "created_fields": created_fields,
            "rules_table_id": table_id,
            "created_rules_table": created_table,
        }

    def _ensure_fields(self, base_id: str, table_id: str, fields: list[dict[str, Any]]) -> dict[str, str]:
        table = self.find_table_metadata(base_id, table_id=table_id)
        if not table:
            raise AirtableError("Airtable table was not found in metadata")
        existing = {field.get("name"): field.get("id") for field in table.get("fields", [])}
        created: dict[str, str] = {}
        for field in fields:
            name = field.get("name")
            if not name or name in existing:
                continue
            create_response = self.session.post(
                f"{self._meta_table_url(base_id, table_id)}/fields",
                json={key: value for key, value in field.items() if value is not None},
                timeout=30,
            )
            if create_response.status_code >= 400:
                raise AirtableError(f"Airtable metadata {create_response.status_code}: {create_response.text[:500]}")
            payload = create_response.json()
            created[name] = payload.get("id") or ""
        return created

    def _ensure_processing_rules_table(self) -> tuple[str, bool]:
        table = self.find_table_metadata(
            self.settings.voice_inbox_base_id,
            table_id=self.settings.voice_processor_rules_table_id,
            table_name=self.settings.voice_processor_rules_table_name,
        )
        if table:
            table_id = str(table.get("id") or "")
            if not table_id:
                raise AirtableError("Airtable processing rules table has no id")
            self._ensure_fields(self.settings.voice_inbox_base_id, table_id, processing_rule_fields(primary=False))
            return table_id, False

        create_response = self.session.post(
            self._meta_tables_url(self.settings.voice_inbox_base_id),
            json={
                "name": self.settings.voice_processor_rules_table_name,
                "fields": processing_rule_fields(primary=True),
            },
            timeout=30,
        )
        if create_response.status_code >= 400:
            raise AirtableError(f"Airtable metadata {create_response.status_code}: {create_response.text[:500]}")
        payload = create_response.json()
        table_id = str(payload.get("id") or "")
        if not table_id:
            raise AirtableError("Airtable processing rules table create did not return id")
        return table_id, True

    def _voice_fields(
        self,
        structured: dict,
        raw_text: str,
        message_type: str,
        project: ProjectMatch | None,
        *,
        include_optional: bool,
        external_id: str | None = None,
        google_drive_url: str | None = None,
        source: str | None = None,
        processing_error: str | None = None,
    ) -> dict:
        fields: dict = {}
        self._set(fields, self.settings.voice_field_title, structured.get("title") or _first_line(raw_text))
        self._set(fields, self.settings.voice_field_summary, structured.get("summary"))
        self._set(fields, self.settings.voice_field_clean_text, structured.get("clean_text") or raw_text)
        self._set(fields, self.settings.voice_field_raw_text, raw_text)
        self._set(fields, self.settings.voice_field_processing_status, "Processed")
        if include_optional:
            self._set(fields, self.settings.voice_field_type, structured.get("type") or message_type)
            self._set(fields, self.settings.voice_field_priority, structured.get("priority"))
            self._set(fields, self.settings.voice_field_next_action, structured.get("next_action"))
            tags = structured.get("tags")
            if isinstance(tags, list) and tags:
                self._set(fields, self.settings.voice_field_tags, tags[:10])
            if project:
                self._set(fields, self.settings.voice_field_project, [project.record_id])
            self._set_voice_metadata(fields, external_id, google_drive_url, source, processing_error)
        return fields

    @staticmethod
    def _set(fields: dict, field_id: str, value: object) -> None:
        if not field_id or value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        fields[field_id] = value

    def _set_voice_metadata(
        self,
        fields: dict[str, Any],
        external_id: str | None,
        google_drive_url: str | None,
        source: str | None,
        processing_error: str | None,
    ) -> None:
        self._set(fields, self.settings.voice_field_external_id, external_id)
        self._set(fields, self.settings.voice_field_google_drive, google_drive_url)
        self._set(fields, self.settings.voice_field_source, source)
        self._set(fields, self.settings.voice_field_processing_error, processing_error)

    def _drop_voice_metadata(self, fields: dict[str, Any]) -> None:
        for field in (
            self.settings.voice_field_external_id,
            self.settings.voice_field_google_drive,
            self.settings.voice_field_source,
            self.settings.voice_field_processing_error,
        ):
            fields.pop(field, None)


def _first_line(text: str, limit: int = 90) -> str:
    collapsed = " ".join(text.strip().split())
    if not collapsed:
        return "Заметка из Telegram"
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "..."


def _is_unknown_field_error(error: AirtableError) -> bool:
    return _is_unknown_field_text(str(error))


def _is_unknown_field_text(message: str) -> bool:
    text = message.upper()
    return "UNKNOWN_FIELD" in text or "UNKNOWN FIELD" in text or "INVALID_FIELD_NAME" in text


def processing_rule_fields(*, primary: bool) -> list[dict[str, Any]]:
    fields = [
        {"name": "Правило", "type": "singleLineText"},
        {"name": "Активно", "type": "checkbox"},
        {
            "name": "Область",
            "type": "singleSelect",
            "options": {
                "choices": [
                    {"name": "Все"},
                    {"name": "Маршрутизация"},
                    {"name": "Тип"},
                    {"name": "Приоритет"},
                    {"name": "Извлечение"},
                    {"name": "Следующее действие"},
                ]
            },
        },
        {"name": "Условие", "type": "multilineText"},
        {"name": "Правильное решение", "type": "multilineText"},
        {"name": "Проект", "type": "singleLineText"},
        {"name": "Тип", "type": "singleLineText"},
        {"name": "Положительный пример", "type": "multilineText"},
        {"name": "Источник записи", "type": "singleLineText"},
        {"name": "Комментарий пользователя", "type": "multilineText"},
        {"name": "Использований", "type": "number", "options": {"precision": 0}},
        {
            "name": "Последнее использование",
            "type": "dateTime",
            "options": {
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
                "timeZone": "utc",
            },
        },
    ]
    if primary:
        return fields
    return fields[1:]
