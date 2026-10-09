"""Explicit, revision-bound hints for one saved expense; never edit its money."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from .account_cycle_rules import PAID_PLAN_IDS, effective_user_plan
from .cloudflare_ledger_api import _error, _revision, _timestamp, _valid_resource_id, _write_guard
from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_ledger_expenses import _snapshot
from .cloudflare_ledger_reports import VISIBLE_EXPENSE_TARGET_SQL
from .cloudflare_ledger_suggestions import (
    CONFIDENCE_THRESHOLD, QUESTION_VERSION, _project_visibility_guards, suggestion_questions,
)
from .cloudflare_plan_limits import PlanLimitError
from .cloudflare_principal import PrincipalAuthError
from .typesafe_client import DEFAULT_JEV_MODEL, build_request, validate_response
from .cloudflare_jev_preferences import jev_enabled


def _source_guard(binding, source):
    return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN EXISTS(
        SELECT 1 FROM expenses WHERE owner_id=? AND id=? AND revision=?
            AND deleted_at IS NULL AND target_kind=? AND project_id IS ?)
        THEN 1 ELSE 0 END)""").bind(source["owner_id"], source["id"], source["revision"],
            source["target_kind"], source["project_id"])


def _source_query(binding, source):
    return binding.prepare("""SELECT revision,target_kind,project_id FROM expenses
        WHERE owner_id=? AND id=? AND deleted_at IS NULL """ + VISIBLE_EXPENSE_TARGET_SQL).bind(
            source["owner_id"], source["id"])


def _same_source(rows, source):
    return len(rows) == 1 and all(rows[0][field] == source[field]
        for field in ("revision", "target_kind", "project_id"))


async def _fresh_snapshot(binding, headers, now, source, proof, queries):
    """Resolve routine permission/source changes with reads before atomic guards."""
    fresh = {}
    user, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
        scope="expenses:write", now=now, target_kind=source["target_kind"],
        project_id=source["project_id"], proof=fresh)
    parts = await binding.batch([*auth, _source_query(binding, source), *queries])
    validate([part.results for part in parts[:len(auth)]])
    if effective_user_plan(user, timestamp=now) not in PAID_PLAN_IDS:
        raise PrincipalAuthError(403, "JEV_PLAN_REQUIRED", "A current paid ledger plan is required.")
    if fresh != proof:
        raise PrincipalAuthError(403, "AUTHORIZATION_CHANGED", "Ledger authority changed.")
    if not _same_source(parts[len(auth)].results, source):
        return None
    return [part.results for part in parts[len(auth) + 1:]]


def _recent_query(binding, source):
    occurred = date.fromisoformat(source["occurred_on"])
    start = date.fromordinal(max(date.min.toordinal(), occurred.toordinal() - 7)).isoformat()
    end = date.fromordinal(min(date.max.toordinal(), occurred.toordinal() + 7)).isoformat()
    return binding.prepare("""SELECT id,revision,purpose FROM expenses
        WHERE owner_id=? AND deleted_at IS NULL AND target_kind=? AND project_id IS ?
            AND occurred_on BETWEEN ? AND ? AND amount_minor=? AND currency=? AND id!=?
        """ + VISIBLE_EXPENSE_TARGET_SQL + " ORDER BY occurred_on DESC,id DESC LIMIT 30").bind(
            source["owner_id"], source["target_kind"], source["project_id"], start, end,
            source["amount_minor"], source["currency"], source["id"])


def _model_checks(source, answer, categories, reason, *, category_requested):
    category = {"status": "unavailable", "current": source["category_id"]}
    target = {"status": "unavailable", "current": {"kind": source["target_kind"],
        **({"projectId": source["project_id"]} if source["project_id"] else {})}}
    if reason:
        category["reason"] = target["reason"] = reason
    chosen_category = chosen_target = None
    if answer is not None:
        category_answer = answer["answers"].get("category")
        if category_answer is not None:
            choice, confidence = category_answer["choice"], category_answer["confidence"]
            category["status"] = "uncertain"
            category["confidence"] = confidence
            if choice in categories and confidence >= CONFIDENCE_THRESHOLD:
                chosen_category = choice
                category.update(status="checked" if choice == source["category_id"] else "issue",
                    suggested=choice, confidence=confidence)
        target_answer = answer["answers"]["target"]
        choice, confidence = target_answer["choice"], target_answer["confidence"]
        target["status"] = "uncertain"
        target["confidence"] = confidence
        if choice in {"project", "shared"} and confidence >= CONFIDENCE_THRESHOLD:
            chosen_target = choice
            target.update(status="checked" if choice == source["target_kind"] else "issue",
                suggested={"kind": choice}, confidence=confidence)
    if not category_requested:
        category["reason"] = "no_categories"
    return category, target, chosen_category, chosen_target


async def _evaluate(binding, source, user, proof, now, gateway, categories, daily, day):
    limit = max(1, min(20, int(getattr(gateway, "daily_limit", 20))))
    if daily and daily[0]["attempts"] >= limit:
        return None, None, _error(429, "SUGGESTION_LIMIT"), None
    questions = suggestion_questions(categories)
    try:
        request = build_request(state={"purpose": source["purpose"], "note": source["note"] or ""},
            questions=questions, model=DEFAULT_JEV_MODEL)
    except (ValueError, UnicodeError):
        return None, None, None, "invalid_context"
    commands = [_write_guard(binding, proof, user["id"], now), _source_guard(binding, source),
        *_project_visibility_guards(binding, user["id"], source["project_id"]),
        binding.prepare("""INSERT INTO expense_suggestion_budget(owner_id,day,attempts)
            VALUES(?,?,1) ON CONFLICT(owner_id,day) DO UPDATE SET attempts=attempts+1
            WHERE attempts<? RETURNING attempts""").bind(user["id"], day, limit),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        admitted = await binding.batch(commands)
    except PlanLimitError as error:
        return None, None, error.response(), None
    except Exception:
        return None, None, _error(412, "PRECONDITION_FAILED"), None
    if not admitted[-2].results:
        return None, None, _error(429, "SUGGESTION_LIMIT"), None
    answer = None
    try:
        raw = await gateway.evaluate(request)
        answer = validate_response(raw, expected_questions={key: tuple(value["criteria"])
            for key, value in questions.items()}, requested_model=DEFAULT_JEV_MODEL)
    except PlanLimitError as error:
        return None, None, error.response(), None
    except Exception:
        # Transport and invalid provider output reveal no exception details.
        pass
    return answer, "sg_" + uuid.uuid4().hex, None, None if answer else "provider_unavailable"


async def handle_expense_review(*, binding, method, item_id, headers, body, now, gateway):
    if not _valid_resource_id(item_id):
        return _error(404, "NOT_FOUND")
    if method != "POST":
        return _error(405, "METHOD_NOT_ALLOWED")
    if not isinstance(body, dict) or body:
        return _error(422, "INVALID_INPUT")
    expected = _revision(headers)
    if expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if expected < 0:
        return _error(422, "INVALID_INPUT")
    try:
        return await _review(binding, headers, item_id, expected, now, gateway)
    except PrincipalAuthError as error:
        return _error(error.status, error.code)


async def _review(binding, headers, item_id, expected, now, gateway):
    user, restrictions, proof, rows = await _snapshot(binding, headers, now, "expenses:write",
        lambda owner: [binding.prepare("""SELECT * FROM expenses
            WHERE owner_id=? AND id=? AND deleted_at IS NULL """ + VISIBLE_EXPENSE_TARGET_SQL).bind(
                owner, item_id)])
    if not rows[0]:
        return _error(404, "NOT_FOUND")
    source = rows[0][0]
    if not target_allowed(restrictions, source["target_kind"], source["project_id"]):
        return _error(403, "TARGET_FORBIDDEN")
    if source["revision"] != expected:
        return _error(412, "PRECONDITION_FAILED")
    if effective_user_plan(user, timestamp=now) not in PAID_PLAN_IDS:
        return _error(403, "JEV_PLAN_REQUIRED")
    enabled = gateway is not None and gateway.enabled and jev_enabled(user)
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    context_queries = [binding.prepare("""SELECT id,name FROM expense_categories
        WHERE owner_id=? AND archived_at IS NULL ORDER BY name,id LIMIT 30""").bind(user["id"])]
    if enabled:
        context_queries.append(binding.prepare("""SELECT attempts FROM expense_suggestion_budget
            WHERE owner_id=? AND day=?""").bind(user["id"], day))
    context = await _fresh_snapshot(binding, headers, now, source, proof, context_queries)
    if context is None:
        return _error(412, "PRECONDITION_FAILED")
    categories = context[0]
    answer = None
    reason = "disabled" if not enabled else "provider_unavailable"
    event_id = None
    if enabled:
        answer, event_id, error, reason = await _evaluate(binding, source, user, proof, now, gateway,
            categories, context[1], day)
        if error is not None:
            return error

    # The model cannot observe these candidate texts. Recompute the local rule
    # after inference so archived categories and deleted/moved duplicates cannot
    # be published from an earlier snapshot.
    chosen = answer["answers"].get("category", {}).get("choice") if answer else None
    final_queries = [_recent_query(binding, source),
        binding.prepare("""SELECT id FROM expense_categories
            WHERE owner_id=? AND id=? AND archived_at IS NULL""").bind(user["id"], chosen or "")]
    final = await _fresh_snapshot(binding, headers, now, source, proof, final_queries)
    if final is None:
        return _error(412, "PRECONDITION_FAILED")
    duplicate = next((item for item in final[0]
        if item["purpose"].casefold() == source["purpose"].casefold()), None)
    category_check, target_check, chosen_category, chosen_target = _model_checks(
        source, answer, {row["id"] for row in final[1]}, reason, category_requested=bool(categories))
    duplicate_check = {"status": "checked"}
    if duplicate is not None:
        duplicate_check.update(status="issue", candidate={"id": duplicate["id"], "revision": duplicate["revision"]})
    result = {"expenseId": source["id"], "revision": source["revision"],
        "questionVersion": QUESTION_VERSION, "modelVersion": DEFAULT_JEV_MODEL if answer else None,
        "checks": {"category": category_check, "target": target_check, "duplicate": duplicate_check}}
    commands = [_write_guard(binding, proof, user["id"], now), _source_guard(binding, source),
        *_project_visibility_guards(binding, user["id"], source["project_id"])]
    if duplicate is not None:
        commands.append(_source_guard(binding, {**source, "id": duplicate["id"], "revision": duplicate["revision"]}))
    if chosen_category:
        commands.append(binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN EXISTS(
            SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
            THEN 1 ELSE 0 END)""").bind(user["id"], chosen_category))
    if event_id:
        category_answer = answer["answers"].get("category") if answer else None
        target_answer = answer["answers"]["target"] if answer else None
        outcome = "available" if chosen_category or chosen_target else ("uncertain" if answer else "unavailable")
        commands.append(binding.prepare("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,
            question_version,draft_target_kind,draft_project_id,model_version,outcome,category_id,target_kind,
            category_probabilities_json,target_probabilities_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""").bind(event_id, user["id"], _timestamp(now), QUESTION_VERSION,
                source["target_kind"], source["project_id"], DEFAULT_JEV_MODEL if answer else None, outcome,
                chosen_category, chosen_target,
                json.dumps(category_answer["probabilities"]) if category_answer else None,
                json.dumps(target_answer["probabilities"]) if target_answer else None))
        result["suggestionId"] = event_id
    commands.append(binding.prepare("DELETE FROM d1_command_guard"))
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(412, "PRECONDITION_FAILED")
    return 200, result
