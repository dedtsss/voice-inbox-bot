#!/usr/bin/env python3
from __future__ import annotations

import json

from app.airtable import AirtableClient
from app.config import get_settings


def main() -> None:
    settings = get_settings()
    airtable = AirtableClient(settings)
    metadata_created = airtable.ensure_voice_inbox_metadata_fields()
    processor_schema = airtable.ensure_voice_processor_schema()
    print(
        json.dumps(
            {
                "metadata_created": metadata_created,
                "processor_schema": processor_schema,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
