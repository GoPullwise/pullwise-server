"""Project one saved PR issue comment assessment into a follow-up Item."""
from __future__ import annotations

import json
from typing import Mapping

from .pr_followup import _ACTIONS, _segments
from .product_dto_rules import iso_timestamp


def reconcile_pr_comment_item(store, connection, *, source: Mapping, fence: Mapping,
                              assessment: dict, coverage: dict, evidence: list[dict],
                              observed_at: int) -> str | None:
    source_id, context_id = source["sourceId"], fence["contextId"]
    record = connection.execute("SELECT * FROM source_records WHERE source_id=?", (source_id,)).fetchone()
    context = connection.execute(
        "SELECT * FROM source_contexts WHERE source_id=? AND context_id=?",
        (source_id, context_id)).fetchone()
    if record is None or record["source_type"] != "pr_comment" or context is None:
        raise ValueError("PR_COMMENT_ITEM_PROJECTION_INVALID")
    previous = connection.execute(
        "SELECT * FROM items WHERE context_id=? AND unit_type='pr_comment' AND unit_key=?",
        (context_id, source_id)).fetchone()
    if context["item_id"] is not None and (previous is None or previous["id"] != context["item_id"]):
        raise ValueError("STALE_CONTEXT")
    source_read = {"id": source_id, "sourceVersion": source["sourceVersion"]}
    segments, known = _segments(assessment, evidence, source_read)
    complete = (known and record["completeness"] == "complete"
                and coverage.get("state") == "complete"
                and coverage.get("rawSourcePartial") is False)
    actions: set[str] = set()
    item_evidence = []
    for values, rows in segments:
        labels = sorted(action for question, action in _ACTIONS.items()
                        if values.get(question) == "present")
        actions.update(labels)
        for row in rows:
            row["actionTypes"] = labels
            if values.get("completion_claim") == "present":
                row["progressType"] = "completion_claim"
            item_evidence.append(row)
    if not actions and previous is None:
        return None
    prior_snapshot = None
    if previous is not None:
        prior = connection.execute(
            "SELECT snapshot_json FROM item_versions WHERE item_id=? AND item_version=?",
            (previous["id"], previous["current_item_version"])).fetchone()
        prior_snapshot = json.loads(prior["snapshot_json"]) if prior else None
    if complete and not actions:
        lifecycle, attention, actions_out = "superseded", "closed", []
    elif not complete:
        lifecycle, attention = "active", "needs_confirmation"
        actions_out = sorted(actions | set((prior_snapshot or {}).get("actionTypes") or []))
        present = {row["id"] for row in item_evidence}
        item_evidence.extend({**row, "status": "expired"}
            for row in (prior_snapshot or {}).get("evidence", []) if row["id"] not in present)
    else:
        lifecycle, actions_out = "active", sorted(actions)
        attention = "optional" if actions == {"optional_suggestion"} else "needs_action"
    facts = json.loads(record["source_facts_json"] or "{}")
    facts["semanticComplete"] = bool(complete)
    actor = (facts.get("author") or {}).get("githubId")
    pull_author = (facts.get("pullAuthor") or {}).get("githubId")
    recipient = pull_author if actor and pull_author and actor != pull_author and actions_out else None
    snapshot = {"module": "pr", "repositoryId": record["repository_id"], "watchId": None,
        "actionTypes": actions_out, "title": "Follow up PR comment",
        "sourceUrl": record["source_url"], "sourceFacts": facts,
        "evidence": item_evidence, "assessments": [assessment],
        "nextActors": [{"kind": "user", "githubId": recipient}] if recipient else [],
        "lifecycle": lifecycle, "attentionState": attention,
        "closureReason": "superseded" if lifecycle != "active" else None,
        "attentionUpdatedAt": iso_timestamp(observed_at),
        "lastSyncedAt": iso_timestamp(int(record["last_synced_at"] or observed_at)),
        "semanticComplete": bool(complete), "coverage": coverage}
    item = store._item_dto(previous) if previous else store.create_item(
        context_id=context_id, unit_type="pr_comment", unit_key=source_id)
    store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
        sources=[source], context_fences=[fence], snapshot=snapshot, observed_at=observed_at)
    connection.execute("UPDATE source_contexts SET item_id=? WHERE source_id=? AND context_id=?",
        (item["id"], source_id, context_id))
    return item["id"]
