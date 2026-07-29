from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import re
from collections import Counter
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
from app.select_options import canonical_select_options, canonical_select_value, clean_select_value
from app.voice_processor import allowed_context_from_metadata, get_field, is_insufficient_quota

TECHNICAL_PATTERNS = ("smoke", "canary", "production test", "TG-SMOKE", "dashboard-canary")
EMPTY_SOURCE_QUERY_VALUE = "__empty__"
EMPTY_SOURCE_LABEL = "Источник не указан"
CONTENT_MEDIA_TYPE_KEYS = {"text", "voice", "photo", "video", "file", "mixed", "audio"}
KANBAN_MOVE_STATUSES = (
    "Awaiting Subscription",
    "Processing",
    "Processing Disabled",
    "Needs Review",
    "Processed",
)
TRAINING_STATUSES = ("Pending", "In Progress", "Completed", "Skipped", "Auto Confirmed")
TRAINING_SCOPE_OPTIONS = ("Личное", "Рабочее", "Смешанное", "Не уверен")
TRAINING_ENTRY_TYPE_OPTIONS = (
    "Задача",
    "Заметка",
    "Идея",
    "Напоминание",
    "Документ",
    "Финансовая запись",
    "Контакт",
    "Событие",
    "Другое",
)
TRAINING_FORM_KEYS = {
    "csrf_token",
    "scope",
    "project",
    "life_area",
    "entry_type",
    "next_action",
    "priority",
    "due_date",
    "category",
    "subcategory",
    "tags",
}
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
SORTING_MODE_AIRTABLE_VIEW = "airtable_view"
SORTING_MODE_AIRTABLE_FIELD = "airtable_field"
SORTING_MODE_PAGE_ONLY_UNSAFE = "page_only_unsafe"
SORT_COMPATIBLE_FIELD_TYPES = {
    "singleLineText",
    "email",
    "url",
    "phoneNumber",
    "singleSelect",
    "date",
    "dateTime",
    "createdTime",
    "lastModifiedTime",
    "number",
    "currency",
    "percent",
}


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
    helper_text: str = ""
    placeholder: str = ""
    legacy_option: str = ""


@dataclass(frozen=True)
class ValidationResult:
    fields: dict[str, Any]
    errors: dict[str, str]


@dataclass(frozen=True)
class RuleProposal:
    key: str
    label: str
    condition: str
    decision: dict[str, Any]
    count: int


@dataclass(frozen=True)
class SortingConfig:
    mode: str
    direction: str
    params: tuple[tuple[str, str], ...] = ()
    is_exact: bool = False


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
        taxonomy_table = self.airtable.find_table_metadata(
            self.settings.voice_inbox_base_id,
            table_name=self.settings.voice_training_taxonomy_table_name,
        )
        return {
            "table": table,
            "projects": projects,
            "allowed": allowed,
            "bindings": bindings,
            "rules_table": rules_table,
            "taxonomy_table": taxonomy_table,
        }

    def overview(self) -> dict[str, Any]:
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        params = limited_fields_params(
            [
                bindings["status"],
                bindings["processing_route"],
                bindings["source"],
                bindings["entry_type"],
                bindings["project"],
                bindings["title"],
                bindings["external_id"],
                bindings["raw_text"],
                bindings["clean_text"],
                bindings["notes"],
                bindings["processing_error"],
                bindings["training_status"],
                bindings["scope"],
                bindings["life_area"],
                bindings["category"],
                bindings["subcategory"],
                bindings["training_confirmed_at"],
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
            "Awaiting Subscription": 0,
            "Processing Disabled": 0,
            "today": 0,
            "last7": 0,
            "Android": 0,
            "Telegram": 0,
            "Web": 0,
            "stale": 0,
            "errors": 0,
            "training_requested": 0,
            "training_pending": 0,
            "training_applied": 0,
            "rules_total": 0,
            "rules_active": 0,
            "training_queue": 0,
            "training_completed_today": 0,
            "training_auto_confirmed": 0,
            "training_needs_clarification": 0,
            "training_rules_proposed": 0,
            "ai_confidence_avg": None,
        }
        technical = 0
        status_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        project_counts: Counter[str] = Counter()
        type_counts: Counter[str] = Counter()
        priority_counts: Counter[str] = Counter()
        confidence_values: list[float] = []
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            item = normalize_record(record, bindings, self.settings)
            normalized_records.append(item)
            status = item["status"]
            status_counts[status or "Без статуса"] += 1
            if status in {
                "New",
                "Processing",
                "Processed",
                "Needs Review",
                "Awaiting Subscription",
                "Processing Disabled",
            }:
                cards[status] += 1
            source = item["source"].strip()
            source_counts[source] += 1
            if source in cards:
                cards[source] += 1
            project_counts[item.get("project") or "Без проекта"] += 1
            type_counts[item.get("entry_type") or "Без типа"] += 1
            priority_counts[item.get("priority") or "Без приоритета"] += 1
            created_at = item["created_at"]
            if created_at and created_at >= today_start:
                cards["today"] += 1
            if created_at and created_at >= seven_start:
                cards["last7"] += 1
            if item["is_stale"]:
                cards["stale"] += 1
            if is_technical_record(item):
                technical += 1
            if item.get("processing_error"):
                cards["errors"] += 1
            train_requested = truthy_value(item.get("train_on_correction"))
            train_applied = truthy_value(item.get("training_applied"))
            if train_requested:
                cards["training_requested"] += 1
            if train_requested and not train_applied:
                cards["training_pending"] += 1
            if train_applied:
                cards["training_applied"] += 1
            training_status = effective_training_status(item)
            if training_status in {"Pending", "In Progress"} and training_queue_eligible(item, self.settings):
                cards["training_queue"] += 1
            if training_status == "Auto Confirmed":
                cards["training_auto_confirmed"] += 1
            confirmed_at = parse_airtable_datetime(item.get("training_confirmed_at"))
            if training_status == "Completed" and confirmed_at and confirmed_at >= today_start:
                cards["training_completed_today"] += 1
            if training_status in {"Pending", "In Progress"} and training_needs_clarification(item):
                cards["training_needs_clarification"] += 1
            with contextlib.suppress(TypeError, ValueError):
                confidence = float(item.get("ai_confidence"))
                if 1 < confidence <= 100:
                    confidence = confidence / 100
                if 0 <= confidence <= 1:
                    confidence_values.append(confidence)
        rules = self.safe_list_rules()
        cards["rules_total"] = len(rules)
        cards["rules_active"] = sum(1 for rule in rules if normalize_rule(rule)["active"])
        if confidence_values:
            cards["ai_confidence_avg"] = round(sum(confidence_values) / len(confidence_values), 2)
        cards["training_rules_proposed"] = len(self.rule_proposals(metadata=metadata, records=normalized_records))
        recent_records = sorted(
            normalized_records,
            key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[:8]
        return {
            "cards": cards,
            "technical": technical,
            "status_counts": count_items(status_counts),
            "source_counts": source_count_items(source_counts),
            "project_counts": count_items(project_counts),
            "type_counts": count_items(type_counts),
            "priority_counts": count_items(priority_counts),
            "recent_records": recent_records,
            "timezone": self.settings.timezone,
            "max_records": self.settings.dashboard_overview_max_records,
        }

    def list_records(self, query: dict[str, str]) -> dict[str, Any]:
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        page_size = parse_int(query.get("page_size"), default=self.settings.dashboard_page_size, minimum=1, maximum=50)
        offset = query.get("offset", "").strip()
        sorting = resolve_sorting_config(self.settings, metadata["table"], query.get("sort"))
        formula = build_records_formula(query, bindings, self.settings)
        params = limited_fields_params(list(bindings.values()))
        if formula:
            params.append(("filterByFormula", formula))
        params.extend(sorting.params)
        payload = self.airtable.list_voice_records_page(params=params, page_size=page_size, offset=offset)
        records = [normalize_record(record, bindings, self.settings) for record in payload.get("records") or []]
        if sorting.mode == SORTING_MODE_PAGE_ONLY_UNSAFE:
            records.sort(
                key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC),
                reverse=sorting.direction == "desc",
            )
        return {
            "records": records,
            "next_offset": payload.get("offset") or "",
            "next_query": next_query(query, str(payload.get("offset") or "")),
            "view_query": view_query(query),
            "page_size": page_size,
            "sort": sorting.direction,
            "filters": query,
            "options": filter_options(metadata),
            "created_sort_is_exact": sorting.is_exact,
            "sorting_mode": sorting.mode,
        }

    def kanban(self, query: dict[str, str]) -> dict[str, Any]:
        kanban_query = dict(query)
        kanban_query.setdefault("page_size", "50")
        data = self.list_records(kanban_query)
        columns = [
            {
                "key": "subscription",
                "title": "Ожидают ChatGPT",
                "status": "Awaiting Subscription",
                "hint": "Очередь подписки ChatGPT в Airtable и Google Drive.",
                "records": [],
            },
            {
                "key": "processing",
                "title": "OpenAI API",
                "status": "Processing",
                "hint": "Новые или занятые автоматической обработкой OpenAI API.",
                "records": [],
            },
            {
                "key": "disabled",
                "title": "Отключено",
                "status": "Processing Disabled",
                "hint": "Оригиналы сохранены, AI-обработка не выполняется.",
                "records": [],
            },
            {
                "key": "review",
                "title": "Нужна проверка",
                "status": "Needs Review",
                "hint": "Низкая уверенность, ошибки или ручная проверка.",
                "records": [],
            },
            {
                "key": "done",
                "title": "Готово",
                "status": "Processed",
                "hint": "Обработано и готово к использованию.",
                "records": [],
            },
            {
                "key": "training",
                "title": "Обучение",
                "status": "",
                "hint": "Исправления, ожидающие учета processor.",
                "records": [],
            },
        ]
        by_key = {column["key"]: column for column in columns}
        for record in data["records"]:
            if truthy_value(record.get("train_on_correction")) and not truthy_value(record.get("training_applied")):
                by_key["training"]["records"].append(record)
                continue
            status = record.get("status")
            if status == "Awaiting Subscription":
                by_key["subscription"]["records"].append(record)
            elif status in {"New", "Processing"}:
                by_key["processing"]["records"].append(record)
            elif status == "Processing Disabled":
                by_key["disabled"]["records"].append(record)
            elif status == "Needs Review":
                by_key["review"]["records"].append(record)
            elif status == "Processed":
                by_key["done"]["records"].append(record)
            else:
                by_key["subscription"]["records"].append(record)
        data["columns"] = columns
        return data

    def review_records(self, query: dict[str, str]) -> dict[str, Any]:
        review_query = dict(query)
        review_query["status"] = "Needs Review"
        data = self.list_records(review_query)
        metadata = self.metadata()
        for record in data["records"]:
            record["editable_fields"] = editable_fields(record, metadata)
        return data

    def fetch_record(self, record_id: str) -> dict[str, Any]:
        ensure_record_id(record_id)
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        try:
            record = self.airtable.fetch_voice_record(record_id)
        except KeyError as exc:
            raise AirtableError("Voice Inbox record was not found") from exc
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

    def move_kanban_record(self, record_id: str, status: str) -> dict[str, Any]:
        """Move one Kanban card by changing only its processing status."""
        ensure_record_id(record_id)
        target_status = str(status or "").strip()
        if target_status not in KANBAN_MOVE_STATUSES:
            raise ValueError("Unsupported Kanban status")
        metadata = self.metadata()
        binding: FieldBinding = metadata["bindings"]["status"]
        if binding.options and target_status not in binding.options:
            raise ValueError("Kanban status is not available in Airtable")
        try:
            updated = self.airtable.update_voice_record_fields(record_id, {binding.write_name: target_status})
        except KeyError as exc:
            raise AirtableError("Voice Inbox record was not found") from exc
        return normalize_record(updated, metadata["bindings"], self.settings)

    def list_rules(self) -> dict[str, Any]:
        metadata = self.metadata()
        rules = self.airtable.list_processing_rules(active_only=False, page_size=100)
        return {
            "rules": [normalize_rule(rule) for rule in rules],
            "active_supported": rules_active_supported(metadata.get("rules_table")),
        }

    def learning_dashboard(self, query: dict[str, str] | None = None) -> dict[str, Any]:
        query = dict(query or {})
        rules_data = self.list_rules()
        overview = self.overview()
        queue_data = self.training_queue(query)
        structure = self.learning_structure()
        proposals = self.rule_proposals()
        recent_cases = [
            record
            for record in overview["recent_records"]
            if truthy_value(record.get("train_on_correction"))
            or truthy_value(record.get("training_applied"))
            or effective_training_status(record) in {"Completed", "Auto Confirmed"}
            or record.get("correction_comment")
        ][:6]
        return {
            "active_tab": query.get("tab") if query.get("tab") in {"queue", "session", "rules", "structure"} else "queue",
            "rules": rules_data["rules"],
            "active_supported": rules_data["active_supported"],
            "cards": overview["cards"],
            "queue": queue_data,
            "recent_cases": recent_cases,
            "project_counts": overview["project_counts"],
            "type_counts": overview["type_counts"],
            "proposed_rules": proposals,
            "structure": structure,
            "cutoff": self.settings.voice_training_created_after or "",
            "batch_limit": max(1, min(self.settings.voice_training_batch_limit, 20)),
        }

    def training_queue(self, query: dict[str, str] | None = None) -> dict[str, Any]:
        query = dict(query or {})
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        backlog = str(query.get("backlog") or "") == "1"
        configured_limit = self.settings.voice_training_backlog_limit if backlog else self.settings.voice_training_queue_limit
        page_size = parse_int(query.get("page_size"), default=configured_limit, minimum=1, maximum=min(100, max(1, configured_limit)))
        offset = query.get("offset", "").strip()
        sorting = resolve_sorting_config(self.settings, metadata["table"], query.get("sort"))
        formula = build_training_queue_formula(query, bindings, self.settings, include_cutoff=not backlog)
        params = limited_fields_params([bindings[key] for key in training_record_binding_keys()])
        if formula:
            params.append(("filterByFormula", formula))
        params.extend(sorting.params)
        payload = self.airtable.list_voice_records_page(params=params, page_size=page_size, offset=offset)
        records = [
            item
            for item in (normalize_record(record, bindings, self.settings) for record in payload.get("records") or [])
            if training_queue_eligible(item, self.settings, include_cutoff=not backlog)
            and training_query_matches(item, query)
        ]
        if sorting.mode == SORTING_MODE_PAGE_ONLY_UNSAFE:
            records.sort(
                key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC),
                reverse=sorting.direction == "desc",
            )
        in_progress = [record for record in records if effective_training_status(record) == "In Progress"]
        return {
            "records": records,
            "in_progress": in_progress,
            "next_offset": payload.get("offset") or "",
            "next_query": next_query(query, str(payload.get("offset") or "")),
            "view_query": view_query(query),
            "filters": query,
            "options": training_filter_options(metadata, self.settings),
            "page_size": page_size,
            "backlog": backlog,
            "created_sort_is_exact": sorting.is_exact,
            "sorting_mode": sorting.mode,
        }

    def start_training_session(self, query: dict[str, str] | None = None, *, mark_in_progress: bool = True) -> str:
        queue = self.training_queue(query)
        records = queue["in_progress"] or queue["records"]
        if not records:
            return ""
        record = records[0]
        if mark_in_progress and effective_training_status(record) != "In Progress":
            bindings: dict[str, FieldBinding] = self.metadata()["bindings"]
            self.airtable.update_voice_record_fields(record["id"], {bindings["training_status"].write_name: "In Progress"})
        return str(record["id"])

    def learning_session(self, record_id: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        record = self.fetch_record(record_id)
        metadata = self.metadata()
        queue_data = self.training_queue(query)
        queue_ids = [item["id"] for item in queue_data["records"]]
        position = queue_ids.index(record_id) + 1 if record_id in queue_ids else 1
        previous_id = queue_ids[position - 2] if position > 1 and position - 2 < len(queue_ids) else ""
        next_id = queue_ids[position] if position < len(queue_ids) else ""
        return {
            "record": record,
            "options": training_form_options(metadata, self.settings),
            "questions": question_keys_for_training(record.get("scope") or infer_scope_from_record(record)),
            "ai_proposal": ai_proposal_for_record(record),
            "similar_records": self.similar_records(record, queue_data["records"]),
            "progress": {
                "position": position,
                "total": max(len(queue_ids), position),
                "previous_id": previous_id,
                "next_id": next_id,
            },
            "queue": queue_data,
        }

    def complete_training_record(self, record_id: str, form: dict[str, Any]) -> ValidationResult:
        ensure_record_id(record_id)
        metadata = self.metadata()
        current = self.fetch_record(record_id)
        fields, errors = validate_training_form(form, current, metadata, self.settings)
        if errors:
            return ValidationResult(fields={}, errors=errors)
        self.airtable.update_voice_record_fields(record_id, fields)
        return ValidationResult(fields=fields, errors={})

    def skip_training_record(self, record_id: str) -> None:
        ensure_record_id(record_id)
        bindings: dict[str, FieldBinding] = self.metadata()["bindings"]
        self.airtable.update_voice_record_fields(
            record_id,
            {
                bindings["training_status"].write_name: "Skipped",
                bindings["training_confirmed_at"].write_name: datetime.now(UTC).isoformat(),
            },
        )

    def batch_apply_training(self, source_record_id: str, selected_record_ids: list[str]) -> ValidationResult:
        ensure_record_id(source_record_id)
        limit = max(1, min(self.settings.voice_training_batch_limit, 20))
        cleaned = list(dict.fromkeys(record_id for record_id in selected_record_ids if record_id))
        errors: dict[str, str] = {}
        if not cleaned:
            errors["record_ids"] = "Выберите хотя бы одну запись"
        if len(cleaned) > limit:
            errors["record_ids"] = f"Максимум записей за раз: {limit}"
        for record_id in cleaned:
            with contextlib.suppress(AirtableError):
                ensure_record_id(record_id)
                continue
            errors[record_id] = "Недопустимый record ID"
        source = self.fetch_record(source_record_id)
        answers = parse_training_answers(source.get("training_answers_json"))
        if not answers:
            errors["source_record_id"] = "У исходной записи нет сохраненной классификации"
        if errors:
            return ValidationResult(fields={}, errors=errors)

        metadata = self.metadata()
        updated: dict[str, Any] = {}
        for record_id in cleaned:
            target = self.fetch_record(record_id)
            if not training_queue_eligible(target, self.settings, include_cutoff=False):
                errors[record_id] = "Запись не входит в безопасную очередь обучения"
                continue
            fields = fields_from_training_answers(
                answers,
                target,
                metadata,
                self.settings,
                status="Completed",
                applied_from=source_record_id,
            )
            self.airtable.update_voice_record_fields(record_id, fields)
            updated[record_id] = fields
        if errors:
            return ValidationResult(fields={}, errors=errors)
        return ValidationResult(fields=updated, errors={})

    def similar_records(self, record: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates[: max(1, self.settings.voice_training_queue_limit)]:
            if candidate.get("id") == record.get("id"):
                continue
            score = record_similarity(record, candidate)
            if score >= 0.18:
                item = dict(candidate)
                item["similarity_percent"] = round(score * 100)
                scored.append((score, item))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[: max(1, min(self.settings.voice_training_similarity_limit, 10))]]

    def rule_proposals(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        metadata = metadata or self.metadata()
        if records is None:
            bindings: dict[str, FieldBinding] = metadata["bindings"]
            params = limited_fields_params([bindings[key] for key in training_record_binding_keys()])
            params.append(("filterByFormula", equals_formula(bindings["training_status"], "Completed")))
            raw_records, _ = self.airtable.list_voice_records_limited(params=params, max_records=100)
            records = [normalize_record(record, bindings, self.settings) for record in raw_records]
        existing_conditions = {str(rule.get("condition") or "") for rule in self.list_rules()["rules"]}
        proposals = build_rule_proposals(records, threshold=max(2, self.settings.voice_training_rule_threshold))
        return [
            {
                "key": proposal.key,
                "label": proposal.label,
                "condition": proposal.condition,
                "decision_json": json.dumps(proposal.decision, ensure_ascii=False, sort_keys=True),
                "count": proposal.count,
            }
            for proposal in proposals
            if proposal.condition not in existing_conditions
        ][:6]

    def create_training_rule(self, proposal_key: str) -> ValidationResult:
        proposals = self.rule_proposals()
        proposal = next((item for item in proposals if item["key"] == proposal_key), None)
        if not proposal:
            return ValidationResult(fields={}, errors={"proposal_key": "Предложение правила не найдено"})
        decision = parse_training_answers(proposal["decision_json"])
        fields = {
            "Правило": f"Training: {proposal['label']}"[:120],
            "Активно": True,
            "Область": "Маршрутизация" if decision.get("project") else "Тип",
            "Условие": proposal["condition"],
            "Правильное решение": proposal["decision_json"],
            "Проект": decision.get("project") or "",
            "Тип": decision.get("type") or "",
            "Положительный пример": f"dashboard training confirmations: {proposal['count']}",
            "Источник записи": "Dashboard training",
            "Комментарий пользователя": "Создано после явного подтверждения в модуле Разбор и обучение",
        }
        self.airtable.create_processing_rule(fields)
        return ValidationResult(fields=fields, errors={})

    def learning_structure(self) -> dict[str, Any]:
        metadata = self.metadata()
        bindings: dict[str, FieldBinding] = metadata["bindings"]
        params = limited_fields_params(
            [
                bindings["project"],
                bindings["scope"],
                bindings["life_area"],
                bindings["category"],
                bindings["subcategory"],
                bindings["training_status"],
            ]
        )
        raw_records, limited = self.airtable.list_voice_records_limited(params=params, max_records=300)
        records = [normalize_record(record, bindings, self.settings) for record in raw_records]
        projects = Counter(str(record.get("project") or "") for record in records if record.get("project"))
        life_areas = Counter(str(record.get("life_area") or "") for record in records if record.get("life_area"))
        categories = Counter(str(record.get("category") or "") for record in records if record.get("category"))
        subcategories = Counter(str(record.get("subcategory") or "") for record in records if record.get("subcategory"))
        taxonomy_records = []
        if hasattr(self.airtable, "list_taxonomy_records"):
            with contextlib.suppress(AirtableError):
                taxonomy_records = [normalize_taxonomy_record(record) for record in self.airtable.list_taxonomy_records(page_size=100)]
        return {
            "projects": [{"title": project.title, "count": projects.get(project.title, 0)} for project in metadata["allowed"].projects],
            "life_areas": count_items(life_areas),
            "categories": count_items(categories),
            "subcategories": count_items(subcategories),
            "taxonomy_records": taxonomy_records,
            "taxonomy_supported": bool(metadata.get("taxonomy_table")),
            "limited": limited,
        }

    def projects_dashboard(self) -> dict[str, Any]:
        metadata = self.metadata()
        overview = self.overview()
        project_counts = dict(overview["project_counts"])
        projects = [
            {
                "title": project.title,
                "record_id": project.record_id,
                "count": project_counts.get(project.title, 0),
            }
            for project in metadata["allowed"].projects
        ]
        return {
            "projects": projects,
            "project_counts": overview["project_counts"],
            "recent_records": overview["recent_records"],
        }

    def sources_dashboard(self) -> dict[str, Any]:
        overview = self.overview()
        source_cards = []
        for source in overview["source_counts"]:
            source_cards.append(
                {
                    "name": source["label"],
                    "value": source["value"],
                    "count": source["count"],
                    "records": [
                        record
                        for record in overview["recent_records"]
                        if (record.get("source") or "") == ("" if source["value"] == EMPTY_SOURCE_QUERY_VALUE else source["value"])
                    ][:4],
                }
            )
        return {
            "cards": overview["cards"],
            "source_cards": source_cards,
            "source_counts": overview["source_counts"],
            "status_counts": overview["status_counts"],
            "processing_mode": self._processing_mode_summary(overview),
        }

    def analytics_dashboard(self) -> dict[str, Any]:
        return self.overview()

    def settings_dashboard(self) -> dict[str, Any]:
        overview = self.overview()
        return {
            "timezone": self.settings.timezone,
            "page_size": self.settings.dashboard_page_size,
            "overview_max_records": self.settings.dashboard_overview_max_records,
            "sorting_mode": configured_sorting_mode(self.settings),
            "attachment_timeout_seconds": self.settings.dashboard_attachment_timeout_seconds,
            "write_rate_limit_per_minute": self.settings.dashboard_write_rate_limit_per_minute,
            "max_form_bytes": self.settings.dashboard_max_form_bytes,
            "allowed_hosts": sorted(self.settings.dashboard_allowed_host_set),
            "public_origin_configured": bool(self.settings.dashboard_public_origin.strip()),
            "cloudflare_access_expected": True,
            "editable_keys": EDITABLE_KEYS,
            "processing_mode": self._processing_mode_summary(overview),
        }

    def _processing_mode_summary(self, overview: dict[str, Any]) -> dict[str, Any]:
        mode = self.settings.voice_processing_mode
        return {
            "route": mode.route,
            "airtable_value": mode.airtable_value,
            "russian_name": mode.russian_name,
            "description": mode.description,
            "openai_processor_running": self.settings.openai_api_processor_enabled,
            "awaiting_subscription": overview["cards"].get("Awaiting Subscription", 0),
        }

    def safe_list_rules(self) -> list[dict[str, Any]]:
        with contextlib.suppress(AirtableError):
            return self.airtable.list_processing_rules(active_only=False, page_size=100)
        return []

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
        "processing_route": ("Processing Route", settings.voice_field_processing_route),
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
        "training_status": ("Training Status", settings.voice_field_training_status),
        "scope": ("Scope", settings.voice_field_scope),
        "life_area": ("Life Area", settings.voice_field_life_area),
        "category": ("Category", settings.voice_field_category),
        "subcategory": ("Subcategory", settings.voice_field_subcategory),
        "training_confirmed_at": ("Training Confirmed At", settings.voice_field_training_confirmed_at),
        "training_answers_json": ("Training Answers JSON", settings.voice_field_training_answers_json),
    }
    bindings: dict[str, FieldBinding] = {}
    for key, (fallback_label, configured) in definitions.items():
        field = find_field_metadata(table, configured) or find_field_metadata(table, fallback_label)
        read_name = str(field.get("name") or configured or fallback_label) if field else configured or fallback_label
        field_type = str((field or {}).get("type") or "")
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
    item["ai_confidence_percent"] = ai_confidence_percent(item.get("ai_confidence"))
    item["training_status_effective"] = effective_training_status(item)
    item["status_display"] = record_status_display(item)
    processing_error = str(item.get("processing_error") or "")
    if is_insufficient_quota(processing_error):
        item["processing_error_summary"] = "Недоступен баланс OpenAI API"
        item["processing_error_diagnostic"] = processing_error
    else:
        item["processing_error_summary"] = processing_error
        item["processing_error_diagnostic"] = ""
    return item


def record_status_display(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    route = str(item.get("processing_route") or "")
    if status == "Needs Review":
        return "Требуется проверка"
    if status == "Processed":
        return "Обработано"
    if status == "Awaiting Subscription" or route == "ChatGPT Subscription":
        return "Ожидает обработки ChatGPT"
    if status == "Processing Disabled" or route == "Disabled":
        return "Обработка отключена"
    if route == "OpenAI API" and status in {"New", "Processing"}:
        return "Обрабатывается через OpenAI API"
    return status or "Без статуса"


def field_value(fields: dict[str, Any], bindings: dict[str, FieldBinding], key: str) -> Any:
    binding = bindings[key]
    return get_field(fields, *binding.read_names)


def filter_options(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = metadata["allowed"]
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    return {
        "statuses": sorted(allowed.status_options or set(bindings["status"].options), key=str.casefold),
        "sources": [
            {"value": "Android", "label": "Android"},
            {"value": "Telegram", "label": "Telegram"},
            {"value": EMPTY_SOURCE_QUERY_VALUE, "label": EMPTY_SOURCE_LABEL},
        ],
        "projects": [project.title for project in allowed.projects] or list(bindings["project"].options),
        "types": canonical_select_options(allowed.type_options or set(bindings["entry_type"].options)),
        "priorities": sorted(allowed.priority_options or set(bindings["priority"].options), key=str.casefold),
    }


def training_filter_options(metadata: dict[str, Any], settings: Settings) -> dict[str, list[str]]:
    base = filter_options(metadata)
    return {
        **base,
        "training_statuses": list(TRAINING_STATUSES),
        "scopes": list(TRAINING_SCOPE_OPTIONS),
        "life_areas": training_life_area_options(metadata, settings),
    }


def training_form_options(metadata: dict[str, Any], settings: Settings) -> dict[str, list[str]]:
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    base = filter_options(metadata)
    entry_types = unique_preserve([*TRAINING_ENTRY_TYPE_OPTIONS, *base["types"]])
    return {
        "scopes": list(TRAINING_SCOPE_OPTIONS),
        "projects": base["projects"],
        "life_areas": training_life_area_options(metadata, settings),
        "entry_types": entry_types,
        "priorities": base["priorities"],
        "tags": list(bindings["tags"].options),
    }


def training_life_area_options(metadata: dict[str, Any], settings: Settings) -> list[str]:
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    return unique_preserve([*bindings["life_area"].options, *settings.voice_training_life_area_options])


def unique_preserve(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def training_record_binding_keys() -> tuple[str, ...]:
    return (
        "title",
        "entry_type",
        "project",
        "priority",
        "due_date",
        "next_action",
        "summary",
        "clean_text",
        "raw_text",
        "tags",
        "status",
        "attachments",
        "notes",
        "external_id",
        "source",
        "ai_result_json",
        "ai_confidence",
        "training_status",
        "scope",
        "life_area",
        "category",
        "subcategory",
        "training_confirmed_at",
        "training_answers_json",
    )


def build_training_queue_formula(
    query: dict[str, str],
    bindings: dict[str, FieldBinding],
    settings: Settings,
    *,
    include_cutoff: bool,
) -> str:
    parts = [
        "OR("
        + ",".join(
            [
                equals_formula(bindings["status"], "Processed"),
                equals_formula(bindings["status"], "Needs Review"),
                equals_formula(bindings["status"], "New"),
                f"{{{bindings['status'].read_names[-1]}}} = ''",
            ]
        )
        + ")",
        "OR("
        + ",".join(
            [
                f"{{{bindings['training_status'].read_names[-1]}}} = ''",
                equals_formula(bindings["training_status"], "Pending"),
                equals_formula(bindings["training_status"], "In Progress"),
            ]
        )
        + ")",
        "NOT(" + technical_formula(bindings) + ")",
        display_data_formula(bindings),
    ]
    source = str(query.get("source") or "").strip()
    if source:
        parts.append(equals_formula(bindings["source"], source))
    period_formula = period_filter_formula(str(query.get("period") or "").strip(), settings)
    if period_formula:
        parts.append(period_formula)
    if include_cutoff and settings.voice_training_created_after_datetime is not None:
        parts.append(
            "IS_AFTER(CREATED_TIME(), "
            f"DATETIME_PARSE('{_format_airtable_datetime(settings.voice_training_created_after_datetime)}'))"
        )
    return parts[0] if len(parts) == 1 else "AND(" + ",".join(parts) + ")"


def display_data_formula(bindings: dict[str, FieldBinding]) -> str:
    fields = [bindings[key] for key in ("title", "raw_text", "clean_text", "summary")]
    return "OR(" + ",".join(f"LEN({{{binding.read_names[-1]}}} & '') > 0" for binding in fields) + ")"


def training_queue_eligible(item: dict[str, Any], settings: Settings, *, include_cutoff: bool = True) -> bool:
    if item.get("is_technical") or not has_display_data(item):
        return False
    if effective_training_status(item) not in {"Pending", "In Progress"}:
        return False
    status = str(item.get("status") or "")
    if status and status not in {"Processed", "Needs Review", "New"}:
        return False
    cutoff = settings.voice_training_created_after_datetime
    if include_cutoff and cutoff is not None:
        created_at = item.get("created_at")
        if not isinstance(created_at, datetime) or created_at <= cutoff:
            return False
    return True


def training_query_matches(item: dict[str, Any], query: dict[str, str]) -> bool:
    source = str(query.get("source") or "").strip()
    if source and item.get("source") != source:
        return False
    period = str(query.get("period") or "").strip()
    if period and not period_matches(item.get("created_at"), period):
        return False
    return True


def period_matches(created_at: Any, period: str) -> bool:
    if not isinstance(created_at, datetime):
        return False
    now = datetime.now(UTC)
    if period == "today":
        return created_at >= datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    if period == "7d":
        return created_at >= now - timedelta(days=7)
    if period == "30d":
        return created_at >= now - timedelta(days=30)
    return True


def effective_training_status(item: dict[str, Any]) -> str:
    status = str(item.get("training_status") or "").strip()
    return status if status in TRAINING_STATUSES else "Pending"


def has_display_data(item: dict[str, Any]) -> bool:
    return any(str(item.get(key) or "").strip() for key in ("title", "raw_text", "clean_text", "summary"))


def training_needs_clarification(item: dict[str, Any]) -> bool:
    if not item.get("scope"):
        return True
    if not item.get("entry_type"):
        return True
    if item.get("ai_confidence_percent") is not None and item["ai_confidence_percent"] < 80:
        return True
    return False


def infer_scope_from_record(record: dict[str, Any]) -> str:
    if record.get("scope"):
        return str(record.get("scope"))
    if record.get("project"):
        return "Рабочее"
    if record.get("life_area"):
        return "Личное"
    return "Не уверен"


def question_keys_for_training(scope: str) -> list[str]:
    keys = ["scope"]
    if scope in {"Рабочее", "Смешанное"}:
        keys.append("project")
    if scope in {"Личное", "Смешанное"}:
        keys.append("life_area")
    keys.extend(["entry_type", "next_action", "optional"])
    return keys


def ai_proposal_for_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": infer_scope_from_record(record),
        "project": record.get("project") or "",
        "entry_type": record.get("entry_type") or "",
        "next_action": record.get("next_action") or "",
        "priority": record.get("priority") or "",
        "due_date": record.get("due_date") or "",
        "tags": ",".join(record.get("tags") or []) if isinstance(record.get("tags"), list) else "",
    }


def validate_training_form(
    form: dict[str, Any],
    current: dict[str, Any],
    metadata: dict[str, Any],
    settings: Settings,
    *,
    status: str = "Completed",
    applied_from: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    for key in form:
        if key not in TRAINING_FORM_KEYS:
            errors[key] = "Unknown training field"
    options = training_form_options(metadata, settings)
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    scope = clean_form_text(form.get("scope"), limit=40)
    if scope not in options["scopes"]:
        errors["scope"] = "Выберите область"
    project = clean_form_text(form.get("project"), limit=120)
    if project and options["projects"] and project not in options["projects"]:
        errors["project"] = "Недопустимый проект"
    if scope == "Рабочее" and not project:
        errors["project"] = "Выберите рабочий проект"
    life_area = clean_form_text(form.get("life_area"), limit=120)
    if life_area and options["life_areas"] and life_area not in options["life_areas"]:
        errors["life_area"] = "Недопустимая сфера"
    if scope == "Личное" and not life_area:
        errors["life_area"] = "Выберите жизненную сферу"
    entry_type = clean_form_text(form.get("entry_type"), limit=120)
    if entry_type not in options["entry_types"]:
        errors["entry_type"] = "Выберите тип записи"
    next_action = clean_form_text(form.get("next_action"), limit=500)
    priority = clean_form_text(form.get("priority"), limit=120)
    if priority and options["priorities"] and priority not in options["priorities"]:
        errors["priority"] = "Недопустимый приоритет"
    due_date = clean_form_text(form.get("due_date"), limit=20)
    if due_date:
        with contextlib.suppress(ValueError):
            date.fromisoformat(due_date)
        if not is_iso_date(due_date):
            errors["due_date"] = "Дата должна быть в формате YYYY-MM-DD"
    category = clean_form_text(form.get("category"), limit=120)
    subcategory = clean_form_text(form.get("subcategory"), limit=120)
    tags = clean_tags(form.get("tags"), allowed=options["tags"], errors=errors)
    if errors:
        return {}, errors

    answers = {
        "scope": scope,
        "project": project,
        "life_area": life_area,
        "category": category,
        "subcategory": subcategory,
        "type": entry_type,
        "next_action": next_action,
        "priority": priority,
        "due_date": due_date,
        "tags": tags,
    }
    return fields_from_training_answers(
        answers,
        current,
        metadata,
        settings,
        status=status,
        applied_from=applied_from,
    ), {}


def is_iso_date(value: str) -> bool:
    with contextlib.suppress(ValueError):
        date.fromisoformat(value)
        return True
    return False


def clean_tags(value: Any, *, allowed: list[str], errors: dict[str, str]) -> list[str]:
    raw = str(value or "").replace(";", ",")
    tags = unique_preserve([part.strip() for part in raw.split(",") if part.strip()])[:10]
    if allowed:
        invalid = [tag for tag in tags if tag not in allowed]
        if invalid:
            errors["tags"] = "Недопустимый тег"
            return []
    return tags


def fields_from_training_answers(
    answers: dict[str, Any],
    current: dict[str, Any],
    metadata: dict[str, Any],
    settings: Settings,
    *,
    status: str,
    applied_from: str = "",
) -> dict[str, Any]:
    bindings: dict[str, FieldBinding] = metadata["bindings"]
    confirmed_at = datetime.now(UTC).isoformat()
    fields: dict[str, Any] = {
        bindings["training_status"].write_name: status,
        bindings["training_confirmed_at"].write_name: confirmed_at,
    }
    mapping = {
        "scope": "scope",
        "life_area": "life_area",
        "category": "category",
        "subcategory": "subcategory",
        "project": "project",
        "type": "entry_type",
        "next_action": "next_action",
        "priority": "priority",
        "due_date": "due_date",
    }
    for answer_key, binding_key in mapping.items():
        value = answers.get(answer_key)
        if value in ("", [], None):
            value = None
        add_if_changed(fields, bindings[binding_key], value, current.get(binding_key))
    tags = answers.get("tags")
    if tags:
        add_if_changed(fields, bindings["tags"], tags, current.get("tags"))
    payload = {
        "schema_version": 1,
        "record_id": current.get("id") or "",
        "applied_from_record_id": applied_from,
        "confirmed_at": confirmed_at,
        "answers": {key: answers.get(key) for key in sorted(answers)},
    }
    fields[bindings["training_answers_json"].write_name] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return fields


def parse_training_answers(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str) and value.strip():
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(value)
        if not isinstance(payload, dict):
            return {}
    else:
        return {}
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else payload
    return dict(answers) if isinstance(answers, dict) else {}


def record_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "summary", "clean_text", "raw_text"))


def token_set(text: str) -> set[str]:
    return set(re.findall(r"[0-9A-Za-zА-Яа-яЁё]{3,}", text.casefold()))


def record_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_tokens = token_set(record_text(left))
    right_tokens = token_set(record_text(right))
    if not left_tokens or not right_tokens:
        overlap = 0.0
    else:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    bonuses = 0.0
    for key in ("project", "entry_type", "source"):
        if left.get(key) and left.get(key) == right.get(key):
            bonuses += 0.08
    left_tags = set(left.get("tags") or []) if isinstance(left.get("tags"), list) else set()
    right_tags = set(right.get("tags") or []) if isinstance(right.get("tags"), list) else set()
    if left_tags and right_tags:
        bonuses += min(0.15, len(left_tags & right_tags) * 0.05)
    return min(1.0, overlap + bonuses)


def build_rule_proposals(records: list[dict[str, Any]], *, threshold: int) -> list[RuleProposal]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if effective_training_status(record) != "Completed":
            continue
        answers = parse_training_answers(record.get("training_answers_json"))
        if not answers:
            continue
        key_parts = [
            str(answers.get("scope") or ""),
            str(answers.get("project") or answers.get("life_area") or ""),
            str(answers.get("category") or ""),
            str(answers.get("subcategory") or ""),
            str(answers.get("type") or ""),
        ]
        key = "\t".join(part.casefold() for part in key_parts)
        grouped.setdefault(key, []).append(answers)
    proposals: list[RuleProposal] = []
    for answers_list in grouped.values():
        if len(answers_list) < threshold:
            continue
        first = answers_list[0]
        decision = {
            key: first.get(key)
            for key in ("project", "type", "priority", "next_action", "life_area", "category", "subcategory")
            if first.get(key)
        }
        condition_parts = [
            f"scope={first.get('scope') or '—'}",
            f"project={first.get('project') or '—'}",
            f"life_area={first.get('life_area') or '—'}",
            f"category={first.get('category') or '—'}",
            f"type={first.get('type') or '—'}",
        ]
        condition = "Dashboard training pattern: " + "; ".join(condition_parts)
        label = " / ".join(
            part
            for part in (
                first.get("project") or first.get("life_area"),
                first.get("category"),
                first.get("type"),
            )
            if part
        ) or "classification pattern"
        proposal_key = hashlib.sha256((condition + json.dumps(decision, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()[:16]
        proposals.append(RuleProposal(key=proposal_key, label=label, condition=condition, decision=decision, count=len(answers_list)))
    proposals.sort(key=lambda item: (-item.count, item.label.casefold()))
    return proposals


def normalize_taxonomy_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    return {
        "id": str(record.get("id") or ""),
        "name": fields.get("Название") or "Без названия",
        "type": fields.get("Тип") or "",
        "parent": fields.get("Родитель") or "",
        "active": bool(fields.get("Активно")),
        "uses": fields.get("Количество применений"),
        "last_used": fields.get("Дата последнего применения") or "",
    }


def build_records_formula(query: dict[str, str], bindings: dict[str, FieldBinding], settings: Settings) -> str:
    parts: list[str] = []
    exact_filters = {
        "status": "status",
        "project": "project",
        "entry_type": "entry_type",
    }
    for query_key, binding_key in exact_filters.items():
        value = str(query.get(query_key) or "").strip()
        if value:
            parts.append(equals_formula(bindings[binding_key], value))
    source = str(query.get("source") or "").strip()
    if source == EMPTY_SOURCE_QUERY_VALUE:
        parts.append(empty_field_formula(bindings["source"]))
    elif source:
        parts.append(equals_formula(bindings["source"], source))
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
            + ","
            + equals_formula(bindings["status"], "Awaiting Subscription")
            + ")"
        )
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "AND(" + ",".join(parts) + ")"


def equals_formula(binding: FieldBinding, value: str) -> str:
    field_name = binding.read_names[-1]
    return f"{{{field_name}}} = '{_escape_airtable_formula_string(value)}'"


def empty_field_formula(binding: FieldBinding) -> str:
    field_name = binding.read_names[-1]
    return f"OR({{{field_name}}} = '',NOT({{{field_name}}}))"


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


def configured_sorting_mode(settings: Settings) -> str:
    if settings.dashboard_airtable_view.strip():
        return SORTING_MODE_AIRTABLE_VIEW
    if settings.dashboard_created_time_field.strip():
        return SORTING_MODE_AIRTABLE_FIELD
    return SORTING_MODE_PAGE_ONLY_UNSAFE


def resolve_sorting_config(settings: Settings, table: dict[str, Any], requested_sort: Any) -> SortingConfig:
    mode = configured_sorting_mode(settings)
    requested_direction = "asc" if requested_sort == "asc" else "desc"
    if mode == SORTING_MODE_AIRTABLE_VIEW:
        configured_view = settings.dashboard_airtable_view.strip()
        if not find_view_metadata(table, configured_view):
            raise AirtableError("Configured dashboard Airtable view was not found")
        return SortingConfig(
            mode=SORTING_MODE_AIRTABLE_VIEW,
            direction="desc",
            params=(("view", configured_view),),
            is_exact=True,
        )

    if mode == SORTING_MODE_AIRTABLE_FIELD:
        field = find_field_metadata(table, settings.dashboard_created_time_field.strip())
        if not field:
            raise AirtableError("Configured dashboard Created time field was not found")
        if not is_created_time_sort_field(field):
            raise AirtableError(
                "Configured dashboard Created time field must use Airtable Created time type "
                "or CREATED_TIME() formula"
            )
        sort_field = str(field.get("name") or settings.dashboard_created_time_field.strip())
        params: list[tuple[str, str]] = [
            ("sort[0][field]", sort_field),
            ("sort[0][direction]", requested_direction),
        ]
        secondary = stable_secondary_sort_field(settings, table, exclude=sort_field)
        if secondary:
            params.extend(
                [
                    ("sort[1][field]", secondary),
                    ("sort[1][direction]", "asc"),
                ]
            )
        return SortingConfig(
            mode=SORTING_MODE_AIRTABLE_FIELD,
            direction=requested_direction,
            params=tuple(params),
            is_exact=True,
        )

    return SortingConfig(mode=SORTING_MODE_PAGE_ONLY_UNSAFE, direction=requested_direction, is_exact=False)


def find_view_metadata(table: dict[str, Any], configured_view: str) -> dict[str, Any] | None:
    if not configured_view:
        return None
    for view in table.get("views") or []:
        if configured_view in {view.get("id"), view.get("name")}:
            return view
    return None


def stable_secondary_sort_field(settings: Settings, table: dict[str, Any], *, exclude: str) -> str:
    for candidate in (settings.voice_field_external_id, "External ID", settings.voice_field_title, "Название"):
        field = find_field_metadata(table, candidate)
        if not field:
            continue
        name = str(field.get("name") or candidate)
        if name == exclude:
            continue
        if field.get("type") in SORT_COMPATIBLE_FIELD_TYPES:
            return name
    return ""


def is_created_time_sort_field(field: dict[str, Any]) -> bool:
    field_type = field.get("type")
    if field_type == "createdTime":
        return True
    if field_type != "formula":
        return False
    formula = str((field.get("options") or {}).get("formula") or "")
    normalized = re.sub(r"\s+", "", formula).upper()
    return normalized == "CREATED_TIME()"


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
    entry_type = clean_select_value(item.get("entry_type"))
    canonical_entry_type = canonical_select_value(entry_type, options["types"])
    legacy_entry_type = (
        entry_type
        if entry_type
        and (
            entry_type.casefold() in CONTENT_MEDIA_TYPE_KEYS
            or canonical_entry_type is None
            or canonical_entry_type != entry_type
        )
        else ""
    )
    editable_type_options = tuple(
        option for option in options["types"] if option.casefold() not in CONTENT_MEDIA_TYPE_KEYS
    )
    return [
        EditableField("project", "Проект", "select", item.get("project") or "", tuple(options["projects"])),
        EditableField("entry_type", "Тип", "select", entry_type, editable_type_options, legacy_option=legacy_entry_type),
        EditableField("priority", "Приоритет", "select", item.get("priority") or "", tuple(options["priorities"])),
        EditableField("due_date", "Срок", "date", item.get("due_date") or ""),
        EditableField("amount", "Сумма", "number", item.get("amount") if item.get("amount") is not None else ""),
        EditableField("counterparty", "Контрагент", "text", item.get("counterparty") or "", max_length=300),
        EditableField("period", "Период", "text", item.get("period") or "", max_length=300),
        EditableField(
            "next_action",
            "Следующий конкретный шаг",
            "textarea",
            item.get("next_action") or "",
            max_length=1000,
            helper_text="Необязательно. Заполняйте, только если из записи следует действие",
            placeholder="Например: Проверить документацию",
        ),
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
    set_entry_type_select(fields, errors, bindings["entry_type"], form, allowed["types"], current)
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


def set_entry_type_select(
    fields: dict[str, Any],
    errors: dict[str, str],
    binding: FieldBinding,
    form: dict[str, Any],
    allowed: list[str],
    current: dict[str, Any],
) -> None:
    if "entry_type" not in form:
        return
    value = clean_form_text(form.get("entry_type"), limit=120)
    if not value:
        add_if_changed(fields, binding, None, current.get("entry_type"))
        return
    canonical = canonical_select_value(value, allowed)
    current_value = clean_select_value(current.get("entry_type"))
    if canonical is None:
        if value == current_value:
            add_if_changed(fields, binding, current_value, current.get("entry_type"))
            return
        errors["entry_type"] = "Недопустимое значение"
        return
    selected = current_value if value == current_value else canonical
    add_if_changed(fields, binding, selected, current.get("entry_type"))


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


def view_query(query: dict[str, str]) -> str:
    return urlencode({key: value for key, value in query.items() if key != "offset" and value})


def count_items(counter: Counter[str], *, limit: int = 8) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0].casefold()))[:limit]


def source_count_items(counter: Counter[str], *, limit: int = 8) -> list[dict[str, Any]]:
    result = []
    for source, count in count_items(counter, limit=limit):
        result.append(
            {
                "label": source or EMPTY_SOURCE_LABEL,
                "value": source or EMPTY_SOURCE_QUERY_VALUE,
                "count": count,
            }
        )
    return result


def truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "да"}


def ai_confidence_percent(value: Any) -> int | None:
    with contextlib.suppress(TypeError, ValueError):
        confidence = float(value)
        if confidence <= 1:
            confidence *= 100
        return max(0, min(100, round(confidence)))
    return None


def ensure_record_id(record_id: str) -> None:
    if not RECORD_ID_RE.match(record_id or ""):
        raise AirtableError("Invalid Airtable record id")


def safe_content_disposition(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip() or "attachment"
    return f"inline; filename*=UTF-8''{quote(safe)}"
