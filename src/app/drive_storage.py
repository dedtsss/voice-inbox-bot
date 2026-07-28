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
DRIVE_ITEM_KEY_PROPERTY = "voiceInboxItemKey"
DRIVE_FILE_KEY_PROPERTY = "voiceInboxFileKey"
DRIVE_SHA256_PROPERTY = "voiceInboxSha256"
DRIVE_KIND_PROPERTY = "voiceInboxKind"
SAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9._-]+")
SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE),
    re.compile(r"((?:access|refresh|client)_token['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", re.IGNORECASE),
    re.compile(r"(client_secret['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|authorization)['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", re.IGNORECASE),
    re.compile(r"((?:sk-|pat|gh[opsur]_))[A-Za-z0-9_-]+", re.IGNORECASE),
)


class DriveStorageError(RuntimeError):
    pass


class DriveSpoolError(DriveStorageError):
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


@dataclass(frozen=True)
class DriveStorageState:
    storage: DriveStorage | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.storage is not None and not self.error


def new_item_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_drive_storage(settings: Settings) -> DriveStorage | None:
    if not settings.google_drive_enabled:
        return None
    return GoogleDriveStorage(settings)


def build_drive_storage_fail_safe(settings: Settings) -> DriveStorageState:
    if not settings.google_drive_enabled:
        return DriveStorageState(storage=None, error="Google Drive storage is disabled")
    try:
        storage = build_drive_storage(settings)
    except Exception as exc:
        return DriveStorageState(
            storage=None,
            error=f"Google Drive storage initialization failed: {safe_error(exc)}",
        )
    if storage is None:
        return DriveStorageState(storage=None, error="Google Drive storage is unavailable")
    return DriveStorageState(storage=storage)


def build_google_drive_service(settings: Settings):
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
            save_refreshed_google_drive_token(token_file, creds)
        if not creds.valid:
            raise DriveStorageError("Google Drive OAuth credentials are not valid")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def save_refreshed_google_drive_token(token_file: Path, creds: Any) -> None:
    try:
        token_file.write_text(creds.to_json())
        os.chmod(token_file, 0o600)
    except OSError:
        logger.warning("Could not persist refreshed Google Drive OAuth token")


class GoogleDriveStorage:
    def __init__(self, settings: Settings) -> None:
        if not settings.google_drive_root_folder_id:
            raise DriveStorageError("GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured")
        self.settings = settings
        self.root_folder_id = settings.google_drive_root_folder_id
        self.service = build_google_drive_service(settings)

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
        item_key = drive_item_key(item_id)
        folder = self._find_by_property(
            self.root_folder_id,
            DRIVE_ITEM_KEY_PROPERTY,
            item_key,
            mime_type=DRIVE_FOLDER_MIME_TYPE,
        )
        if folder is None:
            folder = self._find_legacy_item_folder(folder_name, item_id, item_key)
        if folder is None:
            folder = self._create_folder(folder_name, self.root_folder_id, item_key=item_key)

        stored_files: list[DriveStoredFile] = []
        for index, file in enumerate(files, start=1):
            file_key = drive_file_key(item_id, f"original:{index}:{file.name}")
            existing = self._find_by_property(folder["id"], DRIVE_FILE_KEY_PROPERTY, file_key)
            if existing is None:
                existing = self._find_verified_legacy_file(folder["id"], file)
                if existing is not None:
                    existing = self._set_file_identity(existing["id"], file_key=file_key, sha256=file.sha256)
            if (
                existing
                and str(existing.get("size") or "") == str(file.size)
                and (existing.get("appProperties") or {}).get(DRIVE_SHA256_PROPERTY) == file.sha256
            ):
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
            if existing:
                created = self._update_bytes(
                    file_id=existing["id"],
                    name=file.name,
                    mime_type=file.mime_type,
                    content=file.content,
                    app_properties={
                        DRIVE_KIND_PROPERTY: "original",
                        DRIVE_FILE_KEY_PROPERTY: file_key,
                        DRIVE_SHA256_PROPERTY: file.sha256,
                    },
                )
            else:
                created = self._upload_bytes(
                    parent_id=folder["id"],
                    name=file.name,
                    mime_type=file.mime_type,
                    content=file.content,
                    app_properties={
                        DRIVE_KIND_PROPERTY: "original",
                        DRIVE_FILE_KEY_PROPERTY: file_key,
                        DRIVE_SHA256_PROPERTY: file.sha256,
                    },
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
        manifest_key = drive_file_key(item_id, "manifest")
        manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
        existing_manifest = self._find_by_property(folder["id"], DRIVE_FILE_KEY_PROPERTY, manifest_key)
        if existing_manifest is None:
            existing_manifest = self._find_verified_legacy_manifest(folder["id"], item_id)
            if existing_manifest is not None:
                existing_manifest = self._set_file_identity(
                    existing_manifest["id"],
                    file_key=manifest_key,
                    sha256=manifest_sha256,
                    kind="manifest",
                )
        if (
            existing_manifest
            and str(existing_manifest.get("size") or "") == str(len(manifest_content))
            and (existing_manifest.get("appProperties") or {}).get(DRIVE_SHA256_PROPERTY) == manifest_sha256
        ):
            manifest_file = existing_manifest
        elif existing_manifest:
            manifest_file = self._update_bytes(
                file_id=existing_manifest["id"],
                name="manifest.json",
                mime_type="application/json",
                content=manifest_content,
                app_properties={
                    DRIVE_KIND_PROPERTY: "manifest",
                    DRIVE_FILE_KEY_PROPERTY: manifest_key,
                    DRIVE_SHA256_PROPERTY: manifest_sha256,
                },
            )
        else:
            manifest_file = self._upload_bytes(
                parent_id=folder["id"],
                name="manifest.json",
                mime_type="application/json",
                content=manifest_content,
                app_properties={
                    DRIVE_KIND_PROPERTY: "manifest",
                    DRIVE_FILE_KEY_PROPERTY: manifest_key,
                    DRIVE_SHA256_PROPERTY: manifest_sha256,
                },
            )

        return DriveStoredItem(
            item_id=item_id,
            folder_id=folder["id"],
            folder_url=folder_url(folder["id"]),
            manifest_file_id=manifest_file["id"],
            files=stored_files,
            manifest=manifest,
        )

    def _find_children(self, parent_id: str, name: str, mime_type: str | None = None) -> list[dict[str, Any]]:
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = [f"'{parent_id}' in parents", "trashed = false", f"name = '{escaped_name}'"]
        if mime_type:
            query.append(f"mimeType = '{mime_type}'")
        response = (
            self.service.files()
            .list(
                q=" and ".join(query),
                spaces="drive",
                fields="files(id,name,mimeType,size,webViewLink,appProperties)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        return response.get("files") or []

    def _find_child(self, parent_id: str, name: str, mime_type: str | None = None) -> dict[str, Any] | None:
        files = self._find_children(parent_id, name, mime_type)
        return files[0] if files else None

    def _find_by_property(
        self,
        parent_id: str,
        property_name: str,
        property_value: str,
        *,
        mime_type: str | None = None,
    ) -> dict[str, Any] | None:
        escaped_property_name = _drive_query_value(property_name)
        escaped_property_value = _drive_query_value(property_value)
        query = [
            f"'{_drive_query_value(parent_id)}' in parents",
            "trashed = false",
            (
                "appProperties has { "
                f"key='{escaped_property_name}' and value='{escaped_property_value}'"
                " }"
            ),
        ]
        if mime_type:
            query.append(f"mimeType = '{_drive_query_value(mime_type)}'")
        response = (
            self.service.files()
            .list(
                q=" and ".join(query),
                spaces="drive",
                fields="files(id,name,mimeType,size,webViewLink,appProperties)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files") or []
        if len(files) > 1:
            raise DriveStorageError("Google Drive contains duplicate stable item identities")
        return files[0] if files else None

    def _find_legacy_item_folder(
        self,
        folder_name: str,
        item_id: str,
        item_key: str,
    ) -> dict[str, Any] | None:
        folders = self._find_children(self.root_folder_id, folder_name, mime_type=DRIVE_FOLDER_MIME_TYPE)
        if not folders:
            return None
        if len(folders) != 1:
            raise DriveStorageError("Google Drive legacy item folder is ambiguous")
        folder = folders[0]
        manifest_file = self._find_verified_legacy_manifest(folder["id"], item_id)
        if manifest_file is None:
            raise DriveStorageError("Google Drive legacy item folder has no verifiable manifest")
        return self._set_folder_identity(folder["id"], item_key)

    def _find_verified_legacy_file(
        self,
        parent_id: str,
        file: DriveUploadFile,
    ) -> dict[str, Any] | None:
        candidates = self._find_children(parent_id, file.name)
        candidates = [
            candidate
            for candidate in candidates
            if not (candidate.get("appProperties") or {}).get(DRIVE_FILE_KEY_PROPERTY)
        ]
        if not candidates:
            return None
        matching: list[dict[str, Any]] = []
        for candidate in candidates:
            if str(candidate.get("size") or "") != str(file.size):
                continue
            content = self._download_bytes(candidate["id"], max_bytes=file.size)
            if hashlib.sha256(content).hexdigest() == file.sha256:
                matching.append(candidate)
        if len(matching) != 1:
            raise DriveStorageError("Google Drive legacy original is ambiguous")
        return matching[0]

    def _find_verified_legacy_manifest(self, parent_id: str, item_id: str) -> dict[str, Any] | None:
        matching: list[dict[str, Any]] = []
        for candidate in self._find_children(parent_id, "manifest.json"):
            if (candidate.get("appProperties") or {}).get(DRIVE_FILE_KEY_PROPERTY):
                continue
            try:
                payload = json.loads(self._download_bytes(candidate["id"], max_bytes=1_000_000).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError, DriveStorageError):
                continue
            if isinstance(payload, dict) and str(payload.get("item_id") or "") == item_id:
                matching.append(candidate)
        if len(matching) > 1:
            raise DriveStorageError("Google Drive legacy manifest is ambiguous")
        return matching[0] if matching else None

    def _create_folder(self, name: str, parent_id: str, *, item_key: str) -> dict[str, Any]:
        metadata = {
            "name": name,
            "mimeType": DRIVE_FOLDER_MIME_TYPE,
            "parents": [parent_id],
            "appProperties": {
                DRIVE_KIND_PROPERTY: "item",
                DRIVE_ITEM_KEY_PROPERTY: item_key,
            },
        }
        return (
            self.service.files()
            .create(
                body=metadata,
                fields="id,name,mimeType,webViewLink,appProperties",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _upload_bytes(
        self,
        *,
        parent_id: str,
        name: str,
        mime_type: str,
        content: bytes,
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from googleapiclient.http import MediaIoBaseUpload

        metadata: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if app_properties:
            metadata["appProperties"] = app_properties
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        return (
            self.service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,size,webViewLink,appProperties",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _update_bytes(
        self,
        *,
        file_id: str,
        name: str,
        mime_type: str,
        content: bytes,
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        metadata: dict[str, Any] = {"name": name}
        if app_properties:
            metadata["appProperties"] = app_properties
        return (
            self.service.files()
            .update(
                fileId=file_id,
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,size,webViewLink,appProperties",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _set_folder_identity(self, folder_id: str, item_key: str) -> dict[str, Any]:
        return (
            self.service.files()
            .update(
                fileId=folder_id,
                body={
                    "appProperties": {
                        DRIVE_KIND_PROPERTY: "item",
                        DRIVE_ITEM_KEY_PROPERTY: item_key,
                    }
                },
                fields="id,name,mimeType,size,webViewLink,appProperties",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _set_file_identity(
        self,
        file_id: str,
        *,
        file_key: str,
        sha256: str,
        kind: str = "original",
    ) -> dict[str, Any]:
        return (
            self.service.files()
            .update(
                fileId=file_id,
                body={
                    "appProperties": {
                        DRIVE_KIND_PROPERTY: kind,
                        DRIVE_FILE_KEY_PROPERTY: file_key,
                        DRIVE_SHA256_PROPERTY: sha256,
                    }
                },
                fields="id,name,mimeType,size,webViewLink,appProperties",
                supportsAllDrives=True,
            )
            .execute()
        )

    def _download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise DriveStorageError("Google Drive dependencies are not installed") from exc
        target = io.BytesIO()
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(target, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if target.tell() > max_bytes:
                raise DriveStorageError("Google Drive legacy file exceeds the expected size")
        return target.getvalue()


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


def drive_item_key(item_id: str) -> str:
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()


def drive_file_key(item_id: str, name: str) -> str:
    identity = f"{item_id}\0{name}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


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
