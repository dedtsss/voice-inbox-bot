#!/usr/bin/env python3
"""Emit only anonymised, aggregate dashboard-cleanup audit data.

This script never prints record IDs, titles, texts, URLs, filenames, or secrets.
It is deliberately read-only.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from app.airtable import AirtableClient
from app.config import get_settings
from app.dashboard.data_cleanup_audit import aggregate_audit


def field_name(table: dict[str, Any], configured: str, fallback: str) -> str:
    for field in table.get("fields") or []:
        if configured in {field.get("id"), field.get("name")} or fallback == field.get("name"):
            return str(field.get("name") or configured or fallback)
    return configured or fallback


def select_choices(table: dict[str, Any], configured: str, fallback: str) -> list[str]:
    for field in table.get("fields") or []:
        if configured in {field.get("id"), field.get("name")} or fallback == field.get("name"):
            return [
                str(choice.get("name") or "").strip()
                for choice in (field.get("options") or {}).get("choices") or []
                if str(choice.get("name") or "").strip()
            ]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only anonymised Voice Inbox cleanup audit")
    parser.add_argument("--max-records", type=int, default=10_000)
    args = parser.parse_args()
    settings = get_settings()
    client = AirtableClient(settings)
    table = client.find_table_metadata(settings.voice_inbox_base_id, table_id=settings.voice_inbox_table_id)
    if not table:
        raise RuntimeError("Voice Inbox table metadata was not found")
    fields = {
        "title": field_name(table, settings.voice_field_title, "Название"),
        "entry_type": field_name(table, settings.voice_field_type, "Тип"),
        "source": field_name(table, settings.voice_field_source, "Источник"),
        "status": field_name(table, settings.voice_field_processing_status, "Статус обработки"),
        "processing_route": field_name(table, settings.voice_field_processing_route, "Processing Route"),
        "external_id": field_name(table, settings.voice_field_external_id, "External ID"),
        "google_drive": field_name(table, settings.voice_field_google_drive, "Google Drive"),
        "raw_text": field_name(table, settings.voice_field_raw_text, "Исходная фраза"),
        "clean_text": field_name(table, settings.voice_field_clean_text, "Очищенный текст"),
        "notes": field_name(table, settings.voice_field_notes, "Notes"),
        "training_answers_json": field_name(table, settings.voice_field_training_answers_json, "Training Answers JSON"),
    }
    params = [("fields[]", field) for field in dict.fromkeys(fields.values()) if field]
    records, limited = client.list_voice_records_limited(params=params, max_records=max(1, args.max_records))
    taxonomy_types: list[str] = []
    taxonomy = client.find_table_metadata(settings.voice_inbox_base_id, table_name=settings.voice_training_taxonomy_table_name)
    if taxonomy:
        taxonomy_type_field = field_name(taxonomy, "Тип", "Тип")
        taxonomy_records = client.list_taxonomy_records(page_size=1000)
        taxonomy_types = [str((record.get("fields") or {}).get(taxonomy_type_field) or "") for record in taxonomy_records]
    report = aggregate_audit(
        records,
        fields=fields,
        active_type_choices=select_choices(table, settings.voice_field_type, "Тип"),
        training_taxonomy_types=taxonomy_types,
    )
    report["records_scanned"] = len(records)
    report["records_scan_limited"] = limited
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
