from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile

from app.airtable import AirtableClient, AirtableError
from app.config import Settings
from app.drive_storage import (
    DriveStorage,
    DriveStorageError,
    DriveUploadFile,
    build_drive_storage,
    safe_error,
    spool_drive_item,
    utc_now,
)

logger = logging.getLogger(__name__)

ALLOWED_MESSAGE_TYPES = {"Text", "Voice", "Photo", "File", "Mixed"}
TEXT_PAYLOAD_KEYS = ("text", "raw_text", "source_text", "message", "caption", "description", "content")
TYPE_PAYLOAD_KEYS = ("type", "message_type", "kind")
FILE_FIELD_NAMES = {"files[]", "files"}
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
UPLOAD_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class MobileFile:
    filename: str
    content_type: str
    content: bytes


def create_mobile_api(
    settings: Settings,
    airtable: AirtableClient,
    drive_storage: DriveStorage | None = None,
) -> FastAPI:
    app = FastAPI(title="Voice Inbox", version="1.1.0")
    resolved_drive_storage = drive_storage if drive_storage is not None else build_drive_storage(settings)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/mobile-inbox/items", response_model=None)
    async def create_mobile_inbox_item(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> JSONResponse | dict[str, str | bool]:
        _check_authorization(authorization, settings)
        _check_content_length(content_length, settings)

        form = await _read_form(request, settings)
        payload_raw = _get_payload_raw(form, settings)
        payload = _parse_payload(payload_raw)
        upload_files = _extract_uploads(form, settings)

        text = _extract_payload_text(payload)
        mobile_files = await _read_validated_files(upload_files, settings, len(payload_raw.encode("utf-8")))
        message_type = _infer_message_type(payload, text, mobile_files)
        title = _build_title(text, message_type, settings)
        notes = _build_notes(payload, mobile_files, settings)
        item_id = _extract_item_id(payload)
        created_at = utc_now()

        existing = await asyncio.to_thread(airtable.find_voice_record_by_external_id, item_id)
        if existing:
            return {"ok": True, "remote_id": str(existing.get("id")), "status": "stored"}

        drive_url: str | None = None
        drive_error: str | None = None
        drive_files = [
            DriveUploadFile(name=file.filename, mime_type=file.content_type, content=file.content)
            for file in mobile_files
        ]
        if resolved_drive_storage:
            try:
                stored_item = await asyncio.to_thread(
                    resolved_drive_storage.store_item,
                    item_id=item_id,
                    created_at=created_at,
                    source="android",
                    message_type=message_type,
                    text=text or None,
                    files=drive_files,
                    extra={"payload": _safe_payload_for_manifest(payload)},
                )
                drive_url = stored_item.folder_url
            except DriveStorageError as exc:
                drive_error = safe_error(exc)
                try:
                    spool_path = await asyncio.to_thread(
                        spool_drive_item,
                        settings=settings,
                        item_id=item_id,
                        created_at=created_at,
                        source="android",
                        message_type=message_type,
                        text=text or None,
                        files=drive_files,
                        error=drive_error,
                        extra={"payload": _safe_payload_for_manifest(payload)},
                    )
                    drive_error = f"{drive_error}; spooled={spool_path}"
                except Exception as spool_exc:
                    logger.exception("Could not spool mobile inbox item %s after Google Drive failure", item_id)
                    drive_error = f"{drive_error}; spool_failed={safe_error(spool_exc)}"

        try:
            record = await asyncio.to_thread(
                airtable.create_mobile_inbox_record,
                title=title,
                raw_text=text,
                message_type=message_type,
                notes=_notes_with_drive(notes, item_id, drive_url, drive_error),
                external_id=item_id,
                google_drive_url=drive_url,
                source="Android",
                processing_error=drive_error,
                processing_status="Needs Review" if drive_error else "New",
            )
        except AirtableError as exc:
            logger.exception("Could not create mobile inbox Airtable record")
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "status": "airtable_create_failed", "error": _safe_error(exc)},
            ) from exc

        record_id = str(record.get("id") or "")
        if not record_id:
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "status": "airtable_create_failed", "error": "Airtable did not return record id"},
            )

        if drive_error:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "remote_id": record_id,
                    "status": "drive_upload_failed",
                    "error": drive_error,
                },
            )

        upload_errors: list[str] = []
        for mobile_file in mobile_files:
            try:
                await asyncio.to_thread(
                    airtable.upload_voice_attachment,
                    record_id=record_id,
                    filename=mobile_file.filename,
                    content_type=mobile_file.content_type,
                    content=mobile_file.content,
                )
            except AirtableError as exc:
                logger.exception("Could not upload mobile attachment to Airtable record %s", record_id)
                upload_errors.append(f"{mobile_file.filename}: {_safe_error(exc)}")

        if upload_errors:
            error_summary = "; ".join(upload_errors)[:900]
            error_notes = f"{notes}\nAttachment upload error: {error_summary}"
            try:
                await asyncio.to_thread(airtable.mark_mobile_upload_failed, record_id, error_notes)
            except AirtableError:
                logger.exception("Could not mark mobile inbox Airtable record %s as upload failed", record_id)
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "remote_id": record_id,
                    "status": "attachment_upload_failed",
                    "error": error_summary,
                },
            )

        return {"ok": True, "remote_id": record_id, "status": "stored"}

    return app


def _check_authorization(authorization: str | None, settings: Settings) -> None:
    if not settings.mobile_inbox_token:
        raise HTTPException(status_code=503, detail="MOBILE_INBOX_TOKEN is not configured")
    if len(settings.mobile_inbox_token.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail="MOBILE_INBOX_TOKEN must be at least 32 bytes")
    expected = f"Bearer {settings.mobile_inbox_token}"
    if not authorization or not hmac.compare_digest(authorization.strip(), expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _check_content_length(content_length: int | None, settings: Settings) -> None:
    if content_length is not None and content_length > settings.mobile_inbox_max_request_bytes:
        raise HTTPException(status_code=413, detail="Multipart request is too large")


async def _read_form(request: Request, settings: Settings) -> FormData:
    try:
        return await request.form(max_files=settings.mobile_inbox_max_files, max_fields=10)
    except TypeError:
        return await request.form()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid multipart form data") from exc


def _get_payload_raw(form: FormData, settings: Settings) -> str:
    payload_raw = form.get("payload")
    if not isinstance(payload_raw, str):
        raise HTTPException(status_code=422, detail="Missing multipart field: payload")
    payload_size = len(payload_raw.encode("utf-8"))
    if payload_size > settings.mobile_inbox_max_payload_bytes:
        raise HTTPException(status_code=413, detail="Payload JSON is too large")
    return payload_raw


def _parse_payload(payload_raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload JSON must be an object")
    return payload


def _extract_uploads(form: FormData, settings: Settings) -> list[UploadFile]:
    uploads: list[UploadFile] = []
    for key, value in form.multi_items():
        if key not in FILE_FIELD_NAMES:
            continue
        if not isinstance(value, UploadFile):
            continue
        if not value.filename:
            continue
        uploads.append(value)

    if len(uploads) > settings.mobile_inbox_max_files:
        raise HTTPException(status_code=413, detail="Too many files")
    return uploads


async def _read_validated_files(
    uploads: list[UploadFile],
    settings: Settings,
    payload_size: int,
) -> list[MobileFile]:
    files: list[MobileFile] = []
    total_size = payload_size
    for index, upload in enumerate(uploads, start=1):
        filename = _safe_filename(upload.filename or "", index, upload.content_type)
        content_type = _content_type(upload.content_type, filename, settings)
        content = await _read_limited_upload(upload, settings.mobile_inbox_max_file_bytes)
        total_size += len(content)
        if total_size > settings.mobile_inbox_max_request_bytes:
            raise HTTPException(status_code=413, detail="Multipart request is too large")
        files.append(MobileFile(filename=filename, content_type=content_type, content=content))
    return files


async def _read_limited_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="File is too large")
            chunks.append(chunk)
    finally:
        await upload.close()
    return b"".join(chunks)


def _content_type(raw_content_type: str | None, filename: str, settings: Settings) -> str:
    content_type = (raw_content_type or "").split(";", 1)[0].strip().casefold()
    guessed_type = (mimetypes.guess_type(filename)[0] or "").casefold()
    allowed = settings.allowed_mobile_mime_types

    if content_type == "application/octet-stream" and guessed_type in allowed:
        content_type = guessed_type
    if not content_type and guessed_type in allowed:
        content_type = guessed_type
    if content_type not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported MIME type: {content_type or 'unknown'}")
    return content_type


def _safe_filename(raw_filename: str, index: int, content_type: str | None) -> str:
    basename = PurePath(raw_filename.replace("\\", "/")).name.strip()
    if not basename or basename in {".", ".."}:
        basename = f"file_{index}{mimetypes.guess_extension(content_type or '') or '.bin'}"

    basename = SAFE_FILENAME_RE.sub("_", basename).strip("._")
    if not basename:
        basename = f"file_{index}{mimetypes.guess_extension(content_type or '') or '.bin'}"
    if len(basename) > 120:
        path = PurePath(basename)
        suffix = path.suffix[:16]
        stem_limit = max(1, 120 - len(suffix))
        basename = f"{path.stem[:stem_limit]}{suffix}"
    return basename


def _extract_payload_text(payload: dict[str, Any]) -> str:
    for key in TEXT_PAYLOAD_KEYS:
        value = payload.get(key)
        text = _string_value(value)
        if text:
            return text
    for value in payload.values():
        if isinstance(value, dict):
            text = _extract_payload_text(value)
            if text:
                return text
    return ""


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _infer_message_type(payload: dict[str, Any], text: str, files: list[MobileFile]) -> str:
    explicit_type = _payload_message_type(payload)
    if explicit_type:
        return explicit_type

    if not files:
        return "Text"

    file_types = {_file_message_type(file.content_type) for file in files}
    if text.strip() or len(file_types) > 1:
        return "Mixed"
    return next(iter(file_types))


def _payload_message_type(payload: dict[str, Any]) -> str | None:
    mapping = {
        "text": "Text",
        "voice": "Voice",
        "audio": "Voice",
        "photo": "Photo",
        "image": "Photo",
        "file": "File",
        "mixed": "Mixed",
    }
    for key in TYPE_PAYLOAD_KEYS:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        normalized = mapping.get(value.strip().casefold())
        if normalized in ALLOWED_MESSAGE_TYPES:
            return normalized
    return None


def _file_message_type(content_type: str) -> str:
    if content_type.startswith("audio/"):
        return "Voice"
    if content_type.startswith("image/"):
        return "Photo"
    return "File"


def _build_title(text: str, message_type: str, settings: Settings) -> str:
    collapsed = " ".join(text.strip().split())
    if collapsed:
        if len(collapsed) <= 90:
            return collapsed
        return collapsed[:89].rstrip() + "..."

    return f"Android: {message_type} {_now_for_title(settings)}"


def _now_for_title(settings: Settings) -> str:
    try:
        tzinfo = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        tzinfo = ZoneInfo("UTC")
    return datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M:%S")


def _build_notes(payload: dict[str, Any], files: list[MobileFile], settings: Settings) -> str:
    keys = ", ".join(sorted(str(key) for key in payload.keys())[:20]) or "-"
    return "\n".join(
        [
            "Source: Android Dispatcher",
            f"Android raw mode: {str(settings.android_raw_mode).lower()}",
            f"Payload keys: {keys}",
            f"Files: {len(files)}",
        ]
    )


def _extract_item_id(payload: dict[str, Any]) -> str:
    for key in ("item_id", "id", "external_id", "client_item_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return str(uuid.uuid4())


def _safe_payload_for_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
        elif isinstance(value, list):
            safe[str(key)] = value[:20]
        elif isinstance(value, dict):
            safe[str(key)] = {
                str(child_key): child_value
                for child_key, child_value in value.items()
                if isinstance(child_value, (str, int, float, bool)) or child_value is None
            }
    return safe


def _notes_with_drive(notes: str, item_id: str, drive_url: str | None, drive_error: str | None) -> str:
    lines = [notes, f"External ID: {item_id}"]
    if drive_url:
        lines.append(f"Google Drive: {drive_url}")
    if drive_error:
        lines.append(f"Google Drive error: {drive_error}")
    return "\n".join(lines)


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:500]
