from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    ) -> dict:
        fields = self._voice_fields(structured, raw_text, message_type, project, include_optional=True)
        try:
            return self.create_record(self.settings.voice_inbox_base_id, self.settings.voice_inbox_table_id, fields)
        except AirtableError:
            minimal = self._voice_fields(structured, raw_text, message_type, project, include_optional=False)
            return self.create_record(
                self.settings.voice_inbox_base_id,
                self.settings.voice_inbox_table_id,
                minimal,
            )

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

    def _voice_fields(
        self,
        structured: dict,
        raw_text: str,
        message_type: str,
        project: ProjectMatch | None,
        *,
        include_optional: bool,
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
        return fields

    @staticmethod
    def _set(fields: dict, field_id: str, value: object) -> None:
        if not field_id or value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        fields[field_id] = value


def _first_line(text: str, limit: int = 90) -> str:
    collapsed = " ".join(text.strip().split())
    if not collapsed:
        return "Заметка из Telegram"
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "..."
