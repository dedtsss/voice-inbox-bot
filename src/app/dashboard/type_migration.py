from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from app.select_options import canonical_select_options, clean_select_value


@dataclass(frozen=True)
class TypePatch:
    record_id: str
    before: str
    after: str


def case_only_aliases(choices: Iterable[Any]) -> dict[str, str]:
    """Map only values equal after surrounding-trim and casefold normalization."""
    choices = [clean_select_value(choice) for choice in choices if clean_select_value(choice)]
    canonical_by_key = {choice.casefold(): choice for choice in canonical_select_options(choices)}
    return {
        choice: canonical_by_key[choice.casefold()]
        for choice in choices
        if canonical_by_key[choice.casefold()] != choice
    }


def build_case_only_type_plan(
    records: Iterable[dict[str, Any]],
    *,
    type_field: str,
    choices: Iterable[Any],
) -> tuple[list[TypePatch], dict[str, int], dict[str, int], dict[str, str]]:
    aliases = case_only_aliases(choices)
    before: Counter[str] = Counter()
    patches: list[TypePatch] = []
    for record in records:
        fields = record.get("fields") or {}
        current = clean_select_value(fields.get(type_field))
        if current:
            before[current] += 1
        canonical = aliases.get(current)
        if canonical:
            patches.append(TypePatch(str(record.get("id") or ""), current, canonical))
    after = Counter(before)
    for patch in patches:
        after[patch.before] -= 1
        if after[patch.before] == 0:
            del after[patch.before]
        after[patch.after] += 1
    return patches, dict(sorted(before.items())), dict(sorted(after.items())), aliases
