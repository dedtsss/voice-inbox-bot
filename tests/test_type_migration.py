from app.dashboard.type_migration import build_case_only_type_plan, case_only_aliases


def test_case_only_migration_never_merges_different_type_meanings() -> None:
    aliases = case_only_aliases(["file", "File", "note", "Note", "Voice", "Задача", "задача"])

    assert aliases == {"file": "File", "note": "Note", "задача": "Задача"}
    assert "Voice" not in aliases


def test_case_only_migration_plan_preserves_counts_and_only_patches_aliases() -> None:
    records = [
        {"id": "recType001", "fields": {"Тип": "file"}},
        {"id": "recType002", "fields": {"Тип": "note"}},
        {"id": "recType003", "fields": {"Тип": "Voice"}},
        {"id": "recType004", "fields": {"Тип": "Задача"}},
    ]

    patches, before, after, aliases = build_case_only_type_plan(
        records,
        type_field="Тип",
        choices=["file", "File", "note", "Note", "Voice", "Задача"],
    )

    assert [(patch.before, patch.after) for patch in patches] == [("file", "File"), ("note", "Note")]
    assert before == {"Voice": 1, "file": 1, "note": 1, "Задача": 1}
    assert after == {"File": 1, "Note": 1, "Voice": 1, "Задача": 1}
    assert aliases == {"file": "File", "note": "Note"}
