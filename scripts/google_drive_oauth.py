#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Google Drive OAuth token.json for Voice Inbox.")
    parser.add_argument("--client", required=True, help="Path to OAuth client JSON.")
    parser.add_argument("--token", required=True, help="Output path for OAuth token JSON.")
    parser.add_argument("--port", type=int, default=8090, help="Local callback port.")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser.")
    args = parser.parse_args()

    client_path = Path(args.client)
    token_path = Path(args.token)
    if not client_path.exists():
        raise SystemExit(f"Client file is missing: {client_path}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(
        port=args.port,
        open_browser=not args.no_browser,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Open this URL in a browser and approve Drive access:\n{url}\n",
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    print(f"Wrote OAuth token JSON to {token_path}")


if __name__ == "__main__":
    main()
