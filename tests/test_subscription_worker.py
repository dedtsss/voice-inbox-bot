from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_processing_routes import make_settings

from app.airtable import AirtableError, ProjectMatch
from app.local_media import MediaTemporaryError, PreparedMedia
from app.subscription_queue import (
    SubscriptionQueueBundle,
    SubscriptionQueueItem,
    SubscriptionQueueStateError,
)
from app.subscription_worker import (
    CLAIM_PREFIX,
    CodexAuthUnavailable,
    CodexProcessTimeout,
    CodexSubscriptionRunner,
    OutputValidationError,
    ProcessLock,
    SubscriptionWorker,
    WorkerTemporaryError,
    validate_output,
)
from app.voice_processor import AllowedContext, ProcessorOutput


def allowed_context() -> AllowedContext:
    return AllowedContext(
        type_options={"Задача", "Заметка"},
        priority_options={"Обычный", "Высокий"},
        status_options={"Awaiting Subscription", "Processed", "Needs Review"},
        tag_options={"покупки", "работа"},
        projects=[ProjectMatch("project-private-id", "Дом")],
    )


def queue_item() -> SubscriptionQueueItem:
    return SubscriptionQueueItem(
        record_id="record-private-id",
        external_id="external-private-id",
        source="Android",
        entry_type="Text",
        created_at="2026-07-28T10:00:00Z",
        raw_text="Купить 2 фильтра только если цена ниже 500 рублей",
        google_drive_url="https://drive.invalid/private-folder",
    )


def good_output(**overrides: Any) -> ProcessorOutput:
    values: dict[str, Any] = {
        "title": "Купить 2 фильтра",
        "clean_text": "Купить 2 фильтра только если цена ниже 500 рублей.",
        "summary": "Покупка 2 фильтров с условием цены 500 рублей.",
        "type": "Задача",
        "project": "Дом",
        "priority": "Обычный",
        "due_date": None,
        "counterparty": None,
        "amount": 500.0,
        "period": None,
        "next_action": "Проверить цену и купить 2 фильтра только при выполнении условия.",
        "tags": ["покупки"],
        "confidence": 0.91,
        "needs_review_reasons": [],
        "routing_reason": "Существующий проект Дом.",
    }
    values.update(overrides)
    return ProcessorOutput.model_validate(values)


class FakeDrive:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def check_access(self) -> None:
        if self.fail:
            raise OSError("refresh_token=private-drive-token")

    def manifest_exists(self, _: str) -> bool:
        if self.fail:
            raise OSError("refresh_token=private-drive-token")
        return True


class FakeAirtable:
    def __init__(self) -> None:
        self.claimed_records: list[dict[str, Any]] = []
        self.fail_rules = False

    def list_processing_rules(self, *, active_only: bool, page_size: int) -> list[dict[str, Any]]:
        assert active_only is True
        assert page_size > 0
        if self.fail_rules:
            raise AirtableError("token=private-airtable-token")
        return [{"id": "private-rule-id", "fields": {"Условие": "фильтр", "Проект": "Дом"}}]

    def list_claimed_subscription_queue_records(self, *, max_records: int) -> list[dict[str, Any]]:
        return list(self.claimed_records[:max_records])

    def fetch_voice_record(self, _: str) -> dict[str, Any]:
        raise AirtableError("not configured")


class FakeQueue:
    def __init__(self, items: list[SubscriptionQueueItem] | None = None, *, drive: FakeDrive | None = None) -> None:
        self.items = list(items or [])
        self.airtable = FakeAirtable()
        self.drive_reader = drive or FakeDrive()
        self.claimed: list[SubscriptionQueueItem] = []
        self.processed: list[ProcessorOutput] = []
        self.reviewed: list[ProcessorOutput] = []
        self.released: list[tuple[SubscriptionQueueItem, str]] = []
        self.fail_claim = False

    def allowed_context(self) -> AllowedContext:
        return allowed_context()

    def next_batch(self, *, batch_size: int, dry_run: bool) -> list[SubscriptionQueueItem]:
        assert batch_size == 1
        assert dry_run is True
        return self.items[:1]

    def claim_item(self, item: SubscriptionQueueItem, *, claim: str) -> SubscriptionQueueItem:
        if self.fail_claim:
            raise SubscriptionQueueStateError("claim_mismatch")
        claimed = replace(item, claim=claim)
        self.claimed.append(claimed)
        return claimed

    def load_bundle(self, item: SubscriptionQueueItem, target_dir: Path) -> SubscriptionQueueBundle:
        assert target_dir.is_dir()
        return SubscriptionQueueBundle(item=item, manifest={"type": "text", "text": item.raw_text}, originals=[])

    def finalize_processed(self, _: SubscriptionQueueItem, output: ProcessorOutput, *, processor_version: str) -> dict:
        assert processor_version
        self.processed.append(output)
        self.items.clear()
        return {}

    def finalize_needs_review(self, _: SubscriptionQueueItem, output: ProcessorOutput, *, processor_version: str) -> dict:
        assert processor_version
        self.reviewed.append(output)
        self.items.clear()
        return {}

    def release_claim(self, item: SubscriptionQueueItem, *, error_code: str) -> dict:
        self.released.append((item, error_code))
        self.items.clear()
        return {}


class FakeCodex:
    def __init__(self, outputs: list[Any] | None = None, *, auth_error: bool = False) -> None:
        self.outputs = list(outputs or [good_output()])
        self.auth_error = auth_error
        self.calls = 0

    def check_auth(self) -> None:
        if self.auth_error:
            raise CodexAuthUnavailable("codex_chatgpt_auth_unavailable")

    def run(self, prepared: PreparedMedia, allowed: AllowedContext, rules: list[dict[str, Any]]) -> ProcessorOutput:
        self.calls += 1
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeMedia:
    def __init__(self, prepared: PreparedMedia | None = None, *, failure: Exception | None = None) -> None:
        self.prepared = prepared or PreparedMedia(
            extracted_text=queue_item().raw_text,
            sanitized_manifest={"type": "text", "files": []},
            image_paths=[],
            all_essential_processed=True,
        )
        self.failure = failure
        self.seen_dir: Path | None = None

    def prepare(self, _: SubscriptionQueueBundle, prepared_dir: Path) -> PreparedMedia:
        self.seen_dir = prepared_dir
        if self.failure:
            raise self.failure
        return self.prepared


@pytest.fixture
def worker_settings(tmp_path: Path):
    return make_settings(
        tmp_path,
        VOICE_PROCESSING_ROUTE="chatgpt_subscription",
        GOOGLE_DRIVE_ENABLED=True,
        GOOGLE_DRIVE_CREDENTIALS_FILE=str(tmp_path / "client.json"),
        GOOGLE_DRIVE_TOKEN_FILE=str(tmp_path / "token.json"),
        SUBSCRIPTION_WORKER_LOCK_FILE=str(tmp_path / "worker.lock"),
        SUBSCRIPTION_WORKER_TMP_ROOT=str(tmp_path / "temp"),
        SUBSCRIPTION_WORKER_INSTANCE="test-host",
    )


@pytest.fixture(autouse=True)
def no_real_oauth_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.subscription_worker.verify_google_drive_token_persistence", lambda settings: None)
    monkeypatch.setattr("app.subscription_worker._check_media_prerequisites", lambda settings: None)


def run_worker(settings: Any, queue: FakeQueue, codex: FakeCodex, media: FakeMedia, **overrides: Any):
    arguments = {
        "dry_run": False,
        "once": True,
        "batch_size": 1,
        "record_id": "",
        "max_runtime": 1800,
        "no_finalize": False,
    }
    arguments.update(overrides)
    return SubscriptionWorker(
        settings,
        queue=queue,  # type: ignore[arg-type]
        codex_runner=codex,  # type: ignore[arg-type]
        media_processor=media,  # type: ignore[arg-type]
    ).run(**arguments)


def test_empty_queue(worker_settings: Any) -> None:
    stats = run_worker(worker_settings, FakeQueue(), FakeCodex(), FakeMedia())
    assert stats.queue_seen == stats.claimed == stats.processed == 0


def test_successful_text_record(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    stats = run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert stats.claimed == stats.processed == 1
    assert not queue.released


def test_successful_audio_record(worker_settings: Any) -> None:
    item = replace(queue_item(), entry_type="Voice")
    media = FakeMedia(
        PreparedMedia(
            extracted_text=item.raw_text,
            sanitized_manifest={"type": "voice", "files": [{"mime_type": "audio/ogg", "size": 10}]},
            image_paths=[],
            all_essential_processed=True,
        )
    )
    queue = FakeQueue([item])
    stats = run_worker(worker_settings, queue, FakeCodex(), media)
    assert stats.processed == 1


def test_codex_timeout_releases_claim(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    stats = run_worker(
        worker_settings,
        queue,
        FakeCodex([CodexProcessTimeout("codex_timeout")]),
        FakeMedia(),
    )
    assert stats.codex_failed == stats.released == 1
    assert queue.released[0][1] == "codex_timeout"


def test_codex_auth_missing_claims_nothing(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    with pytest.raises(CodexAuthUnavailable):
        run_worker(worker_settings, queue, FakeCodex(auth_error=True), FakeMedia())
    assert not queue.claimed


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(OutputValidationError, match="json_invalid"):
        validate_output("```json\n{}\n```", allowed_context(), queue_item().raw_text)


class SequencedRunner(CodexSubscriptionRunner):
    def __init__(self, settings: Any, responses: list[str]) -> None:
        self.settings = settings
        self.responses = responses

    def _invoke(self, prompt: str, images: list[Path]) -> str:
        assert "private-id" not in prompt
        return self.responses.pop(0)


def test_successful_second_codex_response(worker_settings: Any) -> None:
    good = json.dumps(good_output().model_dump(), ensure_ascii=False)
    runner = SequencedRunner(worker_settings, ["not json", good])
    prepared = FakeMedia().prepared
    assert runner.run(prepared, allowed_context(), []).confidence == 0.91
    assert runner.responses == []


def test_two_invalid_codex_responses(worker_settings: Any) -> None:
    runner = SequencedRunner(worker_settings, ["not json", "still not json"])
    with pytest.raises(OutputValidationError):
        runner.run(FakeMedia().prepared, allowed_context(), [])
    assert runner.responses == []


def test_temporary_airtable_error_releases_claim(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    queue.airtable.fail_rules = True
    stats = run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert stats.released == 1
    assert queue.released[0][1] == "airtable_rules_unavailable"


def test_temporary_drive_error_claims_nothing(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()], drive=FakeDrive(fail=True))
    with pytest.raises(WorkerTemporaryError, match="drive_unavailable"):
        run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert not queue.claimed


def test_claim_owned_by_another_worker_is_skipped(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    queue.fail_claim = True
    stats = run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert stats.claimed == 0
    assert not queue.released


def stale_record(claim: str, *, age_seconds: int = 7200) -> dict[str, Any]:
    return {
        "id": "record-private-id",
        "createdTime": "2026-07-28T10:00:00Z",
        "fields": {
            "Processing Route": "ChatGPT Subscription",
            "Статус обработки": "Awaiting Subscription",
            "Google Drive": "https://drive.invalid/private-folder",
            "Subscription Queue Claim": claim,
            "Subscription Queue Claimed At": (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(),
            "External ID": "external-private-id",
            "Источник": "Android",
            "Тип": "Text",
            "Исходная фраза": "private user text",
        },
    }


def test_stale_claim_from_own_worker_is_recovered(worker_settings: Any) -> None:
    queue = FakeQueue()
    queue.airtable.claimed_records = [stale_record(f"{CLAIM_PREFIX}:test-host:dead")]
    stats = run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert stats.stale_claims_recovered == 1
    assert queue.released[0][1] == "stale_worker_claim_recovered"


def test_foreign_stale_claim_is_never_released(worker_settings: Any) -> None:
    queue = FakeQueue()
    queue.airtable.claimed_records = [stale_record("manual-worker:private")]
    stats = run_worker(worker_settings, queue, FakeCodex(), FakeMedia())
    assert stats.stale_claims_recovered == 0
    assert not queue.released


def test_process_lock_rejects_parallel_instance(tmp_path: Path) -> None:
    first = ProcessLock(tmp_path / "worker.lock")
    second = ProcessLock(tmp_path / "worker.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()


def test_temporary_record_directory_is_cleaned(worker_settings: Any) -> None:
    media = FakeMedia()
    run_worker(worker_settings, FakeQueue([queue_item()]), FakeCodex(), media)
    assert media.seen_dir is not None
    assert not media.seen_dir.exists()


def test_codex_environment_is_allowlisted_without_dotenv_or_secrets(tmp_path: Path) -> None:
    environment = CodexSubscriptionRunner._minimal_environment(tmp_path)
    assert set(environment) == {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL"}
    assert "OPENAI_API_KEY" not in environment
    assert not any("TOKEN" in key or "SECRET" in key or "PASSWORD" in key for key in environment)


def test_codex_sandbox_reuses_outer_read_only_proc(tmp_path: Path) -> None:
    runner = object.__new__(CodexSubscriptionRunner)
    runner.bwrap = "/usr/bin/bwrap"
    runner.binary = Path("/usr/bin/codex")
    command = runner._sandbox_command(
        tmp_path / "work",
        tmp_path / "auth",
        tmp_path / "result.json",
        tmp_path / "schema.json",
        [],
    )
    assert "--proc" not in command
    assert any(command[index : index + 3] == ["--ro-bind", "/proc", "/proc"] for index in range(len(command)))


def test_logs_do_not_contain_user_data_or_secrets(worker_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    queue = FakeQueue([queue_item()])
    run_worker(
        worker_settings,
        queue,
        FakeCodex([CodexProcessTimeout("codex_timeout")]),
        FakeMedia(),
    )
    assert "private" not in caplog.text
    assert queue_item().raw_text not in caplog.text


def test_route_must_be_chatgpt_subscription(worker_settings: Any) -> None:
    settings = worker_settings.model_copy(update={"voice_processing_route": "openai_api"})
    with pytest.raises(WorkerTemporaryError, match="route_not_chatgpt_subscription"):
        run_worker(settings, FakeQueue([queue_item()]), FakeCodex(), FakeMedia())


def test_worker_does_not_use_openai_processor(monkeypatch: pytest.MonkeyPatch, worker_settings: Any) -> None:
    monkeypatch.setattr(
        "app.voice_processor.VoiceProcessorAI",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("OpenAI processor forbidden")),
    )
    stats = run_worker(worker_settings, FakeQueue([queue_item()]), FakeCodex(), FakeMedia())
    assert stats.processed == 1


def test_media_temporary_failure_is_released(worker_settings: Any) -> None:
    queue = FakeQueue([queue_item()])
    media = FakeMedia(failure=MediaTemporaryError("local_stt_unavailable"))
    stats = run_worker(worker_settings, queue, FakeCodex(), media)
    assert stats.media_failed == stats.released == 1


def test_prompt_limit_and_strict_output_limits(worker_settings: Any) -> None:
    settings = worker_settings.model_copy(update={"subscription_max_prompt_chars": 100})
    runner = SequencedRunner(settings, [])
    with pytest.raises(OutputValidationError, match="prompt_limit_exceeded"):
        runner.run(FakeMedia().prepared, allowed_context(), [])

    payload = good_output().model_dump()
    payload["extra"] = "forbidden"
    with pytest.raises(OutputValidationError, match="additional_fields"):
        validate_output(json.dumps(payload, ensure_ascii=False), allowed_context(), queue_item().raw_text)

    response = Path(worker_settings.subscription_worker_tmp_root) / "oversized-response.json"
    response.parent.mkdir(parents=True, exist_ok=True)
    response.write_text("x" * 100, encoding="utf-8")
    small_response_settings = worker_settings.model_copy(update={"subscription_max_response_bytes": 10})
    response_runner = SequencedRunner(small_response_settings, [])
    with pytest.raises(OutputValidationError, match="response_limit_exceeded"):
        response_runner._read_output(response)


def test_invalid_confidence_date_selects_and_content_loss() -> None:
    payload = good_output().model_dump()
    payload.update(
        {
            "confidence": 1.1,
            "due_date": "28.07.2026",
            "project": "Invented project",
            "tags": ["invented"],
            "clean_text": "Купить фильтры.",
            "summary": "Покупка.",
            "amount": None,
            "next_action": "Купить.",
        }
    )
    with pytest.raises(OutputValidationError):
        validate_output(json.dumps(payload, ensure_ascii=False), allowed_context(), queue_item().raw_text)


def test_systemd_unit_has_bounded_timeout_and_single_batch() -> None:
    unit = Path("deploy/systemd/voice-inbox-subscription-worker.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/voice-inbox-subscription-worker.timer").read_text(encoding="utf-8")
    assert "TimeoutStartSec=35min" in unit
    assert "--batch-size 1 --max-runtime 1800" in unit
    assert "OnUnitInactiveSec=10min" in timer
    assert "RandomizedDelaySec=60s" in timer
