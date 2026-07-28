from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.airtable import AirtableClient
from app.config import Settings, validate_openai_api_configuration
from app.main import build_dispatcher, extract_content
from app.quota_migration import migrate_insufficient_quota_records
from app.subscription_queue import SubscriptionQueue
from app.voice_processor import is_transient_error, retry_async


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "123:test",
        "ALLOWED_TELEGRAM_USER_IDS": "1",
        "OPENAI_API_KEY": "",
        "AIRTABLE_TOKEN": "pat-test",
        "VOICE_INBOX_BASE_ID": "appTest",
        "VOICE_INBOX_TABLE_ID": "tblInbox",
        "VOICE_FIELD_TITLE": "Название",
        "VOICE_FIELD_TYPE": "Тип",
        "VOICE_FIELD_PROJECT": "Проект",
        "VOICE_FIELD_PRIORITY": "Приоритет",
        "VOICE_FIELD_NEXT_ACTION": "Следующее действие",
        "VOICE_FIELD_SUMMARY": "Краткое содержание",
        "VOICE_FIELD_CLEAN_TEXT": "Очищенный текст",
        "VOICE_FIELD_RAW_TEXT": "Исходная фраза",
        "VOICE_FIELD_TAGS": "Теги",
        "VOICE_FIELD_PROCESSING_STATUS": "Статус обработки",
        "PROJECTS_BASE_ID": "appProjects",
        "PROJECTS_TABLE_ID": "tblProjects",
        "PROJECTS_FIELD_TITLE": "Name",
        "ITEMS_TABLE_ID": "tblItems",
        "ITEMS_FIELD_TITLE": "Name",
        "ITEMS_FIELD_PROJECT": "Project",
        "ITEMS_FIELD_TYPE": "Type",
        "ITEMS_FIELD_STATUS": "Status",
        "ITEMS_FIELD_PRIORITY": "Priority",
        "ITEMS_FIELD_TEXT": "Text",
        "ITEMS_FIELD_NEXT_ACTION": "Next",
        "ITEMS_FIELD_SOURCE": "Source",
        "ITEMS_FIELD_DATE": "Date",
        "DATA_DIR": str(tmp_path),
        "GOOGLE_DRIVE_ENABLED": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("route", ["chatgpt_subscription", "disabled"])
def test_non_openai_routes_do_not_create_openai_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    created = 0

    class ForbiddenOpenAIProcessor:
        def __init__(self, settings: Settings) -> None:
            nonlocal created
            created += 1
            raise AssertionError("OpenAI client must not be created")

    monkeypatch.setattr("app.main.OpenAIProcessor", ForbiddenOpenAIProcessor)
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE=route, OPENAI_API_KEY="")

    asyncio.run(build_dispatcher(settings, SimpleNamespace()))

    assert created == 0
    assert settings.openai_api_processor_enabled is False


def test_chatgpt_subscription_audio_does_not_transcribe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE="chatgpt_subscription")
    converted = 0

    async def forbidden_convert(source: Path, target: Path) -> None:
        nonlocal converted
        converted += 1
        raise AssertionError("transcription conversion must not run")

    class FakeBot:
        async def get_file(self, file_id: str) -> SimpleNamespace:
            return SimpleNamespace(file_path="voice.ogg")

        async def download_file(self, file_path: str, *, destination: Path) -> None:
            destination.write_bytes(b"ogg")

    message = SimpleNamespace(
        voice=SimpleNamespace(file_id="voice-file"),
        audio=None,
        document=None,
        video=None,
        photo=None,
        text=None,
        caption=None,
        message_id=7,
        chat=SimpleNamespace(id=10),
        from_user=SimpleNamespace(id=1),
    )
    monkeypatch.setattr("app.main.convert_audio_to_mp3", forbidden_convert)

    content = asyncio.run(extract_content(message, FakeBot(), settings, None))

    assert converted == 0
    assert content.raw_text == ""
    assert len(content.files) == 1
    assert content.files[0].content == b"ogg"


def test_chatgpt_subscription_cannot_call_structuring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    structured = 0

    class ForbiddenOpenAIProcessor:
        def __init__(self, settings: Settings) -> None:
            raise AssertionError("OpenAI structuring client must not be created")

        async def structure_text(self, raw_text: str, message_type: str) -> dict[str, Any]:
            nonlocal structured
            structured += 1
            raise AssertionError("OpenAI structuring must not run")

    monkeypatch.setattr("app.main.OpenAIProcessor", ForbiddenOpenAIProcessor)
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE="chatgpt_subscription")

    asyncio.run(build_dispatcher(settings, SimpleNamespace()))

    assert structured == 0


def test_openai_api_route_keeps_explicit_openai_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = 0

    class FakeOpenAIProcessor:
        def __init__(self, settings: Settings) -> None:
            nonlocal created
            created += 1

    monkeypatch.setattr("app.main.OpenAIProcessor", FakeOpenAIProcessor)
    settings = make_settings(
        tmp_path,
        VOICE_PROCESSING_ROUTE="openai_api",
        OPENAI_API_KEY="sk-proj-valid-test-key-1234567890",
    )

    asyncio.run(build_dispatcher(settings, SimpleNamespace()))

    assert created == 1
    assert settings.openai_api_processor_enabled is True
    assert settings.voice_processing_mode.intake_status == "New"


def test_legacy_enabled_flag_does_not_enable_openai(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, VOICE_PROCESSOR_ENABLED=True)

    assert settings.effective_voice_processing_route == "disabled"
    assert settings.openai_api_processor_enabled is False


def test_missing_route_is_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert settings.effective_voice_processing_route == "disabled"
    assert "missing" in settings.voice_processing_route_warning


def test_unknown_route_is_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE="some-provider")

    assert settings.effective_voice_processing_route == "disabled"
    assert "unknown" in settings.voice_processing_route_warning


def test_openai_api_requires_valid_key_at_startup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE="openai_api", OPENAI_API_KEY="invalid")

    with pytest.raises(RuntimeError, match="valid OPENAI_API_KEY"):
        validate_openai_api_configuration(settings)


class Quota429Error(RuntimeError):
    status_code = 429
    body = {"error": {"code": "insufficient_quota", "message": "private diagnostic"}}


def test_insufficient_quota_is_permanent_and_not_retried() -> None:
    calls = 0

    async def fail() -> None:
        nonlocal calls
        calls += 1
        raise Quota429Error("insufficient_quota")

    with pytest.raises(Quota429Error):
        asyncio.run(retry_async(fail, max_attempts=3, base_delay=0))

    assert calls == 1
    assert is_transient_error(Quota429Error("insufficient_quota")) is False


@dataclass
class FakeMigrationAirtable:
    records: list[dict[str, Any]]
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def list_insufficient_quota_review_records(self, *, max_records: int) -> list[dict[str, Any]]:
        return self.records[:max_records]

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((record_id, fields))
        for record in self.records:
            if record.get("id") == record_id:
                record.setdefault("fields", {}).update(fields)
        return {"id": record_id, "fields": fields}


def test_quota_migration_does_not_touch_regular_needs_review(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    quota = {
        "id": "recQuota",
        "fields": {
            "Статус обработки": "Needs Review",
            "Ошибка обработки": "voice_processor OpenAI insufficient_quota",
        },
    }
    regular = {
        "id": "recRegular",
        "fields": {
            "Статус обработки": "Needs Review",
            "Ошибка обработки": "Пользователь должен проверить тип",
        },
    }
    fake = FakeMigrationAirtable([quota, regular])

    dry_run = migrate_insufficient_quota_records(settings, fake, dry_run=True)
    applied = migrate_insufficient_quota_records(settings, fake, dry_run=False)
    repeated = migrate_insufficient_quota_records(settings, fake, dry_run=False)

    assert dry_run.matched == 1
    assert dry_run.migrated == 0
    assert applied.migrated == 1
    assert repeated.migrated == 0
    assert fake.updates == [
        (
            "recQuota",
            {"Processing Route": "ChatGPT Subscription", "Статус обработки": "Awaiting Subscription"},
        )
    ]


@dataclass
class FakeQueueAirtable:
    records: list[dict[str, Any]]
    claims: list[str] = field(default_factory=list)

    def list_subscription_queue_records(self, *, batch_size: int, created_after: datetime | None) -> list[dict[str, Any]]:
        return self.records[:batch_size]

    def claim_subscription_queue_record(self, record_id: str, *, claim: str, claimed_at: datetime) -> dict[str, Any]:
        self.claims.append(record_id)
        record = self.fetch_voice_record(record_id)
        record["fields"]["Subscription Queue Claim"] = claim
        record["fields"]["Subscription Queue Claimed At"] = claimed_at.isoformat()
        return record

    def fetch_voice_record(self, record_id: str) -> dict[str, Any]:
        return next(record for record in self.records if record["id"] == record_id)


class FakeQueueDrive:
    def manifest_exists(self, google_drive_url: str) -> bool:
        return not google_drive_url.endswith("missing")


def queue_record(record_id: str, *, route: str, status: str, claim: str = "", title: str = "Real note") -> dict[str, Any]:
    return {
        "id": record_id,
        "createdTime": "2026-07-27T10:00:00.000Z",
        "fields": {
            "Processing Route": route,
            "Статус обработки": status,
            "Google Drive": f"https://drive.google.com/drive/folders/{record_id}",
            "Subscription Queue Claim": claim,
            "External ID": f"external-{record_id}",
            "Источник": "Android",
            "Тип": "Voice",
            "Название": title,
            "Исходная фраза": "private text",
        },
    }


def test_subscription_queue_selects_only_unclaimed_awaiting_records(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, VOICE_PROCESSING_ROUTE="chatgpt_subscription")
    records = [
        queue_record("recEligible", route="ChatGPT Subscription", status="Awaiting Subscription"),
        queue_record("recWrongStatus", route="ChatGPT Subscription", status="Needs Review"),
        queue_record("recWrongRoute", route="OpenAI API", status="Awaiting Subscription"),
        queue_record(
            "recClaimed",
            route="ChatGPT Subscription",
            status="Awaiting Subscription",
            claim="subscription_queue lock_id=busy",
        ),
        queue_record(
            "recSmoke",
            route="ChatGPT Subscription",
            status="Awaiting Subscription",
            title="production smoke",
        ),
    ]
    airtable = FakeQueueAirtable(records)
    queue = SubscriptionQueue(settings, airtable=airtable, drive_reader=FakeQueueDrive())  # type: ignore[arg-type]

    dry_items = queue.next_batch(batch_size=10, dry_run=True)
    claimed_items = queue.next_batch(batch_size=10, dry_run=False)

    assert [item.record_id for item in dry_items] == ["recEligible"]
    assert [item.record_id for item in claimed_items] == ["recEligible"]
    assert airtable.claims == ["recEligible"]


class FormulaResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FormulaSession:
    def __init__(self) -> None:
        self.params: list[tuple[str, str]] = []

    def get(self, url: str, *, timeout: int = 30) -> FormulaResponse:
        fields = [
            {"name": name, "type": "singleLineText"}
            for name in (
                "Статус обработки",
                "Processing Route",
                "Google Drive",
                "Subscription Queue Claim",
                "Название",
                "Исходная фраза",
                "Очищенный текст",
                "External ID",
                "Notes",
            )
        ]
        return FormulaResponse({"tables": [{"id": "tblInbox", "fields": fields}]})

    def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> FormulaResponse:
        self.params = list(params or [])
        return FormulaResponse({"records": []})


def test_subscription_queue_airtable_filter_is_exact_and_created_after_supported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = AirtableClient(settings)
    session = FormulaSession()
    client.session = session  # type: ignore[assignment]

    client.list_subscription_queue_records(
        batch_size=7,
        created_after=datetime(2026, 7, 1, tzinfo=UTC),
    )

    formula = dict(session.params)["filterByFormula"]
    assert "{Processing Route} = 'ChatGPT Subscription'" in formula
    assert "{Статус обработки} = 'Awaiting Subscription'" in formula
    assert "NOT({Google Drive} = '')" in formula
    assert "{Subscription Queue Claim} = ''" in formula
    assert "NOT(OR(SEARCH('smoke'" in formula
    assert "IS_AFTER(CREATED_TIME(), DATETIME_PARSE('2026-07-01T00:00:00.000Z'))" in formula
