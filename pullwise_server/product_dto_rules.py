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
