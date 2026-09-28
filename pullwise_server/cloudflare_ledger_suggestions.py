"""Bounded, optional Jev hints for a draft expense; no ledger write occurs here."""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from .cloudflare_ledger_api import _error, _write_guard
from .cloudflare_ledger_auth import ledger_principal
from .cloudflare_principal import PrincipalAuthError
from .cloudflare_ledger_expenses import _amount
from .typesafe_client import DEFAULT_JEV_MODEL, build_request, validate_response
from .account_cycle_rules import effective_user_plan
from .cloudflare_plan_limits import PlanLimitError


QUESTION_VERSION = "ledger-suggest-v1"
CONFIDENCE_THRESHOLD = 0.80


def _draft(body):
    if not isinstance(body, dict) or set(body) - {"purpose", "note", "target", "occurredOn", "amount", "currency"}:
        return None
    purpose, note, target = body.get("purpose"), body.get("note", ""), body.get("target")
    if (not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 500
            or not isinstance(note, str) or len(note) > 1000 or not isinstance(target, dict)):
        return None
    kind = target.get("kind")
    project_id = target.get("projectId")
    context = [body.get(key) for key in ("occurredOn", "amount", "currency")]
    if any(value is not None for value in context) and any(value is None for value in context):
        return None
    occurred, minor, currency = None, None, None
    if all(value is not None for value in context):
        occurred, amount, currency = context
        if (not isinstance(occurred, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", occurred)
                or not isinstance(currency, str)):
            return None
        try:
            if date.fromisoformat(occurred).isoformat() != occurred:
                return None
            minor = _amount(amount, currency)
        except ValueError:
            return None
    if kind == "shared" and set(target) == {"kind"}:
        return purpose.strip(), note, kind, None, occurred, minor, currency
    if (kind == "project" and set(target) == {"kind", "projectId"}
            and isinstance(project_id, str) and project_id.startswith("prj_")
            and len(project_id) <= 104):
        return purpose.strip(), note, kind, project_id, occurred, minor, currency
    return None


def suggestion_questions(categories):
    questions = {"target": {"type": "choice",
        "instructions": "Is this cost specific to one project or shared across projects? Choose uncertain when the text does not establish that.",
        "criteria": {"project": "Specific to one project", "shared": "Used across projects",
                     "uncertain": "Insufficient information"}}}
    if categories:
        questions["category"] = {"type": "choice",
            "instructions": "Choose the best expense category from these existing account categories. Choose uncertain if none clearly fits. Do not create a category.",
            "criteria": {**{item["id"]: item["name"] for item in categories},
                         "uncertain": "No clear existing category"}}
    return questions


async def handle_suggestion_request(*, binding, method, headers, body, now, gateway):
    if method != "POST":
        return _error(405, "METHOD_NOT_ALLOWED")
    draft = _draft(body)
    if draft is None:
        return _error(422, "INVALID_INPUT")
    purpose, note, target_kind, project_id, occurred, minor, currency = draft
    start = (date.fromisoformat(occurred) - timedelta(days=7)).isoformat() if occurred else None
    end = (date.fromisoformat(occurred) + timedelta(days=7)).isoformat() if occurred else None
    proof = {}
    try:
        user, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
            scope="suggestions:use", now=now, target_kind=target_kind, project_id=project_id,
            proof=proof)
        if gateway is not None and gateway.enabled and effective_user_plan(user, timestamp=now) != "max":
            return _error(403, "MAX_REQUIRED")
        commands = [binding.prepare("""SELECT id,name FROM expense_categories
            WHERE owner_id=? AND archived_at IS NULL ORDER BY name,id LIMIT 30""").bind(user["id"]),
            binding.prepare("""SELECT id,purpose FROM expenses WHERE owner_id=? AND deleted_at IS NULL
            AND target_kind=? AND (project_id=? OR (project_id IS NULL AND ? IS NULL))
            AND occurred_on BETWEEN ? AND ? AND amount_minor=? AND currency=?
            ORDER BY occurred_on DESC,id DESC LIMIT 30""").bind(
                user["id"], target_kind, project_id, project_id, start, end, minor, currency)]
        if project_id:
            commands.append(binding.prepare("""SELECT id FROM ledger_projects
                WHERE owner_id=? AND id=?""").bind(user["id"], project_id))
        rows = await binding.batch([*auth, *commands])
        validate([part.results for part in rows[:len(auth)]])
        categories, recent = rows[len(auth)].results, rows[len(auth) + 1].results
        if project_id and not rows[-1].results:
            return _error(404, "NOT_FOUND")
    except PrincipalAuthError as exc:
        return _error(exc.status, exc.code)
    if gateway is None or not gateway.enabled:
        return 200, {"status": "unavailable", "reason": "disabled", "suggestions": {}}
    if not categories:
        return 200, {"status": "unavailable", "reason": "no_categories", "suggestions": {}}
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    limit = max(1, min(20, int(getattr(gateway, "daily_limit", 10))))
    try:
        attempt = await binding.batch([_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO expense_suggestion_budget(owner_id,day,attempts)
                VALUES(?,?,1) ON CONFLICT(owner_id,day) DO UPDATE SET attempts=attempts+1
                WHERE attempts<? RETURNING attempts""").bind(user["id"], day, limit),
            binding.prepare("DELETE FROM d1_command_guard")])
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(409, "AUTHORIZATION_CHANGED")
    if not attempt[-2].results:
        return 429, {"error": {"code": "SUGGESTION_LIMIT"}}
    questions = suggestion_questions(categories)
    request = build_request(state={"purpose": purpose, "note": note},
        questions=questions, model=DEFAULT_JEV_MODEL)
    outcome = "unavailable"
    suggestions = {}
    category_probs = target_probs = None
    try:
        raw = await gateway.evaluate(request)
        answer = validate_response(raw, expected_questions={key: tuple(value["criteria"])
            for key, value in questions.items()}, requested_model=DEFAULT_JEV_MODEL)
        category = answer["answers"].get("category")
        target = answer["answers"]["target"]
        category_probs = category["probabilities"] if category else None
        target_probs = target["probabilities"]
        allowed = {item["id"] for item in categories}
        if category and category["choice"] in allowed and category["confidence"] >= CONFIDENCE_THRESHOLD:
            suggestions["categoryId"] = category["choice"]
        if target["choice"] in {"project", "shared"} and target["confidence"] >= CONFIDENCE_THRESHOLD:
            suggestions["targetKind"] = target["choice"]
        duplicate = next((item for item in recent if item["purpose"].casefold() == purpose.casefold()), None)
        if duplicate:
            suggestions["duplicateExpenseId"] = duplicate["id"]
        outcome = "available" if suggestions else "uncertain"
    except PlanLimitError as error:
        return error.response()
    except Exception:
        # Provider transport, timeout, malformed output and FFI errors all leave
        # the manual draft usable. No provider exception is returned to clients.
        outcome = "unavailable"
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
    event_id = "sg_" + uuid.uuid4().hex
    try:
        await binding.batch([_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,
        question_version,draft_target_kind,draft_project_id,model_version,outcome,category_id,target_kind,
        category_probabilities_json,target_probabilities_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""").bind(event_id, user["id"], stamp, QUESTION_VERSION,
            target_kind, project_id, DEFAULT_JEV_MODEL if outcome != "unavailable" else None, outcome,
            suggestions.get("categoryId"), suggestions.get("targetKind"),
            json.dumps(category_probs) if category_probs else None,
            json.dumps(target_probs) if target_probs else None),
            binding.prepare("DELETE FROM d1_command_guard")])
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(409, "AUTHORIZATION_CHANGED")
    return 200, {"status": outcome, "suggestionId": event_id,
        "questionVersion": QUESTION_VERSION,
        "modelVersion": DEFAULT_JEV_MODEL if outcome != "unavailable" else None,
        "suggestions": suggestions,
        "probabilities": {"category": category_probs, "target": target_probs}}


async def handle_suggestion_decision(*, binding, method, path, headers, body, now):
    if method != "POST":
        return _error(405, "METHOD_NOT_ALLOWED")
    suggestion_id = path.removeprefix("/api/v1/expense-suggestions/").removesuffix("/decision")
    if (not suggestion_id.startswith("sg_") or len(suggestion_id) != 35
            or not isinstance(body, dict) or set(body) != {"target", "categoryId"}
            or not isinstance(body["categoryId"], str)
            or not body["categoryId"].startswith("cat_")
            or not isinstance(body["target"], dict)):
        return _error(422, "INVALID_INPUT")
    draft = _draft({"purpose": "decision", "target": body["target"]})
    if draft is None:
        return _error(422, "INVALID_INPUT")
    _, _, target_kind, project_id, _, _, _ = draft
    proof = {}
    try:
        user, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
            scope="suggestions:use", now=now, target_kind=target_kind, project_id=project_id,
            proof=proof)
        checks = [binding.prepare("""SELECT id FROM expense_suggestion_events WHERE id=?
            AND owner_id=? AND outcome IN ('available','uncertain') AND decided_at IS NULL""").bind(
                suggestion_id, user["id"]),
            binding.prepare("""SELECT id FROM expense_categories WHERE id=? AND owner_id=?
                AND archived_at IS NULL""").bind(body["categoryId"], user["id"])]
        if project_id:
            checks.append(binding.prepare("SELECT id FROM ledger_projects WHERE id=? AND owner_id=?").bind(
                project_id, user["id"]))
        parts = await binding.batch([*auth, *checks])
        validate([part.results for part in parts[:len(auth)]])
        event, category = parts[len(auth)].results, parts[len(auth) + 1].results
    except PrincipalAuthError as exc:
        return _error(exc.status, exc.code)
    if not event:
        return _error(404, "NOT_FOUND")
    if not category:
        return _error(422, "INVALID_CATEGORY")
    if project_id and not parts[-1].results:
        return _error(404, "NOT_FOUND")
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parts = await binding.batch([_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN EXISTS(
                SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
                THEN 1 ELSE 0 END)""").bind(user["id"], body["categoryId"]),
            binding.prepare("""UPDATE expense_suggestion_events
                SET accepted_category_id=?,accepted_target_kind=?,project_id=?,decided_at=?
                WHERE id=? AND owner_id=? AND decided_at IS NULL RETURNING id""").bind(
                    body["categoryId"], target_kind, project_id, stamp, suggestion_id, user["id"]),
            binding.prepare("DELETE FROM d1_command_guard")])
    except Exception:
        return _error(409, "AUTHORIZATION_CHANGED")
    if not parts[-2].results:
        return _error(409, "DECISION_CONFLICT")
    return 204, None
