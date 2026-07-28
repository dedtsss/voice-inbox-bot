from __future__ import annotations

import json
import os
import shutil
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.drive_spool_recovery import DriveSpoolRecovery, ItemLock, clear_drive_failure_error, main
from app.drive_storage import (
    DRIVE_FILE_KEY_PROPERTY,
    DRIVE_ITEM_KEY_PROPERTY,
    DRIVE_KIND_PROPERTY,
    DRIVE_SHA256_PROPERTY,
    DriveStoredFile,
    DriveStoredItem,
    DriveUploadFile,
    GoogleDriveStorage,
    build_manifest,
    folder_url,
    spool_drive_item,
)
from app.subscription_queue import SubscriptionQueue


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="123:test",
        AIRTABLE_TOKEN="pat-test",
        VOICE_INBOX_BASE_ID="app-test",
        VOICE_INBOX_TABLE_ID="table-test",
        VOICE_FIELD_TITLE="Title",
        VOICE_FIELD_TYPE="Type",
        VOICE_FIELD_PROJECT="Project",
        VOICE_FIELD_PRIORITY="Priority",
        VOICE_FIELD_NEXT_ACTION="Next",
        VOICE_FIELD_SUMMARY="Summary",
        VOICE_FIELD_CLEAN_TEXT="Clean Text",
        VOICE_FIELD_RAW_TEXT="Raw Text",
        VOICE_FIELD_TAGS="Tags",
        VOICE_FIELD_PROCESSING_STATUS="Processing Status",
        VOICE_FIELD_PROCESSING_STATUS_QUERY_NAME="Processing Status",
        VOICE_FIELD_PROCESSING_ROUTE="Processing Route",
        VOICE_FIELD_PROCESSING_ROUTE_QUERY_NAME="Processing Route",
        VOICE_FIELD_GOOGLE_DRIVE="Google Drive",
        VOICE_FIELD_EXTERNAL_ID="External ID",
        VOICE_FIELD_EXTERNAL_ID_QUERY_NAME="External ID",
        VOICE_FIELD_PROCESSING_ERROR="Processing Error",
        PROJECTS_BASE_ID="projects-base",
        PROJECTS_TABLE_ID="projects-table",
        PROJECTS_FIELD_TITLE="Name",
        ITEMS_TABLE_ID="items-table",
        ITEMS_FIELD_TITLE="Name",
        ITEMS_FIELD_PROJECT="Project",
        ITEMS_FIELD_TYPE="Type",
        ITEMS_FIELD_STATUS="Status",
        ITEMS_FIELD_PRIORITY="Priority",
        ITEMS_FIELD_TEXT="Text",
        ITEMS_FIELD_NEXT_ACTION="Next",
        ITEMS_FIELD_SOURCE="Source",
        ITEMS_FIELD_DATE="Date",
        GOOGLE_DRIVE_ENABLED=True,
        GOOGLE_DRIVE_ROOT_FOLDER_ID="root-test",
        GOOGLE_DRIVE_SPOOL_DIR=str(tmp_path / "spool"),
        VOICE_PROCESSING_ROUTE="chatgpt_subscription",
    )


@dataclass
class FakeAirtable:
    settings: Settings
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    external_index: dict[str, str] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    fail_find: bool = False
    fail_update_once: bool = False
    fail_fetch_once: bool = False

    def add_drive_failure(self, item_id: str, spool_folder: Path, *, error: str = "temporary Drive failure") -> dict:
        record_id = f"rec-{len(self.records) + 1}"
        record = {
            "id": record_id,
            "createdTime": "2026-07-28T00:00:00.000Z",
            "fields": {
                self.settings.voice_field_external_id: item_id,
                self.settings.voice_field_processing_route: "ChatGPT Subscription",
                self.settings.voice_field_processing_status: "Needs Review",
                self.settings.voice_field_google_drive: "",
                self.settings.voice_field_processing_error: f"{error}; spooled={spool_folder}",
                self.settings.voice_field_title: "ordinary record",
            },
        }
        self.records[record_id] = record
        self.external_index[item_id] = record_id
        return record

    def find_voice_record_by_external_id(self, external_id: str) -> dict | None:
        if self.fail_find:
            raise RuntimeError("Airtable token=private-token")
        record_id = self.external_index.get(external_id)
        return deepcopy(self.records.get(record_id)) if record_id else None

    def fetch_voice_record(self, record_id: str) -> dict:
        if self.fail_fetch_once:
            self.fail_fetch_once = False
            raise RuntimeError("Airtable unavailable")
        return deepcopy(self.records[record_id])

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        if self.fail_update_once:
            self.fail_update_once = False
            raise RuntimeError("Airtable refresh_token=private-token")
        self.records[record_id]["fields"].update(fields)
        self.updates.append(deepcopy(fields))
        return deepcopy(self.records[record_id])

    def list_subscription_queue_records(self, *, batch_size: int, created_after=None) -> list[dict]:
        return [deepcopy(record) for record in self.records.values()][:batch_size]


@dataclass
class FakeRecoveryDrive:
    stored: dict[str, DriveStoredItem] = field(default_factory=dict)
    store_calls: int = 0
    physical_uploads: int = 0
    verify_calls: int = 0
    fail_store: bool = False
    fail_verify: bool = False

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
        self.store_calls += 1
        if self.fail_store:
            raise RuntimeError("Drive authorization=private-token user-file-name.txt")
        if item_id in self.stored:
            return self.stored[item_id]
        self.physical_uploads += 1
        stored_files = [
            DriveStoredFile(
                name=upload.name,
                mime_type=upload.mime_type,
                size=upload.size,
                drive_file_id=f"file-{index}",
                sha256=upload.sha256,
            )
            for index, upload in enumerate(files, start=1)
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
        stored = DriveStoredItem(
            item_id=item_id,
            folder_id=f"folder-{len(self.stored) + 1}",
            folder_url=folder_url(f"folder-{len(self.stored) + 1}"),
            manifest_file_id=f"manifest-{len(self.stored) + 1}",
            files=stored_files,
            manifest=manifest,
        )
        self.stored[item_id] = stored
        return stored

    def verify(self, drive_folder_url: str, expected_manifest: dict[str, Any]) -> None:
        self.verify_calls += 1
        if self.fail_verify:
            raise RuntimeError("Drive verification failed filename=private-user-file.txt")
        item_id = str(expected_manifest.get("item_id") or "")
        stored = self.stored.get(item_id)
        if stored is None or stored.folder_url != drive_folder_url:
            raise RuntimeError("Drive item missing")


class FakeQueueDrive:
    def manifest_exists(self, google_drive_url: str) -> bool:
        return bool(google_drive_url)


class MemoryGoogleDriveStorage(GoogleDriveStorage):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root_folder_id = settings.google_drive_root_folder_id
        self.objects: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.uploaded_names: list[str] = []
        self.fail_manifest_once = False

    def _find_by_property(
        self,
        parent_id: str,
        property_name: str,
        property_value: str,
        *,
        mime_type: str | None = None,
    ) -> dict[str, Any] | None:
        matches = [
            value
            for value in self.objects.values()
            if parent_id in value.get("parents", [])
            and (value.get("appProperties") or {}).get(property_name) == property_value
            and (mime_type is None or value.get("mimeType") == mime_type)
        ]
        assert len(matches) <= 1
        return deepcopy(matches[0]) if matches else None

    def _find_legacy_item_folder(self, folder_name: str, item_id: str, item_key: str):
        return None

    def _find_children(self, parent_id: str, name: str, mime_type: str | None = None):
        return [
            deepcopy(value)
            for value in self.objects.values()
            if (
                parent_id in value.get("parents", [])
                and value.get("name") == name
                and (mime_type is None or value.get("mimeType") == mime_type)
            )
        ]

    def _find_child(self, parent_id: str, name: str, mime_type: str | None = None):
        for value in self.objects.values():
            if (
                parent_id in value.get("parents", [])
                and value.get("name") == name
                and (mime_type is None or value.get("mimeType") == mime_type)
            ):
                return deepcopy(value)
        return None

    def _create_folder(self, name: str, parent_id: str, *, item_key: str) -> dict[str, Any]:
        object_id = f"folder-{len(self.objects) + 1}"
        value = {
            "id": object_id,
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
            "appProperties": {
                DRIVE_KIND_PROPERTY: "item",
                DRIVE_ITEM_KEY_PROPERTY: item_key,
            },
        }
        self.objects[object_id] = value
        return deepcopy(value)

    def _upload_bytes(
        self,
        *,
        parent_id: str,
        name: str,
        mime_type: str,
        content: bytes,
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if name == "manifest.json" and self.fail_manifest_once:
            self.fail_manifest_once = False
            raise RuntimeError("simulated process interruption")
        object_id = f"file-{len(self.objects) + 1}"
        value = {
            "id": object_id,
            "name": name,
            "mimeType": mime_type,
            "size": len(content),
            "parents": [parent_id],
            "appProperties": dict(app_properties or {}),
        }
        self.objects[object_id] = value
        self.contents[object_id] = content
        self.uploaded_names.append(name)
        return deepcopy(value)

    def _update_bytes(
        self,
        *,
        file_id: str,
        name: str,
        mime_type: str,
        content: bytes,
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        value = self.objects[file_id]
        value.update(
            {
                "name": name,
                "mimeType": mime_type,
                "size": len(content),
                "appProperties": dict(app_properties or {}),
            }
        )
        self.contents[file_id] = content
        return deepcopy(value)


def make_spool(
    settings: Settings,
    *,
    item_id: str = "item-1",
    content: bytes = b"original-content",
    error: str = "temporary Drive failure",
) -> Path:
    return spool_drive_item(
        settings=settings,
        item_id=item_id,
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        source="android",
        message_type="File",
        text="test text",
        files=[DriveUploadFile("original.bin", "application/octet-stream", content)],
        error=error,
    )


def make_recovery(
    settings: Settings,
    airtable: FakeAirtable,
    drive: FakeRecoveryDrive,
    *,
    lock_timeout_seconds: int = 3600,
) -> DriveSpoolRecovery:
    return DriveSpoolRecovery(
        settings,
        airtable=airtable,
        drive_storage=drive,
        verifier=drive,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def test_successful_recovery_and_status_transition(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    record = airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.scanned == stats.eligible == stats.recovered == 1
    assert not spool.exists()
    fields = airtable.records[record["id"]]["fields"]
    assert fields[settings.voice_field_processing_status] == "Awaiting Subscription"
    assert fields[settings.voice_field_processing_route] == "ChatGPT Subscription"
    assert fields[settings.voice_field_google_drive]
    assert fields[settings.voice_field_processing_error] == ""


def test_dry_run_has_no_side_effects_or_locks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()

    stats = make_recovery(settings, airtable, drive).run(dry_run=True, batch_size=5)

    assert stats.eligible == 1
    assert stats.recovered == 0
    assert drive.store_calls == 0
    assert airtable.updates == []
    assert spool.exists()
    assert not list(Path(settings.google_drive_spool_dir).glob(".recovery-lock-*"))


def test_repeat_run_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    recovery = make_recovery(settings, airtable, drive)

    first = recovery.run(dry_run=False, batch_size=5)
    second = recovery.run(dry_run=False, batch_size=5)

    assert first.recovered == 1
    assert second.scanned == 0
    assert drive.physical_uploads == 1
    assert len(airtable.records) == 1


def test_resume_after_drive_upload_before_airtable_update(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings, fail_update_once=True)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    recovery = make_recovery(settings, airtable, drive)

    first = recovery.run(dry_run=False, batch_size=5)
    second = recovery.run(dry_run=False, batch_size=5)

    assert first.failed == 1
    assert second.recovered == 1
    assert drive.store_calls == 2
    assert drive.physical_uploads == 1
    assert not spool.exists()


def test_existing_stable_drive_folder_is_reused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    drive.store_item(
        item_id="item-1",
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        source="android",
        message_type="File",
        text="test text",
        files=[DriveUploadFile("original.bin", "application/octet-stream", b"original-content")],
    )
    drive.store_calls = 0
    drive.physical_uploads = 0

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.recovered == 1
    assert drive.store_calls == 1
    assert drive.physical_uploads == 0
    assert len(drive.stored) == 1


def test_missing_original_is_corrupted_and_spool_is_preserved(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    (spool / "original.bin").unlink()
    airtable = FakeAirtable(settings)
    record = airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.corrupted == 1
    assert spool.exists()
    assert drive.store_calls == 0
    error = airtable.records[record["id"]]["fields"][settings.voice_field_processing_error]
    assert "drive_spool_recovery:corrupted_spool" in error


def test_invalid_manifest_does_not_stop_next_item(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    broken = make_spool(settings, item_id="item-broken")
    (broken / "manifest.json").write_text("{invalid", encoding="utf-8")
    good = make_spool(settings, item_id="item-good")
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-good", good)
    drive = FakeRecoveryDrive()

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.corrupted == 1
    assert stats.recovered == 1
    assert broken.exists()
    assert not good.exists()


def test_drive_error_preserves_safe_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    record = airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive(fail_store=True)

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    fields = airtable.records[record["id"]]["fields"]
    assert stats.failed == 1
    assert spool.exists()
    assert fields[settings.voice_field_processing_status] == "Needs Review"
    assert fields[settings.voice_field_google_drive] == ""
    assert "drive_spool_recovery:drive_upload_or_verification_failed" in fields[
        settings.voice_field_processing_error
    ]


def test_airtable_error_preserves_spool(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings, fail_find=True)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.failed == 1
    assert spool.exists()
    assert drive.store_calls == 0


def test_concurrent_run_skips_fresh_item_lock(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    lock = ItemLock(Path(settings.google_drive_spool_dir), spool, timeout_seconds=3600)
    assert lock.acquire()
    try:
        stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)
    finally:
        lock.release()

    assert stats.skipped == 1
    assert drive.store_calls == 0
    assert spool.exists()


def test_stale_lock_is_recovered(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    lock = ItemLock(Path(settings.google_drive_spool_dir), spool, timeout_seconds=1)
    lock.path.mkdir(mode=0o700)
    old = time.time() - 60
    os.utime(lock.path, (old, old))

    stats = make_recovery(settings, airtable, drive, lock_timeout_seconds=1).run(
        dry_run=False,
        batch_size=5,
    )

    assert stats.recovered == 1
    assert not lock.path.exists()


def test_spool_is_not_deleted_before_drive_verification(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    record = airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive(fail_verify=True)

    stats = make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    assert stats.failed == 1
    assert spool.exists()
    fields = airtable.records[record["id"]]["fields"]
    assert fields[settings.voice_field_google_drive] == ""
    assert fields[settings.voice_field_processing_status] == "Needs Review"


def test_crash_after_airtable_update_is_confirmed_on_next_run(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("item-1", spool)
    drive = FakeRecoveryDrive()
    recovery = make_recovery(settings, airtable, drive)
    real_rmtree = shutil.rmtree
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash")
        real_rmtree(path)

    monkeypatch.setattr("app.drive_spool_recovery.shutil.rmtree", fail_once)
    first = recovery.run(dry_run=False, batch_size=5)
    second = recovery.run(dry_run=False, batch_size=5)

    assert first.failed == 1
    assert second.already_recovered == 1
    assert drive.physical_uploads == 1
    assert drive.store_calls == 1
    assert not spool.exists()


def test_recovered_record_enters_subscription_queue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings, item_id="ordinary-item")
    airtable = FakeAirtable(settings)
    airtable.add_drive_failure("ordinary-item", spool)
    drive = FakeRecoveryDrive()
    make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    items = SubscriptionQueue(
        settings,
        airtable=airtable,  # type: ignore[arg-type]
        drive_reader=FakeQueueDrive(),  # type: ignore[arg-type]
    ).next_batch(batch_size=5, dry_run=True)

    assert len(items) == 1
    assert items[0].external_id == "ordinary-item"


def test_only_drive_error_is_removed_and_manual_marker_is_preserved(tmp_path: Path) -> None:
    spool = tmp_path / "spool-item"
    manual_marker = "manual review marker " + "x" * 600
    existing = f"temporary Drive failure; spooled={spool}; {manual_marker}"

    cleaned = clear_drive_failure_error(
        existing,
        manifest_drive_error="temporary Drive failure",
        spool_folder=spool,
    )

    assert cleaned == manual_marker


def test_logs_and_saved_errors_do_not_leak_secrets_or_names(tmp_path: Path, caplog) -> None:
    settings = make_settings(tmp_path)
    spool = make_spool(settings)
    airtable = FakeAirtable(settings)
    record = airtable.add_drive_failure("item-1", spool, error="temporary Drive failure")
    drive = FakeRecoveryDrive(fail_store=True)

    make_recovery(settings, airtable, drive).run(dry_run=False, batch_size=5)

    saved_error = airtable.records[record["id"]]["fields"][settings.voice_field_processing_error]
    combined = f"{caplog.text}\n{saved_error}"
    assert "private-token" not in combined
    assert "private-user-file" not in combined
    assert "user-file-name" not in combined


def test_google_drive_storage_resumes_partial_upload_by_stable_properties(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    drive = MemoryGoogleDriveStorage(settings)
    drive.fail_manifest_once = True
    kwargs = {
        "item_id": "stable-item",
        "created_at": datetime(2026, 7, 28, tzinfo=UTC),
        "source": "android",
        "message_type": "File",
        "text": "test",
        "files": [DriveUploadFile("original.bin", "application/octet-stream", b"content")],
    }

    try:
        drive.store_item(**kwargs)
    except RuntimeError:
        pass
    stored = drive.store_item(**kwargs)
    repeated = drive.store_item(**kwargs)

    folders = [value for value in drive.objects.values() if value.get("mimeType", "").endswith("folder")]
    originals = [
        value
        for value in drive.objects.values()
        if (value.get("appProperties") or {}).get(DRIVE_KIND_PROPERTY) == "original"
    ]
    manifests = [
        value
        for value in drive.objects.values()
        if (value.get("appProperties") or {}).get(DRIVE_KIND_PROPERTY) == "manifest"
    ]
    assert stored.folder_id == repeated.folder_id
    assert len(folders) == len(originals) == len(manifests) == 1
    assert drive.uploaded_names.count("original.bin") == 1
    assert drive.uploaded_names.count("manifest.json") == 1
    assert originals[0]["appProperties"][DRIVE_FILE_KEY_PROPERTY]
    assert originals[0]["appProperties"][DRIVE_SHA256_PROPERTY]


def test_cli_returns_nonzero_and_redacts_system_error(tmp_path: Path, monkeypatch, capsys, caplog) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr("app.drive_spool_recovery.get_settings", lambda: settings)

    def fail_drive(_settings: Settings):
        raise RuntimeError("refresh_token=private-token private-user-file.txt")

    monkeypatch.setattr("app.drive_spool_recovery.GoogleDriveStorage", fail_drive)

    exit_code = main(["--apply", "--batch-size", "1"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert json.loads(output) == {"error": "system_error"}
    assert "private-token" not in caplog.text
    assert "private-user-file" not in caplog.text


def test_drive_manifest_identity_cannot_collide_with_original_filename(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    drive = MemoryGoogleDriveStorage(settings)

    drive.store_item(
        item_id="stable-item",
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        source="android",
        message_type="File",
        text=None,
        files=[DriveUploadFile("manifest.json", "application/octet-stream", b"user-original")],
    )

    matching = [value for value in drive.objects.values() if value.get("name") == "manifest.json"]
    assert len(matching) == 2
    assert {
        value["appProperties"][DRIVE_KIND_PROPERTY]
        for value in matching
    } == {"original", "manifest"}
