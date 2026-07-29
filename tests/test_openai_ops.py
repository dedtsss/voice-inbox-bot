from app.openai_ops import _fallback_structure, _normalize_next_action, _normalize_structure


def test_next_action_is_empty_when_a_note_has_no_concrete_follow_up() -> None:
    assert _fallback_structure("Справочная заметка", "Text")["next_action"] == ""
    assert _normalize_next_action("Не требуется") == ""
    assert _normalize_next_action("Review note content") == ""
    assert _normalize_structure({}, "Идея без решения", "Text")["next_action"] == ""


def test_next_action_keeps_a_real_concrete_step() -> None:
    assert _normalize_next_action("Позвонить подрядчику и уточнить цену") == "Позвонить подрядчику и уточнить цену"
