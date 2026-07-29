#!/usr/bin/env python3
"""Safely migrate Voice Inbox `Тип` values that differ only by case or spaces.

The script never translates semantic or media/content categories. It prints only
type names and aggregate counts; private backups contain record IDs and old type
values, but no texts, URLs, names, or secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.airtable import AirtableClient
from app.config import get_settings
from app.dashboard.type_migration import build_case_only_type_plan


def field_name(table: dict[str, Any], configured: str, fallback: str) -> str:
    for field in table.get("fields") or []:
        if configured in {field.get("id"), field.get("name")} or field.get("name") == fallback:
            return str(field.get("name") or configured or fallback)
    return configured or fallback


def select_choices(table: dict[str, Any], configured: str, fallback: str) -> list[str]:
    for field in table.get("fields") or []:
        if configured in {field.get("id"), field.get("name")} or field.get("name") == fallback:
            return [
                str(choice.get("name") or "").strip()
                for choice in (field.get("options") or {}).get("choices") or []
                if str(choice.get("name") or "").strip()
            ]
    return []


def write_private_backup(path: Path, patches: list[Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        "kind": "voice-inbox-case-only-type-migration",
        "records": [{"id": patch.record_id, "type": patch.before} for patch in patches],
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as backup:
            json.dump(payload, backup, ensure_ascii=False, sort_keys=True)
            backup.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def safe_report(*, dry_run: bool, aliases: dict[str, str], before: dict[str, int], after: dict[str, int], matched: int, migrated: int, limited: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "case_only_aliases": dict(sorted(aliases.items(), key=lambda item: item[0].casefold())),
        "counts_before": before,
        "counts_after": after,
        "matched": matched,
        "migrated": migrated,
        "records_scan_limited": limited,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Case-only Voice Inbox type migration")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", help="new private backup path; required for --apply when matches exist")
    parser.add_argument("--max-records", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()
    settings = get_settings()
    client = AirtableClient(settings)
    table = client.find_table_metadata(settings.voice_inbox_base_id, table_id=settings.voice_inbox_table_id)
    if not table:
        raise RuntimeError("Voice Inbox table metadata was not found")
    type_field = field_name(table, settings.voice_field_type, "Тип")
    records, limited = client.list_voice_records_limited(params=[("fields[]", type_field)], max_records=max(1, args.max_records))
    patches, before, after, aliases = build_case_only_type_plan(
        records,
        type_field=type_field,
        choices=select_choices(table, settings.voice_field_type, "Тип"),
    )
    if args.dry_run:
        print(json.dumps(safe_report(dry_run=True, aliases=aliases, before=before, after=after, matched=len(patches), migrated=0, limited=limited), ensure_ascii=False, sort_keys=True))
        return
    if limited:
        raise RuntimeError("Refusing apply: records scan hit --max-records")
    if patches and not args.backup:
        raise RuntimeError("Refusing apply without --backup")
    if args.backup:
        backup_path = Path(args.backup)
        if backup_path.exists():
            raise RuntimeError("Refusing to overwrite an existing backup")
        write_private_backup(backup_path, patches)
    migrated = 0
    for patch in patches:
        client.update_voice_record_fields(patch.record_id, {type_field: patch.after})
        migrated += 1
    verified, verification_limited = client.list_voice_records_limited(params=[("fields[]", type_field)], max_records=max(1, args.max_records))
    verified_patches, _, verified_after, _ = build_case_only_type_plan(
        verified,
        type_field=type_field,
        choices=select_choices(table, settings.voice_field_type, "Тип"),
    )
    if verification_limited or len(verified) != len(records) or verified_patches:
        raise RuntimeError("Post-migration verification failed; use the private backup before retrying")
    print(json.dumps(safe_report(dry_run=False, aliases=aliases, before=before, after=verified_after, matched=len(patches), migrated=migrated, limited=False), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
