"""Conservative, deterministic candidates from authorized saved CI Items."""

from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from typing import Mapping, Sequence


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ERROR = re.compile(r"^(?:[\w.]+(?:Error|Exception)|error|fatal):\s*\S", re.I)


def error_signature(item: Mapping) -> str | None:
    """Return a readable normalized error line only when it is unambiguous."""
    windows = item.get("sourceFacts", {}).get("windows")
    if not isinstance(windows, list) or not windows:
        return None
    window_ids = {row["windowId"] for row in windows if isinstance(row, Mapping)
                  and isinstance(row.get("windowId"), str)}
    signatures = set()
    for evidence in item.get("evidence") or []:
        if (not isinstance(evidence, Mapping) or evidence.get("status", "available") != "available"
                or evidence.get("segmentAnchor") not in window_ids):
            continue
        value = evidence.get("text")
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            normalized = " ".join(_ANSI.sub("", line).strip().split())
            if _ERROR.match(normalized) and len(normalized) <= 500:
                signatures.add(normalized)
    return next(iter(signatures)) if len(signatures) == 1 else None


def _identity(item: Mapping) -> tuple | None:
    facts = item.get("sourceFacts") or {}
    repository = item.get("repositoryId")
    workflow = facts.get("workflowId")
    job = facts.get("jobName")
    run = facts.get("runId")
    job_id = facts.get("jobId")
    if (item.get("module") != "ci" or not all(isinstance(x, str) and x.strip()
            for x in (repository, workflow, job, run, job_id))):
        return None
    matrix = facts.get("matrix")
    if matrix is not None and not isinstance(matrix, Mapping):
        return None
    return repository, workflow, job, json.dumps(matrix, sort_keys=True) if matrix is not None else None


def _completion(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo == timezone.utc else None
    except ValueError:
        return None


def history_candidates(current: Mapping, visible_items: Sequence[Mapping]) -> list[dict]:
    """Caller supplies only currently authorized Items, including history."""
    identity, signature = _identity(current), error_signature(current)
    if identity is None or signature is None:
        return []
    current_completed = _completion(current["sourceFacts"].get("completedAt"))
    if current_completed is None:
        return []
    candidates = []
    for previous in visible_items:
        if (previous.get("id") == current.get("id") or _identity(previous) != identity
                or (error_signature(previous) or "").casefold() != signature.casefold()):
            continue
        current_facts, facts = current["sourceFacts"], previous["sourceFacts"]
        if facts["runId"] == current_facts["runId"] or facts["jobId"] == current_facts["jobId"]:
            continue
        previous_completed = _completion(facts.get("completedAt"))
        if previous_completed is None or previous_completed >= current_completed:
            continue
        # A saved handling event must be an explicit human update. Carried state
        # is not evidence that this previous failure was handled.
        events = [event for event in previous.get("handlingHistory") or []
                  if event.get("eventKind") == "user_update"
                  and type(event.get("createdAt")) is int
                  and event["createdAt"] < current_completed.timestamp()
                  and (event.get("note") or event.get("disposition") in {"done", "dismissed"})]
        if not events:
            continue
        event = events[-1]
        url = previous.get("sourceUrl")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            continue
        candidates.append({"itemId": previous["id"],
                           "sourceUrl": url, "relation": "same_observed_symptom",
                           "disposition": event["disposition"], "note": event.get("note"),
                           "handledAt": event.get("createdAt")})
    return sorted(candidates, key=lambda row: (row["handledAt"] or 0, row["itemId"]), reverse=True)[:5]
