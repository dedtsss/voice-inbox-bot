from __future__ import annotations

from typing import Any, Iterable


def clean_select_value(value: Any) -> str:
    return str(value or "").strip()


def canonical_select_options(values: Iterable[Any], *, preferred: Iterable[str] = ()) -> list[str]:
    """Choose one stable spelling for trim/case-only select aliases.

    This intentionally does not translate, merge, or otherwise infer semantics.
    """
    groups: dict[str, set[str]] = {}
    for value in values:
        text = clean_select_value(value)
        if text:
            groups.setdefault(text.casefold(), set()).add(text)
    preferred_by_key = {
        clean_select_value(value).casefold(): clean_select_value(value)
        for value in preferred
        if clean_select_value(value)
    }
    result: list[str] = []
    for key in sorted(groups):
        variants = groups[key]
        preferred_value = preferred_by_key.get(key)
        if preferred_value in variants:
            result.append(preferred_value)
        else:
            result.append(sorted(variants, key=lambda value: (value.casefold(), value))[0])
    return result


def canonical_select_value(value: Any, options: Iterable[Any]) -> str | None:
    text = clean_select_value(value)
    if not text:
        return None
    for option in canonical_select_options(options):
        if option.casefold() == text.casefold():
            return option
    return None
