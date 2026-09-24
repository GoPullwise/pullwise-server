"""Timeline projection from persisted Item versions and handling events only."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Mapping

from .product_dto_rules import iso_timestamp


def item_timeline(item: dict, versions: list[Mapping], *, owner_id: str,
                  visibility_key: str, limit: int, cursor: str | None,
                  request_id: str) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("INVALID_CONFIGURATION")
    revision = item["revision"]
    scope = hashlib.sha256(json.dumps([owner_id, visibility_key, item["id"],
        item["itemVersion"], revision, limit], separators=(",", ":")).encode()).hexdigest()
    offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 256:
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (not isinstance(value, dict) or set(value) != {"scope", "offset"}
                    or value["scope"] != scope or type(value["offset"]) is not int
                    or value["offset"] < 1):
                raise ValueError
            offset = value["offset"]
        except Exception as error:
            raise ValueError("INVALID_CURSOR") from error
    current_sources = {source["sourceId"] for source in item["sources"]}
    events = []
    limitations = set()
    seen_assessments = set()
    for version in versions:
        snapshot = json.loads(version["snapshot_json"])
        historical_sources = json.loads(version["sources_json"])
        source_refs = [source for source in historical_sources
                       if source["sourceId"] in current_sources]
        if len(source_refs) != len(historical_sources):
            limitations.add("historical_source_not_current")
        when = int(version["observed_at"])
        number = int(version["item_version"])
        events.append({"id": f"{item['id']}:iv:{number}:observed", "itemId": item["id"],
            "itemVersion": number, "sourceKind": "github",
            "eventType": "snapshot_observed", "occurredAt": None,
            "observedAt": iso_timestamp(when), "timeBasis": "observed",
            "sourceRefs": source_refs, "evidenceIds": [], "actor": None,
            "_sort": (when, number, 0)})
        for assessment in snapshot.get("assessments") or ():
            if not isinstance(assessment, Mapping):
                continue
            assessment_id = assessment.get("id")
            if not isinstance(assessment_id, str) or not assessment_id or assessment_id in seen_assessments:
                continue
            seen_assessments.add(assessment_id)
            bindings = assessment.get("bindings") or {}
            evidence_ids = sorted({identifier for binding in bindings.values()
                if isinstance(binding, Mapping)
                for identifier in binding.get("evidenceIds") or ()
                if isinstance(identifier, str) and identifier})
            events.append({"id": f"{item['id']}:assessment:{assessment_id}",
                "itemId": item["id"], "itemVersion": number,
                "sourceKind": "model", "eventType": "assessed",
                "occurredAt": None, "observedAt": iso_timestamp(when),
                "timeBasis": "observed", "sourceRefs": source_refs,
                "evidenceIds": evidence_ids, "actor": {"kind": "system", "id": "system"},
                "_sort": (when, number, 1)})
    previous = {"disposition": "open", "assigneeId": None, "feedback": None}
    for order, handling in enumerate(item.get("handlingHistory") or ()):
        when = int(handling["createdAt"])
        event_kind = handling.get("eventKind")
        changes = (["disposition_carried_forward"] if event_kind == "handling_carried" else
            [event_type for field, event_type in (
                ("disposition", "disposition_changed"),
                ("assigneeId", "assignee_changed"),
                ("feedback", "feedback_changed"))
             if handling.get(field) != previous.get(field)])
        actor_id = handling.get("actorId")
        actor = ({"kind": "system", "id": "system"} if actor_id == "system" else
                 {"kind": "user", "id": actor_id} if actor_id else None)
        for index, event_type in enumerate(changes):
            events.append({"id": f"{handling['id']}:{event_type}",
                "itemId": item["id"], "itemVersion": handling["itemVersion"],
                "sourceKind": "handling", "eventType": event_type,
                "occurredAt": iso_timestamp(when), "observedAt": iso_timestamp(when),
                "timeBasis": "source", "sourceRefs": [], "evidenceIds": [],
                "actor": actor, "_sort": (when, handling["itemVersion"], 2 + order * 3 + index)})
        previous = handling
    events.sort(key=lambda event: event["_sort"])
    if offset > len(events):
        raise ValueError("INVALID_CURSOR")
    page = [{key: value for key, value in event.items() if key != "_sort"}
            for event in events[offset:offset + limit]]
    next_offset = offset + len(page)
    has_more = next_offset < len(events)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({"scope": scope,
        "offset": next_offset}, separators=(",", ":")).encode()).decode().rstrip("=")
        if has_more else None)
    relations = []
    facts = item.get("sourceFacts")
    facts = facts if isinstance(facts, Mapping) else {}
    recovery = facts.get("recovery")
    if (facts.get("recoveryStatus") == "verified" and isinstance(recovery, Mapping)
            and recovery.get("kind") in {"same_run_retry_succeeded", "later_run_succeeded"}
            and isinstance(recovery.get("runId"), str) and recovery["runId"]
            and type(recovery.get("runAttempt")) is int and recovery["runAttempt"] > 0
            and isinstance(recovery.get("jobId"), str) and recovery["jobId"]
            and isinstance(recovery.get("matchRuleVersion"), str)
            and recovery["matchRuleVersion"] and versions):
        from_id = f"{item['id']}:iv:{int(versions[-1]['item_version'])}:observed"
        relations.append({"kind": recovery["kind"], "fromEventId": from_id,
            "fromLoaded": any(event["id"] == from_id for event in page),
            "toExecution": {"repositoryId": item.get("repositoryId"),
                "runId": recovery["runId"], "runAttempt": recovery["runAttempt"],
                "jobId": recovery["jobId"], "loaded": False},
            "matchRuleVersion": recovery["matchRuleVersion"]})
    return {"items": page, "relations": relations, "nextCursor": next_cursor,
        "hasMore": has_more, "coverage": {"historicalStartAt":
            iso_timestamp(int(versions[0]["observed_at"])) if versions else None,
            "limitations": sorted(limitations)}, "requestId": request_id}
