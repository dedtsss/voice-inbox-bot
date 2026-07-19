from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.airtable import AirtableClient, AirtableError, ProjectMatch
from app.config import Settings, get_settings
from app.drive_storage import DriveStorageError, build_google_drive_service, safe_error

logger = logging.getLogger(__name__)

PROCESSOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "clean_text": {"type": "string"},
        "summary": {"type": "string"},
        "type": {"type": ["string", "null"]},
        "project": {"type": ["string", "null"]},
        "priority": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
        "counterparty": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        "period": {"type": ["string", "null"]},
        "next_action": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "needs_review_reasons": {"type": "array", "items": {"type": "string"}},
        "routing_reason": {"type": "string"},
    },
    "required": [
        "title",
        "clean_text",
        "summary",
        "type",
        "project",
        "priority",
        "due_date",
        "counterparty",
        "amount",
        "period",
        "next_action",
        "tags",
        "confidence",
        "needs_review_reasons",
        "routing_reason",
    ],
}

IMAGE_MIME_PREFIX = "image/"
AUDIO_MIME_PREFIX = "audio/"
VIDEO_MIME_PREFIX = "video/"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
AUDIO_EXTENSIONS = {".aac", ".m4a", ".mp3", ".mpeg", ".oga", ".ogg", ".opus", ".wav"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".webm"}
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
ATTEMPT_RE = re.compile(r"\battempt=(\d+)\b")
SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]+|pat[A-Za-z0-9]+|gh[opsu]_[A-Za-z0-9_]+)")


class VoiceProcessorError(RuntimeError):
    transient = False


class TransientVoiceProcessorError(VoiceProcessorError):
    transient = True


class PermanentVoiceProcessorError(VoiceProcessorError):
    transient = False


class ProcessorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    clean_text: str
    summary: str
    type: str | None = None
    project: str | None = None
    priority: str | None = None
    due_date: str | None = None
    counterparty: str | None = None
    amount: float | None = None
    period: str | None = None
    next_action: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    needs_review_reasons: list[str] = Field(default_factory=list)
    routing_reason: str = ""

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class DriveOriginal:
    name: str
    mime_type: str
    path: Path
    size: int
    drive_file_id: str


@dataclass(frozen=True)
class MediaExtraction:
    source_text: str
    content_blocks: list[str]
    trace: dict[str, Any]


@dataclass(frozen=True)
class AllowedContext:
    type_options: set[str]
    priority_options: set[str]
    status_options: set[str]
    tag_options: set[str]
    projects: list[ProjectMatch]


@dataclass(frozen=True)
class ValidatedResult:
    output: ProcessorOutput
    status: str
    project: ProjectMatch | None
    review_reasons: list[str]
    source_text: str
    used_rule_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessingClaim:
    record_id: str
    lock_id: str
    attempt: int


@dataclass
class ProcessRunStats:
    processed: int = 0
    needs_review: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0
    learned: int = 0


class GoogleDriveInboxReader:
    def __init__(self, settings: Settings, service: Any | None = None) -> None:
        self.settings = settings
        self.service = service if service is not None else build_google_drive_service(settings)

    def download_record_originals(self, google_drive_url: str, target_dir: Path) -> tuple[dict[str, Any], list[DriveOriginal]]:
        folder_id = drive_folder_id_from_url(google_drive_url)
        if not folder_id:
            raise PermanentVoiceProcessorError("Google Drive folder id was not found on the Airtable record")
        manifest_file = self._find_child(folder_id, "manifest.json")
        if not manifest_file:
            raise PermanentVoiceProcessorError("Google Drive manifest.json was not found")

        manifest_path = target_dir / "manifest.download.json"
        try:
            self._download_to_path(
                manifest_file["id"],
                manifest_path,
                max_bytes=max(1_000_000, self.settings.voice_processor_max_file_bytes),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentVoiceProcessorError("Google Drive manifest.json is invalid") from exc
        finally:
            manifest_path.unlink(missing_ok=True)

        originals: list[DriveOriginal] = []
        downloaded_record_bytes = 0
        for index, item in enumerate(manifest.get("files") or [], start=1):
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("drive_file_id") or "")
            if not file_id or file_id == "spooled":
                continue
            name = safe_original_name(str(item.get("name") or f"file_{index}.bin"))
            mime_type = str(item.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
            manifest_size = manifest_file_size(item)
            if manifest_size is not None and manifest_size > self.settings.voice_processor_max_file_bytes:
                raise PermanentVoiceProcessorError(
                    f"Google Drive file {name} exceeds VOICE_PROCESSOR_MAX_FILE_BYTES "
                    f"manifest_size={manifest_size} max={self.settings.voice_processor_max_file_bytes}"
                )
            if (
                manifest_size is not None
                and downloaded_record_bytes + manifest_size > self.settings.voice_processor_max_record_bytes
            ):
                raise PermanentVoiceProcessorError(
                    "Google Drive record exceeds VOICE_PROCESSOR_MAX_RECORD_BYTES "
                    f"manifest_total={downloaded_record_bytes + manifest_size} "
                    f"max={self.settings.voice_processor_max_record_bytes}"
                )

            path = unique_download_path(target_dir, name)
            max_remaining = max(0, self.settings.voice_processor_max_record_bytes - downloaded_record_bytes)
            max_file_bytes = min(self.settings.voice_processor_max_file_bytes, max_remaining)
            try:
                actual_size = self._download_to_path(file_id, path, max_bytes=max_file_bytes)
                if manifest_size is not None and actual_size != manifest_size:
                    raise PermanentVoiceProcessorError(
                        f"Google Drive file {name} size mismatch manifest_size={manifest_size} actual_size={actual_size}"
                    )
                if downloaded_record_bytes + actual_size > self.settings.voice_processor_max_record_bytes:
                    raise PermanentVoiceProcessorError(
                        "Google Drive record exceeds VOICE_PROCESSOR_MAX_RECORD_BYTES "
                        f"actual_total={downloaded_record_bytes + actual_size} "
                        f"max={self.settings.voice_processor_max_record_bytes}"
                    )
                verify_manifest_sha256(item, path, name)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            downloaded_record_bytes += actual_size
            originals.append(
                DriveOriginal(
                    name=name,
                    mime_type=mime_type,
                    path=path,
                    size=actual_size,
                    drive_file_id=file_id,
                )
            )
        return manifest, originals

    def _find_child(self, folder_id: str, name: str) -> dict[str, Any] | None:
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        response = (
            self.service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false and name = '{escaped_name}'",
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

    def _download_to_path(self, file_id: str, target_path: Path, *, max_bytes: int) -> int:
        if max_bytes <= 0:
            raise PermanentVoiceProcessorError("Google Drive record byte limit is exhausted")
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise DriveStorageError("Google Drive dependencies are not installed") from exc

        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as output:
            downloader = MediaIoBaseDownload(output, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
                if output.tell() > max_bytes:
                    raise PermanentVoiceProcessorError(
                        f"Google Drive file exceeds configured byte limit actual_size>{max_bytes}"
                    )
        return target_path.stat().st_size


class MediaExtractor:
    def __init__(self, settings: Settings, ai: "VoiceProcessorAI") -> None:
        self.settings = settings
        self.ai = ai

    async def extract(
        self,
        *,
        record: dict,
        manifest: dict[str, Any],
        originals: list[DriveOriginal],
        temp_dir: Path,
    ) -> MediaExtraction:
        text_parts = collect_record_text(record, manifest, self.settings)
        blocks: list[str] = []
        trace: dict[str, Any] = {
            "manifest_item_id": manifest.get("item_id"),
            "source": manifest.get("source"),
            "manifest_type": manifest.get("type"),
            "files": [
                {"name": item.name, "mime_type": item.mime_type, "size": item.size, "drive_file_id": item.drive_file_id}
                for item in originals
            ],
            "audio_files": 0,
            "image_files": 0,
            "video_files": 0,
            "video_frames": 0,
        }

        if text_parts:
            blocks.append("Текст записи:\n" + "\n".join(text_parts))

        for original in originals:
            media_type = classify_media(original)
            if media_type == "audio":
                trace["audio_files"] += 1
                transcript = await self.ai.transcribe_audio(original.path)
                if transcript:
                    blocks.append(f"Транскрипт аудио {original.name}:\n{transcript}")
                    text_parts.append(transcript)
                continue

            if media_type == "image":
                trace["image_files"] += 1
                image_path = await self.prepare_image(original.path, temp_dir)
                description = await self.ai.describe_images([image_path], f"Проанализируй изображение {original.name}.")
                if description:
                    blocks.append(f"Анализ изображения {original.name}:\n{description}")
                continue

            if media_type == "video":
                trace["video_files"] += 1
                audio_path = temp_dir / f"{original.path.stem}_audio.mp3"
                await extract_video_audio(original.path, audio_path)
                transcript = await self.ai.transcribe_audio(audio_path)
                if transcript:
                    blocks.append(f"Транскрипт видео {original.name}:\n{transcript}")
                    text_parts.append(transcript)
                frames = await extract_video_frames(
                    original.path,
                    temp_dir / f"{original.path.stem}_frames",
                    max_frames=self.settings.voice_processor_max_video_frames,
                    interval_seconds=self.settings.voice_processor_video_frame_interval_seconds,
                    max_edge=self.settings.voice_processor_image_max_edge,
                )
                trace["video_frames"] += len(frames)
                if frames:
                    description = await self.ai.describe_images(frames, f"Проанализируй кадры видео {original.name}.")
                    if description:
                        blocks.append(f"Анализ кадров видео {original.name}:\n{description}")
                continue

            blocks.append(f"Неподдержанный файл сохранён в Drive: {original.name} ({original.mime_type})")

        source_text = "\n".join(part for part in text_parts if part).strip()
        return MediaExtraction(source_text=source_text, content_blocks=blocks, trace=trace)

    async def prepare_image(self, image_path: Path, temp_dir: Path) -> Path:
        if image_path.stat().st_size <= self.settings.voice_processor_max_image_bytes:
            return image_path
        target = temp_dir / f"{image_path.stem}_resized.jpg"
        await run_ffmpeg(
            "-y",
            "-i",
            str(image_path),
            "-vf",
            f"scale='if(gt(iw,{self.settings.voice_processor_image_max_edge}),{self.settings.voice_processor_image_max_edge},iw)':-2",
            "-q:v",
            "5",
            str(target),
        )
        return target


class VoiceProcessorAI:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client if client is not None else AsyncOpenAI(api_key=settings.openai_api_key)

    async def transcribe_audio(self, audio_path: Path) -> str:
        with audio_path.open("rb") as audio_file:
            result = await self.client.audio.transcriptions.create(
                model=self.settings.voice_processor_transcription_model,
                file=audio_file,
            )
        return str(getattr(result, "text", "")).strip()

    async def describe_images(self, image_paths: list[Path], prompt: str) -> str:
        if not image_paths:
            return ""
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    f"{prompt}\n"
                    "Извлеки видимый текст на любом языке, назови важные объекты/сцену, "
                    "суммы, даты, людей/организации и любые сигналы для маршрутизации."
                ),
            }
        ]
        for path in image_paths:
            mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            content.append({"type": "input_image", "image_url": image_data_url(path, mime_type)})

        response = await self.client.responses.create(
            model=self.settings.voice_processor_text_model,
            input=[{"role": "user", "content": content}],
        )
        return str(getattr(response, "output_text", "")).strip()

    async def structure_record(
        self,
        *,
        context: str,
        allowed: AllowedContext,
        rules: list[dict],
    ) -> dict[str, Any]:
        completion = await self.client.chat.completions.create(
            model=self.settings.voice_processor_text_model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "voice_inbox_processor_result",
                    "strict": True,
                    "schema": PROCESSOR_OUTPUT_SCHEMA,
                },
            },
            messages=[
                {"role": "system", "content": build_structure_system_prompt(allowed, rules)},
                {"role": "user", "content": context[: self.settings.voice_processor_max_prompt_chars]},
            ],
        )
        message = completion.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise PermanentVoiceProcessorError(f"OpenAI refused structured processing: {safe_error(refusal)}")
        content = message.content or "{}"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise TransientVoiceProcessorError("OpenAI returned invalid JSON despite schema mode") from exc
        if not isinstance(parsed, dict):
            raise TransientVoiceProcessorError("OpenAI structured output was not an object")
        return parsed


class VoiceInboxProcessor:
    def __init__(
        self,
        settings: Settings,
        *,
        airtable: AirtableClient | None = None,
        drive_reader: GoogleDriveInboxReader | None = None,
        ai: VoiceProcessorAI | None = None,
        media_extractor: MediaExtractor | None = None,
    ) -> None:
        self.settings = settings
        self.airtable = airtable if airtable is not None else AirtableClient(settings)
        self._drive_reader = drive_reader
        self._ai = ai
        self._media_extractor = media_extractor

    @property
    def drive_reader(self) -> GoogleDriveInboxReader:
        if self._drive_reader is None:
            self._drive_reader = GoogleDriveInboxReader(self.settings)
        return self._drive_reader

    @property
    def ai(self) -> VoiceProcessorAI:
        if self._ai is None:
            self._ai = VoiceProcessorAI(self.settings)
        return self._ai

    @property
    def media_extractor(self) -> MediaExtractor:
        if self._media_extractor is None:
            self._media_extractor = MediaExtractor(self.settings, self.ai)
        return self._media_extractor

    async def run_loop(self, stop_event: asyncio.Event | None = None) -> None:
        logger.info("Voice processor loop started")
        while stop_event is None or not stop_event.is_set():
            stats = await self.run_once()
            logger.info("Voice processor run stats: %s", stats)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(self.settings.voice_processor_interval_seconds),
                    timeout=self.settings.voice_processor_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, batch_size: int | None = None) -> ProcessRunStats:
        stats = ProcessRunStats()
        stats.learned += await self.apply_pending_corrections()
        records = await asyncio.to_thread(
            self.airtable.list_voice_records_for_processing,
            batch_size=batch_size or self.settings.voice_processor_batch_size,
            stale_processing_seconds=self.settings.voice_processor_stale_processing_seconds,
        )
        for record in records:
            result = await self.process_record(record)
            if result == "processed":
                stats.processed += 1
            elif result == "needs_review":
                stats.needs_review += 1
            elif result == "retry":
                stats.retried += 1
            elif result == "failed":
                stats.failed += 1
            else:
                stats.skipped += 1
        return stats

    async def run_record(self, record_id: str) -> str:
        record = await asyncio.to_thread(self.airtable.fetch_voice_record, record_id)
        return await self.process_record(record)

    async def process_record(self, record: dict) -> str:
        if not should_process_record(record, self.settings):
            logger.info("Voice processor skipped already handled record %s", record.get("id"))
            return "skipped"
        claim = await self.claim_record(record)
        if claim is None:
            return "skipped"
        try:
            validated = await self._process_claimed_record(claim)
        except Exception as exc:
            return await self._handle_processing_exception(claim, exc)

        fields = build_airtable_update_fields(self.settings, validated)
        await asyncio.to_thread(self.airtable.update_voice_record_fields, claim.record_id, fields)
        await self._mark_used_rules(validated.used_rule_ids)
        if self.settings.voice_processor_create_project_items:
            logger.warning("PROCESSOR_CREATE_PROJECT_ITEMS is true, but project item creation is not implemented in v1")
        return "needs_review" if validated.status == "Needs Review" else "processed"

    async def claim_record(self, record: dict) -> ProcessingClaim | None:
        record_id = str(record.get("id") or "")
        if not record_id:
            return None
        fields = record.get("fields") or {}
        error_text = str(get_field(fields, self.settings.voice_field_processing_error, "Ошибка обработки") or "")
        attempt = parse_attempt(error_text) + 1
        if attempt > self.settings.voice_processor_max_retries:
            await asyncio.to_thread(
                self.airtable.update_voice_record_fields,
                record_id,
                {
                    self.settings.voice_field_processing_status: "Needs Review",
                    self.settings.voice_field_processing_error: (
                        f"voice_processor max retries exceeded attempt={attempt - 1} "
                        f"max={self.settings.voice_processor_max_retries}"
                    ),
                },
            )
            return None

        lock_id = uuid.uuid4().hex
        started_at = datetime.now(UTC).isoformat()
        trace = (
            f"voice_processor lock_id={lock_id} started_at={started_at} "
            f"attempt={attempt} version={self.settings.voice_processor_version}"
        )
        await asyncio.to_thread(
            self.airtable.update_voice_record_fields,
            record_id,
            {
                self.settings.voice_field_processing_status: "Processing",
                self.settings.voice_field_processing_error: trace,
                self.settings.voice_field_processor_version: self.settings.voice_processor_version,
            },
        )
        claimed = await asyncio.to_thread(self.airtable.fetch_voice_record, record_id)
        claimed_fields = claimed.get("fields") or {}
        claimed_error = str(get_field(claimed_fields, self.settings.voice_field_processing_error, "Ошибка обработки") or "")
        claimed_status = get_field(
            claimed_fields,
            self.settings.voice_field_processing_status,
            self.settings.voice_field_processing_status_query_name,
            "Статус обработки",
        )
        if claimed_status != "Processing" or lock_id not in claimed_error:
            logger.info("Voice processor claim lost for record %s", record_id)
            return None
        return ProcessingClaim(record_id=record_id, lock_id=lock_id, attempt=attempt)

    async def _process_claimed_record(self, claim: ProcessingClaim) -> ValidatedResult:
        record = await asyncio.to_thread(self.airtable.fetch_voice_record, claim.record_id)
        table_metadata, projects, rules = await asyncio.gather(
            asyncio.to_thread(
                self.airtable.find_table_metadata,
                self.settings.voice_inbox_base_id,
                table_id=self.settings.voice_inbox_table_id,
            ),
            asyncio.to_thread(self.airtable.list_projects),
            asyncio.to_thread(
                self.airtable.list_processing_rules,
                active_only=True,
                page_size=max(1, self.settings.voice_processor_max_rules * 4),
            ),
        )
        if not table_metadata:
            raise PermanentVoiceProcessorError("Voice Inbox Airtable metadata was not found")
        allowed = allowed_context_from_metadata(table_metadata, self.settings, projects)
        fields = record.get("fields") or {}
        drive_url = get_field(fields, self.settings.voice_field_google_drive, "Google Drive")
        manifest: dict[str, Any] = {}
        originals: list[DriveOriginal] = []
        with tempfile.TemporaryDirectory(prefix="voice_processor_") as tmp:
            temp_dir = Path(tmp)
            if drive_url:
                manifest, originals = await asyncio.to_thread(
                    self.drive_reader.download_record_originals,
                    str(drive_url),
                    temp_dir,
                )
            media = await self.media_extractor.extract(
                record=record,
                manifest=manifest,
                originals=originals,
                temp_dir=temp_dir,
            )

        context = build_record_context(
            record=record,
            manifest=manifest,
            media=media,
            settings=self.settings,
        )
        relevant_rules = select_relevant_rules(
            rules,
            context,
            limit=self.settings.voice_processor_max_rules,
        )
        raw_result = await retry_async(
            lambda: self.ai.structure_record(context=context, allowed=allowed, rules=relevant_rules),
            max_attempts=self.settings.voice_processor_max_retries,
            base_delay=self.settings.voice_processor_retry_base_seconds,
        )
        used_rule_ids: list[str] = []
        raw_result, used_rule_ids = apply_deterministic_rules(raw_result, relevant_rules, context)
        return validate_processor_output(
            raw_result,
            allowed=allowed,
            settings=self.settings,
            media=media,
            manifest=manifest,
            record_id=claim.record_id,
            attempt=claim.attempt,
            lock_id=claim.lock_id,
            used_rule_ids=used_rule_ids,
        )

    async def _handle_processing_exception(self, claim: ProcessingClaim, error: Exception) -> str:
        redacted = redact_secrets(safe_error(error))
        is_transient = is_transient_error(error)
        next_attempt = claim.attempt + 1
        if is_transient and next_attempt <= self.settings.voice_processor_max_retries:
            status = "New"
            result = "retry"
        else:
            status = "Needs Review"
            result = "failed"
        await asyncio.to_thread(
            self.airtable.update_voice_record_fields,
            claim.record_id,
            {
                self.settings.voice_field_processing_status: status,
                self.settings.voice_field_processing_error: (
                    f"voice_processor error transient={str(is_transient).lower()} "
                    f"attempt={claim.attempt} max={self.settings.voice_processor_max_retries} "
                    f"lock_id={claim.lock_id} error={redacted}"
                ),
                self.settings.voice_field_processor_version: self.settings.voice_processor_version,
            },
        )
        logger.warning("Voice processor failed for %s: %s", claim.record_id, redacted)
        return result

    async def apply_pending_corrections(self) -> int:
        records = await asyncio.to_thread(
            self.airtable.list_voice_correction_candidates,
            max_records=self.settings.voice_processor_batch_size,
        )
        learned = 0
        for record in records:
            if await self.apply_correction_learning(record):
                learned += 1
        return learned

    async def apply_correction_learning(self, record: dict) -> bool:
        fields = record.get("fields") or {}
        record_id = str(record.get("id") or "")
        if not truthy(get_field(fields, self.settings.voice_field_train_on_correction, "Обучить на исправлении")):
            return False
        if truthy(get_field(fields, self.settings.voice_field_training_applied, "Обучение учтено")):
            return False

        snapshot_raw = get_field(fields, self.settings.voice_field_ai_result_json, "AI результат JSON")
        if not isinstance(snapshot_raw, str) or not snapshot_raw.strip():
            await asyncio.to_thread(
                self.airtable.update_voice_record_fields,
                record_id,
                {
                    self.settings.voice_field_training_applied: True,
                    self.settings.voice_field_train_on_correction: False,
                    self.settings.voice_field_processing_error: "voice_processor learning skipped: missing AI snapshot",
                },
            )
            return False
        try:
            snapshot = json.loads(snapshot_raw)
        except json.JSONDecodeError:
            snapshot = {}
        before = snapshot.get("validated") if isinstance(snapshot, dict) else {}
        if not isinstance(before, dict):
            before = {}

        projects = await asyncio.to_thread(self.airtable.list_projects)
        changes = correction_changes(fields, before, self.settings, projects)
        comment = str(get_field(fields, self.settings.voice_field_correction_comment, "Комментарий к исправлению") or "")
        if not changes:
            await asyncio.to_thread(
                self.airtable.update_voice_record_fields,
                record_id,
                {
                    self.settings.voice_field_training_applied: True,
                    self.settings.voice_field_train_on_correction: False,
                    self.settings.voice_field_processing_error: "voice_processor learning skipped: no structured diff",
                },
            )
            return False

        source_text = str(
            get_field(fields, self.settings.voice_field_raw_text, "Исходная фраза")
            or before.get("clean_text")
            or before.get("summary")
            or ""
        )
        rule_fields = build_rule_from_correction(
            record_id=record_id,
            changes=changes,
            source_text=source_text,
            comment=comment,
        )
        await asyncio.to_thread(self.airtable.create_processing_rule, rule_fields)
        await asyncio.to_thread(
            self.airtable.update_voice_record_fields,
            record_id,
            {
                self.settings.voice_field_training_applied: True,
                self.settings.voice_field_train_on_correction: False,
                self.settings.voice_field_processing_error: "voice_processor learning rule created",
            },
        )
        return True

    async def _mark_used_rules(self, rule_ids: list[str]) -> None:
        if not rule_ids:
            return
        for rule_id in rule_ids:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.airtable.update_processing_rule_fields,
                    rule_id,
                    {
                        "Последнее использование": datetime.now(UTC).isoformat(),
                    },
                )


def build_structure_system_prompt(allowed: AllowedContext, rules: list[dict]) -> str:
    projects = [project.title for project in allowed.projects]
    rule_lines: list[str] = []
    for rule in rules:
        fields = rule.get("fields") or {}
        rule_lines.append(
            json.dumps(
                {
                    "id": rule.get("id"),
                    "area": fields.get("Область"),
                    "condition": fields.get("Условие"),
                    "correct_decision": fields.get("Правильное решение"),
                    "project": fields.get("Проект"),
                    "type": fields.get("Тип"),
                },
                ensure_ascii=False,
            )
        )

    return (
        "Ты обрабатываешь личный Voice Inbox в Airtable. Верни только объект по JSON Schema. "
        "Пиши title, clean_text, summary, next_action, tags и reasons по-русски, если запись русская. "
        "Не выдумывай проекты или значения select. Если проект, тип или приоритет не очевидны, ставь null "
        "и добавляй причину в needs_review_reasons. Даты нормализуй в YYYY-MM-DD, суммы в number. "
        "Не создавай задачи, только классифицируй запись.\n\n"
        f"Допустимые type: {json.dumps(sorted(allowed.type_options), ensure_ascii=False)}\n"
        f"Допустимые priority: {json.dumps(sorted(allowed.priority_options), ensure_ascii=False)}\n"
        f"Допустимые tags: {json.dumps(sorted(allowed.tag_options), ensure_ascii=False)}\n"
        f"Существующие проекты: {json.dumps(projects, ensure_ascii=False)}\n"
        f"Активные правила/примеры: {chr(10).join(rule_lines) if rule_lines else 'нет'}"
    )


def build_record_context(
    *,
    record: dict,
    manifest: dict[str, Any],
    media: MediaExtraction,
    settings: Settings,
) -> str:
    fields = record.get("fields") or {}
    parts = [
        f"Airtable record id: {record.get('id')}",
        f"External ID: {get_field(fields, settings.voice_field_external_id, 'External ID') or manifest.get('item_id') or ''}",
        f"Источник: {get_field(fields, settings.voice_field_source, 'Источник') or manifest.get('source') or ''}",
        f"Тип входящих данных: {get_field(fields, settings.voice_field_type, 'Тип') or manifest.get('type') or ''}",
    ]
    parts.extend(media.content_blocks)
    if not media.content_blocks:
        source_text = media.source_text.strip()
        parts.append("Текст записи:\n" + (source_text or "(пусто)"))
    return trim_prompt("\n\n".join(parts), settings.voice_processor_max_prompt_chars)


def should_process_record(record: dict, settings: Settings) -> bool:
    fields = record.get("fields") or {}
    status = get_field(
        fields,
        settings.voice_field_processing_status,
        settings.voice_field_processing_status_query_name,
        "Статус обработки",
    )
    if status in (None, ""):
        return True
    return clean_string(status, "").casefold() in {"new", "processing"}


def collect_record_text(record: dict, manifest: dict[str, Any], settings: Settings) -> list[str]:
    fields = record.get("fields") or {}
    candidates = [
        manifest.get("text"),
        get_field(fields, settings.voice_field_raw_text, "Исходная фраза"),
        get_field(fields, settings.voice_field_clean_text, "Очищенный текст"),
        get_field(fields, settings.voice_field_notes, "Notes"),
    ]
    parts: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            text = candidate.strip()
            if text and text not in parts:
                parts.append(text)
    extra_payload = manifest.get("extra", {}).get("payload") if isinstance(manifest.get("extra"), dict) else None
    if isinstance(extra_payload, dict):
        for key in ("text", "raw_text", "caption", "description", "content"):
            value = extra_payload.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in parts:
                parts.append(value.strip())
    return parts


def validate_processor_output(
    raw_result: dict[str, Any],
    *,
    allowed: AllowedContext,
    settings: Settings,
    media: MediaExtraction,
    manifest: dict[str, Any],
    record_id: str,
    attempt: int,
    lock_id: str,
    used_rule_ids: list[str],
) -> ValidatedResult:
    try:
        output = ProcessorOutput.model_validate(raw_result)
    except ValidationError as exc:
        raise TransientVoiceProcessorError(f"Structured output failed local validation: {safe_error(exc)}") from exc

    reasons = list(output.needs_review_reasons)
    output.title = clean_string(output.title, "Заметка")[:120]
    output.clean_text = clean_string(output.clean_text, media.source_text)[:12000]
    output.summary = clean_string(output.summary, output.clean_text[:500])[:1000]
    output.routing_reason = clean_string(output.routing_reason, "")[:1000]
    output.next_action = nullable_clean(output.next_action, limit=500)
    output.counterparty = nullable_clean(output.counterparty, limit=300)
    output.period = nullable_clean(output.period, limit=300)
    output.due_date = normalize_due_date(output.due_date)
    output.tags = normalize_tags(output.tags, allowed.tag_options)

    output.type = normalize_select(output.type, allowed.type_options)
    if raw_result.get("type") and not output.type:
        reasons.append("AI предложил недопустимый тип Airtable")
    output.priority = normalize_select(output.priority, allowed.priority_options)
    if raw_result.get("priority") and not output.priority:
        reasons.append("AI предложил недопустимый приоритет Airtable")

    project = find_exact_project(output.project, allowed.projects)
    if raw_result.get("project") and project is None:
        reasons.append("AI предложил несуществующий проект")
    output.project = project.title if project else None

    if output.confidence < settings.voice_processor_confidence_threshold:
        reasons.append("Низкая уверенность AI")
    if not output.type:
        reasons.append("Тип не определён уверенно")
    if not output.project:
        reasons.append("Проект не определён уверенно")

    deduped_reasons = dedupe_strings(reasons)[:10]
    output.needs_review_reasons = deduped_reasons
    status = "Needs Review" if deduped_reasons else "Processed"
    if status not in allowed.status_options and allowed.status_options:
        status = "Needs Review" if "Needs Review" in allowed.status_options else sorted(allowed.status_options)[0]

    snapshot = {
        "raw_model": raw_result,
        "validated": output.model_dump(),
        "status": status,
        "processor": {
            "version": settings.voice_processor_version,
            "record_id": record_id,
            "attempt": attempt,
            "lock_id": lock_id,
            "processed_at": datetime.now(UTC).isoformat(),
            "manifest_item_id": manifest.get("item_id"),
            "used_rule_ids": used_rule_ids,
        },
        "media_trace": media.trace,
    }
    output_snapshot = ProcessorOutput.model_validate(output.model_dump())
    output_snapshot.routing_reason = json.dumps(snapshot, ensure_ascii=False)
    return ValidatedResult(
        output=output_snapshot,
        status=status,
        project=project,
        review_reasons=deduped_reasons,
        source_text=media.source_text,
        used_rule_ids=used_rule_ids,
    )


def build_airtable_update_fields(settings: Settings, validated: ValidatedResult) -> dict[str, Any]:
    output = validated.output
    snapshot_json = output.routing_reason
    fields: dict[str, Any] = {
        settings.voice_field_title: output.title,
        settings.voice_field_summary: output.summary,
        settings.voice_field_clean_text: output.clean_text,
        settings.voice_field_raw_text: validated.source_text or output.clean_text,
        settings.voice_field_type: output.type,
        settings.voice_field_priority: output.priority,
        settings.voice_field_due_date: output.due_date,
        settings.voice_field_counterparty: output.counterparty,
        settings.voice_field_amount: output.amount,
        settings.voice_field_period: output.period,
        settings.voice_field_next_action: output.next_action,
        settings.voice_field_tags: output.tags,
        settings.voice_field_processing_status: validated.status,
        settings.voice_field_processing_error: None,
        settings.voice_field_ai_result_json: snapshot_json,
        settings.voice_field_ai_confidence: output.confidence,
        settings.voice_field_processor_version: settings.voice_processor_version,
    }
    if validated.project:
        fields[settings.voice_field_project] = validated.project.title
    else:
        fields[settings.voice_field_project] = None
    return fields


def allowed_context_from_metadata(table: dict, settings: Settings, projects: list[ProjectMatch]) -> AllowedContext:
    project_options = select_options_for_field(table, settings.voice_field_project)
    return AllowedContext(
        type_options=select_options_for_field(table, settings.voice_field_type),
        priority_options=select_options_for_field(table, settings.voice_field_priority),
        status_options=select_options_for_field(table, settings.voice_field_processing_status),
        tag_options=select_options_for_field(table, settings.voice_field_tags),
        projects=project_matches_from_voice_options(project_options, projects),
    )


def project_matches_from_voice_options(options: set[str], projects: list[ProjectMatch]) -> list[ProjectMatch]:
    projects_os_by_title = {project.title.casefold(): project.record_id for project in projects}
    return [
        ProjectMatch(record_id=projects_os_by_title.get(option.casefold(), ""), title=option)
        for option in sorted(options, key=str.casefold)
    ]


def select_options_for_field(table: dict, configured_field: str) -> set[str]:
    if not configured_field:
        return set()
    for field in table.get("fields") or []:
        if configured_field in {field.get("id"), field.get("name")}:
            options = field.get("options") or {}
            return {
                str(choice.get("name")).strip()
                for choice in options.get("choices") or []
                if str(choice.get("name") or "").strip()
            }
    return set()


def select_relevant_rules(rules: list[dict], context: str, *, limit: int) -> list[dict]:
    context_words = keyword_set(context)
    scored: list[tuple[int, dict]] = []
    for rule in rules:
        fields = rule.get("fields") or {}
        haystack = " ".join(
            str(fields.get(key) or "")
            for key in ("Условие", "Правильное решение", "Проект", "Тип", "Положительный пример", "Комментарий пользователя")
        )
        score = len(context_words & keyword_set(haystack))
        if score > 0 or not context_words:
            scored.append((score, rule))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [rule for _, rule in scored[: max(0, limit)]]


def apply_deterministic_rules(raw_result: dict[str, Any], rules: list[dict], context: str) -> tuple[dict[str, Any], list[str]]:
    updated = dict(raw_result)
    used_rule_ids: list[str] = []
    context_lower = context.casefold()
    for rule in rules:
        fields = rule.get("fields") or {}
        condition = str(fields.get("Условие") or fields.get("Положительный пример") or "").strip()
        if condition:
            keywords = [word for word in keyword_set(condition) if len(word) >= 4]
            if keywords and not any(word in context_lower for word in keywords):
                continue
        decision = parse_rule_decision(fields.get("Правильное решение"))
        if fields.get("Проект") and not decision.get("project"):
            decision["project"] = fields.get("Проект")
        if fields.get("Тип") and not decision.get("type"):
            decision["type"] = fields.get("Тип")
        if not decision:
            continue
        for key in ("project", "type", "priority", "next_action"):
            if decision.get(key):
                updated[key] = decision[key]
        used_rule_ids.append(str(rule.get("id") or ""))
    return updated, [rule_id for rule_id in used_rule_ids if rule_id]


def parse_rule_decision(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    decision: dict[str, Any] = {}
    for line in value.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        normalized_key = key.strip().casefold()
        if normalized_key in {"project", "проект"}:
            decision["project"] = raw.strip()
        elif normalized_key in {"type", "тип"}:
            decision["type"] = raw.strip()
        elif normalized_key in {"priority", "приоритет"}:
            decision["priority"] = raw.strip()
        elif normalized_key in {"next_action", "следующее действие"}:
            decision["next_action"] = raw.strip()
    return decision


def correction_changes(
    fields: dict[str, Any],
    before: dict[str, Any],
    settings: Settings,
    projects: list[ProjectMatch],
) -> dict[str, Any]:
    current_project = project_name_from_field(get_field(fields, settings.voice_field_project, "Проект"), projects)
    candidates = {
        "title": get_field(fields, settings.voice_field_title, "Название"),
        "type": get_field(fields, settings.voice_field_type, "Тип"),
        "project": current_project,
        "priority": get_field(fields, settings.voice_field_priority, "Приоритет"),
        "due_date": normalize_due_date(get_field(fields, settings.voice_field_due_date, "Срок")),
        "counterparty": get_field(fields, settings.voice_field_counterparty, "Контрагент"),
        "amount": get_field(fields, settings.voice_field_amount, "Сумма"),
        "period": get_field(fields, settings.voice_field_period, "Период"),
        "next_action": get_field(fields, settings.voice_field_next_action, "Следующее действие"),
    }
    changes: dict[str, Any] = {}
    for key, current in candidates.items():
        if current in (None, "", []):
            continue
        previous = before.get(key)
        if normalize_comparable(current) != normalize_comparable(previous):
            changes[key] = current
    return changes


def build_rule_from_correction(
    *,
    record_id: str,
    changes: dict[str, Any],
    source_text: str,
    comment: str,
) -> dict[str, Any]:
    area = "Все"
    if "project" in changes:
        area = "Маршрутизация"
    elif "type" in changes:
        area = "Тип"
    elif "priority" in changes:
        area = "Приоритет"
    elif "next_action" in changes:
        area = "Следующее действие"

    condition = "\n".join(
        part
        for part in [
            "Применять к похожим записям.",
            f"Ключевые слова: {', '.join(sorted(keyword_set(source_text))[:12])}" if source_text else "",
            f"Комментарий: {comment.strip()}" if comment.strip() else "",
        ]
        if part
    )
    return {
        "Правило": f"Correction {record_id} {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}",
        "Активно": True,
        "Область": area,
        "Условие": condition[:2000],
        "Правильное решение": json.dumps(changes, ensure_ascii=False, sort_keys=True),
        "Проект": str(changes.get("project") or ""),
        "Тип": str(changes.get("type") or ""),
        "Положительный пример": source_text[:4000],
        "Источник записи": record_id,
        "Комментарий пользователя": comment[:2000],
        "Использований": 0,
    }


def project_name_from_field(value: Any, projects: list[ProjectMatch]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        exact = find_exact_project(value, projects)
        return exact.title if exact else value
    project_by_id = {project.record_id: project.title for project in projects}
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            return project_by_id.get(first, first)
        if isinstance(first, dict):
            linked_id = first.get("id")
            name = first.get("name")
            return project_by_id.get(linked_id, name)
    return None


def find_exact_project(project_name: str | None, projects: list[ProjectMatch]) -> ProjectMatch | None:
    wanted = clean_string(project_name, "").casefold()
    if not wanted:
        return None
    for project in projects:
        if project.title.casefold() == wanted:
            return project
    return None


def normalize_select(value: Any, allowed: set[str]) -> str | None:
    text = clean_string(value, "")
    if not text:
        return None
    if not allowed:
        return text
    for option in allowed:
        if option.casefold() == text.casefold():
            return option
    return None


def normalize_due_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = clean_string(value, "")
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return date.fromisoformat(text[:10]).isoformat()
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        day, month, year = match.groups()
        with contextlib.suppress(ValueError):
            return date(int(year), int(month), int(day)).isoformat()
    return None


def normalize_tags(tags: list[str], allowed: set[str]) -> list[str]:
    normalized: list[str] = []
    for tag in tags:
        text = clean_string(tag, "")[:50]
        if not text:
            continue
        if allowed:
            matched = normalize_select(text, allowed)
            if not matched:
                continue
            text = matched
        if text not in normalized:
            normalized.append(text)
    return normalized[:10]


def clean_string(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).strip().split())
    return text or fallback


def nullable_clean(value: Any, *, limit: int) -> str | None:
    text = clean_string(value, "")
    return text[:limit] if text else None


def dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = clean_string(value, "")
        if text and text not in result:
            result.append(text)
    return result


def trim_prompt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 80
    return text[:head] + "\n\n[...context truncated by VOICE_PROCESSOR_MAX_PROMPT_CHARS...]\n\n" + text[-tail:]


def get_field(fields: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name and name in fields:
            return fields[name]
    return None


def parse_attempt(error_text: str) -> int:
    match = ATTEMPT_RE.search(error_text or "")
    if not match:
        return 0
    with contextlib.suppress(ValueError):
        return int(match.group(1))
    return 0


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "да", "y"}
    return False


def keyword_set(text: str) -> set[str]:
    return {
        word.casefold()
        for word in re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", text or "")
        if word.casefold()
        not in {
            "and",
            "the",
            "для",
            "или",
            "что",
            "это",
            "как",
            "при",
            "над",
            "под",
            "все",
        }
    }


def normalize_comparable(value: Any) -> Any:
    if isinstance(value, str):
        return clean_string(value, "").casefold()
    if isinstance(value, list):
        return [normalize_comparable(item) for item in value]
    return value


def drive_folder_id_from_url(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if "/" not in text and "?" not in text:
        return text
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    if query.get("id"):
        return query["id"][0]
    parts = [part for part in parsed.path.split("/") if part]
    if "folders" in parts:
        index = parts.index("folders")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def safe_original_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._-]+", "_", Path(name).name).strip("._")
    return clean or "file.bin"


def unique_download_path(target_dir: Path, name: str) -> Path:
    candidate = target_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem or "file"
    suffix = candidate.suffix
    for index in range(2, 10_000):
        next_candidate = target_dir / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise PermanentVoiceProcessorError(f"Could not allocate unique download path for {name}")


def manifest_file_size(item: dict[str, Any]) -> int | None:
    raw_size = item.get("size")
    if raw_size in (None, ""):
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError) as exc:
        raise PermanentVoiceProcessorError(f"Google Drive manifest file size is invalid: {raw_size!r}") from exc
    if size < 0:
        raise PermanentVoiceProcessorError(f"Google Drive manifest file size is negative: {size}")
    return size


def verify_manifest_sha256(item: dict[str, Any], path: Path, name: str) -> None:
    expected = str(item.get("sha256") or item.get("hash") or "").strip().casefold()
    if not expected:
        return
    actual = sha256_file(path)
    if actual.casefold() != expected:
        raise PermanentVoiceProcessorError(
            f"Google Drive file {name} sha256 mismatch manifest_sha256={expected} actual_sha256={actual}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_media(original: DriveOriginal) -> str:
    mime_type = str(original.mime_type or "").split(";", 1)[0].strip().casefold()
    suffix = original.path.suffix.casefold()
    if mime_type.startswith(VIDEO_MIME_PREFIX):
        return "video"
    if mime_type.startswith(AUDIO_MIME_PREFIX):
        return "audio"
    if mime_type.startswith(IMAGE_MIME_PREFIX):
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return "unsupported"


def is_audio(original: DriveOriginal) -> bool:
    return classify_media(original) == "audio"


def is_image(original: DriveOriginal) -> bool:
    return classify_media(original) == "image"


def is_video(original: DriveOriginal) -> bool:
    return classify_media(original) == "video"


def image_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


async def extract_video_audio(video_path: Path, audio_path: Path) -> None:
    await run_ffmpeg(
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio_path),
    )


async def extract_video_frames(
    video_path: Path,
    target_dir: Path,
    *,
    max_frames: int,
    interval_seconds: int,
    max_edge: int,
) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    duration = await probe_duration_seconds(video_path)
    timestamps = representative_timestamps(duration, max_frames=max_frames, interval_seconds=interval_seconds)
    frames: list[Path] = []
    for index, timestamp in enumerate(timestamps, start=1):
        frame = target_dir / f"frame_{index:03d}.jpg"
        await run_ffmpeg(
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale='if(gt(iw,{max_edge}),{max_edge},iw)':-2",
            "-q:v",
            "5",
            str(frame),
        )
        if frame.exists():
            frames.append(frame)
    return frames


async def probe_duration_seconds(video_path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return 0.0
    with contextlib.suppress(ValueError):
        return max(0.0, float(stdout.decode("utf-8", errors="replace").strip()))
    return 0.0


def representative_timestamps(duration: float, *, max_frames: int, interval_seconds: int) -> list[float]:
    max_frames = max(0, max_frames)
    if max_frames == 0:
        return []
    if duration <= 0:
        return [0.0]
    points = {0.0, max(0.0, duration / 2), max(0.0, duration - 0.5)}
    current = float(max(1, interval_seconds))
    while current < duration and len(points) < max_frames:
        points.add(current)
        current += max(1, interval_seconds)
    return sorted(points)[:max_frames]


async def run_ffmpeg(*args: str) -> None:
    if not shutil.which("ffmpeg"):
        raise PermanentVoiceProcessorError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise PermanentVoiceProcessorError(f"ffmpeg failed with code {process.returncode}: {safe_error(detail)}")


async def retry_async(call, *, max_attempts: int, base_delay: float):
    attempt = 1
    while True:
        try:
            return await call()
        except Exception as exc:
            if attempt >= max_attempts or not is_transient_error(exc):
                raise
            await asyncio.sleep(max(0.0, base_delay) * attempt)
            attempt += 1


def is_transient_error(error: Exception) -> bool:
    if isinstance(error, VoiceProcessorError):
        return error.transient
    if isinstance(error, (AirtableError, DriveStorageError)):
        return any(str(code) in str(error) for code in TRANSIENT_STATUS_CODES)
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int) and status_code in TRANSIENT_STATUS_CODES:
        return True
    name = error.__class__.__name__.casefold()
    return any(part in name for part in ("timeout", "ratelimit", "apierror", "connection"))


def redact_secrets(text: str) -> str:
    return SECRET_VALUE_RE.sub("[redacted]", text)


def make_processor(settings: Settings | None = None) -> VoiceInboxProcessor:
    return VoiceInboxProcessor(settings or get_settings())


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Voice Inbox multimodal processor")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--batch-size", type=int, default=None, help="Override VOICE_PROCESSOR_BATCH_SIZE")
    parser.add_argument("--record-id", default="", help="Process exactly one Airtable record id")
    parser.add_argument(
        "--ignore-enabled-flag",
        action="store_true",
        help="Allow a one-off run even when VOICE_PROCESSOR_ENABLED=false",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.voice_processor_enabled and not args.ignore_enabled_flag:
        logger.info("VOICE_PROCESSOR_ENABLED=false; exiting without processing")
        return 0

    processor = make_processor(settings)
    if args.record_id:
        result = await processor.run_record(args.record_id)
        logger.info("Voice processor completed record %s with result=%s", args.record_id, result)
        return 0
    if args.once:
        stats = await processor.run_once(batch_size=args.batch_size)
        logger.info("Voice processor completed one batch: %s", stats)
        return 0
    await processor.run_loop()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
