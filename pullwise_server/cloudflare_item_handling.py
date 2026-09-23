"""Guard a user Item handling event and Item revision in one D1 batch."""
from __future__ import annotations

import json
import uuid
from typing import Any


def _handling_values(body: dict, previous: dict | None) -> tuple:
    disposition = body.get("disposition", previous["disposition"] if previous else "open")
    assignee = body.get("assigneeId", previous["assigneeId"] if previous else None)
    note = body.get("note", previous["note"] if previous else None)
    feedback = body.get("feedback", previous["feedback"] if previous else None)
    if disposition not in {"open", "done", "dismissed"}:
        raise ValueError("invalid disposition")
    if assignee is not None and (not isinstance(assignee, str) or not assignee.strip()):
        raise ValueError("invalid assignee_id")
    if note is not None and (not isinstance(note, str) or len(note) > 2048):
        raise ValueError("note must be null or at most 2048 characters")
    if feedback not in {None, "classification_inaccurate"}:
        raise ValueError("invalid feedback")
    return disposition, assignee.strip() if assignee is not None else None, note, feedback


class D1ItemHandling:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def patch(self, *, item: dict, owner_id: str, body: dict,
                    expected_revision: int, now: int, proof: dict) -> None:
        previous = item.get("handlingHistory", [])[-1] if item.get("handlingHistory") else None
        disposition, assignee, note, feedback = _handling_values(body, previous)
        version = item["itemVersion"]
        row = await self.binding.prepare("""SELECT sources_json,context_fences_json
            FROM item_versions WHERE item_id=? AND item_version=?""").bind(
                item["id"], version).first()
        if row is None:
            raise ValueError("STALE_ITEM")
        sources = json.loads(row["sources_json"])
        fences = json.loads(row["context_fences_json"])
        fence_by_source = {fence["sourceId"]: fence for fence in fences}
        if not sources or len(fence_by_source) != len(sources):
            raise ValueError("STALE_ITEM")
        checks = ["EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u "
                  "WHERE a.name='users' AND u.key=? AND u.value=?)"]
        params: list = [owner_id, proof["user"]]
        if proof["token"]:
            key = proof["key"]
            checks.append("""EXISTS(SELECT 1 FROM api_keys WHERE key_hash=?
                AND user_id=? AND scopes=? AND restrictions=?
                AND expires_at IS ? AND revoked_at IS NULL)""")
            import hashlib
            params.extend((hashlib.sha256(proof["token"].encode()).hexdigest(),
                           owner_id, key["scopes"], key["restrictions"], key["expires_at"]))
        else:
            checks.append("EXISTS(SELECT 1 FROM app_state WHERE name='sessions' AND payload=?)")
            params.append(proof["sessions"])
        context_id = None
        for source in sources:
            fence = fence_by_source.get(source["sourceId"])
            if fence is None:
                raise ValueError("STALE_ITEM")
            if context_id is None:
                context_id = fence["contextId"]
            elif context_id != fence["contextId"]:
                raise ValueError("STALE_ITEM")
            checks.append("""EXISTS(SELECT 1 FROM source_contexts sc
                JOIN source_records sr ON sr.source_id=sc.source_id
                WHERE sc.source_id=? AND sc.context_id=? AND sc.billing_owner_id=?
                  AND sc.accessible=1 AND sc.context_stale=0
                  AND sc.authorization_valid_until>=?
                  AND sr.latest_version=? AND sr.source_revision=?
                  AND sc.context_version=? AND sc.configuration_revision=?
                  AND sc.authorization_revision=?)""")
            params.extend((source["sourceId"], fence["contextId"], owner_id, now,
                           source["sourceVersion"], source["sourceRevision"],
                           fence["contextVersion"], fence["configurationRevision"],
                           fence["authorizationRevision"]))
        event_id = f"handling_{uuid.uuid4().hex}"
        update = self.binding.prepare("""UPDATE items SET revision=revision+1,updated_at=?
            WHERE id=? AND revision=? AND current_item_version=? AND context_id=? AND """
            + " AND ".join(checks)).bind(
                now, item["id"], expected_revision, version, context_id, *params)
        guard = self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)""")
        event = self.binding.prepare("""INSERT INTO item_handling_events(
            id,item_id,item_version,actor_id,disposition,assignee_id,note,feedback,
            event_kind,carried_from_item_version,created_at)
            VALUES(?,?,?,?,?,?,?,?,'user_update',NULL,?)""").bind(
                event_id, item["id"], version, owner_id, disposition, assignee,
                note, feedback, now)
        clear = self.binding.prepare("DELETE FROM d1_command_guard")
        await self.binding.batch([update, guard, event, clear])
