#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from app.airtable import AirtableClient
from app.config import get_settings
from app.quota_migration import migrate_insufficient_quota_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate quota-only Needs Review records safely")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print only the matching record count")
    mode.add_argument("--apply", action="store_true", help="Apply the idempotent migration")
    parser.add_argument("--max-records", type=int, default=1000)
    args = parser.parse_args()

    settings = get_settings()
    result = migrate_insufficient_quota_records(
        settings,
        AirtableClient(settings),
        dry_run=not args.apply,
        max_records=max(1, args.max_records),
    )
    print(
        json.dumps(
            {
                "dry_run": result.dry_run,
                "matched": result.matched,
                "migrated": result.migrated,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
