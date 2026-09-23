"""Pure projections shared by SQLite ProductStore and async D1 reads."""
from __future__ import annotations

import json
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
