#!/usr/bin/env python3
from __future__ import annotations

import json

from app.airtable import AirtableClient
from app.config import get_settings


def main() -> None:
    settings = get_settings()
    created = AirtableClient(settings).ensure_voice_inbox_metadata_fields()
    print(json.dumps({"created": created}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
