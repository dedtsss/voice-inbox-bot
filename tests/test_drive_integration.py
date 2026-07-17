from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.drive_storage import (
    DriveStorageError,
    DriveStoredFile,
    DriveStoredItem,
    DriveUploadFile,
    build_manifest,
    folder_url,
)
from app.main import IncomingContent, save_to_airtable, store_telegram_originals
from app.mobile_api import create_mobile_api


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="123:test",
        ALLOWED_TELEGRAM_USER_IDS="1",
        OPENAI_API_KEY="sk-test",
        AIRTABLE_TOKEN="pat-test",
        VOICE_INBOX_BASE_ID="appYRukVuHediikiR",
        VOICE_INBOX_TABLE_ID="tblRMsY9zB5tnVfTR",
        VOICE_FIELD_TITLE="Название",
        VOICE_FIELD_TYPE="Тип",
        VOICE_FIELD_PROJECT="Проект",
        VOICE_FIELD_PRIORITY="Приоритет",
        VOICE_FIELD_NEXT_ACTION="Следующее действие",
        VOICE_FIELD_SUMMARY="Кратко",
        VOICE_FIELD_CLEAN_TEXT="Текст",
        VOICE_FIELD_RAW_TEXT="Исходная фраза",
        VOICE_FIELD_TAGS="Теги",
        VOICE_FIELD_PROCESSING_STATUS="Статус обработки",
        VOICE_FIELD_ATTACHMENTS="Attachments",
        VOICE_FIELD_NOTES="Notes",
        PROJECTS_BASE_ID="appProjects",
        PROJECTS_TABLE_ID="tblProjects",
        PROJECTS_FIELD_TITLE="Name",
        ITEMS_TABLE_ID="tblItems",
        ITEMS_FIELD_TITLE="Name",
        ITEMS_FIELD_PROJECT="Project",
        ITEMS_FIELD_TYPE="Type",
        ITEMS_FIELD_STATUS="Status",
        ITEMS_FIELD_PRIORITY="Priority",
        ITEMS_FIELD_TEXT="Text",
        ITEMS_FIELD_NEXT_ACTION="Next",
        ITEMS_FIELD_SOURCE="Source",
        ITEMS_FIELD_DATE="Date",
        MOBILE_INBOX_TOKEN="test-mobile-token-with-more-than-32-bytes",
        GOOGLE_DRIVE_ENABLED=True,
        GOOGLE_DRIVE_ROOT_FOLDER_ID="root-folder",
        GOOGLE_DRIVE_SPOOL_DIR=str(tmp_path / "spool"),
    )


@dataclass
class FakeDrive:
    fail: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def store_item(
        self,
        *,
        item_id: str,
        created_at,
        source: str,
        message_type: str,
        text: str | None,
        files: list[DriveUploadFile],
        extra: dict[str, Any] | None = None,
    ) -> DriveStoredItem:
        self.calls.append(
            {
                "item_id": item_id,
                "source": source,
                "message_type": message_type,
                "text": text,
                "files": files,
                "extra": extra,
            }
        )
        if self.fail:
            raise DriveStorageError("temporary Drive error refresh_token=secret-refresh-token")
        folder_id = f"folder-{item_id}"
        stored_files = [
            DriveStoredFile(
                name=file.name,
                mime_type=file.mime_type,
                size=file.size,
                drive_file_id=f"file-{index}",
                sha256=file.sha256,
            )
            for index, file in enumerate(files, start=1)
        ]
        manifest = build_manifest(
            item_id=item_id,
            created_at=created_at,
            source=source,
            message_type=message_type,
            text=text,
            files=stored_files,
            extra=extra,
        )
        return DriveStoredItem(
            item_id=item_id,
            folder_id=folder_id,
            folder_url=folder_url(folder_id),
            manifest_file_id=f"manifest-{item_id}",
            files=stored_files,
            manifest=manifest,
        )


class FakeAirtable:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.external_index: dict[str, str] = {}
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        self.created_payloads: list[dict[str, Any]] = []
        self.updated_payloads: list[dict[str, Any]] = []

    def find_voice_record_by_external_id(self, external_id: str) -> dict | None:
        record_id = self.external_index.get(external_id)
        return self.records.get(record_id) if record_id else None

    def create_mobile_inbox_record(self, **kwargs) -> dict:
        record_id = f"rec{len(self.records) + 1}"
        fields = dict(kwargs)
        record = {"id": record_id, "fields": fields}
        self.records[record_id] = record
        if kwargs.get("external_id"):
            self.external_index[kwargs["external_id"]] = record_id
        self.created_payloads.append(fields)
        return record

    def create_voice_inbox_record(self, **kwargs) -> dict:
        record_id = f"rec{len(self.records) + 1}"
        record = {"id": record_id, "fields": dict(kwargs)}
        self.records[record_id] = record
        external_id = kwargs.get("external_id")
        if external_id:
            self.external_index[external_id] = record_id
        self.created_payloads.append(dict(kwargs))
        return record

    def upload_voice_attachment(self, *, record_id: str, filename: str, content_type: str, content: bytes) -> dict:
        self.attachments.setdefault(record_id, []).append(
            {"filename": filename, "content_type": content_type, "size": len(content)}
        )
        return {"id": f"att{len(self.attachments[record_id])}"}

    def mark_mobile_upload_failed(self, record_id: str, notes: str) -> dict:
        self.records[record_id]["fields"]["processing_status"] = "Needs Review"
        self.records[record_id]["fields"]["processing_error"] = notes
        return self.records[record_id]

    def update_voice_inbox_metadata(self, record_id: str, **kwargs) -> dict:
        self.records[record_id]["fields"].update(kwargs)
        self.updated_payloads.append(dict(kwargs))
        return self.records[record_id]

    def find_project(self, project_title: str):
        return None

    def create_project_item(self, **kwargs):
        raise AssertionError("Project item should not be created in these tests")


def post_item(client: TestClient, settings: Settings, payload: dict, files: list[tuple[str, bytes, str]] | None = None):
    multipart: list[tuple[str, tuple]] = [
        ("payload", (None, json.dumps(payload, ensure_ascii=False), "application/json"))
    ]
    for name, content, mime_type in files or []:
        multipart.append(("files[]", (name, content, mime_type)))
    return client.post(
        "/api/mobile-inbox/items",
        headers={"Authorization": f"Bearer {settings.mobile_inbox_token}"},
        files=multipart,
    )


def make_client(tmp_path: Path, drive: FakeDrive | None = None):
    settings = make_settings(tmp_path)
    airtable = FakeAirtable()
    app = create_mobile_api(settings, airtable, drive or FakeDrive())
    return TestClient(app), settings, airtable, drive or app


def test_android_text_only(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)
    response = post_item(client, settings, {"item_id": "android-text-1", "text": "hello from android"})

    assert response.status_code == 200
    assert response.json()["remote_id"] == "rec1"
    assert airtable.created_payloads[0]["external_id"] == "android-text-1"
    assert airtable.created_payloads[0]["google_drive_url"] == "https://drive.google.com/drive/folders/folder-android-text-1"
    assert airtable.created_payloads[0]["processing_status"] == "New"
    assert drive.calls[0]["text"] == "hello from android"
    assert drive.calls[0]["files"] == []


def test_android_mp3(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)
    mp3 = b"ID3" + b"\x00" * 13001
    response = post_item(
        client,
        settings,
        {"item_id": "android-mp3-1", "type": "voice", "text": "mp3 note"},
        [("22-33_mono_16khz_64kbps.mp3", mp3, "audio/mpeg")],
    )

    assert response.status_code == 200
    assert drive.calls[0]["files"][0].name == "22-33_mono_16khz_64kbps.mp3"
    assert drive.calls[0]["files"][0].mime_type == "audio/mpeg"
    assert drive.calls[0]["files"][0].size == 13004
    assert airtable.attachments["rec1"][0]["filename"] == "22-33_mono_16khz_64kbps.mp3"


def test_android_photo_and_text(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)
    response = post_item(
        client,
        settings,
        {"item_id": "android-photo-1", "caption": "photo caption"},
        [("photo.png", b"\x89PNG\r\n", "image/png")],
    )

    assert response.status_code == 200
    assert airtable.created_payloads[0]["message_type"] == "Mixed"
    assert drive.calls[0]["files"][0].mime_type == "image/png"


def test_android_video(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)
    response = post_item(
        client,
        settings,
        {"item_id": "android-video-1", "type": "video"},
        [("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")],
    )

    assert response.status_code == 200
    assert airtable.created_payloads[0]["message_type"] == "Video"
    assert drive.calls[0]["files"][0].name == "clip.mp4"
    assert drive.calls[0]["files"][0].mime_type == "video/mp4"


def test_android_multiple_files(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)
    response = post_item(
        client,
        settings,
        {"item_id": "android-multi-1", "text": "multi"},
        [
            ("a.txt", b"hello", "text/plain"),
            ("b.json", b"{}", "application/json"),
        ],
    )

    assert response.status_code == 200
    assert len(drive.calls[0]["files"]) == 2
    assert len(airtable.attachments["rec1"]) == 2


def test_repeat_same_item_id_is_idempotent(tmp_path: Path) -> None:
    drive = FakeDrive()
    client, settings, airtable, _ = make_client(tmp_path, drive)

    first = post_item(client, settings, {"item_id": "same-id", "text": "first"})
    second = post_item(client, settings, {"item_id": "same-id", "text": "second"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["remote_id"] == second.json()["remote_id"] == "rec1"
    assert len(airtable.records) == 1
    assert len(drive.calls) == 1


def test_google_drive_error_spools_and_returns_error(tmp_path: Path) -> None:
    drive = FakeDrive(fail=True)
    client, settings, airtable, _ = make_client(tmp_path, drive)
    response = post_item(
        client,
        settings,
        {"item_id": "drive-error-1", "text": "spool me"},
        [("a.txt", b"hello", "text/plain")],
    )

    assert response.status_code == 502
    assert response.json()["status"] == "drive_upload_failed"
    assert "secret-refresh-token" not in response.text
    assert airtable.created_payloads[0]["processing_status"] == "Needs Review"
    spool_dirs = list((tmp_path / "spool").iterdir())
    assert len(spool_dirs) == 1
    assert (spool_dirs[0] / "manifest.json").exists()
    assert (spool_dirs[0] / "a.txt").read_bytes() == b"hello"


def test_successful_telegram_voice_metadata(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    airtable = FakeAirtable()
    drive = FakeDrive()
    content = IncomingContent(
        raw_text="telegram transcript",
        message_type="Voice",
        item_id="telegram:1:2",
        files=[DriveUploadFile("voice.ogg", "audio/ogg", b"ogg")],
    )

    stored_content = asyncio.run(store_telegram_originals(settings, drive, content))
    voice_record, item_record, project = save_to_airtable(
        airtable,
        settings,
        {"title": "telegram transcript"},
        stored_content,
    )

    assert voice_record["id"] == "rec1"
    assert item_record is None
    assert project is None
    assert airtable.created_payloads[0]["external_id"] == "telegram:1:2"
    assert airtable.created_payloads[0]["google_drive_url"] == "https://drive.google.com/drive/folders/folder-telegram:1:2"
    assert airtable.created_payloads[0]["source"] == "Telegram"


def test_safe_errors_do_not_expose_token_values(tmp_path: Path) -> None:
    drive = FakeDrive(fail=True)
    client, settings, _, _ = make_client(tmp_path, drive)
    response = post_item(client, settings, {"item_id": "secret-check", "text": "x"})

    assert response.status_code == 502
    assert "secret-refresh-token" not in response.text
    assert "refresh_token=[redacted]" in response.text
