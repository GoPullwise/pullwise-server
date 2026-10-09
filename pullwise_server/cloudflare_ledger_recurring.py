"""Persisted calendar rules and trusted, bounded ordinary-expense generation.

HTTP management shares ordinary expense session/API-key authorization. The
internal runner rechecks each rule's real actor and, for API-created grants,
the original key's current validity and restrictions, never a fabricated session.
Its caller must serialize preview execution through the existing coordinator.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta

from .cloudflare_ledger_api import (
    _error, _page_inputs, _param, _revision, _timestamp, _valid_resource_id, _write_guard,
)
from .api_key_dto_rules import parse_api_key_restrictions
from .cloudflare_ledger_auth import ROLE_SCOPES, ledger_principal, target_allowed
from .cloudflare_ledger_activity import activity_commands, retention_commands
from .cloudflare_ledger_expenses import (
    _creation_key, _decimal_amount, _dto as expense_dto, _input as expense_input, _snapshot,
    _target_guard, _valid_target,
)
from .cloudflare_github_gateway import GitHubFailure
from .cloudflare_plan_limits import PlanLimitError
from .cloudflare_principal import PrincipalAuthError, _header, _timestamp as auth_timestamp
from .cloudflare_state_records import record_name
from .json_input import validate_json_unicode
from .ledger_recurrence_calendar import (
    following_occurrence, iso_date, local_today, next_fields, next_occurrence, schedule_input,
)

RESOURCE = "/api/v1/expense-recurring-rules"
MAX_RULES_PER_OWNER = 100
MAX_TICK_RULES = 10
MAX_TICK_OCCURRENCES = 10

# A removal preserves immutable rules and occurrences, but withdraws every live
# rule surface. Correlate with the stored owner as well as the project ID.
_VISIBLE_TARGET = """(target_kind='shared' OR EXISTS(SELECT 1 FROM ledger_projects p
    WHERE p.owner_id=expense_recurring_rules.owner_id
        AND p.id=expense_recurring_rules.project_id AND p.deleted_at IS NULL))"""


def _json(value, maximum=8192):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("recurring input size")
    return encoded


def _input(body):
    allowed = {"target", "amount", "currency", "categoryId", "purpose", "note", "quantity", "unit", "schedule"}
    if not isinstance(body, dict) or "schedule" not in body or set(body) - allowed:
        raise ValueError("recurring fields")
    validate_json_unicode(body)
    schedule = schedule_input(body["schedule"])
    template = expense_input({**{name: value for name, value in body.items() if name != "schedule"},
                              "occurredOn": schedule["startOn"]})
    template.pop("occurred_on")
    _json(template)
    _json(schedule, 2048)
    return template, schedule


def _template(row):
    # This hash identifies a revocable grant, not a credential. Keep it private
    # inside the existing bounded JSON, without changing public expense fields.
    template = json.loads(row["template_json"])
    template.pop("_api_key_hash", None)
    return template


def _grant_template(template, proof):
    saved = dict(template)
    saved.pop("_api_key_hash", None)
    if proof.get("key") is not None:
        saved["_api_key_hash"] = hashlib.sha256(proof["token"].encode("utf-8")).hexdigest()
    return saved


def rule_dto(row):
    template, schedule = json.loads(row["template_json"]), json.loads(row["schedule_json"])
    return {"id": row["id"], "target": {"kind": row["target_kind"],
            **({"projectId": row["project_id"]} if row["target_kind"] == "project" else {})},
            "amount": _decimal_amount(template["amount_minor"], template["currency"]),
            "currency": template["currency"], "categoryId": template["category_id"],
            "purpose": template["purpose"], "note": template["note"],
            "quantity": template["quantity_decimal"], "unit": template["unit"],
            "schedule": schedule, "status": row["status"], "revision": row["revision"],
            "nextOccurrenceOn": row["next_occurrence_on"], "nextRunAt": row["next_run_at"],
            "blockedCode": row["blocked_code"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]}


def _rule_guard(binding, row, *, active=False):
    return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM expense_recurring_rules WHERE id=? AND owner_id=? AND revision=?
            AND (?=0 OR status='active') AND """ + _VISIBLE_TARGET + ") THEN 1 ELSE 0 END)").bind(
                row["id"], row["owner_id"], row["revision"], 1 if active else 0)


async def _validate_template(binding, gateway, user, restrictions, template):
    evidence = {}
    denied = await _valid_target(binding, user, restrictions, template, gateway, True, evidence)
    if denied:
        return denied, evidence
    category = await binding.prepare("""SELECT id FROM expense_categories
        WHERE owner_id=? AND id=? AND archived_at IS NULL""").bind(
            user["id"], template["category_id"]).first()
    return (None if category else _error(422, "INVALID_CATEGORY")), evidence


def _category_guard(binding, owner, category):
    return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
        THEN 1 ELSE 0 END)""").bind(owner, category)


async def handle_recurring_request(*, binding, gateway, method, path, headers, params, body, now):
    if path != RESOURCE and not path.startswith(RESOURCE + "/"):
        return None
    identifier = path[len(RESOURCE) + 1:] if path.startswith(RESOURCE + "/") else None
    if identifier is not None and not _valid_resource_id(identifier):
        return _error(404, "NOT_FOUND")
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        return _error(405, "METHOD_NOT_ALLOWED")
    if method == "POST" and identifier or method in {"PATCH", "DELETE"} and not identifier:
        return _error(404, "NOT_FOUND")
    try:
        return await _http(binding, gateway, method, identifier, headers, params, body, now)
    except (ValueError, UnicodeError):
        return _error(422, "INVALID_INPUT")
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    except PlanLimitError as error:
        return error.response()
    except GitHubFailure as error:
        return _error(error.status, error.code)


async def _http(binding, gateway, method, identifier, headers, params, body, now):
    limit, cursor = _page_inputs(params) if method == "GET" and not identifier else (50, "")
    key = _header(headers, "Idempotency-Key") if method == "POST" else ""
    if method == "POST" and (not 1 <= len(key.encode("utf-8")) <= 128 or any(ord(c) < 33 for c in key)):
        return _error(422, "INVALID_INPUT")
    expected = _revision(headers) if method in {"PATCH", "DELETE"} else None
    if method in {"PATCH", "DELETE"} and expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if expected is not None and expected < 0:
        return _error(422, "INVALID_INPUT")
    if method == "GET" and not identifier:
        target, project = _param(params, "target") or "all", _param(params, "projectId")
        if target not in {"all", "project", "shared"} or target == "shared" and project or project and not _valid_resource_id(project):
            return _error(422, "INVALID_INPUT")
        clauses, values = ["owner_id=?", "status!='canceled'", "id> ?", _VISIBLE_TARGET], [cursor]
        if target != "all":
            clauses.append("target_kind=?")
            values.append(target)
        if project:
            clauses.append("project_id=?")
            values.append(project)
        from .cloudflare_ledger_reports import restriction_filter
        user, restrictions, auth, validate = await ledger_principal(
            binding=binding, headers=headers, scope="expenses:read", now=now)
        if ((target == "shared" and not restrictions.get("shared"))
                or (project and not target_allowed(restrictions, "project", project))):
            return _error(403, "TARGET_FORBIDDEN")
        restricted, restricted_values = restriction_filter(restrictions)
        parts = await binding.batch([*auth, binding.prepare("SELECT * FROM expense_recurring_rules WHERE " +
            " AND ".join(clauses) + " " + restricted + " ORDER BY id LIMIT ?").bind(
                user["id"], *values, *restricted_values, limit + 1)])
        validate([part.results for part in parts[:len(auth)]])
        found = parts[-1].results
        items = found[:limit]
        return 200, {"items": [rule_dto(row) for row in items],
                     "nextCursor": items[-1]["id"] if len(found) > limit else None}
    user, restrictions, proof, found = await _snapshot(binding, headers, now,
        "expenses:read" if method == "GET" else "expenses:write",
        lambda owner, actor: [binding.prepare("SELECT * FROM expense_recurring_rules WHERE owner_id=? AND id=? AND " +
                _VISIBLE_TARGET).bind(owner, identifier or ""),
            binding.prepare("SELECT create_sha256,create_response_json,target_kind,project_id," + _VISIBLE_TARGET + """ AS target_visible
                FROM expense_recurring_rules WHERE owner_id=? AND create_key=?""").bind(owner, _creation_key(owner, actor, key))
            if key else binding.prepare("SELECT id FROM expense_recurring_rules WHERE 0")], actor_queries=True)
    current = found[0][0] if found[0] else None
    if identifier and (current is None or not target_allowed(restrictions, current["target_kind"], current["project_id"])):
        return _error(404, "NOT_FOUND")
    if method == "GET":
        return 200, rule_dto(current)
    if current and expected != current["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if method == "DELETE" or body == {"status": "paused"}:
        status = "canceled" if method == "DELETE" else "paused"
        if current["status"] == "canceled":
            return (204, None) if method == "DELETE" else _error(409, "RECURRING_CANCELED")
        if current["revision"] >= 9007199254740991:
            return _error(409, "REVISION_LIMIT")
        changed = {**current, "status": status, "revision": expected + 1,
                   "updated_at": _timestamp(now), "blocked_code": None}
        activity = await activity_commands(binding, user, "recurring_rule", current["id"],
            "cancel" if method == "DELETE" else "pause", rule_dto(current), rule_dto(changed), now, proof=proof)
        commands = [_write_guard(binding, proof, user["id"], now), _rule_guard(binding, current),
            binding.prepare("UPDATE expense_recurring_rules SET status='" + status + "', revision=revision+1, updated_at=?, blocked_code=NULL WHERE id=? AND owner_id=? AND revision=?").bind(
                _timestamp(now), current["id"], user["id"], expected),
            *activity,
            binding.prepare("DELETE FROM d1_command_guard")]
        await binding.batch(commands)
        return (204, None) if method == "DELETE" else (200, rule_dto({**current, "status": status,
            "revision": expected + 1, "updated_at": _timestamp(now), "blocked_code": None}))
    if current and current["status"] == "canceled":
        return _error(409, "RECURRING_CANCELED")
    if current and current["revision"] >= 9007199254740991:
        return _error(409, "REVISION_LIMIT")
    resuming = method == "PATCH" and body == {"status": "active"}
    if resuming:
        template, schedule = _template(current), json.loads(current["schedule_json"])
    else:
        template, schedule = _input(body)
    if current and (template["target_kind"] != current["target_kind"] or template["project_id"] != current["project_id"]):
        return _error(422, "RECURRING_TARGET_IMMUTABLE")
    digest = hashlib.sha256(_json({"template": template, "schedule": schedule}, 16384).encode("utf-8")).hexdigest()
    if method == "POST" and found[1]:
        saved = found[1][0]
        if not saved["target_visible"] or not target_allowed(
                restrictions, saved["target_kind"], saved["project_id"]):
            return _error(404, "NOT_FOUND")
        return (201, json.loads(saved["create_response_json"])) if digest == saved["create_sha256"] else _error(409, "IDEMPOTENCY_CONFLICT")
    denied, evidence = await _validate_template(binding, gateway, user, restrictions, template)
    if denied:
        return denied
    if method == "POST":
        count = await binding.prepare("SELECT COUNT(*) AS total FROM expense_recurring_rules WHERE owner_id=? AND status!='canceled' AND " +
            _VISIBLE_TARGET).bind(user["id"]).first()
        if count["total"] >= MAX_RULES_PER_OWNER:
            return _error(403, "RECURRING_RULE_LIMIT")
    future = local_today(schedule, now) + timedelta(days=1) if current else None
    occurrence = next_occurrence(schedule, future)
    status = "active" if occurrence else "completed"
    if current and not resuming and current["status"] in {"paused", "blocked"}:
        status = current["status"]
    stamp = _timestamp(now)
    row = {**(current or {}), "id": identifier or "rec_" + uuid.uuid4().hex,
        "owner_id": user["id"], "actor_user_id": proof["actor_user_id"],
        "target_kind": template["target_kind"], "project_id": template["project_id"],
        "template_json": _json(_grant_template(template, proof)), "schedule_json": _json(schedule, 2048), "status": status,
        "revision": expected + 1 if current else 1, **next_fields(schedule, occurrence),
        "blocked_code": current["blocked_code"] if current and status == "blocked" else None,
        "created_at": current["created_at"] if current else stamp, "updated_at": stamp}
    commands = [_write_guard(binding, proof, user["id"], now),
                _category_guard(binding, user["id"], template["category_id"])]
    if template["target_kind"] == "project":
        commands.append(_target_guard(binding, user["id"], evidence))
    if current:
        commands.append(_rule_guard(binding, current))
        commands.append(binding.prepare("""UPDATE expense_recurring_rules SET actor_user_id=?,template_json=?,schedule_json=?,
            status=?,revision=revision+1,next_run_at=?,next_occurrence_on=?,next_period_key=?,blocked_code=?,
            updated_at=? WHERE id=? AND owner_id=? AND revision=?""").bind(row["actor_user_id"], row["template_json"], row["schedule_json"],
                status, row["next_run_at"], row["next_occurrence_on"], row["next_period_key"], row["blocked_code"], stamp,
                row["id"], user["id"], expected))
    else:
        row.update(create_key=_creation_key(user["id"], proof["actor_user_id"], key),
                   create_sha256=digest, create_response_json=_json(rule_dto(row), 16384))
        columns = ("id", "owner_id", "actor_user_id", "target_kind", "project_id", "template_json", "schedule_json",
            "status", "revision", "next_run_at", "next_occurrence_on", "next_period_key", "blocked_code", "created_at",
            "updated_at", "create_key", "create_sha256", "create_response_json")
        commands.append(binding.prepare("INSERT INTO expense_recurring_rules(" + ",".join(columns) + ") VALUES(" +
            ",".join("?" for _ in columns) + ")").bind(*(row[name] for name in columns)))
    commands.extend(await activity_commands(binding, user, "recurring_rule", row["id"],
        "create" if not current else "resume" if resuming else "update",
        rule_dto(current) if current else None, rule_dto(row), now, proof=proof))
    commands.append(binding.prepare("DELETE FROM d1_command_guard"))
    await binding.batch(commands)
    return (200 if current else 201), rule_dto(row)


async def scheduled_authority(binding, row, now):
    """Current persisted authority of the real person who granted this rule."""
    owner, actor = row["owner_id"], row["actor_user_id"]
    statements = [binding.prepare("SELECT payload AS snapshot FROM app_state WHERE name=?").bind(record_name("users", owner))]
    if actor != owner:
        statements.extend([binding.prepare("SELECT payload AS snapshot FROM app_state WHERE name=?").bind(record_name("users", actor)),
            binding.prepare("SELECT role,revision,removed_at FROM workspace_members WHERE workspace_id=? AND user_id=?").bind(owner, actor)])
    stored_template = json.loads(row["template_json"])
    key_hash = stored_template.get("_api_key_hash")
    if "_api_key_hash" in stored_template:
        if not isinstance(key_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", key_hash):
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule key grant is invalid.")
        statements.append(binding.prepare("""SELECT user_id,scopes,expires_at,restrictions,revoked_at
            FROM api_keys WHERE key_hash=?""").bind(key_hash))
    rows = [part.results for part in await binding.batch(statements)]
    if not rows[0]:
        raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule owner is unavailable.")
    owner_user = json.loads(rows[0][0]["snapshot"])
    actor_user, member = owner_user, None
    if actor != owner:
        if not rows[1] or not rows[2]:
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule actor is unavailable.")
        actor_user, member = json.loads(rows[1][0]["snapshot"]), rows[2][0]
        if member["removed_at"] is not None or "expenses:write" not in ROLE_SCOPES.get(member["role"], set()):
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule actor no longer writes expenses.")
    if owner_user.get("id") != owner or actor_user.get("id") != actor:
        raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule actor is unavailable.")
    key, restrictions = None, {"shared": True}
    if key_hash is not None:
        key = rows[-1][0] if len(rows[-1]) == 1 else None
        expiry = auth_timestamp(key["expires_at"]) if key else None
        if (not key or key["user_id"] != actor or key["revoked_at"] is not None
                or (key["expires_at"] is not None and (expiry is None or expiry < now))):
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule API key is unavailable.")
        try:
            scopes = json.loads(key["scopes"])
            raw_restrictions = json.loads(key["restrictions"])
            restrictions = parse_api_key_restrictions(raw_restrictions)
        except (TypeError, ValueError):
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule API key authority is invalid.") from None
        if (not isinstance(scopes, list) or "expenses:write" not in scopes
                or restrictions != raw_restrictions
                or restrictions.get("workspaceId", actor) != owner
                or (member is not None and restrictions.get("workspaceMemberRevision") != member["revision"])
                or not target_allowed(restrictions, row["target_kind"], row["project_id"])):
            raise PrincipalAuthError(403, "SCHEDULE_AUTHORIZATION_CHANGED", "Rule API key no longer grants this target.")
    effective = {**owner_user, "_actor": actor_user}
    return effective, {"owner_snapshot": rows[0][0]["snapshot"],
        "actor_snapshot": rows[1][0]["snapshot"] if actor != owner else rows[0][0]["snapshot"],
        "member": member, "key_hash": key_hash, "key": key, "restrictions": restrictions}


def _schedule_guard(binding, row, authority, now):
    checks = ["EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?)",
              "EXISTS(SELECT 1 FROM expense_recurring_rules WHERE id=? AND owner_id=? AND actor_user_id=? AND revision=? AND status='active' AND " +
                  _VISIBLE_TARGET + ")"]
    values = [record_name("users", row["owner_id"]), authority["owner_snapshot"],
              row["id"], row["owner_id"], row["actor_user_id"], row["revision"]]
    if row["actor_user_id"] != row["owner_id"]:
        member = authority["member"]
        checks.extend(["EXISTS(SELECT 1 FROM app_state a WHERE a.name=? AND a.payload=?)",
            "EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND role=? AND revision=? AND removed_at IS NULL)"])
        values.extend([record_name("users", row["actor_user_id"]), authority["actor_snapshot"],
            row["owner_id"], row["actor_user_id"], member["role"], member["revision"]])
    if authority["key"] is not None:
        key = authority["key"]
        checks.append("""EXISTS(SELECT 1 FROM api_keys WHERE key_hash=? AND user_id=?
            AND scopes=? AND restrictions=? AND revoked_at IS NULL
            AND (expires_at IS NULL OR expires_at>=?))""")
        values.extend([authority["key_hash"], row["actor_user_id"], key["scopes"], key["restrictions"], now])
    return binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN " + " AND ".join(checks) + " THEN 1 ELSE 0 END)").bind(*values)


async def _maintenance(binding, row, *, now, code=None, following=None):
    """Trusted bookkeeping only; never bypass commercial limits for an expense."""
    schedule = json.loads(row["schedule_json"])
    fields = next_fields(schedule, following) if code is None else {
        name: row[name] for name in ("next_run_at", "next_occurrence_on", "next_period_key")}
    status = "blocked" if code else "active" if following else "completed"
    stamp = _timestamp(now)
    await binding.batch([_rule_guard(binding, row, active=True),
        binding.prepare("""UPDATE expense_recurring_rules SET status=?,revision=revision+1,
            next_run_at=?,next_occurrence_on=?,next_period_key=?,blocked_code=?,updated_at=?
            WHERE id=? AND owner_id=? AND revision=?""").bind(status, fields["next_run_at"],
                fields["next_occurrence_on"], fields["next_period_key"], code, stamp,
                row["id"], row["owner_id"], row["revision"]), binding.prepare("DELETE FROM d1_command_guard")])
    return {**row, **fields, "status": status, "revision": row["revision"] + 1,
            "blocked_code": code, "updated_at": stamp}


async def generate_occurrence(*, binding, maintenance_binding, gateway, row, now):
    if row["target_kind"] == "project":
        # A row selected before a concurrent removal must not publish anything,
        # including replay-pointer maintenance, or contact a provider.
        project = await binding.prepare("""SELECT id FROM ledger_projects
            WHERE owner_id=? AND id=? AND deleted_at IS NULL""").bind(
                row["owner_id"], row["project_id"]).first()
        if project is None:
            return row, None
    schedule = json.loads(row["schedule_json"])
    occurred = iso_date(row["next_occurrence_on"])
    following = following_occurrence(schedule, occurred)
    existing = await binding.prepare("SELECT expense_id FROM expense_recurring_occurrences WHERE rule_id=? AND period_key=?").bind(row["id"], row["next_period_key"]).first()
    if existing:
        return await _maintenance(maintenance_binding, row, now=now, following=following), "replayed"
    template = _template(row)
    try:
        user, authority = await scheduled_authority(binding, row, now)
        denied, evidence = await _validate_template(binding, gateway, user, authority["restrictions"], template)
        if denied:
            return await _maintenance(maintenance_binding, row, now=now, code=denied[1]["error"]["code"]), "blocked"
        from .cloudflare_expense_retention import prepare_expense_retention
        retirement = await prepare_expense_retention(binding, user, authority["restrictions"], now,
            schedule_actor_id=row["id"] + ":" + row["actor_user_id"])
    except (PrincipalAuthError, GitHubFailure) as error:
        return await _maintenance(maintenance_binding, row, now=now, code=error.code), "blocked"
    except PlanLimitError as error:
        return await _maintenance(maintenance_binding, row, now=now, code=error.code), "blocked"
    stamp = _timestamp(now)
    record = {**template, "occurred_on": occurred.isoformat(), "id": "exp_" + uuid.uuid4().hex,
              "owner_id": row["owner_id"], "revision": 1, "created_at": stamp, "updated_at": stamp, "deleted_at": None}
    payload = expense_dto(record)
    event_id = "evt_" + uuid.uuid4().hex
    activity = await activity_commands(binding, user, "expense", record["id"], "generate", None,
        payload, now, scheduled=True, operation_id=event_id)
    fields = next_fields(schedule, following)
    columns = ("id", "owner_id", "target_kind", "project_id", "category_id", "occurred_on", "amount_minor",
               "currency", "purpose", "note", "quantity_decimal", "unit", "revision", "created_at", "updated_at", "deleted_at")
    commands = [_schedule_guard(binding, row, authority, now), _category_guard(binding, row["owner_id"], template["category_id"])]
    if row["target_kind"] == "project":
        commands.append(_target_guard(binding, row["owner_id"], evidence))
    commands.extend([*retirement,
        binding.prepare("INSERT INTO expenses(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")").bind(*(record[name] for name in columns)),
        binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
            action,before_json,after_json,created_at) VALUES(?,?,?,'schedule',?,'create',NULL,?,?)""").bind(
                event_id, record["id"], row["owner_id"], row["id"] + ":" + row["actor_user_id"], _json(payload, 16384), stamp),
        binding.prepare("""INSERT INTO expense_recurring_occurrences(rule_id,owner_id,period_key,
            scheduled_on,expense_id,rule_revision,created_at) VALUES(?,?,?,?,?,?,?)""").bind(
                row["id"], row["owner_id"], row["next_period_key"], occurred.isoformat(), record["id"], row["revision"], stamp),
        binding.prepare("""UPDATE expense_recurring_rules SET status=?,revision=revision+1,next_run_at=?,
            next_occurrence_on=?,next_period_key=?,blocked_code=NULL,updated_at=? WHERE id=? AND owner_id=? AND revision=?""").bind(
                "active" if following else "completed", fields["next_run_at"], fields["next_occurrence_on"], fields["next_period_key"], stamp,
                row["id"], row["owner_id"], row["revision"]), *activity, binding.prepare("DELETE FROM d1_command_guard")])
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return await _maintenance(maintenance_binding, row, now=now, code=error.code), "blocked"
    # Unknown native outcomes propagate to the original coordinator/journal.
    return {**row, **fields, "status": "active" if following else "completed",
            "revision": row["revision"] + 1, "updated_at": stamp, "blocked_code": None}, "created"


async def run_due_recurring(*, binding, gateway, now, rule_limit=MAX_TICK_RULES,
                            occurrence_limit=MAX_TICK_OCCURRENCES, maintenance_binding=None):
    if (type(rule_limit) is not int or not 1 <= rule_limit <= MAX_TICK_RULES
            or type(occurrence_limit) is not int or not 1 <= occurrence_limit <= MAX_TICK_OCCURRENCES):
        raise ValueError("recurring tick bounds")
    maintenance_binding = maintenance_binding or getattr(binding, "binding", binding)
    # Reuse the existing bounded hourly maintenance path, including idle ticks.
    # Financial expense/workspace audit history is never part of this cleanup.
    expired = await retention_commands(maintenance_binding, now)
    if expired:
        await maintenance_binding.batch(expired)
    found = await binding.prepare("""SELECT * FROM expense_recurring_rules WHERE status='active'
        AND next_run_at<=? AND """ + _VISIBLE_TARGET + " ORDER BY next_run_at,id LIMIT ?").bind(now, rule_limit).all()
    counts = {"scanned": len(found.results), "created": 0, "blocked": 0, "replayed": 0}
    handled = 0
    for row in found.results:
        schedule = json.loads(row["schedule_json"])
        probe = iso_date(row["next_occurrence_on"])
        due_periods = 0
        while probe is not None and due_periods <= 12 and next_fields(schedule, probe)["next_run_at"] <= now:
            due_periods += 1
            probe = following_occurrence(schedule, probe)
        if due_periods > 12:
            await _maintenance(maintenance_binding, row, now=now, code="CATCHUP_REVIEW_REQUIRED")
            counts["blocked"] += 1
            handled += 1
            if handled >= occurrence_limit:
                break
            continue
        per_rule = 0
        while (row["status"] == "active" and row["next_run_at"] is not None
               and row["next_run_at"] <= now and handled < occurrence_limit and per_rule < 3):
            row, result = await generate_occurrence(binding=binding, maintenance_binding=maintenance_binding,
                gateway=gateway, row=row, now=now)
            if result is None:
                break
            counts[result] += 1
            handled += 1
            per_rule += 1
        if handled >= occurrence_limit:
            break
    return counts
