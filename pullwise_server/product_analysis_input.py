"""Pure, source-bound Jev input preparation from saved GitHub facts.

This module performs no reads, provider calls or usage writes. The caller must
supply the authoritative source version and, for Updates, saved watch interests.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from .github_ci_logs import redact_ci_log
from .jev_questions import (CI_QUESTION_VERSION, PR_QUESTION_VERSION,
                            UPDATES_QUESTION_VERSION, ci_questions,
                            pr_questions, update_questions)
from .typesafe_client import DEFAULT_JEV_MODEL, build_request
from .update_filter import extract_release_units


_PR_TYPES = frozenset({"pr_comment", "pr_review_body", "pr_review_comment"})


def _evidence(source: Mapping, anchor: str, text: str) -> dict:
    digest = hashlib.sha256(json.dumps([source["id"], source["sourceVersion"], anchor, text],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return {"id": f"evidence_{digest}", "sourceId": source["id"],
            "sourceVersion": source["sourceVersion"], "anchor": anchor, "text": text}


def _select(rows: Sequence[dict], *, limit: int, budget: int = 24 * 1024) -> tuple[list[dict], int]:
    selected, used = [], 0
    for row in rows:
        cost = len(row["text"].encode("utf-8")) + 96
        if len(selected) < limit and cost <= budget - used:
            selected.append(row)
            used += cost
    return selected, len(rows) - len(selected)


def _role(facts: Mapping) -> str:
    actor = facts.get("author") or facts.get("reviewer")
    parent = facts.get("pullAuthor")
    actor_id = actor.get("githubId") if isinstance(actor, Mapping) else None
    parent_id = parent.get("githubId") if isinstance(parent, Mapping) else None
    if actor_id and parent_id:
        return "pull_author" if actor_id == parent_id else "reviewer"
    return "unknown"


def prepare_source_questions(source: Mapping, *, interests: Sequence[str] = (), max_units: int = 8) -> dict:
    if (not isinstance(source, Mapping) or any(not isinstance(source.get(key), str) or not source[key]
            for key in ("id", "sourceVersion", "sourceType"))
            or not isinstance(source.get("content"), Mapping)
            or not isinstance(source.get("sourceFacts"), Mapping)
            or type(max_units) is not int or not 1 <= max_units <= 8):
        raise ValueError("ANALYSIS_SOURCE_INVALID")
    kind, content, facts = source["sourceType"], source["content"], source["sourceFacts"]
    original_partial = source.get("completeness") != "complete"
    if kind in _PR_TYPES:
        body = content.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("NO_ANALYZABLE_TEXT")
        rows = [{"anchor": f"p{index}", "text": text.strip()} for index, text in
                enumerate(re.split(r"\n[ \t]*\n+", body.replace("\r\n", "\n"))) if text.strip()]
        chosen, omitted = _select(rows, limit=max_units)
        state = {"segments": [{"text": row["text"], "speakerRole": _role(facts),
                                "quotationContext": "unknown"} for row in chosen]}
        questions = pr_questions(len(chosen)) if chosen else None
        version, extractor, prefix, binding_key = PR_QUESTION_VERSION, "pr-segments/v1", "s", "segmentAnchor"
    elif kind == "ci_failure":
        windows = content.get("windows")
        if not isinstance(windows, list):
            raise ValueError("ANALYSIS_SOURCE_INVALID")
        rows = []
        for window in windows:
            if not isinstance(window, Mapping) or not isinstance(window.get("windowId"), str):
                raise ValueError("ANALYSIS_SOURCE_INVALID")
            text = window.get("text")
            if isinstance(text, str) and text.strip():
                rows.append({"anchor": window["windowId"], "text": redact_ci_log(text),
                             "stage": str(window.get("stage") or "unknown"),
                             "partial": window.get("coverage") != "complete"})
        if len({row["anchor"] for row in rows}) != len(rows):
            raise ValueError("ANALYSIS_SOURCE_INVALID")
        chosen, omitted = _select(rows, limit=min(max_units, 5))
        original_partial |= any(row["partial"] for row in chosen)
        state = {"windows": [{"text": row["text"], "windowId": row["anchor"],
                              "stage": row["stage"]} for row in chosen]}
        questions = ci_questions(len(chosen)) if chosen else None
        version, extractor, prefix, binding_key = CI_QUESTION_VERSION, "ci-windows/v1", "w", "windowId"
    elif kind == "release":
        body = content.get("body")
        if (not isinstance(body, str) or not body.strip() or not isinstance(interests, Sequence)
                or isinstance(interests, str) or not interests
                or any(not isinstance(value, str) or not value.strip() for value in interests)):
            raise ValueError("NO_ANALYZABLE_TEXT")
        extracted = extract_release_units(body, interests=interests, max_units=max_units)
        chosen = [{"anchor": row["changeUnitId"], "text": row["text"]} for row in extracted["units"]]
        omitted = extracted["coverage"]["omittedUnits"]
        state = {"interests": list(interests), "changeUnits": [{"changeUnitId": row["anchor"],
                 "text": row["text"]} for row in chosen]}
        questions = update_questions(len(chosen)) if chosen else None
        version, extractor, prefix, binding_key = UPDATES_QUESTION_VERSION, "updates-units/v1", "u", "changeUnitId"
    else:
        raise ValueError("ANALYSIS_SOURCE_TYPE_UNSUPPORTED")
    if not chosen or questions is None:
        raise ValueError("NO_ANALYZABLE_TEXT")
    coverage = {"state": "partial" if original_partial or omitted else "complete",
                "selectedUnits": len(chosen), "omittedUnits": omitted,
                "rawSourcePartial": bool(original_partial),
                "limitations": ["input_selection_limit"] if omitted else []}
    if kind == "release":
        coverage["totalUnits"] = extracted["coverage"]["totalUnits"]
        coverage["selectionRuleVersion"] = extracted["coverage"]["selectionRuleVersion"]
        coverage["limitations"] = extracted["coverage"]["limitations"]
    evidence = [_evidence(source, row["anchor"], row["text"]) for row in chosen]
    bindings = {}
    for question_id in questions:
        index = int(question_id.split("_", 1)[0][len(prefix):])
        bindings[question_id] = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                                 binding_key: chosen[index]["anchor"],
                                 "evidenceIds": [evidence[index]["id"]]}
    request = build_request(state=state, questions=questions, model=DEFAULT_JEV_MODEL)
    input_hash = hashlib.sha256(json.dumps([version, extractor, request], ensure_ascii=False,
        separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
    return {"state": request["state"], "questions": request["questions"],
            "bindings": bindings, "evidence": evidence, "coverage": coverage,
            "questionVersion": version, "extractorVersion": extractor, "inputHash": input_hash}
