from __future__ import annotations

import argparse
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.airtable import AirtableClient, AirtableError
from app.config import Settings, get_settings, parse_utc_timestamp
from app.voice_processor import (
    AllowedContext,
    DriveOriginal,
    GoogleDriveInboxReader,
    ProcessorOutput,
    allowed_context_from_metadata,
    get_field,
    normalize_due_date,
)

logger = logging.getLogger(__name__)

TECHNICAL_PATTERNS = ("smoke", "canary", "production test", "tg-smoke", "dashboard-canary")
SUBSCRIPTION_ROUTE = "ChatGPT Subscription"
AWAITING_STATUS = "Awaiting Subscription"
PROCESSED_STATUS = "Processed"
NEEDS_REVIEW_STATUS = "Needs Review"
DEFAULT_MANUAL_PROCESSOR_VERSION = "codex-subscription-manual-v1"
SAFE_ERROR_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,119}$")


class SubscriptionQueueStateError(RuntimeError):
    """A content-safe queue state error suitable for operational logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SubscriptionQueueItem:
    record_id: str
    external_id: str
    source: str
    entry_type: str
    created_at: str
    raw_text: str
    google_drive_url: str
    claim: str = ""


@dataclass(frozen=True)
class SubscriptionQueueBundle:
    item: SubscriptionQueueItem
    manifest: dict[str, Any]
    originals: list[DriveOriginal]


class SubscriptionQueue:
    def __init__(
        self,
        settings: Settings,
        *,
        airtable: AirtableClient | None = None,
        drive_reader: GoogleDriveInboxReader | None = None,
    ) -> None:
        self.settings = settings
        self.airtable = airtable if airtable is not None else AirtableClient(settings)
        self.drive_reader = drive_reader if drive_reader is not None else GoogleDriveInboxReader(settings)

    def next_batch(
        self,
        *,
        batch_size: int,
        created_after: datetime | None = None,
        dry_run: bool = True,
    ) -> list[SubscriptionQueueItem]:
        limit = max(1, min(batch_size, 50))
        records = self.airtable.list_subscription_queue_records(
            batch_size=min(100, max(limit, limit * 3)),
            created_after=created_after,
        )
        items: list[SubscriptionQueueItem] = []
        for record in records:
            item = queue_item_from_record(record, self.settings)
            if item is None or not self.drive_reader.manifest_exists(item.google_drive_url):
                continue
            if dry_run:
                items.append(item)
            else:
                try:
                    items.append(self.claim_item(item))
                except SubscriptionQueueStateError as exc:
                    logger.warning("Subscription queue claim skipped code=%s", exc.code)
                    continue
            if len(items) >= limit:
                break
        return items

    def claim_item(self, item: SubscriptionQueueItem, *, claim: str | None = None) -> SubscriptionQueueItem:
        claimed_at = datetime.now(UTC)
        claim_value = claim or (
            f"subscription_queue lock_id={uuid.uuid4().hex} claimed_at={claimed_at.isoformat()}"
        )
        if not claim_value.strip():
            raise SubscriptionQueueStateError("claim_empty")

        current = self.airtable.fetch_voice_record(item.record_id)
        self._assert_protected_fields(current, item)
        if self._record_has_queue_state(current, status=AWAITING_STATUS, claim=claim_value):
            return SubscriptionQueueItem(
                record_id=item.record_id,
                external_id=item.external_id,
                source=item.source,
                entry_type=item.entry_type,
                created_at=item.created_at,
                raw_text=item.raw_text,
                google_drive_url=item.google_drive_url,
                claim=claim_value,
            )
        self._assert_queue_state(current, status=AWAITING_STATUS, claim="")
        try:
            self.airtable.claim_subscription_queue_record(
                item.record_id,
                claim=claim_value,
                claimed_at=claimed_at,
            )
        except AirtableError as exc:
            claimed = self.airtable.fetch_voice_record(item.record_id)
            if not self._record_has_queue_state(claimed, status=AWAITING_STATUS, claim=claim_value):
                raise SubscriptionQueueStateError("claim_update_unconfirmed") from exc

        claimed = self.airtable.fetch_voice_record(item.record_id)
        self._assert_protected_fields(claimed, item)
        self._assert_queue_state(claimed, status=AWAITING_STATUS, claim=claim_value)
        return SubscriptionQueueItem(
            record_id=item.record_id,
            external_id=item.external_id,
            source=item.source,
            entry_type=item.entry_type,
            created_at=item.created_at,
            raw_text=item.raw_text,
            google_drive_url=item.google_drive_url,
            claim=claim_value,
        )

    def load_bundle(self, item: SubscriptionQueueItem, target_dir: Path) -> SubscriptionQueueBundle:
        manifest, originals = self.drive_reader.download_record_originals(item.google_drive_url, target_dir)
        return SubscriptionQueueBundle(item=item, manifest=manifest, originals=originals)

    def allowed_context(self) -> AllowedContext:
        table = self.airtable.find_table_metadata(
            self.settings.voice_inbox_base_id,
            table_id=self.settings.voice_inbox_table_id,
        )
        if not table:
            raise SubscriptionQueueStateError("airtable_metadata_missing")
        return allowed_context_from_metadata(table, self.settings, self.airtable.list_projects())

    def finalize_processed(
        self,
        item: SubscriptionQueueItem,
        result: ProcessorOutput | dict[str, Any],
        *,
        processor_version: str = DEFAULT_MANUAL_PROCESSOR_VERSION,
    ) -> dict[str, Any]:
        output = self._validate_result(result, status=PROCESSED_STATUS)
        if output.confidence < 0.80:
            raise SubscriptionQueueStateError("processed_confidence_below_threshold")
        if output.needs_review_reasons:
            raise SubscriptionQueueStateError("processed_has_review_reasons")
        return self._finalize(
            item,
            output,
            status=PROCESSED_STATUS,
            processor_version=processor_version,
        )

    def finalize_needs_review(
        self,
        item: SubscriptionQueueItem,
        result: ProcessorOutput | dict[str, Any],
        *,
        processor_version: str = DEFAULT_MANUAL_PROCESSOR_VERSION,
    ) -> dict[str, Any]:
        output = self._validate_result(result, status=NEEDS_REVIEW_STATUS)
        if not output.needs_review_reasons:
            raise SubscriptionQueueStateError("needs_review_reasons_missing")
        return self._finalize(
            item,
            output,
            status=NEEDS_REVIEW_STATUS,
            processor_version=processor_version,
        )

    def release_claim(self, item: SubscriptionQueueItem, *, error_code: str = "") -> dict[str, Any]:
        claim = self._required_claim(item)
        if error_code and not SAFE_ERROR_CODE_RE.fullmatch(error_code):
            raise SubscriptionQueueStateError("unsafe_error_code")

        current = self.airtable.fetch_voice_record(item.record_id)
        self._assert_protected_fields(current, item)
        if self._is_expected_release_state(current, error_code=error_code):
            return current
        self._assert_queue_state(current, status=AWAITING_STATUS, claim=claim)
        fields: dict[str, Any] = {
            self.settings.voice_field_subscription_claim: None,
            self.settings.voice_field_subscription_claimed_at: None,
            self.settings.voice_field_processing_route: SUBSCRIPTION_ROUTE,
        }
        if error_code:
            fields[self.settings.voice_field_processing_error] = error_code
        try:
            self.airtable.update_voice_record_fields(item.record_id, fields)
        except AirtableError as exc:
            released = self.airtable.fetch_voice_record(item.record_id)
            if not self._is_expected_release_state(released, error_code=error_code):
                raise SubscriptionQueueStateError("release_update_unconfirmed") from exc

        released = self.airtable.fetch_voice_record(item.record_id)
        self._assert_protected_fields(released, item)
        self._assert_queue_state(released, status=AWAITING_STATUS, claim="")
        if not self._is_expected_release_state(released, error_code=error_code):
            raise SubscriptionQueueStateError("release_postcondition_failed")
        return released

    def _is_expected_release_state(self, record: dict[str, Any], *, error_code: str) -> bool:
        if not self._record_has_queue_state(record, status=AWAITING_STATUS, claim=""):
            return False
        if not error_code:
            return True
        fields = record.get("fields") or {}
        return get_field(fields, self.settings.voice_field_processing_error, "Ошибка обработки") == error_code

    def _validate_result(
        self,
        result: ProcessorOutput | dict[str, Any],
        *,
        status: str,
    ) -> ProcessorOutput:
        output = result if isinstance(result, ProcessorOutput) else ProcessorOutput.model_validate(result)
        allowed = self.allowed_context()
        if allowed.status_options and status not in allowed.status_options:
            raise SubscriptionQueueStateError("status_option_not_allowed")
        if output.type is not None and output.type not in allowed.type_options:
            raise SubscriptionQueueStateError("type_option_not_allowed")
        if output.priority is not None and output.priority not in allowed.priority_options:
            raise SubscriptionQueueStateError("priority_option_not_allowed")
        if any(tag not in allowed.tag_options for tag in output.tags):
            raise SubscriptionQueueStateError("tag_option_not_allowed")
        project_titles = {project.title for project in allowed.projects}
        if output.project is not None and output.project not in project_titles:
            raise SubscriptionQueueStateError("project_option_not_allowed")
        if output.due_date is not None and normalize_due_date(output.due_date) != output.due_date:
            raise SubscriptionQueueStateError("due_date_invalid")
        return output

    def _finalize(
        self,
        item: SubscriptionQueueItem,
        output: ProcessorOutput,
        *,
        status: str,
        processor_version: str,
    ) -> dict[str, Any]:
        claim = self._required_claim(item)
        version = processor_version.strip()
        if not version or len(version) > 120:
            raise SubscriptionQueueStateError("processor_version_invalid")
        result_json = json.dumps(output.model_dump(), ensure_ascii=False, sort_keys=True)
        fields = self._finalization_fields(
            output,
            status=status,
            processor_version=version,
            result_json=result_json,
        )

        current = self.airtable.fetch_voice_record(item.record_id)
        self._assert_protected_fields(current, item)
        if self._is_expected_final_state(
            current,
            item,
            output,
            status=status,
            processor_version=version,
            result_json=result_json,
        ):
            return current

        current_status = self._status(current)
        current_claim = self._claim(current)
        retryable_partial = current_status == status and current_claim == claim
        if not retryable_partial:
            self._assert_queue_state(current, status=AWAITING_STATUS, claim=claim)

        try:
            self.airtable.update_voice_record_fields(item.record_id, fields)
        except AirtableError as exc:
            finalized = self.airtable.fetch_voice_record(item.record_id)
            if self._is_expected_final_state(
                finalized,
                item,
                output,
                status=status,
                processor_version=version,
                result_json=result_json,
            ):
                return finalized
            raise SubscriptionQueueStateError("finalize_update_unconfirmed") from exc

        finalized = self.airtable.fetch_voice_record(item.record_id)
        if not self._is_expected_final_state(
            finalized,
            item,
            output,
            status=status,
            processor_version=version,
            result_json=result_json,
        ):
            raise SubscriptionQueueStateError("finalize_postcondition_failed")
        return finalized

    def _finalization_fields(
        self,
        output: ProcessorOutput,
        *,
        status: str,
        processor_version: str,
        result_json: str,
    ) -> dict[str, Any]:
        review_error = "; ".join(output.needs_review_reasons)[:1000] if status == NEEDS_REVIEW_STATUS else None
        return {
            self.settings.voice_field_title: output.title,
            self.settings.voice_field_clean_text: output.clean_text,
            self.settings.voice_field_summary: output.summary,
            self.settings.voice_field_type: output.type,
            self.settings.voice_field_project: output.project,
            self.settings.voice_field_priority: output.priority,
            self.settings.voice_field_due_date: output.due_date,
            self.settings.voice_field_counterparty: output.counterparty,
            self.settings.voice_field_amount: output.amount,
            self.settings.voice_field_period: output.period,
            self.settings.voice_field_next_action: output.next_action,
            self.settings.voice_field_tags: output.tags,
            self.settings.voice_field_processing_status: status,
            self.settings.voice_field_processing_error: review_error,
            self.settings.voice_field_ai_result_json: result_json,
            self.settings.voice_field_ai_confidence: output.confidence,
            self.settings.voice_field_processor_version: processor_version,
            self.settings.voice_field_processing_route: SUBSCRIPTION_ROUTE,
            self.settings.voice_field_subscription_claim: None,
            self.settings.voice_field_subscription_claimed_at: None,
        }

    def _is_expected_final_state(
        self,
        record: dict[str, Any],
        item: SubscriptionQueueItem,
        output: ProcessorOutput,
        *,
        status: str,
        processor_version: str,
        result_json: str,
    ) -> bool:
        try:
            self._assert_protected_fields(record, item)
        except SubscriptionQueueStateError:
            return False
        fields = record.get("fields") or {}
        expected = [
            (self.settings.voice_field_title, "Название", output.title),
            (self.settings.voice_field_clean_text, "Очищенный текст", output.clean_text),
            (self.settings.voice_field_summary, "Краткое содержание", output.summary),
            (self.settings.voice_field_type, "Тип", output.type),
            (self.settings.voice_field_project, "Проект", output.project),
            (self.settings.voice_field_priority, "Приоритет", output.priority),
            (self.settings.voice_field_due_date, "Срок", output.due_date),
            (self.settings.voice_field_counterparty, "Контрагент", output.counterparty),
            (self.settings.voice_field_amount, "Сумма", output.amount),
            (self.settings.voice_field_period, "Период", output.period),
            (self.settings.voice_field_next_action, "Следующее действие", output.next_action),
            (self.settings.voice_field_tags, "Теги", output.tags),
            (self.settings.voice_field_processing_status, "Статус обработки", status),
            (self.settings.voice_field_ai_result_json, "AI результат JSON", result_json),
            (self.settings.voice_field_ai_confidence, "Уверенность AI", output.confidence),
            (self.settings.voice_field_processor_version, "Версия обработчика", processor_version),
            (self.settings.voice_field_processing_route, "Processing Route", SUBSCRIPTION_ROUTE),
        ]
        if any(not _same_airtable_value(get_field(fields, configured, fallback), value) for configured, fallback, value in expected):
            return False
        expected_error = "; ".join(output.needs_review_reasons)[:1000] if status == NEEDS_REVIEW_STATUS else None
        if not _same_airtable_value(
            get_field(fields, self.settings.voice_field_processing_error, "Ошибка обработки"),
            expected_error,
        ):
            return False
        claimed_at = get_field(
            fields,
            self.settings.voice_field_subscription_claimed_at,
            "Subscription Queue Claimed At",
        )
        return not self._claim(record) and claimed_at in (None, "")

    def _assert_protected_fields(self, record: dict[str, Any], item: SubscriptionQueueItem) -> None:
        fields = record.get("fields") or {}
        protected = [
            (
                get_field(
                    fields,
                    self.settings.voice_field_external_id,
                    self.settings.voice_field_external_id_query_name,
                    "External ID",
                ),
                item.external_id,
            ),
            (get_field(fields, self.settings.voice_field_raw_text, "Исходная фраза"), item.raw_text),
            (get_field(fields, self.settings.voice_field_google_drive, "Google Drive"), item.google_drive_url),
        ]
        if any(not _same_airtable_value(current, expected) for current, expected in protected):
            raise SubscriptionQueueStateError("protected_field_changed")

    def _assert_queue_state(self, record: dict[str, Any], *, status: str, claim: str) -> None:
        fields = record.get("fields") or {}
        route = str(
            get_field(
                fields,
                self.settings.voice_field_processing_route,
                self.settings.voice_field_processing_route_query_name,
                "Processing Route",
            )
            or ""
        ).strip()
        drive_url = str(get_field(fields, self.settings.voice_field_google_drive, "Google Drive") or "").strip()
        if route != SUBSCRIPTION_ROUTE:
            raise SubscriptionQueueStateError("route_changed")
        if not drive_url:
            raise SubscriptionQueueStateError("drive_url_missing")
        if self._status(record) != status:
            raise SubscriptionQueueStateError("status_changed")
        if self._claim(record) != claim:
            raise SubscriptionQueueStateError("claim_mismatch")

    def _record_has_queue_state(self, record: dict[str, Any], *, status: str, claim: str) -> bool:
        try:
            self._assert_queue_state(record, status=status, claim=claim)
        except SubscriptionQueueStateError:
            return False
        return True

    def _status(self, record: dict[str, Any]) -> str:
        fields = record.get("fields") or {}
        return str(
            get_field(
                fields,
                self.settings.voice_field_processing_status,
                self.settings.voice_field_processing_status_query_name,
                "Статус обработки",
            )
            or ""
        ).strip()

    def _claim(self, record: dict[str, Any]) -> str:
        fields = record.get("fields") or {}
        return str(
            get_field(fields, self.settings.voice_field_subscription_claim, "Subscription Queue Claim") or ""
        ).strip()

    @staticmethod
    def _required_claim(item: SubscriptionQueueItem) -> str:
        if not item.claim.strip():
            raise SubscriptionQueueStateError("claim_missing")
        return item.claim


def _same_airtable_value(current: Any, expected: Any) -> bool:
    if expected in (None, "") and current in (None, ""):
        return True
    if expected == [] and current in (None, []):
        return True
    if isinstance(expected, float) and isinstance(current, (int, float)):
        return float(current) == expected
    return current == expected


def queue_item_from_record(record: dict[str, Any], settings: Settings) -> SubscriptionQueueItem | None:
    fields = record.get("fields") or {}
    route = str(
        get_field(
            fields,
            settings.voice_field_processing_route,
            settings.voice_field_processing_route_query_name,
            "Processing Route",
        )
        or ""
    ).strip()
    status = str(
        get_field(
            fields,
            settings.voice_field_processing_status,
            settings.voice_field_processing_status_query_name,
            "Статус обработки",
        )
        or ""
    ).strip()
    drive_url = str(get_field(fields, settings.voice_field_google_drive, "Google Drive") or "").strip()
    claim = str(get_field(fields, settings.voice_field_subscription_claim, "Subscription Queue Claim") or "").strip()
    if route != SUBSCRIPTION_ROUTE or status != AWAITING_STATUS or not drive_url or claim:
        return None

    external_id = str(get_field(fields, settings.voice_field_external_id, "External ID") or "").strip()
    source = str(get_field(fields, settings.voice_field_source, "Источник") or "").strip()
    entry_type = str(get_field(fields, settings.voice_field_type, "Тип") or "").strip()
    raw_text = str(get_field(fields, settings.voice_field_raw_text, "Исходная фраза") or "")
    title = str(get_field(fields, settings.voice_field_title, "Название") or "")
    notes = str(get_field(fields, settings.voice_field_notes, "Notes") or "")
    haystack = " ".join((title, raw_text, external_id, notes)).casefold()
    if any(pattern in haystack for pattern in TECHNICAL_PATTERNS):
        return None
    return SubscriptionQueueItem(
        record_id=str(record.get("id") or ""),
        external_id=external_id,
        source=source,
        entry_type=entry_type,
        created_at=str(record.get("createdTime") or ""),
        raw_text=raw_text,
        google_drive_url=drive_url,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Get a limited ChatGPT Subscription queue batch")
    parser.add_argument("--batch-size", type=int, default=5, help="Maximum records to return (1..50)")
    parser.add_argument("--created-after", default="", help="UTC ISO 8601 lower created-time bound")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read and validate without claiming (default)")
    mode.add_argument("--claim", action="store_true", help="Claim returned records for one subscription worker")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    created_after = (
        parse_utc_timestamp(args.created_after, setting_name="--created-after")
        if args.created_after.strip()
        else None
    )
    queue = SubscriptionQueue(get_settings())
    items = queue.next_batch(
        batch_size=args.batch_size,
        created_after=created_after,
        dry_run=not args.claim,
    )
    payload: dict[str, Any] = {
        "dry_run": not args.claim,
        "eligible_count": len(items),
        "claimed_count": 0 if not args.claim else len(items),
    }
    if args.claim:
        payload["record_ids"] = [item.record_id for item in items]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
