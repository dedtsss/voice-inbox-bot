from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_processing_routes import make_settings

from app.drive_storage import (
    DriveStorageError,
    save_refreshed_google_drive_token,
    verify_google_drive_token_persistence,
)


def oauth_settings(tmp_path: Path):
    client = tmp_path / "secrets" / "client.json"
    token = tmp_path / "secrets" / "token.json"
    client.parent.mkdir(mode=0o700)
    client.write_text(json.dumps({"installed": {"client_id": "client"}}), encoding="utf-8")
    token.write_text(
        json.dumps(
            {
                "token": "old-access",
                "refresh_token": "refresh-private",
                "client_id": "client-private",
                "client_secret": "secret-private",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(client, 0o600)
    os.chmod(token, 0o600)
    settings = make_settings(
        tmp_path,
        GOOGLE_DRIVE_ENABLED=True,
        GOOGLE_DRIVE_CREDENTIALS_FILE=str(client),
        GOOGLE_DRIVE_TOKEN_FILE=str(token),
    )
    return settings, token


def test_oauth_token_refresh_is_atomic_private_and_backed_up(tmp_path: Path) -> None:
    settings, token = oauth_settings(tmp_path)
    refreshed = {
        "token": "new-access",
        "refresh_token": "refresh-private",
        "client_id": "client-private",
        "client_secret": "secret-private",
    }
    save_refreshed_google_drive_token(token, SimpleNamespace(to_json=lambda: json.dumps(refreshed)))
    assert json.loads(token.read_text(encoding="utf-8"))["token"] == "new-access"
    assert json.loads((token.parent / "token.json.bak").read_text(encoding="utf-8"))["token"] == "old-access"
    assert token.stat().st_mode & 0o077 == 0
    verify_google_drive_token_persistence(settings)
    assert not list(token.parent.glob(".oauth-persistence-probe-*"))


def test_oauth_persistence_fails_for_non_writable_runtime_secret_path(tmp_path: Path) -> None:
    settings, token = oauth_settings(tmp_path)
    token.parent.chmod(0o500)
    try:
        if os.access(token.parent, os.W_OK):
            pytest.skip("current user can bypass directory permissions")
        with pytest.raises(DriveStorageError, match="persistence is unavailable"):
            verify_google_drive_token_persistence(settings)
    finally:
        token.parent.chmod(0o700)
