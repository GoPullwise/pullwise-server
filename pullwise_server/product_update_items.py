"""Project a saved Release assessment into one fenced follow-up Item."""
from __future__ import annotations

import json
from typing import Mapping

from .product_dto_rules import iso_timestamp
from .update_filter import project_saved_updates


def reconcile_update_item(store, connection, *, source: Mapping, fence: Mapping,
                          assessment: dict, coverage: dict, evidence: list[dict],
                          observed_at: int) -> str | None:
    projection = project_saved_updates(assessment, coverage)
    if projection is None:
        raise ValueError("UPDATES_PROJECTION_INVALID")
    source_id, context_id = source["sourceId"], fence["contextId"]
    record = connection.execute("SELECT * FROM source_records WHERE source_id=?", (source_id,)).fetchone()
    context = connection.execute(
        "SELECT * FROM source_contexts WHERE source_id=? AND context_id=?",
        (source_id, context_id)).fetchone()
    if record is None or record["source_type"] != "release" or context is None or not context["watch_id"]:
        raise ValueError("UPDATES_ITEM_PROJECTION_INVALID")
    watch = connection.execute("SELECT * FROM update_watches WHERE id=?",
        (context["watch_id"],)).fetchone()
    if (watch is None or watch["archived_at"] is not None or not bool(watch["enabled"])
            or not bool(watch["analysis_enabled"])
            or watch["billing_owner_id"] != context["billing_owner_id"]
            or int(watch["context_version"]) != int(context["context_version"])):
        raise ValueError("STALE_CONTEXT")
    previous = connection.execute(
        "SELECT * FROM items WHERE context_id=? AND unit_type='update_release' AND unit_key=?",
        (context_id, source_id)).fetchone()
    if context["item_id"] is not None and (previous is None or previous["id"] != context["item_id"]):
        raise ValueError("STALE_CONTEXT")
    relevance = projection["relevance"]
    if relevance == "not_relevant" and previous is None:
        return None
    positives = sorted(key for key, value in projection["updateSignals"].items() if value == "present")
    if relevance == "not_relevant":
        attention, lifecycle, actions = "closed", "superseded", []
    elif relevance == "relevant" and positives:
        attention, lifecycle, actions = "needs_action", "active", ["review_update"]
    elif relevance == "relevant":
        attention, lifecycle, actions = "optional", "active", []
    else:
        attention, lifecycle = "needs_confirmation", "active"
        prior = connection.execute(
            "SELECT snapshot_json FROM item_versions WHERE item_id=? AND item_version=?",
            (previous["id"], previous["current_item_version"])).fetchone() if previous else None
        actions = list(json.loads(prior["snapshot_json"]).get("actionTypes") or []) if prior else []
    facts = json.loads(record["source_facts_json"] or "{}")
    facts["relevance"] = relevance
    facts["updateSignals"] = projection["updateSignals"]
    label = facts.get("name") or facts.get("tagName") or "Upstream release"
    snapshot = {"module": "updates", "repositoryId": watch["target_repository_id"],
        "watchId": context["watch_id"], "actionTypes": actions,
        "title": str(label), "sourceUrl": record["source_url"],
        "sourceFacts": facts, "evidence": evidence, "assessments": [assessment],
        "nextActors": [], "lifecycle": lifecycle, "attentionState": attention,
        "closureReason": "superseded" if lifecycle != "active" else None,
        "attentionUpdatedAt": iso_timestamp(observed_at),
        "lastSyncedAt": iso_timestamp(int(record["last_synced_at"] or observed_at)),
        "coverage": coverage}
    item = store._item_dto(previous) if previous else store.create_item(
        context_id=context_id, unit_type="update_release", unit_key=source_id)
    store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
        sources=[source], context_fences=[fence], snapshot=snapshot, observed_at=observed_at)
    connection.execute("UPDATE source_contexts SET item_id=? WHERE source_id=? AND context_id=?",
        (item["id"], source_id, context_id))
    return item["id"]
