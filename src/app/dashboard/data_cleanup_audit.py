from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Iterable

from app.select_options import canonical_select_options, clean_select_value

CONTENT_MEDIA_TYPES = {"text", "voice", "photo", "video", "file", "mixed", "audio"}
TECHNICAL_PATTERNS = ("smoke", "canary", "production test", "tg-smoke", "dashboard-canary")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def clean_choice(value: Any) -> str:
    return clean_select_value(value)


def choice_key(value: Any) -> str:
    return clean_choice(value).casefold()


def type_alias_groups(active_choices: Iterable[Any], record_counts: Counter[str]) -> list[dict[str, Any]]:
    """List only true aliases and their real-record counts (zero included)."""
    aliases = {clean_choice(value) for value in active_choices if clean_choice(value)} | set(record_counts)
    grouped: dict[str, list[str]] = {}
    for value in aliases:
        grouped.setdefault(choice_key(value), []).append(value)
    groups: list[dict[str, Any]] = []
    for key, aliases in grouped.items():
        normalized_aliases = sorted(aliases, key=lambda item: (item.casefold(), item))
        if len(normalized_aliases) > 1 or any(alias != alias.strip() for alias in aliases):
            groups.append(
                {
                    "normalized": key,
                    "variants": [{"value": alias, "count": record_counts[alias]} for alias in normalized_aliases],
                }
            )
    return sorted(groups, key=lambda item: item["normalized"])


def external_id_bucket(value: Any) -> tuple[str, str | None]:
    """Return a non-sensitive ID shape and an unambiguous source, if one exists."""
    text = clean_choice(value)
    lowered = text.casefold()
    if not text:
        return "empty", None
    if lowered.startswith("telegram:"):
        return "telegram", "Telegram"
    if lowered.startswith("android:"):
        return "android", "Android"
    if UUID_RE.fullmatch(text):
        return "uuid", None
    if ":" in text:
        return "other_prefixed", None
    return "unprefixed", None


def is_technical(fields: dict[str, Any], text_fields: Iterable[str]) -> bool:
    haystack = " ".join(str(fields.get(field) or "") for field in text_fields).casefold()
    return any(pattern in haystack for pattern in TECHNICAL_PATTERNS)


def parse_training_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else payload
    return clean_choice(answers.get("type")) if isinstance(answers, dict) else ""


def aggregate_audit(
    records: Iterable[dict[str, Any]],
    *,
    fields: dict[str, str],
    active_type_choices: Iterable[str],
    training_taxonomy_types: Iterable[str] = (),
) -> dict[str, Any]:
    records = list(records)
    type_counts: Counter[str] = Counter()
    training_type_counts: Counter[str] = Counter()
    missing = []
    for record in records:
        record_fields = record.get("fields") or {}
        entry_type = clean_choice(record_fields.get(fields["entry_type"]))
        if entry_type:
            type_counts[entry_type] += 1
        training_type = parse_training_type(record_fields.get(fields["training_answers_json"]))
        if training_type:
            training_type_counts[training_type] += 1
        if not clean_choice(record_fields.get(fields["source"])):
            missing.append(record)

    dates = [str(record.get("createdTime") or "")[:10] for record in missing if str(record.get("createdTime") or "")[:10]]
    status_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    id_buckets: Counter[str] = Counter()
    deterministic_sources: Counter[str] = Counter()
    technical = 0
    legacy_route_missing = 0
    google_drive_bundle = 0
    for record in missing:
        record_fields = record.get("fields") or {}
        status_counts[clean_choice(record_fields.get(fields["status"])) or "empty"] += 1
        route = clean_choice(record_fields.get(fields["processing_route"]))
        route_counts[route or "empty"] += 1
        if not route:
            legacy_route_missing += 1
        bucket, source = external_id_bucket(record_fields.get(fields["external_id"]))
        id_buckets[bucket] += 1
        if source:
            deterministic_sources[source] += 1
        if clean_choice(record_fields.get(fields["google_drive"])):
            google_drive_bundle += 1
        if is_technical(
            record_fields,
            (fields["title"], fields["raw_text"], fields["clean_text"], fields["external_id"], fields["notes"]),
        ):
            technical += 1

    canonical_types = canonical_select_options(active_type_choices)
    media_choices = [choice for choice in canonical_types if choice_key(choice) in CONTENT_MEDIA_TYPES]
    deterministic_total = sum(deterministic_sources.values())
    return {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "type_audit": {
            "active_choices": list(active_type_choices),
            "record_counts": dict(sorted(type_counts.items(), key=lambda item: (item[0].casefold(), item[0]))),
            "case_or_space_alias_groups": type_alias_groups(active_type_choices, type_counts),
            "content_or_media_choices": media_choices,
            "canonical_casefolded_choices": canonical_types,
            "training_taxonomy_types": canonical_select_options(training_taxonomy_types),
            "training_answer_type_counts": dict(sorted(training_type_counts.items(), key=lambda item: (item[0].casefold(), item[0]))),
        },
        "missing_source_audit": {
            "total": len(missing),
            "created_date_range": [min(dates), max(dates)] if dates else [],
            "by_status": dict(sorted(status_counts.items())),
            "by_processing_route": dict(sorted(route_counts.items())),
            "by_external_id_shape": dict(sorted(id_buckets.items())),
            "with_google_drive_bundle": google_drive_bundle,
            "technical_by_existing_pattern": technical,
            "legacy_indicated_by_missing_processing_route": legacy_route_missing,
            "manual": "not_determinable_from_stable_machine_fields",
            "deterministic_source_candidates": dict(sorted(deterministic_sources.items())),
            "deterministically_backfillable": deterministic_total,
            "remaining_undetermined": len(missing) - deterministic_total,
        },
    }
