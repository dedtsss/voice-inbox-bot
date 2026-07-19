from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.airtable import AirtableClient, ProjectMatch
from app.config import Settings
from app.voice_processor import (
    DriveOriginal,
    GoogleDriveInboxReader,
    MediaExtraction,
    MediaExtractor,
    PermanentVoiceProcessorError,
    TransientVoiceProcessorError,
    VoiceInboxProcessor,
    allowed_context_from_metadata,
    build_airtable_update_fields,
    classify_media,
    parse_airtable_created_time,
    validate_processor_output,
)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
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
        VOICE_FIELD_SUMMARY="Краткое содержание",
        VOICE_FIELD_CLEAN_TEXT="Очищенный текст",
        VOICE_FIELD_RAW_TEXT="Исходная фраза",
        VOICE_FIELD_TAGS="Теги",
        VOICE_FIELD_PROCESSING_STATUS="Статус обработки",
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
        GOOGLE_DRIVE_ROOT_FOLDER_ID="root",
        GOOGLE_DRIVE_CREDENTIALS_FILE=str(tmp_path / "creds.json"),
        VOICE_PROCESSOR_BATCH_SIZE=5,
        VOICE_PROCESSOR_MAX_RETRIES=2,
        VOICE_PROCESSOR_RETRY_BASE_SECONDS=0,
    )
    values.update(overrides)
    return Settings(**values)


def valid_ai_result(**overrides: Any) -> dict[str, Any]:
    result = {
        "title": "Проверить счет",
        "clean_text": "Проверить счет и оплатить.",
        "summary": "Нужно проверить счет.",
        "type": "Task",
        "project": "Home",
        "priority": "Normal",
        "due_date": "2026-07-20",
        "counterparty": "ООО Ромашка",
        "amount": 1200.0,
        "period": None,
        "next_action": "Проверить реквизиты",
        "tags": ["finance"],
        "confidence": 0.93,
        "needs_review_reasons": [],
        "routing_reason": "matched project",
    }
    result.update(overrides)
    return result


@dataclass
class FakeDriveReader:
    files: list[tuple[str, str, bytes]] = field(default_factory=list)
    text: str = "Проверить счет и оплатить."
    calls: int = 0

    def download_record_originals(self, google_drive_url: str, target_dir: Path) -> tuple[dict[str, Any], list[DriveOriginal]]:
        self.calls += 1
        originals: list[DriveOriginal] = []
        manifest_files: list[dict[str, Any]] = []
        for index, (name, mime_type, content) in enumerate(self.files, start=1):
            path = target_dir / name
            path.write_bytes(content)
            file_id = f"file-{index}"
            originals.append(DriveOriginal(name, mime_type, path, len(content), file_id))
            manifest_files.append(
                {"name": name, "mime_type": mime_type, "size": len(content), "drive_file_id": file_id}
            )
        return (
            {
                "item_id": "android-1",
                "source": "android",
                "type": "mixed" if len(self.files) > 1 else "text",
                "text": self.text,
                "files": manifest_files,
            },
            originals,
        )


class FakeExecute:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class FakeMediaRequest:
    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeDriveFilesResource:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents

    def list(self, **kwargs: Any) -> FakeExecute:
        query = str(kwargs.get("q") or "")
        if "manifest.json" in query:
            return FakeExecute({"files": [{"id": "manifest", "name": "manifest.json", "size": len(self.contents["manifest"])}]})
        return FakeExecute({"files": []})

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> FakeMediaRequest:
        return FakeMediaRequest(self.contents[fileId])


class FakeDriveService:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.files_resource = FakeDriveFilesResource(contents)

    def files(self) -> FakeDriveFilesResource:
        return self.files_resource


class FakeMediaIoBaseDownload:
    def __init__(self, output: Any, request: FakeMediaRequest) -> None:
        self.output = output
        self.content = request.content
        self.offset = 0

    def next_chunk(self) -> tuple[None, bool]:
        chunk = self.content[self.offset : self.offset + 2]
        self.output.write(chunk)
        self.offset += len(chunk)
        return None, self.offset >= len(self.content)


class FakeAirtableResponse:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200, text: str = "") -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self.payload


class PagingAirtableSession:
    def __init__(self) -> None:
        self.requests: list[list[tuple[str, str]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> FakeAirtableResponse:
        request_params = list(params or [])
        self.requests.append(request_params)
        if any(key == "offset" for key, _ in request_params):
            return FakeAirtableResponse({"records": [{"id": "rec3"}, {"id": "rec4"}]})
        return FakeAirtableResponse({"records": [{"id": "rec1"}, {"id": "rec2"}], "offset": "next"})


class MetadataPatchSession:
    def __init__(self) -> None:
        self.patch_payloads: list[dict[str, Any]] = []

    def get(self, url: str, timeout: int = 30) -> FakeAirtableResponse:
        return FakeAirtableResponse(
            {
                "tables": [
                    {
                        "id": "tblRMsY9zB5tnVfTR",
                        "fields": [
                            {
                                "id": "fldzeZ9TidyPb1NMa",
                                "name": "Статус обработки",
                                "type": "singleSelect",
                                "options": {"choices": [{"id": "selNew", "name": "New"}]},
                            }
                        ],
                    }
                ]
            }
        )

    def patch(self, url: str, *, json: dict[str, Any], timeout: int = 30) -> FakeAirtableResponse:
        self.patch_payloads.append(json)
        return FakeAirtableResponse({"id": "fldzeZ9TidyPb1NMa"})


class MetadataCreateSession:
    def __init__(self) -> None:
        self.post_payloads: list[dict[str, Any]] = []

    def get(self, url: str, timeout: int = 30) -> FakeAirtableResponse:
        return FakeAirtableResponse(
            {
                "tables": [
                    {
                        "id": "tblRMsY9zB5tnVfTR",
                        "name": "Inbox",
                        "fields": [
                            {
                                "id": "fldzeZ9TidyPb1NMa",
                                "name": "Статус обработки",
                                "type": "singleSelect",
                                "options": {"choices": [{"id": "selProcessing", "name": "Processing"}]},
                            }
                        ],
                    }
                ]
            }
        )

    def post(self, url: str, *, json: dict[str, Any], timeout: int = 30) -> FakeAirtableResponse:
        self.post_payloads.append(json)
        if url.endswith("/tables"):
            return FakeAirtableResponse({"id": "tblRules"})
        return FakeAirtableResponse({"id": f"fldCreated{len(self.post_payloads)}"})


class MetadataChoiceFallbackSession:
    def __init__(self) -> None:
        self.has_processing = False
        self.patch_payloads: list[dict[str, Any]] = []
        self.request_payloads: list[dict[str, Any]] = []
        self.deleted_records: list[str] = []

    def get(self, url: str, timeout: int = 30) -> FakeAirtableResponse:
        choices = [{"id": "selNew", "name": "New", "color": "blueLight2"}]
        if self.has_processing:
            choices.append({"id": "selProcessing", "name": "Processing", "color": "blueLight2"})
        return FakeAirtableResponse(
            {
                "tables": [
                    {
                        "id": "tblRMsY9zB5tnVfTR",
                        "name": "Inbox",
                        "fields": [
                            {"id": "fldTitle", "name": "Название", "type": "singleLineText"},
                            {
                                "id": "fldzeZ9TidyPb1NMa",
                                "name": "Статус обработки",
                                "type": "singleSelect",
                                "options": {"choices": choices},
                            },
                        ],
                    }
                ]
            }
        )

    def patch(self, url: str, *, json: dict[str, Any], timeout: int = 30) -> FakeAirtableResponse:
        self.patch_payloads.append(json)
        return FakeAirtableResponse({}, status_code=422, text="parameter validation failed")

    def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> FakeAirtableResponse:
        payload = dict(json or {})
        self.request_payloads.append(payload)
        if payload.get("fields", {}).get("Статус обработки") == "Processing" and payload.get("typecast") is True:
            self.has_processing = True
        return FakeAirtableResponse({"id": "recTypecastCanary", "fields": payload.get("fields") or {}})

    def delete(self, url: str, *, timeout: int = 30) -> FakeAirtableResponse:
        self.deleted_records.append(url.rsplit("/", 1)[-1])
        return FakeAirtableResponse({"deleted": True})


class LegacyCreateSession:
    def __init__(self, *, first_error_text: str = "") -> None:
        self.first_error_text = first_error_text
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> FakeAirtableResponse:
        payload = dict(json or {})
        self.requests.append(payload)
        if len(self.requests) == 1 and self.first_error_text:
            return FakeAirtableResponse({"error": {"message": self.first_error_text}}, status_code=422, text=self.first_error_text)
        return FakeAirtableResponse({"id": f"rec{len(self.requests)}", "fields": payload.get("fields") or {}})


class PatchRecordingSession:
    def __init__(self) -> None:
        self.patch_payloads: list[dict[str, Any]] = []

    def patch(
        self,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> FakeAirtableResponse:
        payload = dict(json or {})
        self.patch_payloads.append(payload)
        return FakeAirtableResponse({"id": "rec1", "fields": payload.get("fields") or {}})


@dataclass
class FakeAI:
    outputs: list[Any]
    structure_calls: list[dict[str, Any]] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    image_calls: list[list[str]] = field(default_factory=list)

    async def transcribe_audio(self, audio_path: Path) -> str:
        self.transcripts.append(audio_path.name)
        return f"Транскрипт {audio_path.name}"

    async def describe_images(self, image_paths: list[Path], prompt: str) -> str:
        self.image_calls.append([path.name for path in image_paths])
        return "На изображении виден счет на оплату."

    async def structure_record(self, *, context: str, allowed: Any, rules: list[dict]) -> dict[str, Any]:
        self.structure_calls.append({"context": context, "rules": rules})
        if not self.outputs:
            return valid_ai_result()
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class FakeMediaExtractor:
    def __init__(self, settings: Settings, ai: FakeAI) -> None:
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
        parts = [manifest.get("text") or record["fields"].get("Исходная фраза") or ""]
        blocks = [f"Текст записи:\n{parts[0]}"] if parts[0] else []
        trace = {"audio_files": 0, "image_files": 0, "video_files": 0, "video_frames": 0}
        for original in originals:
            if original.mime_type.startswith("audio/"):
                trace["audio_files"] += 1
                transcript = await self.ai.transcribe_audio(original.path)
                parts.append(transcript)
                blocks.append(f"Транскрипт аудио {original.name}:\n{transcript}")
            elif original.mime_type.startswith("image/"):
                trace["image_files"] += 1
                description = await self.ai.describe_images([original.path], "image")
                blocks.append(f"Анализ изображения {original.name}:\n{description}")
            elif original.mime_type.startswith("video/"):
                trace["video_files"] += 1
                transcript = await self.ai.transcribe_audio(original.path)
                frame = temp_dir / "frame_001.jpg"
                frame.write_bytes(b"jpeg")
                description = await self.ai.describe_images([frame], "video")
                trace["video_frames"] += 1
                parts.append(transcript)
                blocks.append(f"Транскрипт видео {original.name}:\n{transcript}")
                blocks.append(f"Анализ кадров видео {original.name}:\n{description}")
        return MediaExtraction(source_text="\n".join(part for part in parts if part), content_blocks=blocks, trace=trace)


class FakeAirtable:
    def __init__(self, records: list[dict[str, Any]], rules: list[dict[str, Any]] | None = None) -> None:
        self.records = {record["id"]: record for record in records}
        self.rules = list(rules or [])
        self.created_rules: list[dict[str, Any]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.rule_updates: list[tuple[str, dict[str, Any]]] = []
        self.projects = [ProjectMatch("recHome", "Home"), ProjectMatch("recWork", "Work")]

    def list_voice_records_for_processing(
        self,
        *,
        batch_size: int,
        stale_processing_seconds: int,
        source_filter: str = "",
        created_after: Any = None,
    ) -> list[dict]:
        result = []
        for record in self.records.values():
            status = record["fields"].get("Статус обработки")
            if status != "New":
                continue
            if source_filter and str(record["fields"].get("Источник") or "").casefold() != source_filter.casefold():
                continue
            if created_after is not None:
                created_time = parse_airtable_created_time(record.get("createdTime"))
                if created_time is None or created_time <= created_after:
                    continue
            result.append(record)
        return result[:batch_size]

    def fetch_voice_record(self, record_id: str) -> dict:
        return self.records[record_id]

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        self.records[record_id]["fields"].update(fields)
        self.updates.append((record_id, dict(fields)))
        return self.records[record_id]

    def find_table_metadata(self, base_id: str, *, table_id: str = "", table_name: str = "") -> dict:
        return {
            "id": "tblRMsY9zB5tnVfTR",
            "fields": [
                {"id": "fldType", "name": "Тип", "options": {"choices": [{"name": "Task"}, {"name": "Idea"}]}},
                {
                    "id": "fldPriority",
                    "name": "Приоритет",
                    "options": {"choices": [{"name": "Low"}, {"name": "Normal"}, {"name": "High"}]},
                },
                {
                    "id": "fldmOj3oOUEJGsQcx",
                    "name": "Проект",
                    "type": "singleSelect",
                    "options": {"choices": [{"name": "Home"}, {"name": "Work"}]},
                },
                {
                    "id": "fldStatus",
                    "name": "Статус обработки",
                    "type": "singleSelect",
                    "options": {"choices": [{"name": "New"}, {"name": "Processing"}, {"name": "Processed"}, {"name": "Needs Review"}]},
                },
                {"id": "fldTags", "name": "Теги", "options": {"choices": [{"name": "finance"}, {"name": "home"}]}},
            ],
        }

    def list_projects(self) -> list[ProjectMatch]:
        return self.projects

    def list_processing_rules(self, *, active_only: bool = True, page_size: int = 100) -> list[dict]:
        if not active_only:
            return self.rules[:page_size]
        return [rule for rule in self.rules if rule.get("fields", {}).get("Активно") is True][:page_size]

    def update_processing_rule_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        self.rule_updates.append((record_id, dict(fields)))
        return {"id": record_id, "fields": fields}

    def list_voice_correction_candidates(self, *, page_size: int = 50, max_records: int | None = None) -> list[dict]:
        limit = max_records if max_records is not None else page_size
        return [
            record
            for record in self.records.values()
            if record["fields"].get("Обучить на исправлении") is True
            and record["fields"].get("Обучение учтено") is not True
        ][:limit]

    def create_processing_rule(self, fields: dict[str, Any]) -> dict:
        self.created_rules.append(dict(fields))
        return {"id": f"rule{len(self.created_rules)}", "fields": fields}


class ClaimLosingAirtable(FakeAirtable):
    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict:
        result = super().update_voice_record_fields(record_id, fields)
        if fields.get("Статус обработки") == "Processing":
            self.records[record_id]["fields"]["Ошибка обработки"] = "voice_processor lock_id=other"
        return result


def make_record(record_id: str = "rec1", **fields: Any) -> dict[str, Any]:
    base = {
        "Название": "Raw",
        "Тип": "Task",
        "Статус обработки": "New",
        "Исходная фраза": "Проверить счет и оплатить.",
        "Google Drive": "https://drive.google.com/drive/folders/folder1",
        "Источник": "Android",
        "Ошибка обработки": "",
    }
    base.update(fields)
    return {"id": record_id, "createdTime": "2026-07-19T10:00:00.000Z", "fields": base}


def make_processor(
    tmp_path: Path,
    airtable: FakeAirtable,
    ai: FakeAI,
    drive: FakeDriveReader,
    *,
    settings: Settings | None = None,
) -> VoiceInboxProcessor:
    settings = settings or make_settings(tmp_path)
    return VoiceInboxProcessor(
        settings,
        airtable=airtable,  # type: ignore[arg-type]
        drive_reader=drive,  # type: ignore[arg-type]
        ai=ai,  # type: ignore[arg-type]
        media_extractor=FakeMediaExtractor(settings, ai),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("files", "expected_trace"),
    [
        ([], {"audio_files": 0, "image_files": 0, "video_files": 0}),
        ([("note.mp3", "audio/mpeg", b"mp3")], {"audio_files": 1, "image_files": 0, "video_files": 0}),
        ([("photo.png", "image/png", b"png")], {"audio_files": 0, "image_files": 1, "video_files": 0}),
        ([("clip.mp4", "video/mp4", b"mp4")], {"audio_files": 0, "image_files": 0, "video_files": 1}),
        (
            [("note.mp3", "audio/mpeg", b"mp3"), ("photo.png", "image/png", b"png")],
            {"audio_files": 1, "image_files": 1, "video_files": 0},
        ),
    ],
)
def test_modalities_are_processed(tmp_path: Path, files: list[tuple[str, str, bytes]], expected_trace: dict[str, int]) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result()])
    drive = FakeDriveReader(files=files)
    processor = make_processor(tmp_path, airtable, ai, drive)

    stats = asyncio_run(processor.run_once())

    fields = airtable.records["rec1"]["fields"]
    assert stats.processed == 1
    assert fields["Статус обработки"] == "Processed"
    assert fields["Проект"] == "Home"
    assert fields["Исходная фраза"].startswith("Проверить счет")
    snapshot = json.loads(fields["AI результат JSON"])
    for key, value in expected_trace.items():
        assert snapshot["media_trace"][key] == value


def test_mp3_transcription_is_included(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(files=[("note.mp3", "audio/mpeg", b"mp3")]))

    asyncio_run(processor.run_once())

    assert ai.transcripts == ["note.mp3"]
    assert "Транскрипт note.mp3" in ai.structure_calls[0]["context"]


def test_photo_analysis_is_included(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(files=[("photo.png", "image/png", b"png")]))

    asyncio_run(processor.run_once())

    assert ai.image_calls == [["photo.png"]]
    assert "Анализ изображения photo.png" in ai.structure_calls[0]["context"]


def test_video_audio_and_frames_are_included(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(files=[("clip.mp4", "video/mp4", b"mp4")]))

    asyncio_run(processor.run_once())

    assert ai.transcripts == ["clip.mp4"]
    assert ai.image_calls == [["frame_001.jpg"]]
    snapshot = json.loads(airtable.records["rec1"]["fields"]["AI результат JSON"])
    assert snapshot["media_trace"]["video_frames"] == 1


def test_project_field_uses_voice_inbox_single_select_metadata(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, VOICE_FIELD_PROJECT="fldmOj3oOUEJGsQcx")
    table = {
        "fields": [
            {"id": "fldType", "name": "Тип", "options": {"choices": [{"name": "Task"}]}},
            {"id": "fldPriority", "name": "Приоритет", "options": {"choices": [{"name": "Normal"}]}},
            {
                "id": "fldzeZ9TidyPb1NMa",
                "name": "Статус обработки",
                "options": {"choices": [{"name": "Processed"}, {"name": "Needs Review"}]},
            },
            {"id": "fldTags", "name": "Теги", "options": {"choices": [{"name": "finance"}]}},
            {
                "id": "fldmOj3oOUEJGsQcx",
                "name": "Проект",
                "type": "singleSelect",
                "options": {"choices": [{"name": "Home"}, {"name": "Work"}]},
            },
        ]
    }
    allowed = allowed_context_from_metadata(table, settings, [ProjectMatch("recHome", "Home")])
    media = MediaExtraction(source_text="Проверить счет", content_blocks=[], trace={})

    validated = validate_processor_output(
        valid_ai_result(project="Home"),
        allowed=allowed,
        settings=settings,
        media=media,
        manifest={"item_id": "android-1"},
        record_id="rec1",
        attempt=1,
        lock_id="lock1",
        used_rule_ids=[],
    )
    fields = build_airtable_update_fields(settings, validated)

    assert [project.title for project in allowed.projects] == ["Home", "Work"]
    assert fields["fldmOj3oOUEJGsQcx"] == "Home"
    assert fields["fldmOj3oOUEJGsQcx"] != ["recHome"]


@pytest.mark.parametrize(
    ("name", "mime_type", "expected_kind", "expected_trace", "expected_transcripts", "expected_images"),
    [
        ("upload.bin", "video/mp4", "video", {"audio_files": 0, "video_files": 1, "video_frames": 1}, ["upload_audio.mp3"], [["frame_001.jpg"]]),
        ("clip.webm", "video/webm", "video", {"audio_files": 0, "video_files": 1, "video_frames": 1}, ["clip_audio.mp3"], [["frame_001.jpg"]]),
        ("voice.mp4", "audio/mp4", "audio", {"audio_files": 1, "video_files": 0, "video_frames": 0}, ["voice.mp4"], []),
        ("clip.mp4", "video/mp4", "video", {"audio_files": 0, "video_files": 1, "video_frames": 1}, ["clip_audio.mp3"], [["frame_001.jpg"]]),
    ],
)
def test_media_extractor_classifies_mime_before_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mime_type: str,
    expected_kind: str,
    expected_trace: dict[str, int],
    expected_transcripts: list[str],
    expected_images: list[list[str]],
) -> None:
    async def fake_extract_video_audio(video_path: Path, audio_path: Path) -> None:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"mp3")

    async def fake_extract_video_frames(
        video_path: Path,
        target_dir: Path,
        *,
        max_frames: int,
        interval_seconds: int,
        max_edge: int,
    ) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        frame = target_dir / "frame_001.jpg"
        frame.write_bytes(b"jpeg")
        return [frame]

    monkeypatch.setattr("app.voice_processor.extract_video_audio", fake_extract_video_audio)
    monkeypatch.setattr("app.voice_processor.extract_video_frames", fake_extract_video_frames)

    settings = make_settings(tmp_path)
    ai = FakeAI([])
    extractor = MediaExtractor(settings, ai)  # type: ignore[arg-type]
    media_path = tmp_path / name
    media_path.write_bytes(b"media")
    original = DriveOriginal(name=name, mime_type=mime_type, path=media_path, size=media_path.stat().st_size, drive_file_id="file-1")

    result = asyncio_run(
        extractor.extract(
            record=make_record(),
            manifest={"text": "caption"},
            originals=[original],
            temp_dir=tmp_path / "work",
        )
    )

    assert classify_media(original) == expected_kind
    assert ai.transcripts == expected_transcripts
    assert ai.image_calls == expected_images
    for key, value in expected_trace.items():
        assert result.trace[key] == value


def test_airtable_processing_candidates_use_max_records_and_stop_pagination(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = PagingAirtableSession()
    client.session = session  # type: ignore[assignment]

    records = client.list_voice_records_for_processing(batch_size=2, stale_processing_seconds=900)

    assert [record["id"] for record in records] == ["rec1", "rec2"]
    assert len(session.requests) == 1
    assert ("pageSize", "2") in session.requests[0]
    assert ("maxRecords", "2") in session.requests[0]


def test_airtable_processing_candidates_include_source_and_cutoff_filters(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = PagingAirtableSession()
    client.session = session  # type: ignore[assignment]

    client.list_voice_records_for_processing(
        batch_size=1,
        stale_processing_seconds=900,
        source_filter="Android",
        created_after=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
    )

    formula = [value for key, value in session.requests[0] if key == "filterByFormula"][0]
    assert "{Статус обработки} = 'New'" in formula
    assert "{Источник} = 'Android'" in formula
    assert "IS_AFTER(CREATED_TIME(), DATETIME_PARSE('2026-07-19T10:00:00.000Z'))" in formula
    assert "Processing" not in formula


def test_airtable_correction_candidates_use_max_records_and_stop_pagination(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = PagingAirtableSession()
    client.session = session  # type: ignore[assignment]

    records = client.list_voice_correction_candidates(max_records=2)

    assert [record["id"] for record in records] == ["rec1", "rec2"]
    assert len(session.requests) == 1
    assert ("maxRecords", "2") in session.requests[0]


def test_ensure_voice_processor_schema_adds_processing_choice(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = MetadataPatchSession()
    client.session = session  # type: ignore[assignment]

    added = client.ensure_select_field_choices(
        settings.voice_inbox_base_id,
        settings.voice_inbox_table_id,
        settings.voice_field_processing_status,
        ["Processing"],
    )

    assert added == ["Processing"]
    assert session.patch_payloads[0]["options"]["choices"][-1] == {"name": "Processing", "color": "blueLight2"}


def test_ensure_select_field_choices_falls_back_to_typecast_record(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = MetadataChoiceFallbackSession()
    client.session = session  # type: ignore[assignment]

    added = client.ensure_select_field_choices(
        settings.voice_inbox_base_id,
        settings.voice_inbox_table_id,
        settings.voice_field_processing_status,
        ["Processing"],
        allow_typecast_record_fallback=True,
    )

    assert added == ["Processing"]
    assert session.request_payloads[0]["typecast"] is True
    assert session.request_payloads[0]["fields"]["Статус обработки"] == "Processing"
    assert session.deleted_records == ["recTypecastCanary"]


def test_ensure_voice_processor_schema_creates_checkbox_fields_with_options(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = MetadataCreateSession()
    client.session = session  # type: ignore[assignment]

    client.ensure_voice_processor_schema()

    checkbox_payloads = [
        payload
        for payload in session.post_payloads
        if payload.get("type") == "checkbox"
        or any(field.get("type") == "checkbox" for field in payload.get("fields", []))
    ]
    assert {
        payload["name"]: payload["options"]
        for payload in checkbox_payloads
        if payload.get("type") == "checkbox"
    } == {
        "Обучить на исправлении": {"icon": "check", "color": "greenBright"},
        "Обучение учтено": {"icon": "check", "color": "greenBright"},
    }
    rules_table_payload = next(payload for payload in session.post_payloads if payload.get("name") == "Правила обработки")
    active_field = next(field for field in rules_table_payload["fields"] if field["name"] == "Активно")
    assert active_field["options"] == {"icon": "check", "color": "greenBright"}


def test_legacy_telegram_create_typecasts_priority_normal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = LegacyCreateSession()
    client.session = session  # type: ignore[assignment]

    client.create_voice_inbox_record(
        structured=valid_ai_result(priority="Normal", tags=[]),
        raw_text="legacy telegram text",
        message_type="Text",
        project=None,
        external_id="telegram:1:1",
        source="Telegram",
    )

    payload = session.requests[0]
    assert payload["typecast"] is True
    assert payload["fields"]["Приоритет"] == "Normal"


def test_legacy_telegram_create_typecasts_new_tag(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = LegacyCreateSession()
    client.session = session  # type: ignore[assignment]

    client.create_voice_inbox_record(
        structured=valid_ai_result(tags=["brand-new-tag"]),
        raw_text="legacy telegram text",
        message_type="Text",
        project=None,
        external_id="telegram:1:2",
        source="Telegram",
    )

    payload = session.requests[0]
    assert payload["typecast"] is True
    assert payload["fields"]["Теги"] == ["brand-new-tag"]


def test_voice_processor_writeback_does_not_typecast(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = PatchRecordingSession()
    client.session = session  # type: ignore[assignment]

    client.update_voice_record_fields("rec1", {"Приоритет": "High", "Теги": ["finance"]})

    assert session.patch_payloads == [{"fields": {"Приоритет": "High", "Теги": ["finance"]}}]


def test_legacy_invalid_select_fallback_does_not_repeat_same_payload(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = LegacyCreateSession(first_error_text="INVALID_MULTIPLE_CHOICE_OPTIONS: invalid select option")
    client.session = session  # type: ignore[assignment]

    client.create_voice_inbox_record(
        structured=valid_ai_result(priority="Normal", tags=["new-tag"]),
        raw_text="legacy telegram text",
        message_type="Text",
        project=None,
        external_id="telegram:1:3",
        google_drive_url="https://drive.google.com/drive/folders/folder1",
        source="Telegram",
    )

    assert len(session.requests) == 2
    first_fields = session.requests[0]["fields"]
    fallback_fields = session.requests[1]["fields"]
    assert session.requests[0]["typecast"] is True
    assert session.requests[1]["typecast"] is True
    assert "Приоритет" in first_fields
    assert "Теги" in first_fields
    assert "Приоритет" not in fallback_fields
    assert "Теги" not in fallback_fields
    assert fallback_fields["External ID"] == "telegram:1:3"
    assert fallback_fields != first_fields


def test_run_once_honors_batch_size_when_more_records_exist(tmp_path: Path) -> None:
    records = [make_record(f"rec{index}") for index in range(1, 5)]
    airtable = FakeAirtable(records)
    ai = FakeAI([valid_ai_result() for _ in records])
    settings = make_settings(tmp_path, VOICE_PROCESSOR_BATCH_SIZE=2)
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(), settings=settings)

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 2
    assert [record_id for record_id, fields in airtable.updates if fields.get("Статус обработки") == "Processed"] == [
        "rec1",
        "rec2",
    ]
    assert airtable.records["rec3"]["fields"]["Статус обработки"] == "New"


def test_run_once_selects_new_android_record_after_cutoff(tmp_path: Path) -> None:
    record = make_record("rec1")
    record["createdTime"] = "2026-07-19T10:00:01.000Z"
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    settings = make_settings(
        tmp_path,
        VOICE_PROCESSOR_BATCH_SIZE=1,
        VOICE_PROCESSOR_CREATED_AFTER="2026-07-19T10:00:00Z",
    )
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(), settings=settings)

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 1
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Processed"


def test_run_once_skips_android_record_before_cutoff(tmp_path: Path) -> None:
    record = make_record("rec1")
    record["createdTime"] = "2026-07-19T09:59:59.000Z"
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    settings = make_settings(tmp_path, VOICE_PROCESSOR_CREATED_AFTER="2026-07-19T10:00:00Z")
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(), settings=settings)

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 0
    assert airtable.updates == []
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "New"


def test_run_once_skips_telegram_record(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Источник": "Telegram"})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 0
    assert airtable.updates == []
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "New"


def test_run_once_skips_record_without_android_source(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Источник": ""})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 0
    assert airtable.updates == []
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "New"


def test_explicit_record_ignores_automatic_source_and_cutoff_filters(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Источник": "Telegram"})
    record["createdTime"] = "2026-07-19T09:59:59.000Z"
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    settings = make_settings(tmp_path, VOICE_PROCESSOR_CREATED_AFTER="2026-07-19T10:00:00Z")
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(), settings=settings)

    result = asyncio_run(processor.run_record("rec1"))

    assert result == "processed"
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Processed"


def test_invalid_processor_cutoff_stops_settings_validation(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="VOICE_PROCESSOR_CREATED_AFTER"):
        make_settings(tmp_path, VOICE_PROCESSOR_CREATED_AFTER="not-a-utc-timestamp")


def test_run_once_applies_batch_limit_after_source_and_cutoff_filtering(tmp_path: Path) -> None:
    before = make_record("recBefore")
    before["createdTime"] = "2026-07-19T09:59:59.000Z"
    telegram = make_record("recTelegram", **{"Источник": "Telegram"})
    telegram["createdTime"] = "2026-07-19T10:00:01.000Z"
    first = make_record("recFirst")
    first["createdTime"] = "2026-07-19T10:00:02.000Z"
    second = make_record("recSecond")
    second["createdTime"] = "2026-07-19T10:00:03.000Z"
    airtable = FakeAirtable([before, telegram, first, second])
    ai = FakeAI([valid_ai_result(), valid_ai_result()])
    settings = make_settings(
        tmp_path,
        VOICE_PROCESSOR_BATCH_SIZE=1,
        VOICE_PROCESSOR_CREATED_AFTER="2026-07-19T10:00:00Z",
    )
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader(), settings=settings)

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 1
    assert airtable.records["recFirst"]["fields"]["Статус обработки"] == "Processed"
    assert airtable.records["recSecond"]["fields"]["Статус обработки"] == "New"
    assert airtable.records["recBefore"]["fields"]["Статус обработки"] == "New"
    assert airtable.records["recTelegram"]["fields"]["Статус обработки"] == "New"


def test_correction_learning_honors_batch_size(tmp_path: Path) -> None:
    snapshot = json.dumps({"validated": valid_ai_result(project="Home", type="Task")}, ensure_ascii=False)
    records = [
        make_record(
            f"rec{index}",
            **{
                "Статус обработки": "Processed",
                "AI результат JSON": snapshot,
                "Обучить на исправлении": True,
                "Обучение учтено": False,
                "Проект": "Work",
            },
        )
        for index in range(1, 5)
    ]
    airtable = FakeAirtable(records)
    settings = make_settings(tmp_path, VOICE_PROCESSOR_BATCH_SIZE=2)
    processor = make_processor(tmp_path, airtable, FakeAI([]), FakeDriveReader(), settings=settings)

    learned = asyncio_run(processor.apply_pending_corrections())

    assert learned == 2
    assert len(airtable.created_rules) == 2
    assert airtable.records["rec3"]["fields"]["Обучение учтено"] is False


def test_drive_reader_rejects_file_over_manifest_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", FakeMediaIoBaseDownload)
    manifest = {
        "item_id": "android-1",
        "files": [
            {"name": "big.mp3", "mime_type": "audio/mpeg", "size": 5, "drive_file_id": "file-1"},
        ],
    }
    service = FakeDriveService({"manifest": json.dumps(manifest).encode("utf-8"), "file-1": b"12345"})
    settings = make_settings(tmp_path, VOICE_PROCESSOR_MAX_FILE_BYTES=4, VOICE_PROCESSOR_MAX_RECORD_BYTES=20)
    reader = GoogleDriveInboxReader(settings, service)
    target_dir = tmp_path / "downloads"

    with pytest.raises(PermanentVoiceProcessorError, match="MAX_FILE_BYTES"):
        reader.download_record_originals("https://drive.google.com/drive/folders/folder1", target_dir)

    assert not (target_dir / "big.mp3").exists()
    assert not (target_dir / "manifest.download.json").exists()


def test_drive_reader_rejects_sha256_mismatch_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", FakeMediaIoBaseDownload)
    expected_sha = hashlib.sha256(b"good").hexdigest()
    manifest = {
        "item_id": "android-1",
        "files": [
            {
                "name": "note.mp3",
                "mime_type": "audio/mpeg",
                "size": 3,
                "sha256": expected_sha,
                "drive_file_id": "file-1",
            },
        ],
    }
    service = FakeDriveService({"manifest": json.dumps(manifest).encode("utf-8"), "file-1": b"bad"})
    settings = make_settings(tmp_path, VOICE_PROCESSOR_MAX_FILE_BYTES=20, VOICE_PROCESSOR_MAX_RECORD_BYTES=20)
    reader = GoogleDriveInboxReader(settings, service)
    target_dir = tmp_path / "downloads"

    with pytest.raises(PermanentVoiceProcessorError, match="sha256 mismatch"):
        reader.download_record_originals("https://drive.google.com/drive/folders/folder1", target_dir)

    assert not (target_dir / "note.mp3").exists()
    assert not (target_dir / "manifest.download.json").exists()


def test_unknown_project_is_rejected(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result(project="Missing")])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    fields = airtable.records["rec1"]["fields"]
    assert stats.needs_review == 1
    assert fields["Статус обработки"] == "Needs Review"
    assert fields["Проект"] is None
    assert "несуществующий проект" in fields["AI результат JSON"]


def test_invalid_select_options_are_rejected(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result(type="Meeting", priority="Urgent")])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    asyncio_run(processor.run_once())

    fields = airtable.records["rec1"]["fields"]
    assert fields["Статус обработки"] == "Needs Review"
    assert fields["Тип"] is None
    assert fields["Приоритет"] is None


def test_low_confidence_goes_to_needs_review(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([valid_ai_result(confidence=0.5)])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.needs_review == 1
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Needs Review"


def test_processing_error_is_bounded_retry(tmp_path: Path) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI(
        [
            TransientVoiceProcessorError("temporary OpenAI 429"),
            TransientVoiceProcessorError("temporary OpenAI 429"),
        ]
    )
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    fields = airtable.records["rec1"]["fields"]
    assert stats.retried == 1
    assert fields["Статус обработки"] == "New"
    assert "attempt=1" in fields["Ошибка обработки"]


def test_run_once_skips_processing_record(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Статус обработки": "Processing", "_stale_processing": True})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 0
    assert airtable.updates == []
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Processing"


def test_explicit_processing_record_is_still_processed(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Статус обработки": "Processing", "_stale_processing": True})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    result = asyncio_run(processor.run_record("rec1"))

    assert result == "processed"
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Processed"


def test_explicit_record_rerun_skips_already_handled_record(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Статус обработки": "Needs Review"})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    drive = FakeDriveReader()
    processor = make_processor(tmp_path, airtable, ai, drive)

    result = asyncio_run(processor.run_record("rec1"))

    assert result == "skipped"
    assert airtable.updates == []
    assert ai.structure_calls == []
    assert drive.calls == 0


def test_explicit_record_skip_does_not_initialize_media_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_drive_reader(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Drive reader should not be initialized for skipped records")

    def fail_ai(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("OpenAI client should not be initialized for skipped records")

    monkeypatch.setattr("app.voice_processor.GoogleDriveInboxReader", fail_drive_reader)
    monkeypatch.setattr("app.voice_processor.VoiceProcessorAI", fail_ai)

    record = make_record("rec1", **{"Статус обработки": "Needs Review"})
    airtable = FakeAirtable([record])
    processor = VoiceInboxProcessor(make_settings(tmp_path), airtable=airtable)  # type: ignore[arg-type]

    result = asyncio_run(processor.run_record("rec1"))

    assert result == "skipped"
    assert airtable.updates == []


def test_duplicate_worker_loses_claim_and_skips(tmp_path: Path) -> None:
    airtable = ClaimLosingAirtable([make_record()])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.skipped == 1
    assert ai.structure_calls == []


def test_explicit_correction_creates_learning_rule(tmp_path: Path) -> None:
    snapshot = json.dumps({"validated": valid_ai_result(project="Home", type="Task")}, ensure_ascii=False)
    record = make_record(
        "rec1",
        **{
            "Статус обработки": "Processed",
            "AI результат JSON": snapshot,
            "Обучить на исправлении": True,
            "Обучение учтено": False,
            "Проект": "Work",
            "Комментарий к исправлению": "Всегда относить это к Work",
        },
    )
    airtable = FakeAirtable([record])
    processor = make_processor(tmp_path, airtable, FakeAI([]), FakeDriveReader())

    learned = asyncio_run(processor.apply_pending_corrections())

    assert learned == 1
    assert airtable.created_rules[0]["Область"] == "Маршрутизация"
    assert '"project": "Work"' in airtable.created_rules[0]["Правильное решение"]
    assert airtable.records["rec1"]["fields"]["Обучение учтено"] is True
    assert airtable.records["rec1"]["fields"]["Обучить на исправлении"] is False


def test_correction_learning_is_idempotent_after_applied(tmp_path: Path) -> None:
    snapshot = json.dumps({"validated": valid_ai_result(project="Home", type="Task")}, ensure_ascii=False)
    record = make_record(
        "rec1",
        **{
            "Статус обработки": "Processed",
            "AI результат JSON": snapshot,
            "Обучить на исправлении": True,
            "Обучение учтено": False,
            "Проект": "Work",
        },
    )
    airtable = FakeAirtable([record])
    processor = make_processor(tmp_path, airtable, FakeAI([]), FakeDriveReader())

    first = asyncio_run(processor.apply_correction_learning(record))
    second = asyncio_run(processor.apply_correction_learning(airtable.fetch_voice_record("rec1")))

    assert first is True
    assert second is False
    assert len(airtable.created_rules) == 1


def test_unmarked_user_edit_does_not_create_rule(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Статус обработки": "Processed", "Обучить на исправлении": False})
    airtable = FakeAirtable([record])
    processor = make_processor(tmp_path, airtable, FakeAI([]), FakeDriveReader())

    learned = asyncio_run(processor.apply_pending_corrections())

    assert learned == 0
    assert airtable.created_rules == []


def test_active_rule_affects_later_classification(tmp_path: Path) -> None:
    rule = {
        "id": "rule1",
        "fields": {
            "Активно": True,
            "Условие": "счет",
            "Правильное решение": json.dumps({"project": "Work"}, ensure_ascii=False),
        },
    }
    airtable = FakeAirtable([make_record()], rules=[rule])
    ai = FakeAI([valid_ai_result(project=None)])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    asyncio_run(processor.run_once())

    assert airtable.records["rec1"]["fields"]["Проект"] == "Work"
    assert ai.structure_calls[0]["rules"][0]["id"] == "rule1"
    assert airtable.rule_updates[0][0] == "rule1"


def test_inactive_rule_is_ignored(tmp_path: Path) -> None:
    rule = {
        "id": "rule1",
        "fields": {
            "Активно": False,
            "Условие": "счет",
            "Правильное решение": json.dumps({"project": "Work"}, ensure_ascii=False),
        },
    }
    airtable = FakeAirtable([make_record()], rules=[rule])
    ai = FakeAI([valid_ai_result(project=None)])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    asyncio_run(processor.run_once())

    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Needs Review"
    assert airtable.records["rec1"]["fields"]["Проект"] is None
    assert ai.structure_calls[0]["rules"] == []


def test_no_secrets_appear_in_error_or_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    airtable = FakeAirtable([make_record()])
    ai = FakeAI([PermanentVoiceProcessorError("bad key sk-secret123 and token patSecretValue")])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    with caplog.at_level("WARNING"):
        stats = asyncio_run(processor.run_once())

    error_text = airtable.records["rec1"]["fields"]["Ошибка обработки"]
    log_text = caplog.text
    assert stats.failed == 1
    assert "sk-secret123" not in error_text
    assert "patSecretValue" not in error_text
    assert "sk-secret123" not in log_text
    assert "patSecretValue" not in log_text


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
