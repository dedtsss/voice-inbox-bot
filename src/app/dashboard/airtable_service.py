from __future__ import annotations

import contextlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from app.airtable import (
    AirtableClient,
    AirtableError,
    ProjectMatch,
    _escape_airtable_formula_string,
    _format_airtable_datetime,
    find_field_metadata,
)
from app.config import Settings
from app.voice_processor import allowed_context_from_metadata, get_field

TECHNICAL_PATTERNS = ("smoke", "canary", "production test", "TG-SMOKE", "dashboard-canary")
EDITABLE_KEYS = (
    "project",
    "entry_type",
    "priority",
    "due_date",
    "amount",
    "counterparty",
    "period",
    "next_action",
    "correction_comment",
)
RECORD_ID_RE = re.compile(r"^rec[A-Za-z0-9]{8,32}$")


@dataclass(frozen=True)
class FieldBinding:
    key: str
    label: str
    read_names: tuple[str, ...]
    write_name: str
    field_type: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditableField:
    key: str
    label: str
    input_type: str
    value: Any
    options: tuple[str, ...] = ()
    max_length: int = 0


@dataclass(frozen=True)
class ValidationResult:
    fields: dict[str, Any]
    errors: dict[str, str]


class DashboardAirtableService:
    def __init__(self, settings: Settings, airtable: AirtableClient) -> None:
        self.settings = settings
        self.airtable = airtable

    def metadata(self) -> dict[str, Any]:
        table = self.airtable.find_table_metadata(
            self.settings.voice_inbox_base_id,
            table_id=self.settings.voice_inbox_table_id,
        )
        if not table:
            raise AirtableError("Voice Inbox table metadata was not found")
        projects = self.airtable.list_projects()
        allowed = allowed_context_from_metadata(table, self.settings, projects)
        bindings = build_field_bindings(self.settings, table)
        rules_table = None
        rules_table_id = self.airtable.rules_table_id()
        if rules_table_id:
            rules_table = self.airtable.find_table_metadata(self.settings.voice_inbox_base_id, table_id=rules_table_id)
        return {
            "table": table,
            "projects": projects,
            "allowed": allowed,
            "bindings": bindings,
            "rules_table": rules_table,
        }

    def overview(self) -> dict[str, Any]:
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        params = limited_fields_params(
            [
                bindings["status"],
                bindings["source"],
                bindings["entry_type"],
                bindings["project"],
                bindings["title"],
                bindings["external_id"],
                bindings["raw_text"],
                bindings["clean_text"],
                bindings["notes"],
                bindings["processing_error"],
            ]
        )
        records, limited = self.airtable.list_voice_records_limited(
            params=params,
            max_records=self.settings.dashboard_overview_max_records,
        )
        now = datetime.now(UTC)
        today_start = local_day_start(now, self.settings.timezone)
        seven_start = now - timedelta(days=7)
        cards = {
            "total": len(records),
            "limited": limited,
            "New": 0,
            "Processing": 0,
            "Processed": 0,
            "Needs Review": 0,
            "today": 0,
            "last7": 0,
            "Android": 0,
            "Telegram": 0,
            "stale": 0,
        }
        technical = 0
        for record in records:
            item = normalize_record(record, bindings, self.settings)
            status = item["status"]
            if status in {"New", "Processing", "Processed", "Needs Review"}:
                cards[status] += 1
            if item["source"] == "Android":
                cards["Android"] += 1
            elif item["source"] == "Telegram":
                cards["Telegram"] += 1
            created_at = item["created_at"]
            if created_at and created_at >= today_start:
                cards["today"] += 1
            if created_at and created_at >= seven_start:
                cards["last7"] += 1
            if item["is_stale"]:
                cards["stale"] += 1
            if is_technical_record(item):
                technical += 1
        return {
            "cards": cards,
            "technical": technical,
            "timezone": self.settings.timezone,
            "max_records": self.settings.dashboard_overview_max_records,
        }

    def list_records(self, query: dict[str, str]) -> dict[str, Any]:
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        page_size = parse_int(query.get("page_size"), default=self.settings.dashboard_page_size, minimum=1, maximum=50)
        offset = query.get("offset", "").strip()
        sort_direction = "asc" if query.get("sort") == "asc" else "desc"
        formula = build_records_formula(query, bindings, self.settings)
        params = limited_fields_params(list(bindings.values()))
        if formula:
            params.append(("filterByFormula", formula))
        if self.settings.dashboard_airtable_view.strip():
            params.append(("view", self.settings.dashboard_airtable_view.strip()))
        created_field = resolve_created_time_sort_field(self.settings, metadata["table"])
        if created_field:
            params.extend(
                [
                    ("sort[0][field]", created_field),
                    ("sort[0][direction]", sort_direction),
                ]
            )
        payload = self.airtable.list_voice_records_page(params=params, page_size=page_size, offset=offset)
        records = [normalize_record(record, bindings, self.settings) for record in payload.get("records") or []]
        if not created_field:
            records.sort(key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC), reverse=sort_direction == "desc")
        return {
            "records": records,
            "next_offset": payload.get("offset") or "",
            "next_query": next_query(query, str(payload.get("offset") or "")),
            "page_size": page_size,
            "sort": sort_direction,
            "filters": query,
            "options": filter_options(metadata),
            "created_sort_is_exact": bool(created_field or self.settings.dashboard_airtable_view.strip()),
        }

    def fetch_record(self, record_id: str) -> dict[str, Any]:
        ensure_record_id(record_id)
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        record = self.airtable.fetch_voice_record(record_id)
        item = normalize_record(record, bindings, self.settings)
        item["editable_fields"] = editable_fields(item, metadata)
        item["attachments"] = attachments_for_record(item)
        item["rules_active_supported"] = rules_active_supported(metadata.get("rules_table"))
        return item

    def update_record_from_form(self, record_id: str, form: dict[str, Any], *, train: bool) -> ValidationResult:
        ensure_record_id(record_id)
        metadata = self.metadata()
        current = self.fetch_record(record_id)
        fields, errors = validate_edit_form(form, current, metadata, self.settings)
        if errors:
            return ValidationResult(fields={}, errors=errors)
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        status_options = set(metadata["allowed"].status_options)
        if "Processed" in status_options:
            fields[bindings["status"].write_name] = "Processed"
        if train:
            fields[bindings["train_on_correction"].write_name] = True
            fields[bindings["training_applied"].write_name] = False
        else:
            fields[bindings["train_on_correction"].write_name] = False
        if not fields:
            return ValidationResult(fields={}, errors={})
        self.airtable.update_voice_record_fields(record_id, fields)
        return ValidationResult(fields=fields, errors={})

    def list_rules(self) -> dict[str, Any]:
        metadata = self.metadata()
        rules = self.airtable.list_processing_rules(active_only=False, page_size=100)
        return {
            "rules": [normalize_rule(rule) for rule in rules],
            "active_supported": rules_active_supported(metadata.get("rules_table")),
        }

    def update_rule_active(self, record_id: str, active: bool) -> None:
        ensure_record_id(record_id)
        metadata = self.metadata()
        if not rules_active_supported(metadata.get("rules_table")):
            raise AirtableError("Processing rules table does not support active toggling")
        self.airtable.update_processing_rule_fields(record_id, {"Активно": active})

    def fetch_attachment(self, record_id: str, index: int) -> tuple[str, str, bytes]:
        if index < 0 or index > 100:
            raise AirtableError("Attachment index is out of range")
        record = self.fetch_record(record_id)
        attachments = record.get("attachments") or []
        if index >= len(attachments):
            raise AirtableError("Attachment was not found")
        attachment = attachments[index]
        url = str(attachment.get("url") or "")
        if not url.startswith("https://"):
            raise AirtableError("Attachment URL is not safe")
        response = requests.get(url, timeout=self.settings.dashboard_attachment_timeout_seconds)
        if response.status_code >= 400:
            raise AirtableError(f"Attachment fetch failed with status {response.status_code}")
        filename = str(attachment.get("filename") or "attachment")
        content_type = response.headers.get("content-type") or attachment.get("type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return filename, content_type, response.content


def build_field_bindings(settings: Settings, table: dict[str, Any]) -> dict[str, FieldBinding]:
    definitions = {
        "title": ("Название", settings.voice_field_title),
        "entry_type": ("Тип", settings.voice_field_type),
        "project": ("Проект", settings.voice_field_project),
        "priority": ("Приоритет", settings.voice_field_priority),
        "due_date": ("Срок", settings.voice_field_due_date),
        "counterparty": ("Контрагент", settings.voice_field_counterparty),
        "amount": ("Сумма", settings.voice_field_amount),
        "period": ("Период", settings.voice_field_period),
        "next_action": ("Следующее действие", settings.voice_field_next_action),
        "summary": ("Краткое содержание", settings.voice_field_summary),
        "clean_text": ("Очищенный текст", settings.voice_field_clean_text),
        "raw_text": ("Исходная фраза", settings.voice_field_raw_text),
        "tags": ("Теги", settings.voice_field_tags),
        "status": ("Статус обработки", settings.voice_field_processing_status),
        "attachments": ("Attachments", settings.voice_field_attachments),
        "notes": ("Notes", settings.voice_field_notes),
        "external_id": ("External ID", settings.voice_field_external_id),
        "google_drive": ("Google Drive", settings.voice_field_google_drive),
        "source": ("Источник", settings.voice_field_source),
        "processing_error": ("Ошибка обработки", settings.voice_field_processing_error),
        "ai_result_json": ("AI результат JSON", settings.voice_field_ai_result_json),
        "ai_confidence": ("Уверенность AI", settings.voice_field_ai_confidence),
        "processor_version": ("Версия обработчика", settings.voice_field_processor_version),
        "train_on_correction": ("Обучить на исправлении", settings.voice_field_train_on_correction),
        "correction_comment": ("Комментарий к исправлению", settings.voice_field_correction_comment),
        "training_applied": ("Обучение учтено", settings.voice_field_training_applied),
    }
    bindings: dict[str, FieldBinding] = {}
    for key, (fallback_label, configured) in definitions.items():
        field = find_field_metadata(table, configured) or find_field_metadata(table, fallback_label)
        read_name = str(field.get("name") or configured or fallback_label) if field else configured or fallback_label
        field_type = str(field.get("type") or "")
        options = tuple(
            str(choice.get("name") or "").strip()
            for choice in ((field or {}).get("options") or {}).get("choices") or []
            if str(choice.get("name") or "").strip()
        )
        bindings[key] = FieldBinding(
            key=key,
            label=fallback_label,
            read_names=tuple(dict.fromkeys(name for name in (configured, read_name, fallback_label) if name)),
            write_name=configured or read_name,
            field_type=field_type,
            options=options,
        )
    return bindings


def limited_fields_params(bindings: list[FieldBinding]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    params: list[tuple[str, str]] = []
    for binding in bindings:
        name = next((candidate for candidate in binding.read_names if candidate), "")
        if name and name not in seen:
            params.append(("fields[]", name))
            seen.add(name)
    return params


def normalize_record(record: dict[str, Any], bindings: dict[str, FieldBinding], settings: Settings) -> dict[str, Any]:
    fields = record.get("fields") or {}
    created_at = parse_airtable_datetime(record.get("createdTime"))
    processed_at = processed_time(fields, bindings)
    item: dict[str, Any] = {
        "id": str(record.get("id") or ""),
        "created_at": created_at,
        "created_local": format_local_datetime(created_at, settings.timezone),
        "processed_at": processed_at,
        "processed_local": format_local_datetime(processed_at, settings.timezone),
        "fields": fields,
        "ai_json_pretty": pretty_json(field_value(fields, bindings, "ai_result_json")),
    }
    for key in bindings:
        item[key] = field_value(fields, bindings, key)
    item["title"] = str(item.get("title") or item.get("raw_text") or "Без названия")[:160]
    item["status"] = str(item.get("status") or "")
    item["source"] = str(item.get("source") or "")
    item["entry_type"] = str(item.get("entry_type") or "")
    item["age_minutes"] = age_minutes(created_at)
    item["age_state"] = age_state(item["status"], item["age_minutes"])
    item["is_stale"] = item["age_state"] == "stale"
    item["is_technical"] = is_technical_record(item)
    return item


def field_value(fields: dict[str, Any], bindings: dict[str, FieldBinding], key: str) -> Any:
    binding = bindings[key]
    return get_field(fields, *binding.read_names)


def filter_options(metadata: dict[str, Any]) -> dict[str, list[str]]:
    allowed = metadata["allowed"]
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    return {
        "statuses": sorted(allowed.status_options or set(bindings["status"].options), key=str.casefold),
        "sources": ["Android", "Telegram"],
        "projects": [project.title for project in allowed.projects] or list(bindings["project"].options),
        "types": sorted(allowed.type_options or set(bindings["entry_type"].options), key=str.casefold),
        "priorities": sorted(allowed.priority_options or set(bindings["priority"].options), key=str.casefold),
    }


def build_records_formula(query: dict[str, str], bindings: dict[str, FieldBinding], settings: Settings) -> str:
    parts: list[str] = []
    exact_filters = {
        "status": "status",
        "source": "source",
        "project": "project",
        "entry_type": "entry_type",
    }
    for query_key, binding_key in exact_filters.items():
        value = str(query.get(query_key) or "").strip()
        if value:
            parts.append(equals_formula(bindings[binding_key], value))
    search = str(query.get("q") or "").strip()
    if search:
        searchable = [
            bindings[key]
            for key in ("title", "raw_text", "clean_text", "summary", "next_action", "external_id", "notes")
        ]
        escaped = _escape_airtable_formula_string(search.casefold())
        parts.append(
            "OR("
            + ",".join(f"SEARCH('{escaped}', LOWER({{{binding.read_names[-1]}}} & ''))" for binding in searchable)
            + ")"
        )
    period = str(query.get("period") or "").strip()
    period_formula = period_filter_formula(period, settings)
    if period_formula:
        parts.append(period_formula)
    if str(query.get("technical") or "") == "1":
        parts.append(technical_formula(bindings))
    if str(query.get("queue") or "") == "1" and not str(query.get("status") or "").strip():
        parts.append(
            "OR("
            + equals_formula(bindings["status"], "New")
            + ","
            + equals_formula(bindings["status"], "Processing")
            + ")"
        )
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "AND(" + ",".join(parts) + ")"


def equals_formula(binding: FieldBinding, value: str) -> str:
    field_name = binding.read_names[-1]
    return f"{{{field_name}}} = '{_escape_airtable_formula_string(value)}'"


def period_filter_formula(period: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    if period == "today":
        start = local_day_start(now, settings.timezone)
        end = start + timedelta(days=1)
    elif period == "7d":
        start = now - timedelta(days=7)
        end = None
    elif period == "30d":
        start = now - timedelta(days=30)
        end = None
    else:
        return ""
    start_part = f"IS_AFTER(CREATED_TIME(), DATETIME_PARSE('{_format_airtable_datetime(start)}'))"
    if not end:
        return start_part
    end_part = f"IS_BEFORE(CREATED_TIME(), DATETIME_PARSE('{_format_airtable_datetime(end)}'))"
    return f"AND({start_part},{end_part})"


def technical_formula(bindings: dict[str, FieldBinding]) -> str:
    fields = [bindings[key] for key in ("title", "raw_text", "clean_text", "external_id", "notes")]
    checks: list[str] = []
    for pattern in TECHNICAL_PATTERNS:
        escaped = _escape_airtable_formula_string(pattern.casefold())
        checks.extend(f"SEARCH('{escaped}', LOWER({{{binding.read_names[-1]}}} & ''))" for binding in fields)
    return "OR(" + ",".join(checks) + ")"


def resolve_created_time_sort_field(settings: Settings, table: dict[str, Any]) -> str:
    configured = settings.dashboard_created_time_field.strip()
    if configured and find_field_metadata(table, configured):
        return configured
    return ""


def local_day_start(now_utc: datetime, timezone_name: str) -> datetime:
    zone = timezone_or_default(timezone_name)
    local_now = now_utc.astimezone(zone)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone).astimezone(UTC)


def timezone_or_default(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def parse_airtable_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    return None


def format_local_datetime(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone_or_default(timezone_name)).strftime("%d.%m.%Y %H:%M")


def age_minutes(created_at: datetime | None) -> int | None:
    if created_at is None:
        return None
    return max(0, int((datetime.now(UTC) - created_at).total_seconds() // 60))


def age_state(status: str, minutes: int | None) -> str:
    if status not in {"New", "Processing"} or minutes is None:
        return "done"
    if minutes > 15:
        return "stale"
    if minutes >= 5:
        return "delay"
    return "fresh"


def processed_time(fields: dict[str, Any], bindings: dict[str, FieldBinding]) -> datetime | None:
    raw = field_value(fields, bindings, "ai_result_json")
    if not isinstance(raw, str) or not raw.strip():
        return None
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(raw)
        if isinstance(payload, dict):
            processor = payload.get("processor")
            if isinstance(processor, dict):
                return parse_airtable_datetime(processor.get("processed_at"))
    return None


def pretty_json(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if not isinstance(value, str) or not value.strip():
        return ""
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(value)
        return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    return value


def is_technical_record(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "raw_text", "clean_text", "external_id", "notes")
    ).casefold()
    return any(pattern.casefold() in haystack for pattern in TECHNICAL_PATTERNS)


def editable_fields(item: dict[str, Any], metadata: dict[str, Any]) -> list[EditableField]:
    options = filter_options(metadata)
    return [
        EditableField("project", "Проект", "select", item.get("project") or "", tuple(options["projects"])),
        EditableField("entry_type", "Тип", "select", item.get("entry_type") or "", tuple(options["types"])),
        EditableField("priority", "Приоритет", "select", item.get("priority") or "", tuple(options["priorities"])),
        EditableField("due_date", "Срок", "date", item.get("due_date") or ""),
        EditableField("amount", "Сумма", "number", item.get("amount") if item.get("amount") is not None else ""),
        EditableField("counterparty", "Контрагент", "text", item.get("counterparty") or "", max_length=300),
        EditableField("period", "Период", "text", item.get("period") or "", max_length=300),
        EditableField("next_action", "Следующее действие", "textarea", item.get("next_action") or "", max_length=1000),
        EditableField("correction_comment", "Комментарий к исправлению", "textarea", item.get("correction_comment") or "", max_length=2000),
    ]


def validate_edit_form(
    form: dict[str, Any],
    current: dict[str, Any],
    metadata: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, str]]:
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    allowed = filter_options(metadata)
    errors: dict[str, str] = {}
    fields: dict[str, Any] = {}
    for key in form:
        if key not in EDITABLE_KEYS and key not in {"csrf_token", "action"}:
            errors[key] = "Unknown editable field"
    set_select(fields, errors, bindings["project"], "project", form, allowed["projects"], current)
    set_select(fields, errors, bindings["entry_type"], "entry_type", form, allowed["types"], current)
    set_select(fields, errors, bindings["priority"], "priority", form, allowed["priorities"], current)
    set_date(fields, errors, bindings["due_date"], "due_date", form, current)
    set_decimal(fields, errors, bindings["amount"], "amount", form, current)
    set_text(fields, errors, bindings["counterparty"], "counterparty", form, current, max_length=300)
    set_text(fields, errors, bindings["period"], "period", form, current, max_length=300)
    set_text(fields, errors, bindings["next_action"], "next_action", form, current, max_length=1000)
    set_text(fields, errors, bindings["correction_comment"], "correction_comment", form, current, max_length=2000)
    return fields, errors


def set_select(
    fields: dict[str, Any],
    errors: dict[str, str],
    binding: FieldBinding,
    key: str,
    form: dict[str, Any],
    allowed: list[str],
    current: dict[str, Any],
) -> None:
    if key not in form:
        return
    value = clean_form_text(form.get(key), limit=120)
    if value and allowed and value not in allowed:
        errors[key] = "Недопустимое значение"
        return
    add_if_changed(fields, binding, value or None, current.get(key))


def set_date(
    fields: dict[str, Any],
    errors: dict[str, str],
    binding: FieldBinding,
    key: str,
    form: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if key not in form:
        return
    value = clean_form_text(form.get(key), limit=20)
    if value:
        with contextlib.suppress(ValueError):
            date.fromisoformat(value)
            add_if_changed(fields, binding, value, current.get(key))
            return
        errors[key] = "Дата должна быть в формате YYYY-MM-DD"
        return
    add_if_changed(fields, binding, None, current.get(key))


def set_decimal(
    fields: dict[str, Any],
    errors: dict[str, str],
    binding: FieldBinding,
    key: str,
    form: dict[str, Any],
    current: dict[str, Any],
) -> None:
    if key not in form:
        return
    raw = str(form.get(key) or "").strip().replace(",", ".")
    if not raw:
        add_if_changed(fields, binding, None, current.get(key))
        return
    try:
        decimal = Decimal(raw)
    except InvalidOperation:
        errors[key] = "Сумма должна быть числом"
        return
    if decimal > Decimal("999999999999") or decimal < Decimal("-999999999999"):
        errors[key] = "Сумма вне допустимого диапазона"
        return
    value = float(decimal)
    add_if_changed(fields, binding, value, current.get(key))


def set_text(
    fields: dict[str, Any],
    errors: dict[str, str],
    binding: FieldBinding,
    key: str,
    form: dict[str, Any],
    current: dict[str, Any],
    *,
    max_length: int,
) -> None:
    if key not in form:
        return
    value = clean_form_text(form.get(key), limit=max_length)
    if len(str(form.get(key) or "")) > max_length:
        errors[key] = f"Максимальная длина: {max_length}"
        return
    add_if_changed(fields, binding, value or None, current.get(key))


def clean_form_text(value: Any, *, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def add_if_changed(fields: dict[str, Any], binding: FieldBinding, value: Any, current: Any) -> None:
    if normalize_compare(value) != normalize_compare(current):
        fields[binding.write_name] = value


def normalize_compare(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 4)
    return str(value).strip()


def attachments_for_record(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("attachments")
    if not isinstance(raw, list):
        return []
    attachments: list[dict[str, Any]] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or "attachment")
        content_type = str(attachment.get("type") or mimetypes.guess_type(filename)[0] or "")
        attachments.append(
            {
                "filename": filename,
                "type": content_type,
                "size": attachment.get("size"),
                "kind": media_kind(content_type, filename),
                "url": attachment.get("url"),
            }
        )
    return attachments


def media_kind(content_type: str, filename: str) -> str:
    guessed = content_type or mimetypes.guess_type(filename)[0] or ""
    if guessed.startswith("image/"):
        return "image"
    if guessed.startswith("audio/"):
        return "audio"
    if guessed.startswith("video/"):
        return "video"
    return "file"


def normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    fields = rule.get("fields") or {}
    return {
        "id": str(rule.get("id") or ""),
        "name": fields.get("Правило") or "Без названия",
        "active": bool(fields.get("Активно")),
        "area": fields.get("Область") or "",
        "condition": fields.get("Условие") or "",
        "decision": fields.get("Правильное решение") or "",
        "project": fields.get("Проект") or "",
        "entry_type": fields.get("Тип") or "",
        "uses": fields.get("Использований"),
        "last_used": fields.get("Последнее использование") or "",
    }


def rules_active_supported(table: dict[str, Any] | None) -> bool:
    return bool(table and find_field_metadata(table, "Активно"))


def parse_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    with contextlib.suppress(TypeError, ValueError):
        parsed = int(value)
        return max(minimum, min(maximum, parsed))
    return max(minimum, min(maximum, default))


def next_query(query: dict[str, str], offset: str) -> str:
    if not offset:
        return ""
    cleaned = {key: value for key, value in query.items() if key != "offset" and value}
    cleaned["offset"] = offset
    return urlencode(cleaned)


def ensure_record_id(record_id: str) -> None:
    if not RECORD_ID_RE.match(record_id or ""):
        raise AirtableError("Invalid Airtable record id")


def safe_content_disposition(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip() or "attachment"
    return f"inline; filename*=UTF-8''{quote(safe)}"
