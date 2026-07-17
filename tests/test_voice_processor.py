from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.airtable import ProjectMatch
from app.config import Settings
from app.voice_processor import (
    DriveOriginal,
    MediaExtraction,
    PermanentVoiceProcessorError,
    TransientVoiceProcessorError,
    VoiceInboxProcessor,
)


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

    def list_voice_records_for_processing(self, *, batch_size: int, stale_processing_seconds: int) -> list[dict]:
        result = []
        for record in self.records.values():
            status = record["fields"].get("Статус обработки")
            if status == "New" or record["fields"].get("_stale_processing"):
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
                    "id": "fldStatus",
                    "name": "Статус обработки",
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

    def list_voice_correction_candidates(self, *, page_size: int = 50) -> list[dict]:
        return [
            record
            for record in self.records.values()
            if record["fields"].get("Обучить на исправлении") is True
            and record["fields"].get("Обучение учтено") is not True
        ][:page_size]

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
        "Ошибка обработки": "",
    }
    base.update(fields)
    return {"id": record_id, "fields": base}


def make_processor(tmp_path: Path, airtable: FakeAirtable, ai: FakeAI, drive: FakeDriveReader) -> VoiceInboxProcessor:
    settings = make_settings(tmp_path)
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
    assert fields["Проект"] == ["recHome"]
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


def test_stale_processing_record_is_recovered(tmp_path: Path) -> None:
    record = make_record("rec1", **{"Статус обработки": "Processing", "_stale_processing": True})
    airtable = FakeAirtable([record])
    ai = FakeAI([valid_ai_result()])
    processor = make_processor(tmp_path, airtable, ai, FakeDriveReader())

    stats = asyncio_run(processor.run_once())

    assert stats.processed == 1
    assert airtable.records["rec1"]["fields"]["Статус обработки"] == "Processed"


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
            "Проект": ["recWork"],
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

    assert airtable.records["rec1"]["fields"]["Проект"] == ["recWork"]
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
