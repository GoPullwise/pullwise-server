"""Pure projections shared by SQLite ProductStore and async D1 reads."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping


def watch_dto(row: Mapping[str, object]) -> dict:
    return {
        "id": row["id"],
        "watchScopeKey": row["watch_scope_key"],
        "ownerId": row["owner_id"],
        "targetRepositoryId": row["target_repository_id"],
        "upstreamRepositoryId": row["upstream_repository_id"],
        "billingOwnerId": row["billing_owner_id"],
        "contextVersion": int(row["context_version"]),
        "contextHash": row["context_hash"],
        "interests": json.loads(row["interests_json"]),
        "includePrerelease": bool(row["include_prerelease"]),
        "priorityOrder": int(row["priority_order"]),
        "enabled": bool(row["enabled"]),
        "analysisEnabled": bool(row["analysis_enabled"]),
        "revision": int(row["revision"]),
    }


def repository_service_dto(row: Mapping[str, object]) -> dict:
    return {"repositoryId": row["repository_id"],
        "installationId": row["installation_id"],
        "billingOwnerId": row["billing_owner_id"],
        "enabled": bool(row["enabled"]),
        "modules": json.loads(row["modules_json"]),
        "analysisEnabled": json.loads(row["analysis_enabled_json"]),
        "allowMemberSync": bool(row["allow_member_sync"]),
        "defaultAssigneeId": row["default_assignee_id"],
        "priorityOrder": int(row["priority_order"]),
        "status": row["status"],
        "revision": int(row["revision"])}


def iso_timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def source_context_dto(row: Mapping[str, object]) -> dict:
    return {
        "id": row["context_id"],
        "watchId": row["watch_id"],
        "targetRepositoryId": row["target_repository_id"],
        "itemId": row["item_id"],
        "contextVersion": int(row["context_version"]),
        "processingStatus": row["processing_status"],
        "analysisEnabled": bool(row["analysis_enabled"]),
        "contextStale": bool(row["context_stale"]),
        "coverage": json.loads(row["coverage_json"]),
    }


def source_record_dto(row: Mapping[str, object], contexts: list[dict]) -> dict:
    updated_at = int(row["updated_at"])
    last_synced_at = int(row["last_synced_at"] or updated_at)
    return {
        "id": row["source_id"],
        "type": row["source_type"],
        "repositoryId": row["repository_id"],
        "sourceVersion": row["latest_version"],
        "sourceRevision": int(row["source_revision"]),
        "processingMode": row["processing_mode"],
        "completeness": row["completeness"],
        "sourceFacts": json.loads(row["source_facts_json"] or "{}"),
        "sourceUrl": row["source_url"],
        "lifecycle": row["lifecycle"],
        "updatedAt": iso_timestamp(updated_at),
        "lastSyncedAt": iso_timestamp(last_synced_at),
        "contexts": contexts,
    }


def handling_event_dto(row: Mapping[str, object]) -> dict:
    return {
        "id": row["id"], "itemId": row["item_id"],
        "itemVersion": int(row["item_version"]), "actorId": row["actor_id"],
        "disposition": row["disposition"], "assigneeId": row["assignee_id"],
        "note": row["note"], "feedback": row["feedback"],
        "eventKind": row["event_kind"],
        "carriedFromItemVersion": row["carried_from_item_version"],
        "createdAt": int(row["created_at"]),
    }


def item_read_dto(row: Mapping[str, object], sources: list[dict],
                  snapshot: dict, handling_event: Mapping[str, object] | None) -> dict:
    handling = {"disposition": "open", "assigneeId": None, "note": None,
                "feedback": None, "carriedFromItemVersion": None}
    if handling_event is not None and handling_event["item_version"] == row["current_item_version"]:
        handling = {"disposition": handling_event["disposition"],
            "assigneeId": handling_event["assignee_id"], "note": handling_event["note"],
            "feedback": handling_event["feedback"],
            "carriedFromItemVersion": handling_event["carried_from_item_version"]}
    lifecycle = snapshot.get("lifecycle") or "active"
    attention = snapshot.get("attentionState") or "needs_confirmation"
    closure = snapshot.get("closureReason")
    if lifecycle != "active":
        attention, closure = "closed", lifecycle
    elif closure in {"same_run_retry_succeeded", "later_run_succeeded"}:
        attention = "closed"
    elif handling["disposition"] in {"done", "dismissed"}:
        attention, closure = "closed", "handled_" + handling["disposition"]
    return {
        "id": row["id"], "module": snapshot.get("module"),
        "repositoryId": snapshot.get("repositoryId"), "watchId": snapshot.get("watchId"),
        "unit": {"type": row["unit_type"], "externalId": row["unit_key"]},
        "itemVersion": int(row["current_item_version"]), "sources": sources,
        "actionTypes": snapshot.get("actionTypes") or [], "title": snapshot.get("title") or "",
        "sourceUrl": snapshot.get("sourceUrl"), "sourceFacts": snapshot.get("sourceFacts") or {},
        "evidence": snapshot.get("evidence") or [],
        "assessments": snapshot.get("assessments") or [],
        "nextActors": snapshot.get("nextActors") or [], "lifecycle": lifecycle,
        "attentionState": attention, "handling": handling, "closureReason": closure,
        "revision": int(row["revision"]),
        "attentionUpdatedAt": snapshot.get("attentionUpdatedAt") or iso_timestamp(int(row["observed_at"])),
        "updatedAt": iso_timestamp(int(row["updated_at"])),
        "lastSyncedAt": snapshot.get("lastSyncedAt") or iso_timestamp(int(row["observed_at"])),
    }


def item_dependencies_current(sources: list[dict], fences: list[dict],
                              contexts: Mapping[tuple[str, str], Mapping[str, object]],
                              *, context_id: str, owner_id: str, now: int) -> bool:
    if not sources or len(fences) != len(sources):
        return False
    fence_by_source = {fence.get("sourceId"): fence for fence in fences}
    if len(fence_by_source) != len(sources):
        return False
    for source in sources:
        source_id = source.get("sourceId")
        row = contexts.get((source_id, context_id))
        fence = fence_by_source.get(source_id)
        if (row is None or fence is None or fence.get("contextId") != context_id
                or row["billing_owner_id"] != owner_id or not row["accessible"]
                or row["authorization_valid_until"] < now or row["context_stale"]
                or row["latest_version"] != source.get("sourceVersion")
                or row["source_revision"] != source.get("sourceRevision")
                or row["context_version"] != fence.get("contextVersion")
                or row["authorization_revision"] != fence.get("authorizationRevision")):
            return False
    return True
