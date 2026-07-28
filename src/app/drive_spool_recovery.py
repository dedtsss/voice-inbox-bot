from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.airtable import AirtableClient
from app.config import Settings, get_settings, parse_utc_timestamp
from app.drive_storage import (
    DriveStorage,
    DriveUploadFile,
    GoogleDriveStorage,
    safe_file_name,
)
from app.voice_processor import GoogleDriveInboxReader, get_field

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
MANIFEST_MAX_BYTES = 1_000_000
RECOVERY_ERROR_PREFIX = "drive_spool_recovery:"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoverySystemError(RuntimeError):
    pass


class SpoolValidationError(RuntimeError):
    def __init__(self, code: str, *, item_id: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.item_id = item_id


class DriveVerificationError(RuntimeError):
    pass


class RecoveryAirtable(Protocol):
    def find_voice_record_by_external_id(self, external_id: str) -> dict | None:
        ...

    def fetch_voice_record(self, record_id: str) -> dict:
        ...

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        ...


class RecoveryDriveVerifier(Protocol):
    def verify(self, folder_url: str, expected_manifest: dict[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class SpoolItem:
    folder: Path
    item_id: str
    created_at: datetime
    source: str
    message_type: str
    text: str | None
    files: list[DriveUploadFile]
    manifest: dict[str, Any]
    drive_error: str


@dataclass
class RecoveryStats:
    scanned: int = 0
    eligible: int = 0
    recovered: int = 0
    skipped: int = 0
    corrupted: int = 0
    failed: int = 0
    already_recovered: int = 0


class GoogleDriveRecoveryVerifier:
    def __init__(self, settings: Settings) -> None:
        self.reader = GoogleDriveInboxReader(settings)

    def verify(self, folder_url: str, expected_manifest: dict[str, Any]) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="voice-inbox-drive-recovery-") as temp_dir:
                manifest, originals = self.reader.download_record_originals(folder_url, Path(temp_dir))
                if str(manifest.get("item_id") or "") != str(expected_manifest.get("item_id") or ""):
                    raise DriveVerificationError("drive_manifest_identity_mismatch")
                expected_files = _manifest_file_fingerprints(expected_manifest)
                actual_files = _manifest_file_fingerprints(manifest)
                if actual_files != expected_files or len(originals) != len(expected_files):
                    raise DriveVerificationError("drive_originals_mismatch")
        except DriveVerificationError:
            raise
        except Exception as exc:
            raise DriveVerificationError("drive_verification_failed") from exc


class ItemLock:
    def __init__(self, spool_root: Path, item_folder: Path, *, timeout_seconds: int) -> None:
        digest = hashlib.sha256(item_folder.name.encode("utf-8")).hexdigest()
        self.path = spool_root / f".recovery-lock-{digest}"
        self.timeout_seconds = max(1, timeout_seconds)
        self.acquired = False

    def acquire(self) -> bool:
        try:
            self._mkdir()
            return True
        except FileExistsError:
            pass

        try:
            age = max(0.0, time.time() - self.path.stat().st_mtime)
        except FileNotFoundError:
            return self.acquire()
        except OSError:
            return False
        if age <= self.timeout_seconds:
            return False

        stale_path = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.replace(self.path, stale_path)
        except FileNotFoundError:
            return self.acquire()
        except OSError:
            return False
        try:
            shutil.rmtree(stale_path)
        except OSError:
            logger.warning("Drive spool recovery could not remove an expired lock")
        try:
            self._mkdir()
            return True
        except FileExistsError:
            return False

    def touch(self) -> None:
        if not self.acquired:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            logger.warning("Drive spool recovery could not refresh an item lock")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Drive spool recovery could not release an item lock")
        finally:
            self.acquired = False

    def _mkdir(self) -> None:
        self.path.mkdir(mode=0o700)
        self.acquired = True


class DriveSpoolRecovery:
    def __init__(
        self,
        settings: Settings,
        *,
        airtable: RecoveryAirtable,
        drive_storage: DriveStorage | None = None,
        verifier: RecoveryDriveVerifier | None = None,
        lock_timeout_seconds: int = 3600,
    ) -> None:
        self.settings = settings
        self.airtable = airtable
        self.drive_storage = drive_storage
        self.verifier = verifier
        self.lock_timeout_seconds = max(1, lock_timeout_seconds)

    def run(self, *, dry_run: bool, batch_size: int, item_id: str = "") -> RecoveryStats:
        stats = RecoveryStats()
        spool_root = Path(self.settings.google_drive_spool_dir)
        if not spool_root.exists():
            return stats
        if not spool_root.is_dir() or spool_root.is_symlink():
            raise RecoverySystemError("spool_root_invalid")
        try:
            candidates = sorted(
                (entry for entry in spool_root.iterdir() if not entry.name.startswith(".")),
                key=lambda entry: entry.name,
            )
        except OSError as exc:
            raise RecoverySystemError("spool_scan_failed") from exc

        handled = 0
        wanted_item_id = item_id.strip()
        for folder in candidates:
            if handled >= max(1, batch_size):
                break
            stats.scanned += 1
            if wanted_item_id and _peek_item_id(folder) != wanted_item_id:
                continue
            handled += 1
            if dry_run:
                self._process(folder, stats, dry_run=True)
                continue

            lock = ItemLock(spool_root, folder, timeout_seconds=self.lock_timeout_seconds)
            if not lock.acquire():
                stats.skipped += 1
                logger.info("Drive spool recovery skipped one locked item")
                continue
            try:
                self._process(folder, stats, dry_run=False, lock=lock)
            finally:
                lock.release()
        return stats

    def _process(
        self,
        folder: Path,
        stats: RecoveryStats,
        *,
        dry_run: bool,
        lock: ItemLock | None = None,
    ) -> None:
        try:
            item = load_spool_item(folder, self.settings)
        except SpoolValidationError as exc:
            stats.corrupted += 1
            logger.warning("Drive spool recovery found one corrupted item reason=%s", exc.code)
            if not dry_run and exc.item_id:
                self._record_safe_failure(exc.item_id, "corrupted_spool")
            return

        try:
            record = self.airtable.find_voice_record_by_external_id(item.item_id)
        except Exception:
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=airtable_read_failed")
            return
        if not record or not str(record.get("id") or ""):
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=airtable_record_missing")
            return
        try:
            record = self.airtable.fetch_voice_record(str(record["id"]))
        except Exception:
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=airtable_read_failed")
            return

        state = _record_state(record, self.settings)
        if state["route"] != "ChatGPT Subscription":
            stats.skipped += 1
            return
        if _is_confirmed_recovery_state(state):
            if dry_run:
                stats.already_recovered += 1
                return
            if self.verifier is None:
                stats.failed += 1
                return
            try:
                self.verifier.verify(state["drive_url"], item.manifest)
                confirmed = self.airtable.fetch_voice_record(str(record["id"]))
                if not _is_confirmed_recovery_state(_record_state(confirmed, self.settings)):
                    raise RuntimeError("airtable_confirmation_failed")
                shutil.rmtree(folder)
            except Exception:
                stats.failed += 1
                logger.warning("Drive spool recovery failed one item reason=existing_state_confirmation_failed")
                return
            stats.already_recovered += 1
            return
        if not _is_eligible_drive_failure(state, item):
            stats.skipped += 1
            return

        stats.eligible += 1
        if dry_run:
            return
        if self.drive_storage is None or self.verifier is None:
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=drive_unavailable")
            self._record_failure_for_record(record, state["error"], "drive_unavailable")
            return

        try:
            upload = self.drive_storage.store_item(
                item_id=item.item_id,
                created_at=item.created_at,
                source=item.source,
                message_type=item.message_type,
                text=item.text,
                files=item.files,
                extra=_recovered_manifest_extra(item.manifest),
            )
            if lock:
                lock.touch()
            self.verifier.verify(upload.folder_url, upload.manifest)
            if lock:
                lock.touch()
        except Exception:
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=drive_upload_or_verification_failed")
            self._record_failure_for_record(record, state["error"], "drive_upload_or_verification_failed")
            return

        record_id = str(record["id"])
        try:
            latest = self.airtable.fetch_voice_record(record_id)
            latest_state = _record_state(latest, self.settings)
            if _is_confirmed_recovery_state(latest_state):
                self.verifier.verify(latest_state["drive_url"], upload.manifest)
                confirmed = self.airtable.fetch_voice_record(record_id)
                if not _is_confirmed_recovery_state(_record_state(confirmed, self.settings)):
                    raise RuntimeError("airtable_confirmation_failed")
                shutil.rmtree(folder)
                stats.already_recovered += 1
                return
            if not _is_eligible_drive_failure(latest_state, item):
                stats.skipped += 1
                return

            remaining_error = clear_drive_failure_error(
                latest_state["error"],
                manifest_drive_error=item.drive_error,
                spool_folder=folder,
            )
            self.airtable.update_voice_record_fields(
                record_id,
                {
                    self.settings.voice_field_google_drive: upload.folder_url,
                    self.settings.voice_field_processing_status: "Awaiting Subscription",
                    self.settings.voice_field_processing_error: remaining_error,
                    self.settings.voice_field_processing_route: "ChatGPT Subscription",
                },
            )
            if lock:
                lock.touch()
            confirmed = self.airtable.fetch_voice_record(record_id)
            confirmed_state = _record_state(confirmed, self.settings)
            if not _matches_expected_recovery_state(
                confirmed_state,
                folder_url=upload.folder_url,
                processing_error=remaining_error,
            ):
                raise RuntimeError("airtable_confirmation_failed")
            shutil.rmtree(folder)
        except Exception:
            stats.failed += 1
            logger.warning("Drive spool recovery failed one item reason=airtable_update_or_confirmation_failed")
            return
        stats.recovered += 1

    def _record_safe_failure(self, item_id: str, reason: str) -> None:
        try:
            record = self.airtable.find_voice_record_by_external_id(item_id)
            if not record:
                return
            record = self.airtable.fetch_voice_record(str(record.get("id") or ""))
            state = _record_state(record, self.settings)
            if (
                state["route"] != "ChatGPT Subscription"
                or state["status"] != "Needs Review"
                or state["drive_url"]
                or state["claim"]
            ):
                return
            self._record_failure_for_record(record, state["error"], reason)
        except Exception:
            logger.warning("Drive spool recovery could not persist a safe failure reason")

    def _record_failure_for_record(self, record: dict[str, Any], existing_error: str, reason: str) -> None:
        record_id = str(record.get("id") or "")
        if not record_id:
            return
        marker = f"{RECOVERY_ERROR_PREFIX}{reason}"
        updated_error = _append_technical_marker(existing_error, marker)
        try:
            self.airtable.update_voice_record_fields(
                record_id,
                {self.settings.voice_field_processing_error: updated_error},
            )
        except Exception:
            logger.warning("Drive spool recovery could not persist a safe failure reason")


def load_spool_item(folder: Path, settings: Settings) -> SpoolItem:
    if not folder.is_dir() or folder.is_symlink():
        raise SpoolValidationError("item_folder_invalid")
    manifest_path = folder / MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SpoolValidationError("manifest_missing")
        if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
            raise SpoolValidationError("manifest_too_large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except SpoolValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpoolValidationError("manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise SpoolValidationError("manifest_invalid")

    item_id = str(manifest.get("item_id") or "").strip()
    if not item_id or len(item_id) > 500:
        raise SpoolValidationError("manifest_item_id_invalid")
    try:
        created_at = parse_utc_timestamp(str(manifest.get("created_at") or ""), setting_name="manifest.created_at")
    except ValueError as exc:
        raise SpoolValidationError("manifest_created_at_invalid", item_id=item_id) from exc
    source = str(manifest.get("source") or "").strip().casefold()
    message_type = str(manifest.get("type") or "").strip()
    text = manifest.get("text")
    if source not in {"android", "telegram"}:
        raise SpoolValidationError("manifest_source_invalid", item_id=item_id)
    if not message_type or len(message_type) > 100:
        raise SpoolValidationError("manifest_type_invalid", item_id=item_id)
    if text is not None and not isinstance(text, str):
        raise SpoolValidationError("manifest_text_invalid", item_id=item_id)

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise SpoolValidationError("manifest_files_invalid", item_id=item_id)
    upload_files: list[DriveUploadFile] = []
    local_names: set[str] = set()
    total_bytes = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise SpoolValidationError("manifest_file_invalid", item_id=item_id)
        name = str(raw_file.get("name") or "")
        mime_type = str(raw_file.get("mime_type") or "application/octet-stream")
        sha256 = str(raw_file.get("sha256") or "").casefold()
        try:
            expected_size = int(raw_file.get("size"))
        except (TypeError, ValueError) as exc:
            raise SpoolValidationError("manifest_file_size_invalid", item_id=item_id) from exc
        if not name or len(name) > 500 or expected_size < 0 or not SHA256_RE.fullmatch(sha256):
            raise SpoolValidationError("manifest_file_invalid", item_id=item_id)
        local_name = safe_file_name(name)
        if local_name in local_names:
            raise SpoolValidationError("manifest_file_collision", item_id=item_id)
        local_names.add(local_name)
        path = folder / local_name
        if path.is_symlink() or not path.is_file():
            raise SpoolValidationError("original_missing", item_id=item_id)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SpoolValidationError("original_unreadable", item_id=item_id) from exc
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != sha256:
            raise SpoolValidationError("original_integrity_mismatch", item_id=item_id)
        if len(content) > settings.voice_processor_max_file_bytes:
            raise SpoolValidationError("original_too_large", item_id=item_id)
        total_bytes += len(content)
        if total_bytes > settings.voice_processor_max_record_bytes:
            raise SpoolValidationError("record_too_large", item_id=item_id)
        upload_files.append(DriveUploadFile(name=name, mime_type=mime_type, content=content))

    extra = manifest.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise SpoolValidationError("manifest_extra_invalid", item_id=item_id)
    drive_error = str((extra or {}).get("drive_error") or "").strip()
    if not drive_error:
        raise SpoolValidationError("manifest_drive_error_missing", item_id=item_id)
    return SpoolItem(
        folder=folder,
        item_id=item_id,
        created_at=created_at,
        source=source,
        message_type=message_type,
        text=text,
        files=upload_files,
        manifest=manifest,
        drive_error=drive_error,
    )


def clear_drive_failure_error(existing_error: str, *, manifest_drive_error: str, spool_folder: Path) -> str:
    remaining = str(existing_error or "")
    if manifest_drive_error and manifest_drive_error in remaining:
        remaining = remaining.replace(manifest_drive_error, "", 1)
    spool_marker = f"spooled={spool_folder}"
    remaining = remaining.replace(spool_marker, "")
    parts = [
        part.strip()
        for part in re.split(r"\s*;\s*", remaining)
        if part.strip() and not part.strip().startswith(RECOVERY_ERROR_PREFIX)
    ]
    return "; ".join(parts)


def _record_state(record: dict[str, Any], settings: Settings) -> dict[str, str]:
    fields = record.get("fields") or {}
    return {
        "route": str(
            get_field(
                fields,
                settings.voice_field_processing_route,
                settings.voice_field_processing_route_query_name,
                "Processing Route",
                "processing_route",
            )
            or ""
        ).strip(),
        "status": str(
            get_field(
                fields,
                settings.voice_field_processing_status,
                settings.voice_field_processing_status_query_name,
                "Статус обработки",
                "processing_status",
            )
            or ""
        ).strip(),
        "drive_url": str(
            get_field(fields, settings.voice_field_google_drive, "Google Drive", "google_drive_url") or ""
        ).strip(),
        "error": str(
            get_field(fields, settings.voice_field_processing_error, "Ошибка обработки", "processing_error") or ""
        ).strip(),
        "claim": str(
            get_field(
                fields,
                settings.voice_field_subscription_claim,
                "Subscription Queue Claim",
                "subscription_claim",
            )
            or ""
        ).strip(),
    }


def _is_eligible_drive_failure(state: dict[str, str], item: SpoolItem) -> bool:
    if state["route"] != "ChatGPT Subscription":
        return False
    if state["status"] != "Needs Review" or state["drive_url"] or state["claim"]:
        return False
    error = state["error"]
    return bool(
        error
        and (
            item.drive_error in error
            or "spooled=" in error
            or RECOVERY_ERROR_PREFIX in error
        )
    )


def _is_confirmed_recovery_state(state: dict[str, str]) -> bool:
    return (
        state["route"] == "ChatGPT Subscription"
        and state["status"] == "Awaiting Subscription"
        and bool(state["drive_url"])
        and not state["claim"]
    )


def _matches_expected_recovery_state(
    state: dict[str, str],
    *,
    folder_url: str,
    processing_error: str,
) -> bool:
    return (
        _is_confirmed_recovery_state(state)
        and state["drive_url"] == folder_url
        and state["error"] == processing_error
    )


def _recovered_manifest_extra(manifest: dict[str, Any]) -> dict[str, Any] | None:
    raw_extra = manifest.get("extra")
    if not isinstance(raw_extra, dict):
        return None
    extra = dict(raw_extra)
    extra.pop("drive_error", None)
    extra.pop("spooled", None)
    return extra or None


def _manifest_file_fingerprints(manifest: dict[str, Any]) -> list[tuple[str, str, int, str]]:
    result: list[tuple[str, str, int, str]] = []
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise DriveVerificationError("drive_manifest_files_invalid")
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise DriveVerificationError("drive_manifest_file_invalid")
        try:
            size = int(raw_file.get("size"))
        except (TypeError, ValueError) as exc:
            raise DriveVerificationError("drive_manifest_file_invalid") from exc
        result.append(
            (
                str(raw_file.get("name") or ""),
                str(raw_file.get("mime_type") or ""),
                size,
                str(raw_file.get("sha256") or "").casefold(),
            )
        )
    return result


def _append_technical_marker(existing_error: str, marker: str) -> str:
    existing = str(existing_error or "").strip()
    if marker in existing:
        return existing
    return f"{existing}; {marker}".strip("; ")


def _peek_item_id(folder: Path) -> str:
    try:
        if folder.is_symlink() or not folder.is_dir():
            return ""
        manifest_path = folder / MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
            return ""
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(payload.get("item_id") or "").strip() if isinstance(payload, dict) else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover verified Google Drive spool items safely")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Inspect only; do not mutate anything")
    mode.add_argument("--apply", action="store_true", help="Recover an idempotent limited batch")
    parser.add_argument("--batch-size", type=int, default=5, help="Maximum spool items to inspect (default: 5)")
    parser.add_argument("--item-id", default="", help="Process only the manifest with this exact item id")
    parser.add_argument(
        "--lock-timeout-seconds",
        type=int,
        default=3600,
        help="Age after which an abandoned item lock can be recovered",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.batch_size <= 0 or args.lock_timeout_seconds <= 0:
        print(json.dumps({"error": "invalid_arguments"}, sort_keys=True))
        return 2
    try:
        settings = get_settings()
        if not settings.google_drive_enabled or not settings.google_drive_root_folder_id:
            payload = asdict(RecoveryStats())
            payload.update({"dry_run": bool(args.dry_run), "disabled": True})
            print(json.dumps(payload, sort_keys=True))
            return 0
        airtable = AirtableClient(settings)
        drive_storage: DriveStorage | None = None
        verifier: RecoveryDriveVerifier | None = None
        if args.apply:
            drive_storage = GoogleDriveStorage(settings)
            verifier = GoogleDriveRecoveryVerifier(settings)
        recovery = DriveSpoolRecovery(
            settings,
            airtable=airtable,
            drive_storage=drive_storage,
            verifier=verifier,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
        stats = recovery.run(
            dry_run=bool(args.dry_run),
            batch_size=args.batch_size,
            item_id=args.item_id,
        )
    except Exception:
        logger.error("Drive spool recovery stopped because of a system error")
        print(json.dumps({"error": "system_error"}, sort_keys=True))
        return 1
    payload = asdict(stats)
    payload["dry_run"] = bool(args.dry_run)
    print(json.dumps(payload, sort_keys=True))
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
