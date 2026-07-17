from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings

logger = logging.getLogger(__name__)

DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
SAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9._-]+")
SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE),
    re.compile(r"((?:access|refresh|client)_token['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", re.IGNORECASE),
    re.compile(r"(client_secret['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", re.IGNORECASE),
)


class DriveStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveUploadFile:
    name: str
    mime_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class DriveStoredFile:
    name: str
    mime_type: str
    size: int
    drive_file_id: str
    web_view_link: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class DriveStoredItem:
    item_id: str
    folder_id: str
    folder_url: str
    manifest_file_id: str
    files: list[DriveStoredFile]
    manifest: dict[str, Any]


class DriveStorage(Protocol):
    def store_item(
        self,
        *,
        item_id: str,
        created_at: datetime,
        source: str,
        message_type: str,
        text: str | None,
        files: list[DriveUploadFile],
        extra: dict[str, Any] | None = None,
    ) -> DriveStoredItem:
        ...


def new_item_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_drive_storage(settings: Settings) -> DriveStorage | None:
    if not settings.google_drive_enabled:
        return None
    return GoogleDriveStorage(settings)


class GoogleDriveStorage:
    def __init__(self, settings: Settings) -> None:
        if not settings.google_drive_root_folder_id:
            raise DriveStorageError("GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured")
        self.settings = settings
        self.root_folder_id = settings.google_drive_root_folder_id
        self.service = self._build_service(settings)

    def store_item(
        self,
        *,
        item_id: str,
        created_at: datetime,
        source: str,
        message_type: str,
        text: str | None,
        files: list[DriveUploadFile],
        extra: dict[str, Any] | None = None,
    ) -> DriveStoredItem:
        folder_name = item_folder_name(created_at, item_id)
        folder = self._find_child(self.root_folder_id, folder_name, mime_type=DRIVE_FOLDER_MIME_TYPE)
        if folder is None:
            folder = self._create_folder(folder_name, self.root_folder_id)

        stored_files: list[DriveStoredFile] = []
        for file in files:
            existing = self._find_child(folder["id"], file.name)
            if existing and str(existing.get("size") or "") == str(file.size):
                stored_files.append(
                    DriveStoredFile(
                        name=file.name,
                        mime_type=existing.get("mimeType") or file.mime_type,
                        size=int(existing.get("size") or file.size),
                        drive_file_id=existing["id"],
                        web_view_link=existing.get("webViewLink"),
                        sha256=file.sha256,
                    )
                )
                continue
            created = self._upload_bytes(
                parent_id=folder["id"],
                name=file.name,
                mime_type=file.mime_type,
                content=file.content,
            )
            stored_files.append(
                DriveStoredFile(
                    name=file.name,
                    mime_type=created.get("mimeType") or file.mime_type,
                    size=int(created.get("size") or file.size),
                    drive_file_id=created["id"],
                    web_view_link=created.get("webViewLink"),
                    sha256=file.sha256,
                )
            )

        manifest = build_manifest(
            item_id=item_id,
            created_at=created_at,
            source=source,
            message_type=message_type,
            text=text,
            files=stored_files,
            extra=extra,
        )
        manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        existing_manifest = self._find_child(folder["id"], "manifest.json")
        if existing_manifest:
            manifest_file = self._update_bytes(
                file_id=existing_manifest["id"],
                name="manifest.json",
                mime_type="application/json",
                content=manifest_content,
            )
        else:
            manifest_file = self._upload_bytes(
                parent_id=folder["id"],
                name="manifest.json",
                mime_type="application/json",
                content=manifest_content,
            )

        return DriveStoredItem(
            item_id=item_id,
            folder_id=folder["id"],
            folder_url=folder_url(folder["id"]),
            manifest_file_id=manifest_file["id"],
            files=stored_files,
            manifest=manifest,
        )

    def _build_service(self, settings: Settings):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import credentials as oauth_credentials
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveStorageError("Google Drive dependencies are not installed") from exc

        credentials_file = Path(settings.google_drive_credentials_file)
        token_file = Path(settings.google_drive_token_file) if settings.google_drive_token_file else None
        if not credentials_file.exists():
            raise DriveStorageError("Google Drive credentials file is missing")

        try:
            credentials_payload = json.loads(credentials_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DriveStorageError("Google Drive credentials file is invalid") from exc

        if credentials_payload.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=DRIVE_SCOPES)
        else:
            if token_file is None or not token_file.exists():
                raise DriveStorageError("Google Drive OAuth token file is missing")
            creds = oauth_credentials.Credentials.from_authorized_user_file(str(token_file), DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._save_refreshed_token(token_file, creds)
            if not creds.valid:
                raise DriveStorageError("Google Drive OAuth credentials are not valid")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _save_refreshed_token(self, token_file: Path, creds: Any) -> None:
        try:
            token_file.write_text(creds.to_json())
            os.chmod(token_file, 0o600)
        except OSError:
            logger.warning("Could not persist refreshed Google Drive OAuth token")

    def _find_child(self, parent_id: str, name: str, mime_type: str | None = None) -> dict[str, Any] | None:
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = [f"'{parent_id}' in parents", "trashed = false", f"name = '{escaped_name}'"]
        if mime_type:
            query.append(f"mimeType = '{mime_type}'")
        response = (
            self.service.files()
            .list(
                q=" and ".join(query),
                spaces="drive",
                fields="files(id,name,mimeType,size,webViewLink)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files") or []
        return files[0] if files else None

    def _create_folder(self, name: str, parent_id: str) -> dict[str, Any]:
        metadata = {"name": name, "mimeType": DRIVE_FOLDER_MIME_TYPE, "parents": [parent_id]}
        return (
            self.service.files()
            .create(
                body=metadata,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _upload_bytes(self, *, parent_id: str, name: str, mime_type: str, content: bytes) -> dict[str, Any]:
        from googleapiclient.http import MediaIoBaseUpload

        metadata = {"name": name, "parents": [parent_id]}
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        return (
            self.service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,size,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _update_bytes(self, *, file_id: str, name: str, mime_type: str, content: bytes) -> dict[str, Any]:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        return (
            self.service.files()
            .update(
                fileId=file_id,
                body={"name": name},
                media_body=media,
                fields="id,name,mimeType,size,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )


def build_manifest(
    *,
    item_id: str,
    created_at: datetime,
    source: str,
    message_type: str,
    text: str | None,
    files: list[DriveStoredFile],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "item_id": item_id,
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "source": source,
        "type": message_type.strip().casefold() or "message",
        "text": text if text else None,
        "files": [
            {
                "name": file.name,
                "mime_type": file.mime_type,
                "size": file.size,
                "drive_file_id": file.drive_file_id,
                **({"sha256": file.sha256} if file.sha256 else {}),
            }
            for file in files
        ],
    }
    if extra:
        manifest["extra"] = extra
    return manifest


def item_folder_name(created_at: datetime, item_id: str) -> str:
    safe_item_id = SAFE_FOLDER_RE.sub("_", item_id.strip()).strip("._") or new_item_id()
    return f"{created_at.astimezone(UTC).date().isoformat()}_{safe_item_id}"


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def spool_drive_item(
    *,
    settings: Settings,
    item_id: str,
    created_at: datetime,
    source: str,
    message_type: str,
    text: str | None,
    files: list[DriveUploadFile],
    error: str,
    extra: dict[str, Any] | None = None,
) -> Path:
    spool_root = Path(settings.google_drive_spool_dir)
    folder = spool_root / item_folder_name(created_at, item_id)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)

    stored_files: list[DriveStoredFile] = []
    for file in files:
        target = folder / safe_file_name(file.name)
        target.write_bytes(file.content)
        os.chmod(target, 0o600)
        stored_files.append(
            DriveStoredFile(
                name=file.name,
                mime_type=file.mime_type,
                size=file.size,
                drive_file_id="spooled",
                sha256=file.sha256,
            )
        )

    manifest = build_manifest(
        item_id=item_id,
        created_at=created_at,
        source=source,
        message_type=message_type,
        text=text,
        files=stored_files,
        extra={**(extra or {}), "drive_error": safe_error(error), "spooled": True},
    )
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    os.chmod(manifest_path, 0o600)
    return folder


def safe_file_name(name: str) -> str:
    cleaned = SAFE_FOLDER_RE.sub("_", name.strip()).strip("._")
    return cleaned or "file.bin"


def safe_error(error: Exception | str) -> str:
    message = str(error).replace("\n", " ")
    for pattern in SECRET_PATTERNS:
        message = pattern.sub(r"\1[redacted]", message)
    return message[:500]
