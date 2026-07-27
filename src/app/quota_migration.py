from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.airtable import AirtableClient
from app.config import Settings
from app.voice_processor import get_field


@dataclass(frozen=True)
class QuotaMigrationResult:
    matched: int
    migrated: int
    dry_run: bool


def is_exclusive_insufficient_quota_review(record: dict[str, Any], settings: Settings) -> bool:
    fields = record.get("fields") or {}
    status = str(
        get_field(
            fields,
            settings.voice_field_processing_status,
            settings.voice_field_processing_status_query_name,
            "Статус обработки",
        )
        or ""
    ).strip()
    error = str(get_field(fields, settings.voice_field_processing_error, "Ошибка обработки") or "").strip()
    error_lower = error.casefold()
    if status != "Needs Review" or "insufficient_quota" not in error_lower:
        return False
    if "voice_processor" not in error_lower and "openai" not in error_lower:
        return False

    snapshot_raw = get_field(fields, settings.voice_field_ai_result_json, "AI результат JSON")
    if isinstance(snapshot_raw, str) and snapshot_raw.strip():
        try:
            snapshot = json.loads(snapshot_raw)
        except json.JSONDecodeError:
            return False
        validated = snapshot.get("validated") if isinstance(snapshot, dict) else None
        reasons = validated.get("needs_review_reasons") if isinstance(validated, dict) else None
        if isinstance(reasons, list) and any(str(reason).strip() for reason in reasons):
            return False
    return True


def migrate_insufficient_quota_records(
    settings: Settings,
    airtable: AirtableClient,
    *,
    dry_run: bool,
    max_records: int = 1000,
) -> QuotaMigrationResult:
    candidates = airtable.list_insufficient_quota_review_records(max_records=max_records)
    matched = [record for record in candidates if is_exclusive_insufficient_quota_review(record, settings)]
    if not dry_run:
        for record in matched:
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            airtable.update_voice_record_fields(
                record_id,
                {
                    settings.voice_field_processing_route: "ChatGPT Subscription",
                    settings.voice_field_processing_status: "Awaiting Subscription",
                },
            )
    return QuotaMigrationResult(
        matched=len(matched),
        migrated=0 if dry_run else len(matched),
        dry_run=dry_run,
    )
