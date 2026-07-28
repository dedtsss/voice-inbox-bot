from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.airtable import AirtableError, ProjectMatch
from app.subscription_queue import (
    DEFAULT_MANUAL_PROCESSOR_VERSION,
    SubscriptionQueue,
    SubscriptionQueueItem,
    SubscriptionQueueStateError,
)
from app.voice_processor import ProcessorOutput

from test_processing_routes import make_settings


CLAIM = "subscription_queue lock_id=session-one claimed_at=2026-07-28T10:00:00+00:00"


class FakeFinalizeAirtable:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = copy.deepcopy(record)
        self.updates: list[dict[str, Any]] = []
        self.fail_after_apply = False

    def fetch_voice_record(self, record_id: str) -> dict[str, Any]:
        assert record_id == self.record["id"]
        return copy.deepcopy(self.record)

    def claim_subscription_queue_record(self, record_id: str, *, claim: str, claimed_at: datetime) -> dict[str, Any]:
        return self.update_voice_record_fields(
            record_id,
            {
                "Subscription Queue Claim": claim,
                "Subscription Queue Claimed At": claimed_at.isoformat(),
            },
        )

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        assert record_id == self.record["id"]
        self.updates.append(copy.deepcopy(fields))
        for key, value in fields.items():
            if value is None:
                self.record["fields"].pop(key, None)
            else:
                self.record["fields"][key] = value
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise AirtableError("simulated response loss")
        return copy.deepcopy(self.record)

    def find_table_metadata(self, base_id: str, *, table_id: str = "", table_name: str = "") -> dict[str, Any]:
        return {
            "id": table_id,
            "fields": [
                {
                    "name": "Тип",
                    "options": {"choices": [{"name": "Задача"}, {"name": "Другое"}]},
                },
                {
                    "name": "Приоритет",
                    "options": {"choices": [{"name": "Низкий"}, {"name": "Обычный"}]},
                },
                {
                    "name": "Статус обработки",
                    "options": {
                        "choices": [
                            {"name": "Awaiting Subscription"},
                            {"name": "Processed"},
                            {"name": "Needs Review"},
                        ]
                    },
                },
                {
                    "name": "Проект",
                    "options": {"choices": [{"name": "Существующий проект"}]},
                },
                {
                    "name": "Теги",
                    "options": {"choices": [{"name": "работа"}]},
                },
            ],
        }

    def list_projects(self) -> list[ProjectMatch]:
        return [ProjectMatch("project-record", "Существующий проект")]


def make_record(*, status: str = "Awaiting Subscription", claim: str = CLAIM) -> dict[str, Any]:
    return {
        "id": "record-one",
        "createdTime": "2026-07-28T10:00:00.000Z",
        "fields": {
            "Processing Route": "ChatGPT Subscription",
            "Статус обработки": status,
            "Google Drive": "https://drive.invalid/folder-one",
            "Subscription Queue Claim": claim,
            "Subscription Queue Claimed At": "2026-07-28T10:00:00.000Z",
            "External ID": "external-one",
            "Источник": "Android",
            "Тип": "Задача",
            "Исходная фраза": "private original text",
        },
    }


def make_item(*, claim: str = CLAIM) -> SubscriptionQueueItem:
    return SubscriptionQueueItem(
        record_id="record-one",
        external_id="external-one",
        source="Android",
        entry_type="Voice",
        created_at="2026-07-28T10:00:00.000Z",
        raw_text="private original text",
        google_drive_url="https://drive.invalid/folder-one",
        claim=claim,
    )


def make_output(**overrides: Any) -> ProcessorOutput:
    values: dict[str, Any] = {
        "title": "Короткое название",
        "clean_text": "Полный очищенный смысл.",
        "summary": "Краткое содержание.",
        "type": "Задача",
        "project": "Существующий проект",
        "priority": "Обычный",
        "due_date": "2026-08-01",
        "counterparty": None,
        "amount": None,
        "period": None,
        "next_action": "Выполнить действие",
        "tags": ["работа"],
        "confidence": 0.91,
        "needs_review_reasons": [],
        "routing_reason": "Правило совпало.",
    }
    values.update(overrides)
    return ProcessorOutput.model_validate(values)


def make_queue(tmp_path: Path, airtable: FakeFinalizeAirtable) -> SubscriptionQueue:
    return SubscriptionQueue(make_settings(tmp_path), airtable=airtable, drive_reader=object())  # type: ignore[arg-type]


def test_successful_finalize_is_atomic_and_preserves_protected_fields(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    queue = make_queue(tmp_path, airtable)

    finalized = queue.finalize_processed(make_item(), make_output())

    fields = finalized["fields"]
    assert fields["Статус обработки"] == "Processed"
    assert fields["Processing Route"] == "ChatGPT Subscription"
    assert "Subscription Queue Claim" not in fields
    assert "Subscription Queue Claimed At" not in fields
    assert fields["Исходная фраза"] == "private original text"
    assert fields["Google Drive"] == "https://drive.invalid/folder-one"
    assert fields["External ID"] == "external-one"
    assert json.loads(fields["AI результат JSON"])["confidence"] == 0.91
    assert len(airtable.updates) == 1
    assert not {"Исходная фраза", "Google Drive", "External ID"} & airtable.updates[0].keys()


def test_finalize_rejects_wrong_claim(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record(claim="subscription_queue lock_id=other"))
    queue = make_queue(tmp_path, airtable)

    with pytest.raises(SubscriptionQueueStateError, match="claim_mismatch"):
        queue.finalize_processed(make_item(), make_output())

    assert airtable.updates == []


def test_finalize_rejects_record_processed_by_another_worker(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record(status="Processed", claim=""))
    queue = make_queue(tmp_path, airtable)

    with pytest.raises(SubscriptionQueueStateError, match="status_changed"):
        queue.finalize_processed(make_item(), make_output())

    assert airtable.updates == []


def test_finalize_recovers_from_airtable_response_failure(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    airtable.fail_after_apply = True
    queue = make_queue(tmp_path, airtable)

    finalized = queue.finalize_processed(make_item(), make_output())

    assert finalized["fields"]["Статус обработки"] == "Processed"
    assert len(airtable.updates) == 1


def test_repeated_finalize_is_idempotent(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    queue = make_queue(tmp_path, airtable)
    item = make_item()
    output = make_output()

    queue.finalize_processed(item, output)
    queue.finalize_processed(item, output)

    assert len(airtable.updates) == 1


def test_finalize_needs_review_saves_reasons_and_clears_claim(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    queue = make_queue(tmp_path, airtable)
    output = make_output(confidence=0.62, needs_review_reasons=["audio_unreadable"])

    finalized = queue.finalize_needs_review(make_item(), output)

    assert finalized["fields"]["Статус обработки"] == "Needs Review"
    assert finalized["fields"]["Ошибка обработки"] == "audio_unreadable"
    assert "Subscription Queue Claim" not in finalized["fields"]


def test_release_claim_is_verified_and_idempotent(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    queue = make_queue(tmp_path, airtable)
    item = make_item()

    released = queue.release_claim(item, error_code="local_stt_temporarily_unavailable")
    queue.release_claim(item, error_code="local_stt_temporarily_unavailable")

    assert released["fields"]["Статус обработки"] == "Awaiting Subscription"
    assert released["fields"]["Ошибка обработки"] == "local_stt_temporarily_unavailable"
    assert "Subscription Queue Claim" not in released["fields"]
    assert len(airtable.updates) == 1


@pytest.mark.parametrize("protected_field", ["Google Drive", "Исходная фраза", "External ID"])
def test_finalize_rejects_changed_protected_fields(tmp_path: Path, protected_field: str) -> None:
    record = make_record()
    record["fields"][protected_field] = "changed value"
    airtable = FakeFinalizeAirtable(record)
    queue = make_queue(tmp_path, airtable)

    with pytest.raises(SubscriptionQueueStateError, match="protected_field_changed"):
        queue.finalize_processed(make_item(), make_output())

    assert airtable.updates == []


def test_claim_is_reread_and_unique_to_item(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record(claim=""))
    queue = make_queue(tmp_path, airtable)

    claimed = queue.claim_item(replace(make_item(), claim=""), claim=CLAIM)

    assert claimed.claim == CLAIM
    assert airtable.fetch_voice_record("record-one")["fields"]["Subscription Queue Claim"] == CLAIM


def test_repeated_claim_with_same_value_is_idempotent(tmp_path: Path) -> None:
    airtable = FakeFinalizeAirtable(make_record())
    queue = make_queue(tmp_path, airtable)
    item = replace(make_item(), claim="")

    claimed = queue.claim_item(item, claim=CLAIM)

    assert claimed.claim == CLAIM
    assert airtable.updates == []


def test_queue_errors_do_not_log_secrets_or_user_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret = "sk-secret-value"
    private_content = "private original text"
    airtable = FakeFinalizeAirtable(make_record(claim="subscription_queue lock_id=other"))
    queue = make_queue(tmp_path, airtable)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SubscriptionQueueStateError):
        queue.finalize_processed(make_item(), make_output(title=f"{private_content} {secret}"))

    assert secret not in caplog.text
    assert private_content not in caplog.text
    assert "record-one" not in caplog.text


def test_default_manual_processor_version_is_stable() -> None:
    assert DEFAULT_MANUAL_PROCESSOR_VERSION == "codex-subscription-manual-v1"
