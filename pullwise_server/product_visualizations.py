"""Read-only visualizations over already-authorized product projections."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Mapping

from .product_dto_rules import iso_timestamp
from .product_item_filters import CI_STAGES, CI_SYMPTOMS


ATTENTION_STATES = ("needs_action", "needs_confirmation", "waiting", "optional", "closed")
MODULES = ("pr", "ci", "updates")
PR_ACTIONS = ("change_requested", "reply_needed", "review_requested",
              "optional_suggestion", "confirm_next_step")
UPDATE_SIGNALS = ("migration_stated", "deprecation_stated",
                  "breaking_change_stated", "security_fix_stated")
WORKLOAD_FILTERS = ("module", "repositoryId", "watchId", "view", "attentionState",
                    "actionType", "lifecycle", "disposition", "pullNumber", "runId",
                    "q")


def workload_visualization(items: list[dict], sources: list[dict],
                           params: Mapping[str, object], *, now: int,
                           request_id: str) -> dict:
    """Item counts use the same filtered collection as GET /items."""
    scope = {name: value for name in WORKLOAD_FILTERS
             if (value := _query_value(params, name))}
    scope.setdefault("view", "all")
    distinct = {item["id"]: item for item in items}
    selected = list(distinct.values())
    modules = (scope["module"],) if scope.get("module") else MODULES
    rows = []
    for module in modules:
        module_items = [item for item in selected if item.get("module") == module]
        cells = []
        for state in ATTENTION_STATES:
            filters = {**scope, "module": module, "attentionState": state}
            cells.append({"key": state,
                "count": sum(item.get("attentionState") == state for item in module_items),
                "drilldown": {"resource": "items", "filters": filters}})
        rows.append({"key": module, "totalCount": len(module_items), "cells": cells})
    contexts = [context for source in sources for context in source.get("contexts") or ()]
    if not sources:
        sync_state = "unavailable"
    elif any(context.get("contextStale") for context in contexts):
        sync_state = "stale"
    elif any(source.get("completeness") != "complete" for source in sources):
        sync_state = "partial"
    else:
        sync_state = "complete"
    if not contexts:
        analysis_state = "unavailable"
    elif all(not context.get("analysisEnabled") for context in contexts):
        analysis_state = "disabled"
    elif all(context.get("processingStatus") in {"assessed", "rules_only"}
             for context in contexts):
        analysis_state = "complete"
    else:
        analysis_state = "partial"
    return {"kind": "workload", "scope": scope, "generatedAt": iso_timestamp(now),
        "lastSyncedAt": max((source.get("lastSyncedAt") for source in sources
                              if source.get("lastSyncedAt")), default=None),
        "countUnit": "item", "totalCount": len(selected),
        "coverage": {"syncState": sync_state, "analysisState": analysis_state,
                     "limitations": ["discovered_visible_sources_only"]},
        "data": {"columns": [{"key": state} for state in ATTENTION_STATES],
                 "rows": rows},
        "nextCursor": None, "hasMore": False, "requestId": request_id}


def pr_actions_visualization(items: list[dict], sources: list[dict],
                             params: Mapping[str, object], *, owner_id: str,
                             now: int, request_id: str,
                             visibility_key: str = "") -> dict:
    """Page PR rows while preserving totals over the complete authorized set."""
    if _query_value(params, "module") not in {"", "pr"}:
        raise ValueError("INVALID_CONFIGURATION")
    raw_limit = _query_value(params, "limit") or "20"
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 50:
        raise ValueError("INVALID_CONFIGURATION")
    limit = int(raw_limit)
    scope = {name: value for name in WORKLOAD_FILTERS
             if (value := _query_value(params, name))}
    scope["module"] = "pr"
    scope.setdefault("view", "all")
    action_scope = scope.get("actionType")
    if action_scope and action_scope not in PR_ACTIONS:
        raise ValueError("INVALID_CONFIGURATION")
    actions = (action_scope,) if action_scope else PR_ACTIONS
    scope_hash = hashlib.sha256(json.dumps([owner_id, visibility_key, scope, limit],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cursor = _query_value(params, "cursor")
    offset = 0
    if cursor:
        try:
            if len(cursor) > 256:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (not isinstance(data, dict) or set(data) != {"scope", "offset"}
                    or data["scope"] != scope_hash or type(data["offset"]) is not int
                    or data["offset"] < 1):
                raise ValueError
            offset = data["offset"]
        except Exception as error:
            raise ValueError("INVALID_CURSOR") from error
    distinct = {item["id"]: item for item in items}
    grouped: dict[tuple[str, int], list[dict]] = {}
    for item in distinct.values():
        if item.get("module") != "pr":
            continue
        repository_id = item.get("repositoryId")
        facts = item.get("sourceFacts") or {}
        number = facts.get("pullNumber") if isinstance(facts, Mapping) else None
        if (not isinstance(repository_id, str) or not repository_id
                or type(number) is not int or number < 1):
            raise ValueError("INVALID_CONFIGURATION")
        grouped.setdefault((repository_id, number), []).append(item)
    keys = sorted(grouped)
    if offset > len(keys):
        raise ValueError("INVALID_CURSOR")
    rows = []
    for repository_id, number in keys[offset:offset + limit]:
        row_items = grouped[(repository_id, number)]
        cells = []
        for action in actions:
            filters = {**scope, "repositoryId": repository_id,
                       "pullNumber": str(number), "actionType": action}
            cells.append({"key": action,
                "count": sum(action in (item.get("actionTypes") or ()) for item in row_items),
                "drilldown": {"resource": "items", "filters": filters}})
        rows.append({"key": f"{repository_id}:{number}",
            "repositoryId": repository_id, "pullNumber": number,
            "totalCount": len(row_items), "cells": cells})
    next_offset = offset + len(rows)
    has_more = next_offset < len(keys)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({"scope": scope_hash,
        "offset": next_offset}, separators=(",", ":")).encode()).decode().rstrip("=")
        if has_more else None)
    return {"kind": "pr_actions", "scope": scope, "generatedAt": iso_timestamp(now),
        "lastSyncedAt": max((source.get("lastSyncedAt") for source in sources
                              if source.get("lastSyncedAt")), default=None),
        "countUnit": "item", "totalCount": len(distinct),
        "coverage": {"syncState": "unavailable" if not sources else "partial",
                     "analysisState": "unavailable" if not sources else "partial",
                     "limitations": ["discovered_visible_sources_only"]},
        "data": {"rowsTotal": len(keys),
                 "columns": [{"key": action} for action in actions], "rows": rows},
        "nextCursor": next_cursor, "hasMore": has_more, "requestId": request_id}


def ci_failures_visualization(items: list[dict], sources: list[dict],
                              params: Mapping[str, object], *, now: int,
                              request_id: str) -> dict:
    """Count saved failed job attempts, pairing labels within each window."""
    if _query_value(params, "module") not in {"", "ci"}:
        raise ValueError("INVALID_CONFIGURATION")
    scope = {name: value for name in (*WORKLOAD_FILTERS, "ciStage", "ciSymptom",
                                      "classificationState")
             if (value := _query_value(params, name))}
    scope["module"] = "ci"
    scope.setdefault("view", "all")
    if (scope.get("ciStage") and scope["ciStage"] not in CI_STAGES
            or scope.get("ciSymptom") and scope["ciSymptom"] not in CI_SYMPTOMS):
        raise ValueError("INVALID_CONFIGURATION")
    jobs: dict[tuple[str, str, int, str], dict] = {}
    for item in items:
        if item.get("module") != "ci":
            continue
        facts = item.get("sourceFacts") or {}
        identity = (item.get("repositoryId"), facts.get("runId"),
                    facts.get("runAttempt"), facts.get("jobId"))
        if (not all(isinstance(value, str) and value for value in
                    (identity[0], identity[1], identity[3]))
                or type(identity[2]) is not int or identity[2] < 1):
            raise ValueError("INVALID_CONFIGURATION")
        if identity in jobs and jobs[identity]["id"] != item["id"]:
            raise ValueError("INVALID_CONFIGURATION")
        jobs[identity] = item

    def matching_pairs(item: dict) -> set[tuple[str, str]]:
        facts = item.get("sourceFacts") or {}
        windows = facts.get("windows") if isinstance(facts, Mapping) else None
        pairs = set()
        for window in windows if isinstance(windows, list) else ():
            if not isinstance(window, Mapping):
                continue
            stage = window.get("stage")
            if stage not in CI_STAGES or scope.get("ciStage") not in (None, stage):
                continue
            for symptom in window.get("symptoms") or ():
                if symptom in CI_SYMPTOMS and scope.get("ciSymptom") in (None, symptom):
                    pairs.add((stage, symptom))
        return pairs

    pairs_by_job = {identity: matching_pairs(item) for identity, item in jobs.items()}
    def has_any_symptom(item: dict) -> bool:
        facts = item.get("sourceFacts") or {}
        windows = facts.get("windows") if isinstance(facts, Mapping) else None
        return any(isinstance(window, Mapping) and any(
            symptom in CI_SYMPTOMS for symptom in (window.get("symptoms") or ()))
            for window in (windows if isinstance(windows, list) else ()))
    cells = []
    for stage in CI_STAGES:
        for symptom in CI_SYMPTOMS:
            filters = {**scope, "ciStage": stage, "ciSymptom": symptom}
            cells.append({"rowKey": stage, "columnKey": symptom,
                "count": sum((stage, symptom) in pairs for pairs in pairs_by_job.values()),
                "drilldown": {"resource": "items", "filters": filters}})
    unclassified = sum(not has_any_symptom(item) for item in jobs.values())
    unclassified_drilldown = (None if scope.get("classificationState") == "identified"
        or scope.get("ciSymptom") else {"resource": "items",
            "filters": {**scope, "classificationState": "unclassified"}})
    return {"kind": "ci_failures", "scope": scope,
        "generatedAt": iso_timestamp(now),
        "lastSyncedAt": max((source.get("lastSyncedAt") for source in sources
                              if source.get("lastSyncedAt")), default=None),
        "countUnit": "ci_job_attempt", "totalCount": len(jobs),
        "coverage": {"syncState": "unavailable" if not sources else "partial",
                     "analysisState": "unavailable" if not sources else "partial",
                     "limitations": ["discovered_visible_sources_only"]},
        "data": {"rows": [{"key": stage} for stage in CI_STAGES],
                 "columns": [{"key": symptom} for symptom in CI_SYMPTOMS],
                 "cells": cells, "unclassifiedCount": unclassified,
                 "unclassifiedDrilldown": unclassified_drilldown},
        "nextCursor": None, "hasMore": False, "requestId": request_id}


def updates_releases_visualization(sources: list[dict], params: Mapping[str, object],
                                   *, owner_id: str, now: int, request_id: str,
                                   visibility_key: str = "") -> dict:
    """One row per current Release and visible watch context, including null labels."""
    if _query_value(params, "module") not in {"", "updates"}:
        raise ValueError("INVALID_CONFIGURATION")
    raw_limit = _query_value(params, "limit") or "20"
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 50:
        raise ValueError("INVALID_CONFIGURATION")
    limit = int(raw_limit)
    scope = {name: value for name in ("repositoryId", "watchId", "releaseId",
             "relevance", "updateSignal", "processingStatus")
             if (value := _query_value(params, name))}
    scope["module"] = "updates"
    scope_hash = hashlib.sha256(json.dumps([owner_id, visibility_key, scope, limit],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cursor = _query_value(params, "cursor")
    offset = 0
    if cursor:
        try:
            if len(cursor) > 256:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (not isinstance(data, dict) or set(data) != {"scope", "offset"}
                    or data["scope"] != scope_hash or type(data["offset"]) is not int
                    or data["offset"] < 1):
                raise ValueError
            offset = data["offset"]
        except Exception as error:
            raise ValueError("INVALID_CURSOR") from error
    rows = []
    seen = set()
    for source in sources:
        if source.get("type") != "release":
            continue
        facts = source.get("sourceFacts") or {}
        release_id = str(facts.get("releaseId") or "")
        for context in source.get("contexts") or ():
            watch_id = context.get("watchId")
            identity = (source.get("id"), context.get("id"))
            if (not all(isinstance(value, str) and value for value in identity)
                    or not isinstance(watch_id, str) or not watch_id
                    or identity in seen):
                raise ValueError("INVALID_CONFIGURATION")
            seen.add(identity)
            stale = bool(context.get("contextStale"))
            states = {} if stale else context.get("updateSignals") or {}
            units = () if stale else context.get("units") or ()
            evidence_ids = sorted({evidence_id for unit in units
                if isinstance(unit, Mapping) for evidence_id in unit.get("evidenceIds") or ()
                if isinstance(evidence_id, str) and evidence_id})
            signal_evidence = {signal: sorted({evidence_id for unit in units
                if isinstance(unit, Mapping) and unit.get("relevance") == "relevant"
                and (unit.get("updateSignals") or {}).get(signal) == "present"
                for evidence_id in unit.get("evidenceIds") or ()
                if isinstance(evidence_id, str) and evidence_id})
                for signal in UPDATE_SIGNALS}
            rows.append({"sourceId": source["id"], "contextId": context["id"],
                "watchId": watch_id, "upstreamRepositoryId": source.get("repositoryId"),
                "releaseId": release_id, "tagName": facts.get("tagName"),
                "title": facts.get("name") or facts.get("title"),
                "publishedAt": facts.get("publishedAt"),
                "sourceUrl": source.get("sourceUrl"),
                "itemId": None if stale else context.get("itemId"),
                "contextVersion": context.get("contextVersion"),
                "processingStatus": context.get("processingStatus"),
                "contextStale": stale,
                "coverage": context.get("coverage"),
                "relevance": None if stale else context.get("relevance"),
                "updateSignals": {signal: states.get(signal) for signal in UPDATE_SIGNALS},
                "evidenceIds": evidence_ids, "signalEvidenceIds": signal_evidence,
                "drilldown": {"resource": "sources", "filters": {
                    "module": "updates", "watchId": watch_id,
                    **({"releaseId": release_id} if release_id else {})}}})
    rows.sort(key=lambda row: (row["publishedAt"] or "", row["sourceId"],
                               row["contextId"]), reverse=True)
    if offset > len(rows):
        raise ValueError("INVALID_CURSOR")
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(rows)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({"scope": scope_hash,
        "offset": next_offset}, separators=(",", ":")).encode()).decode().rstrip("=")
        if has_more else None)
    return {"kind": "updates_releases", "scope": scope,
        "generatedAt": iso_timestamp(now),
        "lastSyncedAt": max((source.get("lastSyncedAt") for source in sources
                              if source.get("lastSyncedAt")), default=None),
        "countUnit": "release_watch", "totalCount": len(rows),
        "coverage": {"syncState": "unavailable" if not sources else
                     "partial" if any(source.get("completeness") != "complete"
                                      for source in sources) else "complete",
                     "analysisState": "unavailable" if not rows else
                     "partial" if any(row["relevance"] is None or
                                      (row["coverage"] or {}).get("state") != "complete"
                                      for row in rows) else "complete",
                     "limitations": ["discovered_visible_sources_only"]},
        "data": {"rows": page, "columns": [{"key": signal} for signal in UPDATE_SIGNALS]},
        "nextCursor": next_cursor, "hasMore": has_more, "requestId": request_id}


def _query_value(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()
