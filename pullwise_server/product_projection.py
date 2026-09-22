"""Pure GitHub fact projections; no storage, model, clock or transport access.

An empty result means no proven rule action, never proof that a previous item
may be closed. The publisher reconciles explicit withdrawal/closure evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping

from .product_domain import _canonical_hash


@dataclass(frozen=True)
class RuleItemProjection:
    unit_type: str
    unit_key: str
    snapshot: dict
    action_signature: str
    evidence_complete: bool


def _actor(value: object, kind: str = "user") -> dict | None:
    if not isinstance(value, Mapping):
        return None
    identifier = value.get("githubId")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    return {"kind": kind, "githubId": identifier.strip()}


def project_rule_source(source: Mapping, target: Mapping) -> tuple[RuleItemProjection, ...]:
    """Project only explicit failures, current requests and verified reviews.

    Sources use the reader's normalized camelCase shape. Source-version and
    context fences belong to the publisher; optional sourceVersion is copied
    onto evidence when supplied by that publisher.
    """
    kind = source.get("sourceType")
    if kind not in {"ci_failure", "pr_state", "pr_review_body"}:
        return ()
    facts = source.get("sourceFacts")
    content = source.get("content")
    if not isinstance(facts, Mapping) or not isinstance(content, Mapping):
        return ()
    key = source.get("externalKey")
    source_id = source.get("sourceId")
    if not isinstance(key, str) or not key or not isinstance(source_id, str) or not source_id:
        raise ValueError("rule projection requires stable source identity")
    module = "ci" if kind == "ci_failure" else "pr"
    if target.get("module") != module:
        return ()
    rows = []

    def evidence(identifier: str, text: str, actions: list[str], status: str = "available") -> dict:
        result = {"id": identifier, "sourceId": source_id, "text": text,
                  "status": status, "actionTypes": actions}
        if source.get("sourceVersion"):
            result["sourceVersion"] = source["sourceVersion"]
        return result

    def emit(unit_type, unit_key, action, title, actors, evidence_rows, *, complete, body=None):
        # PR closure is authoritative only when explicitly returned as state.
        closed = (facts.get("state") == "closed" if kind == "pr_state"
                  else facts.get("pullState") == "closed" if kind == "pr_review_body" else False)
        lifecycle = "source_closed" if closed else "active"
        item_facts = deepcopy(dict(facts))
        if kind == "ci_failure":
            item_facts.update(recovery=None, recoveryStatus="unknown", recoveryReason="identity_support_not_verified")
            windows = item_facts.get("windows") or []
            for window in windows:
                window["symptoms"] = []
                window["evidenceIds"] = [row["id"] for row in evidence_rows
                                         if row.get("segmentAnchor") == window.get("windowId")]
            item_facts["windows"] = windows
        snapshot = {"module": module, "repositoryId": source.get("repositoryId"), "watchId": None,
                    "actionTypes": [action], "title": title, "sourceUrl": source.get("sourceUrl"),
                    "sourceFacts": item_facts, "evidence": evidence_rows, "assessments": [],
                    "nextActors": actors, "lifecycle": lifecycle,
                    "attentionState": "closed" if closed else "needs_action",
                    "closureReason": "source_closed" if closed else None}
        signature = {"rule": "github-facts/v1", "unit": [unit_type, unit_key],
                     "action": action, "actors": actors, "lifecycle": lifecycle, "body": body}
        if kind == "ci_failure":
            signature["conclusion"] = facts["conclusion"]
        rows.append(RuleItemProjection(unit_type, unit_key, snapshot,
                                      "rule_v1_" + _canonical_hash(signature), complete))

    if kind == "ci_failure":
        if facts.get("conclusion") not in {"failure", "timed_out"}:
            return ()
        if (any(not isinstance(facts.get(field), str) or not facts[field].strip()
                for field in ("runId", "jobId"))
                or type(facts.get("runAttempt")) is not int or facts["runAttempt"] < 1):
            return ()
        assignee = _actor({"githubId": target.get("default_assignee_id")})
        evidence_rows = []
        for window in content.get("windows") or []:
            if not isinstance(window, Mapping) or not isinstance(window.get("windowId"), str):
                continue
            text = window.get("text")
            if not isinstance(text, str) or not text:
                continue
            row = evidence(f"{source_id}:window:{window['windowId']}", text, ["investigate_failure"])
            row["segmentAnchor"] = window["windowId"]
            evidence_rows.append(row)
        emit("ci_job", key, "investigate_failure", "Investigate CI failure",
             [assignee] if assignee else [], evidence_rows, complete=True)
    elif kind == "pr_state":
        if facts.get("state") not in {"open", "closed"}:
            return ()
        actors = {}
        for field, actor_kind in (("requestedReviewers", "user"), ("requestedTeams", "team")):
            values = facts.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                actor = _actor(value, actor_kind)
                if actor:
                    actors[(actor_kind, actor["githubId"])] = actor
        for (actor_kind, identifier), actor in sorted(actors.items()):
            emit("pr_review_request", f"{key}:{actor_kind}:{identifier}", "review_requested",
                 "Review requested", [actor], [], complete=True)
    elif facts.get("formalReviewStatus") == "effective" and facts.get("reviewState") == "CHANGES_REQUESTED":
        author = _actor(facts.get("pullAuthor"))
        body = content.get("body")
        if not isinstance(body, str):
            return ()
        row = evidence(f"{source_id}:body", body, ["change_requested"])
        emit("pr_review_body", key, "change_requested", "Changes requested",
             [author] if author else [], [row], complete=source.get("completeness") == "complete", body=body)
    return tuple(rows)
