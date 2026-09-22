from __future__ import annotations

import re
from typing import Iterable


_HEADING = re.compile(r"(?m)^(?=#{1,6}[ \t]+\S)")
_ACTION_TERMS = (
    "migration",
    "migrate",
    "deprecat",
    "breaking",
    "security",
    "迁移",
    "弃用",
    "破坏性",
    "安全",
)


def _units(markdown: str) -> list[str]:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    heading_starts = [match.start() for match in _HEADING.finditer(text)]
    if heading_starts:
        starts = ([0] if heading_starts[0] != 0 else []) + heading_starts
        starts = sorted(set(starts))
        return [text[start : starts[index + 1] if index + 1 < len(starts) else len(text)].strip() for index, start in enumerate(starts)]
    return [block.strip() for block in re.split(r"\n[ \t]*\n+", text) if block.strip()]


def _normalized_interests(interests: Iterable[str]) -> tuple[str, ...]:
    result = []
    for interest in interests:
        if not isinstance(interest, str):
            raise ValueError("interests must contain strings")
        value = " ".join(interest.split()).casefold()
        if value and value not in result:
            result.append(value)
    return tuple(result)


def extract_release_units(
    markdown: str,
    *,
    interests: Iterable[str],
    max_units: int = 8,
    state_budget_bytes: int = 24 * 1024,
) -> dict:
    if not isinstance(markdown, str):
        raise ValueError("markdown must be a string")
    if isinstance(max_units, bool) or not isinstance(max_units, int) or max_units < 1:
        raise ValueError("max_units must be a positive integer")
    if (
        isinstance(state_budget_bytes, bool)
        or not isinstance(state_budget_bytes, int)
        or state_budget_bytes < 1
    ):
        raise ValueError("state_budget_bytes must be a positive integer")
    normalized_interests = _normalized_interests(interests)
    units = _units(markdown)
    ranked = []
    for index, text in enumerate(units):
        folded = text.casefold()
        interest_hits = sum(1 for interest in normalized_interests if interest in folded)
        action_hits = sum(1 for term in _ACTION_TERMS if term in folded)
        ranked.append((-(action_hits * 100 + interest_hits * 10), index, text))
    selected: list[tuple[int, str]] = []
    used_bytes = 0
    for _negative_score, index, text in sorted(ranked):
        encoded_bytes = len(text.encode("utf-8")) + 96
        if len(selected) >= max_units:
            continue
        if encoded_bytes > state_budget_bytes - used_bytes:
            continue
        selected.append((index, text))
        used_bytes += encoded_bytes
    selected.sort(key=lambda item: item[0])
    omitted = len(units) - len(selected)
    if not selected:
        coverage_state = "unavailable"
        status = "needs_manual"
    else:
        coverage_state = "complete" if omitted == 0 else "partial"
        status = "pending"
    limitations = []
    if len(units) > max_units:
        limitations.append("unit_limit")
    if omitted and "unit_limit" not in limitations:
        limitations.append("state_budget")
    if not units:
        limitations.append("empty_release_body")
    return {
        "status": status,
        "units": [
            {
                "changeUnitId": f"cu_{index}",
                "sourceIndex": index,
                "text": text,
            }
            for index, text in selected
        ],
        "coverage": {
            "state": coverage_state,
            "totalUnits": len(units),
            "selectedUnits": len(selected),
            "omittedUnits": omitted,
            "rawSourcePartial": False,
            "selectionRuleVersion": "updates-units/v1",
            "limitations": limitations,
        },
    }
