"""Account-owned Jev preference and exact-current authority checks."""
from __future__ import annotations

import json

from .account_cycle_rules import PAID_PLAN_IDS, effective_user_plan
from .cloudflare_principal import (
    PrincipalAuthError, _cookie_sessions, _header, _principal, _resource_auth_snapshot,
)
from .cloudflare_state_records import MAX_SAFE_INTEGER, encode_record, record_name


def jev_preference(user):
    """Only absent legacy fields get defaults; malformed persisted data fails closed."""
    enabled = user.get("jevEnabled", True)
    revision = user.get("jevPreferenceRevision", 1)
    if type(enabled) is not bool or type(revision) is not int or not 1 <= revision <= MAX_SAFE_INTEGER:
        return None
    return enabled, revision


def jev_enabled(user):
    preference = jev_preference(user)
    return preference is not None and preference[0]


async def ensure_current_jev_authority(binding, headers, now, proof, *, scope,
                                       target_kind=None, project_id=None):
    """Reject routine changes before a native CHECK, retaining the atomic last fence."""
    from .cloudflare_ledger_auth import ledger_principal
    fresh = {}
    _, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
        scope=scope, now=now, target_kind=target_kind, project_id=project_id, proof=fresh)
    parts = await binding.batch(auth)
    validate([part.results for part in parts])
    if fresh != proof:
        raise PrincipalAuthError(403, "AUTHORIZATION_CHANGED", "Ledger authority changed.")


def _payload(user, now, binding):
    from .ledger_plan_policy import entitlements
    preference = jev_preference(user)
    if preference is None:
        return None
    enabled, revision = preference
    jev = entitlements(user, now=now, policy=getattr(binding, "plan_policy", None),
        jev_available=getattr(binding, "jev_available", False))["jev"]
    return {"enabled": enabled, "revision": revision, "eligible": jev["eligible"],
        "available": jev["available"], "monthlyBudgetUsd": jev["monthlyBudgetUsd"]}


async def handle_jev_preference(*, binding, method, headers, body, now):
    from .cloudflare_ledger_api import _error, _revision, _write_guard
    from .cloudflare_plan_limits import PlanLimitError
    if method not in {"GET", "PATCH"}:
        return _error(405, "METHOD_NOT_ALLOWED")
    # Account Settings uses the actual cookie account, never a selected ledger
    # or a paid API-key actor. Mixed credentials are rejected as well.
    try:
        cookies = _cookie_sessions(headers)
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    if (_header(headers, "Authorization") or _header(headers, "X-Pullwise-Api-Key") or not cookies):
        return _error(401, "UNAUTHENTICATED")
    expected = None
    if method == "PATCH":
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
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
    payload = _payload(user, now, binding)
    if payload is None:
        return _error(503, "JEV_PREFERENCE_UNAVAILABLE")
    if method == "GET":
        return 200, payload
    if effective_user_plan(user, timestamp=now) not in PAID_PLAN_IDS:
        return _error(403, "JEV_PLAN_REQUIRED")
    if expected != payload["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if body["enabled"] == payload["enabled"]:
        return 200, payload
    if expected == MAX_SAFE_INTEGER:
        return _error(409, "PREFERENCE_REVISION_EXHAUSTED")
    # Persist only the raw canonical record. Never store ledger projections,
    # refresh a stale proof, overwrite billing/identity state, or reset usage.
    updated = {**json.loads(proof["user"]), "jevEnabled": body["enabled"],
        "jevPreferenceRevision": expected + 1}
    commands = [_write_guard(binding, proof, user["id"], now),
        binding.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?").bind(
            encode_record("users", user["id"], updated), now, record_name("users", user["id"]), proof["user"]),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    # A native failure may have no metadata. Let the HTTP boundary return an
    # unavailable response while the existing journal retains its stopped ticket;
    # never disguise an unknown outcome as an ordinary preference CAS conflict.
    return 200, _payload(updated, now, binding)
