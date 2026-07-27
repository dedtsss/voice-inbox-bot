from __future__ import annotations

import argparse
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.airtable import AirtableClient
from app.config import Settings, get_settings, parse_utc_timestamp
from app.voice_processor import DriveOriginal, GoogleDriveInboxReader, get_field

logger = logging.getLogger(__name__)

TECHNICAL_PATTERNS = ("smoke", "canary", "production test", "tg-smoke", "dashboard-canary")


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
                claimed_at = datetime.now(UTC)
                claim = f"subscription_queue lock_id={uuid.uuid4().hex} claimed_at={claimed_at.isoformat()}"
                self.airtable.claim_subscription_queue_record(
                    item.record_id,
                    claim=claim,
                    claimed_at=claimed_at,
                )
                items.append(
                    SubscriptionQueueItem(
                        record_id=item.record_id,
                        external_id=item.external_id,
                        source=item.source,
                        entry_type=item.entry_type,
                        created_at=item.created_at,
                        raw_text=item.raw_text,
                        google_drive_url=item.google_drive_url,
                        claim=claim,
                    )
                )
            if len(items) >= limit:
                break
        return items

    def load_bundle(self, item: SubscriptionQueueItem, target_dir: Path) -> SubscriptionQueueBundle:
        manifest, originals = self.drive_reader.download_record_originals(item.google_drive_url, target_dir)
        return SubscriptionQueueBundle(item=item, manifest=manifest, originals=originals)


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
    if route != "ChatGPT Subscription" or status != "Awaiting Subscription" or not drive_url or claim:
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
