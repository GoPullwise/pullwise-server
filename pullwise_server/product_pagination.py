"""Opaque local REST pagination bound to current authorized result identity."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Mapping, Sequence


def paged_items(rows: Sequence[dict], *, owner_id: str,
                restrictions: Mapping[str, object], filters: Mapping[str, object],
                limit: int, cursor: str | None, request_id: str) -> dict:
    return _page(rows, owner_id=owner_id, restrictions=restrictions,
        filters=filters, limit=limit, cursor=cursor, request_id=request_id,
        path="/api/v1/items", identities=[(row.get("id"), row.get("itemVersion"),
                                           row.get("revision")) for row in rows])


def paged_sources(rows: Sequence[dict], *, owner_id: str,
                  restrictions: Mapping[str, object], filters: Mapping[str, object],
                  limit: int, cursor: str | None, request_id: str) -> dict:
    identities = [(row.get("id"), row.get("sourceVersion"),
                   row.get("sourceRevision"), [
                       (context.get("id"), context.get("contextVersion"),
                        context.get("processingStatus"), context.get("contextStale"),
                        context.get("relevance"), context.get("updateSignals"))
                       for context in row.get("contexts") or ()]) for row in rows]
    return _page(rows, owner_id=owner_id, restrictions=restrictions,
        filters=filters, limit=limit, cursor=cursor, request_id=request_id,
        path="/api/v1/sources", identities=identities)


def paged_watches(rows: Sequence[dict], *, owner_id: str,
                  restrictions: Mapping[str, object], filters: Mapping[str, object],
                  limit: int, cursor: str | None, request_id: str) -> dict:
    identities = [(row.get("id"), row.get("revision"),
                   row.get("contextVersion"), row.get("enabled")) for row in rows]
    return _page(rows, owner_id=owner_id, restrictions=restrictions,
        filters=filters, limit=limit, cursor=cursor, request_id=request_id,
        path="/api/v1/watches", identities=identities)


def paged_repositories(rows: Sequence[dict], *, owner_id: str,
                       restrictions: Mapping[str, object], filters: Mapping[str, object],
                       limit: int, cursor: str | None, request_id: str) -> dict:
    identities = [(row.get("id"), row.get("githubRepoId"),
                   (row.get("service") or {}).get("revision"),
                   (row.get("service") or {}).get("enabled")) for row in rows]
    return _page(rows, owner_id=owner_id, restrictions=restrictions,
        filters=filters, limit=limit, cursor=cursor, request_id=request_id,
        path="/api/v1/repositories", identities=identities)


def _page(rows: Sequence[dict], *, owner_id: str,
          restrictions: Mapping[str, object], filters: Mapping[str, object],
          limit: int, cursor: str | None, request_id: str,
          path: str, identities: list) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("INVALID_CONFIGURATION")
    scope_filters = {key: (value[0] if isinstance(value, list) and value else value)
                     for key, value in filters.items() if key not in {"limit", "cursor"}}
    scope = _digest([owner_id, restrictions, scope_filters, limit, path])
    snapshot = _digest(identities)
    offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (not isinstance(decoded, dict) or set(decoded) != {"scope", "snapshot", "offset"}
                    or decoded["scope"] != scope or decoded["snapshot"] != snapshot
                    or type(decoded["offset"]) is not int
                    or not 1 <= decoded["offset"] < len(rows)):
                raise ValueError
            offset = decoded["offset"]
        except Exception as error:
            raise ValueError("INVALID_CURSOR") from error
    items = list(rows[offset:offset + limit])
    next_offset = offset + len(items)
    has_more = next_offset < len(rows)
    next_cursor = (_encode({"scope": scope, "snapshot": snapshot,
                            "offset": next_offset}) if has_more else None)
    return {"items": items, "nextCursor": next_cursor,
            "hasMore": has_more, "requestId": request_id}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode(value: dict) -> str:
    encoded = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")
