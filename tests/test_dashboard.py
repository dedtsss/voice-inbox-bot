from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.airtable import AirtableError, ProjectMatch
from app.config import Settings
from app.dashboard.airtable_service import DashboardAirtableService
from app.dashboard.app import create_dashboard_app


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        TELEGRAM_BOT_TOKEN="123:test",
        ALLOWED_TELEGRAM_USER_IDS="1",
        OPENAI_API_KEY="sk-test",
        AIRTABLE_TOKEN="pat-test",
        VOICE_INBOX_BASE_ID="appTest",
        VOICE_INBOX_TABLE_ID="tblInbox",
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
        DASHBOARD_CSRF_SECRET="dashboard-test-secret-with-more-than-32-bytes",
        DASHBOARD_ALLOWED_HOSTS="testserver,127.0.0.1,localhost",
        DASHBOARD_PUBLIC_ORIGIN="http://testserver",
        DASHBOARD_PAGE_SIZE=10,
    )
    values.update(overrides)
    return Settings(**values)


def iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_record(record_id: str = "recDashboard1", **fields: Any) -> dict[str, Any]:
    created_time = fields.pop("createdTime", iso(2))
    defaults: dict[str, Any] = {
        "Название": "Dashboard canary",
        "Тип": "Task",
        "Проект": "Home",
        "Приоритет": "Normal",
        "Статус обработки": "Needs Review",
        "Источник": "Android",
        "Исходная фраза": "raw text",
        "Очищенный текст": "clean text",
        "Краткое содержание": "summary",
        "Следующее действие": "next",
        "External ID": "android-1",
        "Срок": "2026-07-24",
        "Сумма": 12.5,
        "AI результат JSON": '{"validated":{"project":"Work","type":"Idea"},"processor":{"processed_at":"2026-07-24T06:00:00Z"}}',
        "Attachments": [
            {
                "filename": "voice.mp3",
                "type": "audio/mpeg",
                "size": 100,
                "url": "https://content.airtable.com/private/voice.mp3",
            }
        ],
    }
    defaults.update(fields)
    return {"id": record_id, "createdTime": created_time, "fields": defaults}


def voice_table() -> dict[str, Any]:
    def select(name: str, choices: list[str]) -> dict[str, Any]:
        return {"name": name, "type": "singleSelect", "options": {"choices": [{"name": choice} for choice in choices]}}

    return {
        "id": "tblInbox",
        "name": "Inbox",
        "fields": [
            {"name": "Название", "type": "singleLineText"},
            select("Тип", ["Task", "Idea", "Note"]),
            select("Проект", ["Home", "Work"]),
            select("Приоритет", ["Low", "Normal", "High"]),
            {"name": "Срок", "type": "date"},
            {"name": "Контрагент", "type": "singleLineText"},
            {"name": "Сумма", "type": "currency"},
            {"name": "Период", "type": "singleLineText"},
            {"name": "Следующее действие", "type": "multilineText"},
            {"name": "Краткое содержание", "type": "multilineText"},
            {"name": "Очищенный текст", "type": "multilineText"},
            {"name": "Исходная фраза", "type": "multilineText"},
            {"name": "Теги", "type": "multipleSelects", "options": {"choices": [{"name": "finance"}]}},
            select("Статус обработки", ["New", "Processing", "Processed", "Needs Review"]),
            {"name": "Attachments", "type": "multipleAttachments"},
            {"name": "Notes", "type": "multilineText"},
            {"name": "External ID", "type": "singleLineText"},
            {"name": "Google Drive", "type": "url"},
            {"name": "Источник", "type": "singleLineText"},
            {"name": "Ошибка обработки", "type": "multilineText"},
            {"name": "AI результат JSON", "type": "multilineText"},
            {"name": "Уверенность AI", "type": "number"},
            {"name": "Версия обработчика", "type": "singleLineText"},
            {"name": "Обучить на исправлении", "type": "checkbox"},
            {"name": "Комментарий к исправлению", "type": "multilineText"},
            {"name": "Обучение учтено", "type": "checkbox"},
        ],
    }


class FakeAirtable:
    def __init__(self, records: list[dict[str, Any]] | None = None, *, raise_on_page: bool = False) -> None:
        self.records = {record["id"]: record for record in (records if records is not None else [make_record()])}
        self.raise_on_page = raise_on_page
        self.page_calls: list[dict[str, Any]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.rule_updates: list[tuple[str, dict[str, Any]]] = []
        self.rules = [
            {
                "id": "recRule12345",
                "fields": {
                    "Правило": "Correction example",
                    "Активно": True,
                    "Область": "Маршрутизация",
                    "Условие": "Ключевые слова: test",
                    "Правильное решение": '{"project":"Home"}',
                },
            }
        ]

    def find_table_metadata(self, base_id: str, *, table_id: str = "", table_name: str = "") -> dict[str, Any] | None:
        if table_id == "tblRules" or table_name == "Правила обработки":
            return {
                "id": "tblRules",
                "name": "Правила обработки",
                "fields": [{"name": "Правило", "type": "singleLineText"}, {"name": "Активно", "type": "checkbox"}],
            }
        return voice_table()

    def list_projects(self) -> list[ProjectMatch]:
        return [ProjectMatch(record_id="recHome", title="Home"), ProjectMatch(record_id="recWork", title="Work")]

    def list_voice_records_limited(self, *, params: list[tuple[str, str]] | None = None, max_records: int = 1000) -> tuple[list[dict[str, Any]], bool]:
        return list(self.records.values())[:max_records], False

    def list_voice_records_page(
        self,
        *,
        params: list[tuple[str, str]] | None = None,
        page_size: int = 25,
        offset: str = "",
    ) -> dict[str, Any]:
        self.page_calls.append({"params": list(params or []), "page_size": page_size, "offset": offset})
        if self.raise_on_page:
            raise AirtableError("fake airtable outage")
        records = list(self.records.values())
        if offset:
            return {"records": records[page_size : page_size * 2]}
        payload: dict[str, Any] = {"records": records[:page_size]}
        if len(records) > page_size:
            payload["offset"] = "itrNEXT"
        return payload

    def fetch_voice_record(self, record_id: str) -> dict[str, Any]:
        return self.records[record_id]

    def update_voice_record_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((record_id, dict(fields)))
        self.records[record_id]["fields"].update(fields)
        return self.records[record_id]

    def rules_table_id(self) -> str:
        return "tblRules"

    def list_processing_rules(self, *, active_only: bool = True, page_size: int = 100) -> list[dict[str, Any]]:
        if active_only:
            return [rule for rule in self.rules if rule["fields"].get("Активно") is True][:page_size]
        return self.rules[:page_size]

    def update_processing_rule_fields(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.rule_updates.append((record_id, dict(fields)))
        return {"id": record_id, "fields": fields}


def make_client(fake: FakeAirtable | None = None) -> tuple[TestClient, FakeAirtable]:
    airtable = fake or FakeAirtable()
    app = create_dashboard_app(make_settings(), airtable)  # type: ignore[arg-type]
    return TestClient(app), airtable


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_health_endpoint_and_security_headers() -> None:
    client, _ = make_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_records_list_renders() -> None:
    client, _ = make_client()

    response = client.get("/records")

    assert response.status_code == 200
    assert "Dashboard canary" in response.text
    assert "summary" in response.text


def test_detail_card_renders_and_does_not_expose_attachment_url() -> None:
    client, _ = make_client()

    response = client.get("/records/recDashboard1")

    assert response.status_code == 200
    assert "Исходный текст" in response.text
    assert "/records/recDashboard1/attachments/0" in response.text
    assert "content.airtable.com/private" not in response.text


def test_empty_airtable_table_renders_empty_state() -> None:
    client, _ = make_client(FakeAirtable(records=[]))

    response = client.get("/records")

    assert response.status_code == 200
    assert "Записей не найдено." in response.text


def test_airtable_error_renders_safe_error() -> None:
    client, _ = make_client(FakeAirtable(raise_on_page=True))

    response = client.get("/records")

    assert response.status_code == 502
    assert "Airtable временно недоступен" in response.text
    assert "fake airtable outage" not in response.text


def test_pagination_uses_airtable_offset() -> None:
    records = [make_record(f"recDashboard{i}") for i in range(12)]
    client, airtable = make_client(FakeAirtable(records=records))

    response = client.get("/records?page_size=10&q=abc")

    assert response.status_code == 200
    assert "offset=itrNEXT" in response.text
    assert airtable.page_calls[-1]["page_size"] == 10
    assert airtable.page_calls[-1]["offset"] == ""


def test_filters_and_search_build_airtable_formula() -> None:
    client, airtable = make_client()

    response = client.get("/records?q=invoice&status=New&source=Android&project=Home&entry_type=Task&period=7d")

    assert response.status_code == 200
    formula = dict(airtable.page_calls[-1]["params"])["filterByFormula"]
    assert "{Статус обработки} = 'New'" in formula
    assert "{Источник} = 'Android'" in formula
    assert "{Проект} = 'Home'" in formula
    assert "{Тип} = 'Task'" in formula
    assert "SEARCH('invoice'" in formula
    assert "CREATED_TIME()" in formula


def test_stale_queue_records_are_marked() -> None:
    stale = make_record("recStale123", **{"Статус обработки": "Processing", "createdTime": iso(20)})
    client, _ = make_client(FakeAirtable(records=[stale]))

    response = client.get("/queue")

    assert response.status_code == 200
    assert "age-stale" in response.text
    assert "20 мин" in response.text


def test_validation_rejects_invalid_editable_values() -> None:
    airtable = FakeAirtable()
    service = DashboardAirtableService(make_settings(), airtable)  # type: ignore[arg-type]

    result = service.update_record_from_form(
        "recDashboard1",
        {"project": "Unknown", "due_date": "24.07.2026", "amount": "abc"},
        train=False,
    )

    assert result.errors["project"] == "Недопустимое значение"
    assert result.errors["due_date"] == "Дата должна быть в формате YYYY-MM-DD"
    assert result.errors["amount"] == "Сумма должна быть числом"
    assert airtable.updates == []


def test_unknown_form_field_is_rejected() -> None:
    airtable = FakeAirtable()
    service = DashboardAirtableService(make_settings(), airtable)  # type: ignore[arg-type]

    result = service.update_record_from_form("recDashboard1", {"raw_text": "overwrite"}, train=False)

    assert result.errors["raw_text"] == "Unknown editable field"
    assert airtable.updates == []


def test_csrf_is_required_for_save() -> None:
    client, _ = make_client()

    response = client.post(
        "/records/recDashboard1/save",
        data={"csrf_token": "bad", "project": "Home"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 403


def test_origin_or_referer_is_required_for_write() -> None:
    client, _ = make_client()
    token = csrf_from(client.get("/records/recDashboard1").text)

    response = client.post("/records/recDashboard1/save", data={"csrf_token": token, "project": "Home"})

    assert response.status_code == 403


def test_host_validation_rejects_unknown_host() -> None:
    client, _ = make_client()

    response = client.get("/records", headers={"Host": "evil.example"})

    assert response.status_code == 400


def test_xss_user_text_is_escaped() -> None:
    record = make_record("recXssTest1", **{"Исходная фраза": "<script>alert(1)</script>", "Очищенный текст": "<b>bold</b>"})
    client, _ = make_client(FakeAirtable(records=[record]))

    response = client.get("/records/recXssTest1")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<b>bold</b>" not in response.text


def test_save_partially_updates_allowed_fields_only() -> None:
    client, airtable = make_client()
    token = csrf_from(client.get("/records/recDashboard1").text)

    response = client.post(
        "/records/recDashboard1/save",
        data={"csrf_token": token, "project": "Work", "amount": "34.20", "action": "save"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    _, fields = airtable.updates[-1]
    assert fields == {
        "Проект": "Work",
        "Сумма": 34.2,
        "Статус обработки": "Processed",
        "Обучить на исправлении": False,
    }
    assert "Исходная фраза" not in fields
    assert "AI результат JSON" not in fields


def test_save_and_train_sets_existing_learning_flags() -> None:
    client, airtable = make_client()
    token = csrf_from(client.get("/records/recDashboard1").text)

    response = client.post(
        "/records/recDashboard1/save",
        data={"csrf_token": token, "project": "Work", "action": "save_train", "correction_comment": "route to Work"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    _, fields = airtable.updates[-1]
    assert fields["Обучить на исправлении"] is True
    assert fields["Обучение учтено"] is False
    assert fields["Комментарий к исправлению"] == "route to Work"


def test_rules_can_toggle_existing_active_field() -> None:
    client, airtable = make_client()
    token = csrf_from(client.get("/rules").text)

    response = client.post(
        "/rules/recRule12345/active",
        data={"csrf_token": token, "active": "0"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert airtable.rule_updates == [("recRule12345", {"Активно": False})]


def test_attachment_proxy_fetches_server_side(monkeypatch: Any) -> None:
    class Response:
        status_code = 200
        content = b"audio"
        headers = {"content-type": "audio/mpeg"}

    called: list[str] = []

    def fake_get(url: str, timeout: int) -> Response:
        called.append(url)
        return Response()

    monkeypatch.setattr("app.dashboard.airtable_service.requests.get", fake_get)
    client, _ = make_client()

    response = client.get("/records/recDashboard1/attachments/0")

    assert response.status_code == 200
    assert response.content == b"audio"
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert called == ["https://content.airtable.com/private/voice.mp3"]


def test_robots_disallows_indexing() -> None:
    client, _ = make_client()

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "Disallow: /" in response.text
