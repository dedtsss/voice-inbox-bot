from __future__ import annotations

from app.dashboard.app import create_dashboard_app
from tests.test_dashboard import FakeAirtable, make_record, make_settings, voice_table


def fixture_table() -> dict:
    table = voice_table(include_training=True)
    status_field = next(field for field in table["fields"] if field["name"] == "Статус обработки")
    status_field["options"]["choices"] = [
        {"name": status}
        for status in (
            "New",
            "Processing",
            "Awaiting Subscription",
            "Processing Disabled",
            "Needs Review",
            "Processed",
        )
    ]
    return table


records = [
    make_record("recFixtureSub1", **{"Название": "Fixture subscription", "Статус обработки": "Awaiting Subscription"}),
    make_record("recFixtureProc1", **{"Название": "Fixture processing one", "Статус обработки": "Processing"}),
    make_record("recFixtureProc2", **{"Название": "Fixture processing two", "Статус обработки": "New"}),
    make_record("recFixtureOff1", **{"Название": "Fixture disabled", "Статус обработки": "Processing Disabled"}),
    make_record("recFixtureReview1", **{"Название": "Fixture review one", "Статус обработки": "Needs Review", "Приоритет": "High"}),
    make_record("recFixtureReview2", **{"Название": "Fixture review two", "Статус обработки": "Needs Review"}),
    make_record("recFixtureDone1", **{"Название": "Fixture completed", "Статус обработки": "Processed"}),
    make_record(
        "recFixtureTrain1",
        **{
            "Название": "Fixture training",
            "Статус обработки": "Processed",
            "Обучить на исправлении": True,
            "Обучение учтено": False,
        },
    ),
]

airtable = FakeAirtable(records=records, table=fixture_table())
settings = make_settings(
    DASHBOARD_ALLOWED_HOSTS="127.0.0.1,localhost",
    DASHBOARD_PUBLIC_ORIGIN="http://127.0.0.1:8765",
    DASHBOARD_PAGE_SIZE=50,
)
app = create_dashboard_app(settings, airtable)  # type: ignore[arg-type]
