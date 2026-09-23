"""Bounded, owner-scoped successful-processing ledger projection."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Mapping

from .product_dto_rules import iso_timestamp


def query_value(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _scope_digest(owner_id: str, module: str | None) -> str:
    raw = json.dumps([owner_id, module], separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def parse_usage_events_query(params: Mapping[str, object], *, owner_id: str) -> tuple[str | None, tuple[int, str] | None, int]:
    module = query_value(params, "module") or None
    if module not in {None, "pr", "ci", "updates"}:
        raise ValueError("INVALID_CONFIGURATION")
    raw_limit = query_value(params, "limit")
    if raw_limit and (not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 50):
        raise ValueError("INVALID_CONFIGURATION")
    limit = int(raw_limit) if raw_limit else 20
    cursor = query_value(params, "cursor")
    if not cursor:
        return module, None, limit
    if len(cursor) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ValueError("INVALID_CURSOR")
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(decoded)
    except (ValueError, UnicodeError):
        raise ValueError("INVALID_CURSOR") from None
    if (not isinstance(value, list) or len(value) != 3
            or type(value[0]) is not int or value[0] < 0
            or not isinstance(value[1], str) or not value[1]
            or value[2] != _scope_digest(owner_id, module)
            or encode_usage_cursor(value[0], value[1], owner_id, module) != cursor):
        raise ValueError("INVALID_CURSOR")
    return module, (value[0], value[1]), limit


def encode_usage_cursor(finished_at: int, reservation_id: str,
                        owner_id: str, module: str | None) -> str:
    raw = json.dumps([finished_at, reservation_id,
                      _scope_digest(owner_id, module)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def usage_event_dto(row: Mapping[str, object]) -> dict:
    return {"id": row["reservation_id"], "metric": "intelligent_processing",
            "module": row["module"], "period": row["period"],
            "processedAt": iso_timestamp(int(row["finished_at"]))}


def usage_events_page(rows: list[Mapping[str, object]], limit: int,
                      *, owner_id: str, module: str | None) -> dict:
    selected = rows[:limit]
    has_more = len(rows) > limit
    last = selected[-1] if has_more and selected else None
    return {"items": [usage_event_dto(row) for row in selected],
            "nextCursor": (encode_usage_cursor(int(last["finished_at"]),
                                               str(last["reservation_id"]),
                                               owner_id, module) if last else None),
            "hasMore": has_more}
