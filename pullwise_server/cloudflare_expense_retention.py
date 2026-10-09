"""Owner capacity policy and bounded, authorized atomic expense replacement."""
from __future__ import annotations

import json
import uuid

from .cloudflare_plan_limits import (
    ACTIVE_EXPENSE_SQL, PlanLimitError, active_expense_count_statement,
)
from .cloudflare_principal import (
    PrincipalAuthError, _cookie_sessions, _header, _principal, _resource_auth_snapshot,
)
from .cloudflare_state_records import MAX_SAFE_INTEGER, encode_record, record_name
from .ledger_plan_policy import entitlements

RESOURCE = "/api/v1/account/expense-retention"


def expense_retention_preference(user):
    enabled = user.get("autoRemoveOldestExpense", False)
    revision = user.get("expenseRetentionRevision", 1)
    if type(enabled) is not bool or type(revision) is not int or not 1 <= revision <= MAX_SAFE_INTEGER:
        return None
    return {"autoRemoveOldestExpense": enabled, "revision": revision}


async def handle_expense_retention_preference(*, binding, method, headers, body, now):
    from .cloudflare_ledger_api import _error, _revision, _write_guard
    if method not in {"GET", "PATCH"}:
        return _error(405, "METHOD_NOT_ALLOWED")
    try:
        cookies = _cookie_sessions(headers)
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    if _header(headers, "Authorization") or _header(headers, "X-Pullwise-Api-Key") or not cookies:
        return _error(401, "UNAUTHENTICATED")
    expected = None
    if method == "PATCH":
        if (not isinstance(body, dict) or set(body) != {"autoRemoveOldestExpense"}
                or type(body["autoRemoveOldestExpense"]) is not bool):
            return _error(422, "INVALID_INPUT")
        expected = _revision(headers)
        if expected is None:
            return _error(428, "PRECONDITION_REQUIRED")
        if expected < 0:
            return _error(422, "INVALID_INPUT")
    try:
        user, _ = await _principal(binding, headers, scope="profile:read", now=now)
        proof = {}
        auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now, "profile:read", proof)
        parts = await binding.batch(auth)
        validate([part.results for part in parts])
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    payload = expense_retention_preference(user)
    if payload is None:
        return _error(503, "RETENTION_PREFERENCE_UNAVAILABLE")
    if method == "GET":
        return 200, payload
    if expected != payload["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if body["autoRemoveOldestExpense"] == payload["autoRemoveOldestExpense"]:
        return 200, payload
    if expected == MAX_SAFE_INTEGER:
        return _error(409, "PREFERENCE_REVISION_EXHAUSTED")
    updated = {**json.loads(proof["user"]), "autoRemoveOldestExpense": body["autoRemoveOldestExpense"],
        "expenseRetentionRevision": expected + 1}
    await binding.batch([_write_guard(binding, proof, user["id"], now),
        binding.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?").bind(
            encode_record("users", user["id"], updated), now, record_name("users", user["id"]), proof["user"]),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")])
    return 200, expense_retention_preference(updated)


async def prepare_expense_retention(binding, user, restrictions, now, *, proof=None, schedule_actor_id=None):
    """Prepare at most one soft removal after create validation and replay checks.

    The caller retains its credential, current owner/policy, target and category
    fences. All returned commands join its original financial transaction.
    """
    from .cloudflare_ledger_activity import activity_commands
    from .cloudflare_ledger_api import _timestamp
    from .cloudflare_ledger_auth import target_allowed
    from .cloudflare_ledger_expenses import _actor, _dto
    preference = expense_retention_preference(user)
    if preference is None:
        raise PlanLimitError(503, "RETENTION_PREFERENCE_UNAVAILABLE")
    limit = entitlements(user, now=now, policy=getattr(binding, "plan_policy", None))["limits"]["expenseRecords"]
    count = await active_expense_count_statement(binding, user["id"]).first()
    if count["records"] < limit:
        return []
    if not preference["autoRemoveOldestExpense"]:
        raise PlanLimitError(403, "RECORD_LIMIT")
    if count["records"] > limit:
        raise PlanLimitError(403, "RETENTION_CLEANUP_REQUIRED")
    victim = await binding.prepare("SELECT * FROM expenses WHERE owner_id=? AND " + ACTIVE_EXPENSE_SQL +
        " ORDER BY occurred_on,created_at,id LIMIT 1").bind(user["id"]).first()
    if victim is None:
        raise PlanLimitError(409, "EXPENSE_CONFLICT")
    if not target_allowed(restrictions, victim["target_kind"], victim["project_id"]):
        raise PlanLimitError(403, "RETENTION_TARGET_FORBIDDEN")
    if victim["revision"] >= MAX_SAFE_INTEGER:
        raise PlanLimitError(409, "REVISION_LIMIT")
    stamp, event_id = _timestamp(now), "evt_" + uuid.uuid4().hex
    before = _dto(victim)
    actor_kind, actor_id = ("schedule", schedule_actor_id) if schedule_actor_id is not None else _actor(proof)
    guard = binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM expenses WHERE owner_id=? AND id=? AND revision=? AND """ + ACTIVE_EXPENSE_SQL + """ )
        AND ?=(SELECT COUNT(*) FROM expenses WHERE owner_id=? AND """ + ACTIVE_EXPENSE_SQL + """ )
        AND ?=(SELECT id FROM expenses WHERE owner_id=? AND """ + ACTIVE_EXPENSE_SQL + """
            ORDER BY occurred_on,created_at,id LIMIT 1) THEN 1 ELSE 0 END)""").bind(
                user["id"], victim["id"], victim["revision"], limit, user["id"], victim["id"], user["id"])
    activity = await activity_commands(binding, user, "expense", victim["id"], "delete", before, None, now,
        proof=proof, scheduled=schedule_actor_id is not None, operation_id=event_id)
    return [guard,
        binding.prepare("""UPDATE expenses SET deleted_at=?,updated_at=?,revision=revision+1
            WHERE id=? AND owner_id=? AND revision=? AND """ + ACTIVE_EXPENSE_SQL).bind(
                stamp, stamp, victim["id"], user["id"], victim["revision"]),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
            action,before_json,after_json,created_at) VALUES(?,?,?,?,?,'delete',?,NULL,?)""").bind(
                event_id, victim["id"], user["id"], actor_kind, actor_id,
                json.dumps(before, ensure_ascii=False, separators=(",", ":")), stamp),
        *activity]
