from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.airtable import AirtableError, ProjectMatch
from app.config import Settings
from app.dashboard.airtable_service import DashboardAirtableService
from app.dashboard.app import create_dashboard_app, safe_return_path


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


def voice_table(
    *,
    include_created_time: bool = False,
    created_time_type: str = "createdTime",
    views: list[dict[str, Any]] | None = None,
    include_training: bool = False,
) -> dict[str, Any]:
    def select(name: str, choices: list[str]) -> dict[str, Any]:
        return {"name": name, "type": "singleSelect", "options": {"choices": [{"name": choice} for choice in choices]}}

    fields = [
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
    ]
    if include_training:
        fields.extend(
            [
                select("Training Status", ["Pending", "In Progress", "Completed", "Skipped", "Auto Confirmed"]),
                select("Scope", ["Личное", "Рабочее", "Смешанное", "Не уверен"]),
                select("Life Area", ["Дом", "Семья", "Здоровье", "Другое"]),
                {"name": "Category", "type": "singleLineText"},
                {"name": "Subcategory", "type": "singleLineText"},
                {"name": "Training Confirmed At", "type": "dateTime"},
                {"name": "Training Answers JSON", "type": "multilineText"},
            ]
        )
    if include_created_time:
        field: dict[str, Any] = {"name": "Dashboard Created Time", "type": created_time_type}
        if created_time_type == "formula":
            field["options"] = {"formula": "CREATED_TIME()", "result": {"type": "date"}}
        fields.append(field)
    return {
        "id": "tblInbox",
        "name": "Inbox",
        "fields": fields,
        "views": views if views is not None else [{"name": "Grid view", "type": "grid"}],
    }


class FakeAirtable:
    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        *,
        table: dict[str, Any] | None = None,
        raise_on_page: bool = False,
    ) -> None:
        self.records = {record["id"]: record for record in (records if records is not None else [make_record()])}
        self.table = table or voice_table()
        self.raise_on_page = raise_on_page
        self.page_calls: list[dict[str, Any]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.rule_updates: list[tuple[str, dict[str, Any]]] = []
        self.created_rules: list[dict[str, Any]] = []
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
        return self.table

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
            raise AirtableError("fake airtable outage fake-user-text https://example.invalid/private-attachment")
        params_dict = dict(params or [])
        view = params_dict.get("view")
        if view and view not in {view_item.get("name") for view_item in self.table.get("views") or []}:
            raise AirtableError("fake missing view")
        records = self._filtered_records(params_dict.get("filterByFormula", ""))
        records = self._sorted_records(records, params or [])
        if offset:
            return {"records": records[page_size : page_size * 2]}
        payload: dict[str, Any] = {"records": records[:page_size]}
        if len(records) > page_size:
            payload["offset"] = "itrNEXT"
        return payload

    def _filtered_records(self, formula: str) -> list[dict[str, Any]]:
        records = list(self.records.values())
        if "{Training Status}" in formula and "{Статус обработки}" in formula:
            records = [
                record
                for record in records
                if str(record.get("fields", {}).get("Статус обработки") or "") in {"", "Processed", "Needs Review", "New"}
            ]
            records = [
                record
                for record in records
                if str(record.get("fields", {}).get("Training Status") or "") in {"", "Pending", "In Progress"}
            ]
            technical_patterns = ("smoke", "canary", "production test", "tg-smoke", "dashboard-canary")
            searchable = ("Название", "Исходная фраза", "Очищенный текст", "External ID", "Notes")
            records = [
                record
                for record in records
                if not any(
                    pattern in " ".join(str(record.get("fields", {}).get(field) or "") for field in searchable).casefold()
                    for pattern in technical_patterns
                )
            ]
            source_match = re.search(r"\{Источник\} = '([^']*)'", formula)
            if source_match:
                source = source_match.group(1)
                records = [record for record in records if str(record.get("fields", {}).get("Источник") or "") == source]
            if "IS_AFTER(CREATED_TIME()" in formula:
                cutoff_match = re.search(r"DATETIME_PARSE\\('([^']+)'\\)", formula)
                cutoff = cutoff_match.group(1) if cutoff_match else ""
                records = [record for record in records if str(record.get("createdTime") or "") > cutoff]
            return records
        equals = re.findall(r"\{([^}]+)\} = '([^']*)'", formula)
        for field_name, value in equals:
            if "OR(" in formula and field_name == "Статус обработки" and value in {"New", "Processing"}:
                continue
            if "OR(" in formula and field_name == "Статус обработки" and value in {"", "Processed", "Needs Review", "New"}:
                continue
            if "OR(" in formula and field_name == "Training Status" and value in {"", "Pending", "In Progress"}:
                continue
            records = [record for record in records if str(record.get("fields", {}).get(field_name) or "") == value]
        if "OR({Статус обработки} = 'New',{Статус обработки} = 'Processing')" in formula:
            records = [
                record
                for record in records
                if str(record.get("fields", {}).get("Статус обработки") or "") in {"New", "Processing"}
            ]
        match = re.search(r"SEARCH\('([^']+)'", formula)
        if match:
            needle = match.group(1).casefold()
            searchable = ("Название", "Исходная фраза", "Очищенный текст", "Краткое содержание", "Следующее действие", "External ID", "Notes")
            records = [
                record
                for record in records
                if any(needle in str(record.get("fields", {}).get(field) or "").casefold() for field in searchable)
            ]
        return records

    def _sorted_records(self, records: list[dict[str, Any]], params: list[tuple[str, str]] | None) -> list[dict[str, Any]]:
        params_dict = dict(params or [])
        sort_fields: list[tuple[str, str]] = []
        for index in range(3):
            field_name = params_dict.get(f"sort[{index}][field]")
            if field_name:
                sort_fields.append((field_name, params_dict.get(f"sort[{index}][direction]", "asc")))
        sorted_records = list(records)
        for field_name, direction in reversed(sort_fields):
            reverse = direction == "desc"
            sorted_records.sort(key=lambda record: self._sort_value(record, field_name), reverse=reverse)
        return sorted_records

    def _sort_value(self, record: dict[str, Any], field_name: str) -> Any:
        if field_name == "Dashboard Created Time":
            return record.get("createdTime") or ""
        return record.get("fields", {}).get(field_name) or ""

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

    def create_processing_rule(self, fields: dict[str, Any]) -> dict[str, Any]:
        self.created_rules.append(dict(fields))
        rule = {"id": f"recRuleCreated{len(self.created_rules)}", "fields": dict(fields)}
        self.rules.append(rule)
        return rule

    def list_taxonomy_records(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        return []


def make_client(fake: FakeAirtable | None = None, **settings_overrides: Any) -> tuple[TestClient, FakeAirtable]:
    airtable = fake or FakeAirtable()
    app = create_dashboard_app(make_settings(**settings_overrides), airtable)  # type: ignore[arg-type]
    return TestClient(app), airtable


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_health_endpoint_and_security_headers() -> None:
    client, _ = make_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "sorting_mode": "page_only_unsafe"}
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_records_list_renders() -> None:
    client, _ = make_client()

    response = client.get("/records")

    assert response.status_code == 200
    assert "Dashboard canary" in response.text
    assert "summary" in response.text


def test_new_dashboard_sections_render() -> None:
    client, _ = make_client()

    expected = {
        "/kanban": "Канбан входящих",
        "/needs-review": "Проверка AI",
        "/learning": "Обучение",
        "/projects": "Проекты",
        "/sources": "Источники",
        "/analytics": "Аналитика",
        "/settings": "Настройки",
    }

    for path, marker in expected.items():
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text


def test_kanban_page_uses_real_record_data() -> None:
    records = [
        make_record("recKanbanNew1", **{"Статус обработки": "New", "Название": "Inbox new"}),
        make_record("recKanbanProc", **{"Статус обработки": "Processing", "Название": "Inbox processing"}),
        make_record("recKanbanDone", **{"Статус обработки": "Processed", "Название": "Inbox done"}),
    ]
    client, airtable = make_client(FakeAirtable(records=records))

    response = client.get("/kanban?q=Inbox")

    assert response.status_code == 200
    assert "Inbox new" in response.text
    assert "Inbox processing" in response.text
    assert "Inbox done" in response.text
    assert "filterByFormula" in dict(airtable.page_calls[-1]["params"])


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

    response = client.get("/records?page_size=10&q=dashboard")

    assert response.status_code == 200
    assert "offset=itrNEXT" in response.text
    assert airtable.page_calls[-1]["page_size"] == 10
    assert airtable.page_calls[-1]["offset"] == ""


def test_dashboard_airtable_view_is_used_with_priority() -> None:
    table = voice_table(include_created_time=True, views=[{"name": "Dashboard Latest", "type": "grid"}])
    client, airtable = make_client(
        FakeAirtable(table=table),
        DASHBOARD_AIRTABLE_VIEW="Dashboard Latest",
        DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time",
    )

    response = client.get("/records?sort=asc")

    assert response.status_code == 200
    params = dict(airtable.page_calls[-1]["params"])
    assert params["view"] == "Dashboard Latest"
    assert "sort[0][field]" not in params
    assert 'option value="desc" selected' in response.text
    assert client.get("/healthz").json()["sorting_mode"] == "airtable_view"


@pytest.mark.parametrize("created_time_type", ["createdTime", "formula"])
def test_created_time_field_adds_desc_server_sort_and_stable_secondary_sort(created_time_type: str) -> None:
    table = voice_table(include_created_time=True, created_time_type=created_time_type)
    client, airtable = make_client(FakeAirtable(table=table), DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time")

    response = client.get("/records?sort=private-user-field")

    assert response.status_code == 200
    params = dict(airtable.page_calls[-1]["params"])
    assert params["sort[0][field]"] == "Dashboard Created Time"
    assert params["sort[0][direction]"] == "desc"
    assert params["sort[1][field]"] == "External ID"
    assert params["sort[1][direction]"] == "asc"
    assert "private-user-field" not in {value for key, value in params.items() if key.startswith("sort")}
    assert "Сортировка по времени применяется к текущей странице" not in response.text


def test_created_time_sort_is_global_across_first_and_second_page() -> None:
    table = voice_table(include_created_time=True)
    records = [
        make_record("recOldest111", createdTime="2026-07-19T10:00:00.000Z", **{"External ID": "d"}),
        make_record("recOlder222", createdTime="2026-07-19T10:05:00.000Z", **{"External ID": "c"}),
        make_record("recNewest333", createdTime="2026-07-19T10:20:00.000Z", **{"External ID": "a"}),
        make_record("recMiddle444", createdTime="2026-07-19T10:10:00.000Z", **{"External ID": "b"}),
    ]
    service = DashboardAirtableService(
        make_settings(DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time"),
        FakeAirtable(records=records, table=table),  # type: ignore[arg-type]
    )

    first = service.list_records({"page_size": "2"})
    second = service.list_records({"page_size": "2", "offset": first["next_offset"]})

    assert [record["id"] for record in first["records"]] == ["recNewest333", "recMiddle444"]
    assert [record["id"] for record in second["records"]] == ["recOlder222", "recOldest111"]
    assert first["created_sort_is_exact"] is True
    assert first["sorting_mode"] == "airtable_field"


def test_created_time_sort_uses_secondary_field_for_stable_ties() -> None:
    table = voice_table(include_created_time=True)
    records = [
        make_record("recTieB2222", createdTime="2026-07-19T10:00:00.000Z", **{"External ID": "b"}),
        make_record("recTieA1111", createdTime="2026-07-19T10:00:00.000Z", **{"External ID": "a"}),
    ]
    service = DashboardAirtableService(
        make_settings(DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time"),
        FakeAirtable(records=records, table=table),  # type: ignore[arg-type]
    )

    data = service.list_records({})

    assert [record["id"] for record in data["records"]] == ["recTieA1111", "recTieB2222"]


def test_filters_and_search_build_airtable_formula() -> None:
    table = voice_table(include_created_time=True)
    client, airtable = make_client(FakeAirtable(table=table), DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time")

    response = client.get("/records?q=invoice&status=New&source=Android&project=Home&entry_type=Task&period=7d")

    assert response.status_code == 200
    formula = dict(airtable.page_calls[-1]["params"])["filterByFormula"]
    assert "{Статус обработки} = 'New'" in formula
    assert "{Источник} = 'Android'" in formula
    assert "{Проект} = 'Home'" in formula
    assert "{Тип} = 'Task'" in formula
    assert "SEARCH('invoice'" in formula
    assert "CREATED_TIME()" in formula
    params = dict(airtable.page_calls[-1]["params"])
    assert params["sort[0][field]"] == "Dashboard Created Time"
    assert params["sort[0][direction]"] == "desc"


@pytest.mark.parametrize(
    ("path", "expected_formula"),
    [
        ("/records?status=New", "{Статус обработки} = 'New'"),
        ("/needs-review", "{Статус обработки} = 'Needs Review'"),
        ("/processed", "{Статус обработки} = 'Processed'"),
        ("/queue", "OR({Статус обработки} = 'New',{Статус обработки} = 'Processing')"),
        ("/technical", "SEARCH('smoke'"),
        ("/records?q=invoice", "SEARCH('invoice'"),
    ],
)
def test_server_sort_is_preserved_for_sections_filters_and_search(path: str, expected_formula: str) -> None:
    table = voice_table(include_created_time=True)
    records = [
        make_record("recFiltered1", createdTime="2026-07-19T10:00:00.000Z", **{"Статус обработки": "New", "Название": "invoice"}),
        make_record("recFiltered2", createdTime="2026-07-19T10:01:00.000Z", **{"Статус обработки": "Processing", "Название": "invoice"}),
        make_record("recFiltered3", createdTime="2026-07-19T10:02:00.000Z", **{"Статус обработки": "Processed", "Название": "invoice"}),
        make_record("recFiltered4", createdTime="2026-07-19T10:03:00.000Z", **{"Статус обработки": "Needs Review", "Название": "invoice"}),
    ]
    client, airtable = make_client(
        FakeAirtable(records=records, table=table),
        DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time",
    )

    response = client.get(path)

    assert response.status_code == 200
    params = dict(airtable.page_calls[-1]["params"])
    assert expected_formula in params["filterByFormula"]
    assert params["sort[0][field]"] == "Dashboard Created Time"
    assert params["sort[0][direction]"] == "desc"


def test_missing_sort_config_is_explicit_page_only_limited_mode() -> None:
    records = [
        make_record("recOldPage11", createdTime="2026-07-19T10:00:00.000Z"),
        make_record("recOldPage12", createdTime="2026-07-19T10:01:00.000Z"),
        make_record("recNewest22", createdTime="2026-07-19T10:20:00.000Z"),
    ]
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=records))  # type: ignore[arg-type]

    data = service.list_records({"page_size": "2"})

    assert [record["id"] for record in data["records"]] == ["recOldPage12", "recOldPage11"]
    assert "recNewest22" not in [record["id"] for record in data["records"]]
    assert data["created_sort_is_exact"] is False
    assert data["sorting_mode"] == "page_only_unsafe"


def test_missing_sort_config_logs_safe_startup_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    make_client()

    log_text = caplog.text
    assert "sorting_mode=page_only_unsafe" in log_text
    assert "pat-" not in log_text
    assert "sk-" not in log_text
    assert "raw text" not in log_text


def test_missing_airtable_view_is_reported_without_page_only_fallback() -> None:
    client, airtable = make_client(FakeAirtable(table=voice_table(views=[])), DASHBOARD_AIRTABLE_VIEW="Missing View")

    response = client.get("/records")

    assert response.status_code == 502
    assert airtable.page_calls == []
    assert "Missing View" not in response.text


def test_missing_created_time_field_is_reported_without_page_only_fallback() -> None:
    client, airtable = make_client(DASHBOARD_CREATED_TIME_FIELD="Missing Created Time")

    response = client.get("/records")

    assert response.status_code == 502
    assert airtable.page_calls == []
    assert "Missing Created Time" not in response.text


def test_created_time_field_must_have_created_time_type() -> None:
    table = voice_table(include_created_time=True, created_time_type="singleLineText")
    client, airtable = make_client(FakeAirtable(table=table), DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time")

    response = client.get("/records")

    assert response.status_code == 502
    assert airtable.page_calls == []


def test_airtable_error_logging_does_not_include_user_text_or_attachment_url(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    client, _ = make_client(FakeAirtable(raise_on_page=True))

    response = client.get("/records")

    assert response.status_code == 502
    assert "fake-user-text" not in caplog.text
    assert "example.invalid/private-attachment" not in caplog.text


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


def test_needs_review_quick_save_and_train_returns_to_review() -> None:
    client, airtable = make_client()
    token = csrf_from(client.get("/needs-review").text)

    response = client.post(
        "/records/recDashboard1/save",
        data={
            "csrf_token": token,
            "project": "Work",
            "action": "save_train",
            "correction_comment": "review screen correction",
            "return_to": "/needs-review?q=Dashboard",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/needs-review?q=Dashboard&saved=trained"
    _, fields = airtable.updates[-1]
    assert fields["Обучить на исправлении"] is True
    assert fields["Комментарий к исправлению"] == "review screen correction"


def test_return_path_rejects_external_redirects() -> None:
    assert safe_return_path("https://evil.example/needs-review") == ""
    assert safe_return_path("//evil.example/needs-review") == ""
    assert safe_return_path("/records/recDashboard1") == ""
    assert safe_return_path("/kanban?q=abc&csrf_token=bad") == "/kanban?q=abc"


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


def training_table() -> dict[str, Any]:
    return voice_table(include_created_time=True, include_training=True)


def training_answers(**overrides: Any) -> str:
    answers = {
        "scope": "Рабочее",
        "project": "Work",
        "life_area": "",
        "category": "Ops",
        "subcategory": "Inbox",
        "type": "Задача",
        "next_action": "Проверить",
        "priority": "Normal",
        "due_date": "",
        "tags": [],
    }
    answers.update(overrides)
    return json.dumps({"schema_version": 1, "record_id": "recSource01", "answers": answers}, ensure_ascii=False)


def test_training_queue_includes_processed_and_needs_review_without_needs_review_dependency() -> None:
    records = [
        make_record("recTrain001", createdTime=iso(1), **{"Название": "Real processed", "Статус обработки": "Processed"}),
        make_record("recTrain002", createdTime=iso(2), **{"Название": "Real review", "Статус обработки": "Needs Review"}),
        make_record("recTrain003", createdTime=iso(3), **{"Название": "Already done", "Training Status": "Completed"}),
        make_record("recTrain004", createdTime=iso(4), **{"Название": "dashboard-canary smoke", "Статус обработки": "Processed"}),
        make_record("recTrain005", createdTime="2026-07-23T10:00:00.000Z", **{"Название": "Old archive", "Статус обработки": "Processed"}),
    ]
    service = DashboardAirtableService(make_settings(DASHBOARD_CREATED_TIME_FIELD="Dashboard Created Time"), FakeAirtable(records=records, table=training_table()))  # type: ignore[arg-type]

    data = service.training_queue({})

    assert [record["id"] for record in data["records"]] == ["recTrain001", "recTrain002"]
    formula = dict(service.airtable.page_calls[-1]["params"])["filterByFormula"]  # type: ignore[attr-defined]
    assert "{Training Status}" in formula
    assert "{Статус обработки} = 'Needs Review'" in formula


def test_training_backlog_is_manual_and_limited() -> None:
    old = make_record("recOldTrain1", createdTime="2026-07-23T10:00:00.000Z", **{"Название": "Old real", "Статус обработки": "Processed"})
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=[old], table=training_table()))  # type: ignore[arg-type]

    assert service.training_queue({})["records"] == []
    assert [record["id"] for record in service.training_queue({"backlog": "1"})["records"]] == ["recOldTrain1"]


def test_training_session_questions_are_adaptive_for_work_and_personal() -> None:
    work = make_record("recWorkPath1", **{"Название": "Work path", "Scope": "Рабочее", "Проект": "Work"})
    personal = make_record("recPersPath1", **{"Название": "Personal path", "Scope": "Личное", "Life Area": "Дом", "Проект": ""})
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=[work, personal], table=training_table()))  # type: ignore[arg-type]

    assert "project" in service.learning_session("recWorkPath1")["questions"]
    assert "life_area" not in service.learning_session("recWorkPath1")["questions"]
    assert "life_area" in service.learning_session("recPersPath1")["questions"]
    assert "project" not in service.learning_session("recPersPath1")["questions"]


def test_training_start_marks_in_progress_only_on_post() -> None:
    record = make_record("recStart001", **{"Название": "Start real", "Статус обработки": "Processed"})
    client, airtable = make_client(FakeAirtable(records=[record], table=training_table()))

    response = client.get("/learning/session", follow_redirects=False)
    assert response.status_code == 307
    assert airtable.updates == []

    token = csrf_from(client.get("/learning").text)
    response = client.post(
        "/learning/session/start",
        data={"csrf_token": token},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert airtable.updates[-1] == ("recStart001", {"Training Status": "In Progress"})


def test_training_complete_saves_structured_answers_without_user_text_blob() -> None:
    record = make_record("recComplete1", **{"Название": "Complete real", "Статус обработки": "Processed"})
    client, airtable = make_client(FakeAirtable(records=[record], table=training_table()))
    token = csrf_from(client.get("/learning/session/recComplete1").text)

    response = client.post(
        "/learning/session/recComplete1/complete",
        data={
            "csrf_token": token,
            "scope": "Рабочее",
            "project": "Work",
            "entry_type": "Задача",
            "next_action": "Проверить",
            "priority": "Normal",
            "category": "Ops",
            "subcategory": "Inbox",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    _, fields = airtable.updates[-1]
    assert fields["Training Status"] == "Completed"
    assert fields["Scope"] == "Рабочее"
    assert fields["Проект"] == "Work"
    assert fields["Тип"] == "Задача"
    payload = json.loads(fields["Training Answers JSON"])
    assert payload["answers"]["category"] == "Ops"
    assert "raw text" not in fields["Training Answers JSON"]
    assert "Attachments" not in fields["Training Answers JSON"]


def test_training_skip_sets_skipped_status() -> None:
    record = make_record("recSkip0001", **{"Название": "Skip real", "Статус обработки": "Processed"})
    client, airtable = make_client(FakeAirtable(records=[record], table=training_table()))
    token = csrf_from(client.get("/learning/session/recSkip0001").text)

    response = client.post(
        "/learning/session/recSkip0001/skip",
        data={"csrf_token": token},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert airtable.updates[-1][1]["Training Status"] == "Skipped"


def test_training_complete_rejects_unknown_form_field_and_requires_csrf() -> None:
    record = make_record("recReject01", **{"Название": "Reject real", "Статус обработки": "Processed"})
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=[record], table=training_table()))  # type: ignore[arg-type]

    result = service.complete_training_record(
        "recReject01",
        {"scope": "Рабочее", "project": "Work", "entry_type": "Задача", "next_action": "Проверить", "raw_text": "bad"},
    )

    assert result.errors["raw_text"] == "Unknown training field"

    client, _ = make_client(FakeAirtable(records=[record], table=training_table()))
    response = client.post(
        "/learning/session/recReject01/complete",
        data={"csrf_token": "bad"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 403


def test_training_batch_apply_updates_only_selected_records_and_enforces_limit() -> None:
    source = make_record(
        "recSource01",
        **{"Название": "Source real", "Training Status": "Completed", "Training Answers JSON": training_answers()},
    )
    selected = make_record("recSelect01", **{"Название": "Similar selected", "Статус обработки": "Processed"})
    unselected = make_record("recSelect02", **{"Название": "Similar unselected", "Статус обработки": "Processed"})
    client, airtable = make_client(FakeAirtable(records=[source, selected, unselected], table=training_table()))
    token = csrf_from(client.get("/learning/session/recSource01").text)

    response = client.post(
        "/learning/batch-apply",
        data={"csrf_token": token, "source_record_id": "recSource01", "record_ids": ["recSelect01"]},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    updated_ids = [record_id for record_id, _ in airtable.updates]
    assert "recSelect01" in updated_ids
    assert "recSelect02" not in updated_ids

    service = DashboardAirtableService(
        make_settings(VOICE_TRAINING_BATCH_LIMIT=2),
        FakeAirtable(records=[source, selected, unselected], table=training_table()),  # type: ignore[arg-type]
    )
    too_many = service.batch_apply_training("recSource01", ["recSelect01", "recSelect02", "recTrain003"])
    assert too_many.errors["record_ids"] == "Максимум записей за раз: 2"


def test_similarity_suggestion_uses_token_overlap_and_existing_hints() -> None:
    current = make_record(
        "recSimilar1",
        **{"Название": "Оплатить счет за сервер", "Исходная фраза": "оплатить счет за сервер", "Очищенный текст": "оплатить счет за сервер", "Краткое содержание": "сервер счет", "Проект": "Work", "Тип": "Задача"},
    )
    similar = make_record(
        "recSimilar2",
        **{"Название": "Проверить счет за сервер", "Исходная фраза": "проверить счет за сервер", "Очищенный текст": "проверить счет за сервер", "Краткое содержание": "сервер счет", "Проект": "Work", "Тип": "Задача"},
    )
    different = make_record(
        "recSimilar3",
        **{"Название": "Купить продукты домой", "Исходная фраза": "купить продукты домой", "Очищенный текст": "купить продукты домой", "Краткое содержание": "дом покупки", "Проект": "Home", "Тип": "Note"},
    )
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=[current, similar, different], table=training_table()))  # type: ignore[arg-type]

    session = service.learning_session("recSimilar1")

    assert [record["id"] for record in session["similar_records"]] == ["recSimilar2"]


def test_rule_is_not_created_after_one_example_but_can_be_created_explicitly_after_threshold() -> None:
    one = make_record("recRuleOne1", **{"Название": "One rule", "Training Status": "Completed", "Training Answers JSON": training_answers()})
    service = DashboardAirtableService(make_settings(), FakeAirtable(records=[one], table=training_table()))  # type: ignore[arg-type]
    assert service.rule_proposals() == []
    assert service.airtable.created_rules == []  # type: ignore[attr-defined]

    records = [
        make_record(f"recRuleThr{i}", **{"Название": f"Rule {i}", "Training Status": "Completed", "Training Answers JSON": training_answers()})
        for i in range(3)
    ]
    fake = FakeAirtable(records=records, table=training_table())
    service = DashboardAirtableService(make_settings(), fake)  # type: ignore[arg-type]

    proposals = service.rule_proposals()
    assert len(proposals) == 1
    result = service.create_training_rule(proposals[0]["key"])

    assert result.errors == {}
    assert fake.created_rules[0]["Активно"] is True
    assert fake.created_rules[0]["Правильное решение"]


def test_learning_pages_render_queue_session_rules_and_structure() -> None:
    record = make_record("recRender01", **{"Название": "Render real", "Статус обработки": "Processed"})
    client, _ = make_client(FakeAirtable(records=[record], table=training_table()))

    for path, marker in {
        "/learning?tab=queue": "Очередь",
        "/learning/session/recRender01": "Мастер",
        "/learning?tab=rules": "Предложения правил",
        "/learning?tab=structure": "Рабочие проекты",
    }.items():
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text


def test_learning_session_escapes_user_text() -> None:
    record = make_record("recLearnXss", **{"Название": "Real", "Исходная фраза": "<script>alert(1)</script>", "Статус обработки": "Processed"})
    client, _ = make_client(FakeAirtable(records=[record], table=training_table()))

    response = client.get("/learning/session/recLearnXss")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_robots_disallows_indexing() -> None:
    client, _ = make_client()

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "Disallow: /" in response.text
