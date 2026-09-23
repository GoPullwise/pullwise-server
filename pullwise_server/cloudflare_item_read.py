"""Read Item versions, source authority and handling in one D1 snapshot."""
from __future__ import annotations

import json
from typing import Any, Callable

from .product_dto_rules import handling_event_dto, iso_timestamp, item_read_dto, item_dependencies_current


class D1ItemReads:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    def statements(self, *, owner_id: str, now: int, include_history: bool = False, item_id: str | None = None) -> list:
        statements = [
            self.binding.prepare("""SELECT i.*,iv.sources_json,iv.context_fences_json,
                iv.snapshot_json,iv.observed_at
                FROM items i JOIN item_versions iv
                  ON iv.item_id=i.id AND iv.item_version=i.current_item_version
                WHERE EXISTS (SELECT 1 FROM source_contexts sc
                    WHERE sc.context_id=i.context_id AND sc.billing_owner_id=?
                      AND sc.accessible=1 AND sc.authorization_valid_until>=?)
                  AND (? IS NULL OR i.id=?)
                ORDER BY i.updated_at DESC,i.id""").bind(owner_id, now, item_id, item_id),
            self.binding.prepare("""SELECT sc.source_id,sc.context_id,sc.billing_owner_id,
                sc.accessible,sc.authorization_valid_until,sc.context_stale,
                sc.context_version,sc.configuration_revision,sc.authorization_revision,
                sr.latest_version,sr.source_revision,sr.last_synced_at
                FROM source_contexts sc JOIN source_records sr ON sr.source_id=sc.source_id
                WHERE sc.billing_owner_id=? AND sc.accessible=1
                  AND sc.authorization_valid_until>=?""").bind(owner_id, now),
            (self.binding.prepare("""SELECT * FROM item_handling_events
                WHERE (? IS NULL OR item_id=?) ORDER BY rowid""").bind(item_id, item_id)
             if include_history else self.binding.prepare("""SELECT h.* FROM item_handling_events h
                JOIN (SELECT item_id,MAX(rowid) AS ordering FROM item_handling_events
                      GROUP BY item_id) latest ON latest.item_id=h.item_id
                  AND h.rowid=latest.ordering""")),
        ]
        return statements

    async def list_items_for_billing_owner(self, *, owner_id: str, now: int,
                                           include_history: bool = False,
                                           item_id: str | None = None,
                                           auth_statements: list | None = None,
                                           validate_auth: Callable[[list], None] | None = None) -> list[dict]:
        if not isinstance(owner_id, str) or not owner_id or type(now) is not int:
            raise ValueError("invalid item read identity")
        if (auth_statements is None) != (validate_auth is None):
            raise ValueError("item snapshot authentication is incomplete")
        statements = self.statements(owner_id=owner_id, now=now, include_history=include_history, item_id=item_id)
        auth_count = len(auth_statements or ())
        result = await self.binding.batch([*(auth_statements or ()), *statements])
        if validate_auth is not None:
            validate_auth([part.results for part in result[:auth_count]])
        return self.project_snapshot(result[auth_count:], owner_id=owner_id, now=now, include_history=include_history)

    @staticmethod
    def project_snapshot(result: list, *, owner_id: str, now: int, include_history: bool = False) -> list[dict]:
        item_rows, readable_rows, handling_rows = (part.results for part in result)
        context_rows = {(row["source_id"], row["context_id"]): row for row in readable_rows}
        sync_times = {key: row["last_synced_at"] for key, row in context_rows.items()}
        latest = {}
        history = {}
        for event in handling_rows:
            latest[event["item_id"]] = event
            if include_history:
                history.setdefault(event["item_id"], []).append(handling_event_dto(event))
        items = []
        for row in item_rows:
            sources = json.loads(row["sources_json"])
            if any((source["sourceId"], row["context_id"]) not in sync_times
                   for source in sources):
                continue
            if not item_dependencies_current(sources, json.loads(row["context_fences_json"]),
                                             context_rows, context_id=row["context_id"],
                                             owner_id=owner_id, now=now):
                continue
            item = item_read_dto(row, sources, json.loads(row["snapshot_json"]),
                                 latest.get(row["id"]))
            if include_history:
                item["handlingHistory"] = history.get(row["id"], [])
            synced = [sync_times[(source["sourceId"], row["context_id"])]
                      for source in sources]
            if synced and all(value is not None for value in synced):
                item["lastSyncedAt"] = iso_timestamp(min(synced))
            items.append(item)
        return items
