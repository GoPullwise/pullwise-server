"""Exact-money ledger expense operations with guarded D1 writes and audit events."""
from __future__ import annotations

from .cloudflare_plan_limits import PlanLimitError

import hashlib
import json
import re
import uuid
from datetime import date
from typing import Any, Mapping

from .cloudflare_ledger_api import (
    _error, _live_repos, _param, _revision, _timestamp, _valid_resource_id, _write_guard,
)
from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_principal import PrincipalAuthError, _header
from .account_cycle_rules import PAID_PLAN_IDS, effective_user_plan
from .cloudflare_jev_preferences import ensure_current_jev_authority, jev_enabled

# ISO 4217 active alphabetic units; exponents are fixed here so Worker runtime
# does not depend on the host locale or a floating point conversion library.
SUPPORTED = set("""AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD
 BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
 CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
 GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD
 JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL
 MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR
 NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG
 SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY
 TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST YER
 ZAR ZMW ZWL""".split())
EXPONENTS = {code: 0 for code in "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV".split()}
EXPONENTS.update({code: 3 for code in "BHD IQD JOD KWD LYD OMR TND".split()})
EXPONENTS.update({"CLF": 4, "UYW": 4})
MAX_MINOR = 9007199254740991

# The stored creation DTO can reference a different target than an expense
# moved later. Check its original target before returning an idempotent replay;
# a removed project must never be exposed through that immutable response.
_REPLAY_PROJECTION = """*, CASE WHEN (
    json_extract(response_json,'$.target.kind')='shared' OR
    (json_extract(response_json,'$.target.kind')='project' AND EXISTS(
        SELECT 1 FROM ledger_projects AS visible_project
        WHERE visible_project.owner_id=expense_create_idempotency.owner_id
            AND visible_project.id=json_extract(response_json,'$.target.projectId')
            AND visible_project.deleted_at IS NULL)))
    AND EXISTS(SELECT 1 FROM expenses AS replayed_expense
        WHERE replayed_expense.owner_id=expense_create_idempotency.owner_id
            AND replayed_expense.id=expense_create_idempotency.expense_id
            AND (replayed_expense.target_kind='shared' OR
                (replayed_expense.target_kind='project' AND EXISTS(
                    SELECT 1 FROM ledger_projects AS current_project
                    WHERE current_project.owner_id=replayed_expense.owner_id
                        AND current_project.id=replayed_expense.project_id
                        AND current_project.deleted_at IS NULL))))
    THEN 1 ELSE 0 END AS target_visible"""


def _amount(value, currency):
    if (not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value)
            or currency not in SUPPORTED):
        raise ValueError("invalid amount")
    exponent = EXPONENTS.get(currency, 2)
    fractional = value.partition(".")[2]
    if len(fractional) > exponent:
        raise ValueError("excess precision")
    minor = int(value.partition(".")[0]) * 10**exponent + int(fractional.ljust(exponent, "0") or "0")
    if minor > MAX_MINOR:
        raise ValueError("amount overflow")
    return minor


def _input(body, *, allow_missing_category=False):
    fields = {"target", "occurredOn", "amount", "currency", "categoryId", "purpose",
              "note", "quantity", "unit"}
    optional = {"note", "quantity", "unit"} | ({"categoryId"} if allow_missing_category else set())
    if not isinstance(body, dict) or set(body) - fields or not fields.difference(optional) <= set(body):
        raise ValueError("fields")
    target = body["target"]
    if (not isinstance(target, dict) or not isinstance(target.get("kind"), str)
            or target["kind"] not in {"project", "shared"}
            or set(target) != ({"kind", "projectId"} if target.get("kind") == "project" else {"kind"})
            or (target["kind"] == "project" and not _valid_resource_id(target["projectId"]))):
        raise ValueError("target")
    occurred = body["occurredOn"]
    if not isinstance(occurred, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", occurred):
        raise ValueError("date")
    try:
        if date.fromisoformat(occurred).isoformat() != occurred:
            raise ValueError("date")
    except ValueError:
        raise ValueError("date") from None
    currency = body["currency"]
    if not isinstance(currency, str):
        raise ValueError("currency")
    minor = _amount(body["amount"], currency)
    category_id = body.get("categoryId")
    if ((category_id not in (None, "") and not _valid_resource_id(category_id))
            or (not allow_missing_category and not category_id)
            or not isinstance(body["purpose"], str) or not 1 <= len(body["purpose"].strip()) <= 500):
        raise ValueError("text")
    for field, maximum in (("note", 4000), ("unit", 40)):
        value = body.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(field)
    quantity = body.get("quantity")
    if quantity is not None and (not isinstance(quantity, str) or len(quantity) > 40
            or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", quantity)):
        raise ValueError("quantity")
    return {"target_kind": target["kind"], "project_id": target.get("projectId"),
            "occurred_on": occurred, "amount_minor": minor, "currency": currency,
            "category_id": category_id or None, "purpose": body["purpose"].strip(),
            "note": body.get("note"), "quantity_decimal": quantity, "unit": body.get("unit")}


def _decimal_amount(minor, currency):
    exponent = EXPONENTS.get(currency, 2)
    return str(minor) if exponent == 0 else f"{minor // 10**exponent}.{minor % 10**exponent:0{exponent}d}"


def _dto(row):
    currency = row["currency"]
    minor = row["amount_minor"]
    amount = _decimal_amount(minor, currency)
    return {"id": row["id"], "target": {"kind": row["target_kind"],
            **({"projectId": row["project_id"]} if row["target_kind"] == "project" else {})},
            "occurredOn": row["occurred_on"], "amount": amount, "amountMinor": minor,
            "currency": currency, "categoryId": row["category_id"], "purpose": row["purpose"],
            "note": row["note"], "quantity": row["quantity_decimal"], "unit": row["unit"],
            "revision": row["revision"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]}


async def _snapshot(binding, headers, now, scope, queries, *, actor_queries=False):
    proof = {}
    user, restrictions, auth, validate = await ledger_principal(
        binding=binding, headers=headers, scope=scope, now=now, proof=proof)
    resource_queries = queries(user["id"], user["_actor"]["id"]) if actor_queries else queries(user["id"])
    rows = await binding.batch([*auth, *resource_queries])
    validate([part.results for part in rows[:len(auth)]])
    return user, restrictions, proof, [part.results for part in rows[len(auth):]]


def _actor(proof):
    actor_id = proof["actor_user_id"]
    if proof.get("key") is None:
        return "session", actor_id
    return "api_key", actor_id + ":sha256:" + hashlib.sha256(proof["token"].encode()).hexdigest()


def _creation_key(owner_id, actor_id, key):
    # Preserve old personal replays. A space is forbidden in incoming keys,
    # giving member requests a disjoint namespace without rewriting history.
    return key if owner_id == actor_id else "member " + hashlib.sha256(
        (actor_id + "\0" + key).encode("utf-8")).hexdigest()


async def _valid_target(binding, user, restrictions, data, gateway, writing, evidence=None):
    kind, project_id = data["target_kind"], data["project_id"]
    if not target_allowed(restrictions, kind, project_id):
        return _error(403, "TARGET_FORBIDDEN")
    if kind == "project":
        row = await binding.prepare("""SELECT status FROM ledger_projects
            WHERE owner_id=? AND id=? AND deleted_at IS NULL""").bind(user["id"], project_id).first()
        if row is None:
            return _error(404, "NOT_FOUND")
        if writing:
            from .cloudflare_project_repositories import project_repository_eligibility
            current = await project_repository_eligibility(binding, user, project_id, gateway)
            if current is None:
                return _error(403, "GITHUB_ACCESS_REQUIRED")
            if evidence is not None:
                evidence.update(current, projectId=project_id)
    return None


def _target_guard(binding, owner_id, evidence):
    if evidence["githubRepoId"] is None:
        return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
            EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=? AND id=? AND revision=?
                AND status='active' AND deleted_at IS NULL AND github_repo_id IS NULL)
            AND NOT EXISTS(SELECT 1 FROM ledger_project_repositories WHERE owner_id=? AND project_id=?)
            THEN 1 ELSE 0 END)""").bind(owner_id, evidence["projectId"], evidence["revision"],
                                          owner_id, evidence["projectId"])
    return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=? AND id=? AND revision=?
            AND status='active' AND deleted_at IS NULL)
        AND EXISTS(SELECT 1 FROM ledger_project_repositories WHERE owner_id=? AND project_id=? AND github_repo_id=?)
        THEN 1 ELSE 0 END)""").bind(owner_id, evidence["projectId"], evidence["revision"],
                                     owner_id, evidence["projectId"], evidence["githubRepoId"])


async def handle_expense_request(*, binding: Any, gateway: Any, method: str, path: str,
                                 headers: Mapping[str, object], params: Mapping[str, object],
                                 body: object, now: int, suggestion_gateway=None):
    if not path.startswith("/api/v1/expenses") or path not in {"/api/v1/expenses"} and not path.startswith("/api/v1/expenses/"):
        return None
    item_id = path[len("/api/v1/expenses/"):] if path.startswith("/api/v1/expenses/") else None
    if item_id and not _valid_resource_id(item_id):
        return _error(404, "NOT_FOUND")
    if item_id == "export":
        return None
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        return _error(405, "METHOD_NOT_ALLOWED")
    if (method == "POST" and item_id) or (method in {"PATCH", "DELETE"} and not item_id):
        return _error(404, "NOT_FOUND")
    try:
        if method == "GET":
            return await _read(binding, headers, params, item_id, now)
        if method in {"POST", "PATCH"}:
            try:
                data = _input(body, allow_missing_category=True)
            except ValueError:
                return _error(422, "INVALID_INPUT")
        else:
            data = None
        return await _write(binding, gateway, method, item_id, headers, data, now, suggestion_gateway)
    except PrincipalAuthError as exc:
        return _error(exc.status, exc.code)


async def _read(binding, headers, params, item_id, now):
    from .cloudflare_ledger_reports import VISIBLE_EXPENSE_TARGET_SQL
    if item_id:
        user, restrictions, _, rows = await _snapshot(binding, headers, now, "expenses:read",
            lambda owner: [binding.prepare("SELECT * FROM expenses WHERE id=? AND owner_id=? "
                "AND deleted_at IS NULL " + VISIBLE_EXPENSE_TARGET_SQL).bind(item_id, owner)])
        if not rows[0]:
            return _error(404, "NOT_FOUND")
        row = rows[0][0]
        if not target_allowed(restrictions, row["target_kind"], row["project_id"]):
            return _error(403, "TARGET_FORBIDDEN")
        return 200, _dto(row)
    from .cloudflare_ledger_reports import expense_filter, restriction_filter
    try:
        where, values, limit, cursor = expense_filter(params, paged=True)
    except ValueError:
        return _error(422, "INVALID_INPUT")
    user, restrictions, auth, validate = await ledger_principal(
        binding=binding, headers=headers, scope="expenses:read", now=now)
    if (_param(params, "target") == "shared" and not restrictions.get("shared")) or (
            _param(params, "projectId") and not target_allowed(
                restrictions, "project", _param(params, "projectId"))):
        return _error(403, "TARGET_FORBIDDEN")
    restricted, restricted_values = restriction_filter(restrictions)
    parts = await binding.batch([*auth, binding.prepare(
        "SELECT * FROM expenses WHERE owner_id=? AND deleted_at IS NULL " + where +
        " " + restricted + " AND id>? ORDER BY id LIMIT ?").bind(
        user["id"], *values, *restricted_values, cursor, limit + 1)])
    validate([part.results for part in parts[:len(auth)]])
    items = parts[-1].results
    page = items[:limit]
    return 200, {"items": [_dto(row) for row in page],
                 "nextCursor": page[-1]["id"] if len(items) > limit else None}


async def _write(binding, gateway, method, item_id, headers, data, now, suggestion_gateway=None):
    from .cloudflare_ledger_reports import VISIBLE_EXPENSE_TARGET_SQL
    key = _header(headers, "Idempotency-Key") if method == "POST" else ""
    if method == "POST" and (not 1 <= len(key) <= 128 or any(ord(c) < 33 for c in key)):
        return _error(422, "INVALID_INPUT")
    expected = _revision(headers) if method != "POST" else None
    if method != "POST" and expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if method != "POST" and expected < 0:
        return _error(422, "INVALID_INPUT")
    user, restrictions, proof, rows = await _snapshot(binding, headers, now, "expenses:write",
        lambda owner, actor: [binding.prepare("SELECT * FROM expenses WHERE id=? AND owner_id=? "
                + VISIBLE_EXPENSE_TARGET_SQL).bind(item_id or "", owner),
            binding.prepare("SELECT " + _REPLAY_PROJECTION + " FROM expense_create_idempotency "
                "WHERE owner_id=? AND idempotency_key=?").bind(owner, _creation_key(owner, actor, key))
                if key else binding.prepare("SELECT * FROM expense_create_idempotency WHERE 0")], actor_queries=True)
    if key:
        key = _creation_key(user["id"], user["_actor"]["id"], key)
    current = rows[0][0] if rows[0] else None
    if item_id and current is None:
        return _error(404, "NOT_FOUND")
    if current and not target_allowed(restrictions, current["target_kind"], current["project_id"]):
        return _error(403, "TARGET_FORBIDDEN")
    if method == "DELETE" and current["deleted_at"] is not None:
        return 204, None
    if method != "POST" and expected != current["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    digest = None
    if method == "POST":
        # Hash the caller's normalized intent, before any inferred category.
        # A replay authenticates current key/target authority but does not
        # revalidate archived categories/projects or call any provider.
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not target_allowed(restrictions, data["target_kind"], data["project_id"]):
            return _error(403, "TARGET_FORBIDDEN")
        if rows[1]:
            saved = rows[1][0]
            if saved["request_sha256"] != digest:
                return _error(409, "IDEMPOTENCY_CONFLICT")
            if not saved["target_visible"]:
                return _error(404, "NOT_FOUND")
            return 201, json.loads(saved["response_json"])
    assistance = None
    automatic_requested = data is not None and data["category_id"] is None
    target_evidence = {}
    if method == "POST" or method == "PATCH":
        new_target = method == "POST" or (current is not None and (
            data["target_kind"] != current["target_kind"] or data["project_id"] != current["project_id"]))
        target_error = await _valid_target(binding, user, restrictions, data, gateway, new_target, target_evidence)
        if target_error:
            return target_error
        if data["category_id"]:
            unchanged_category = method == "PATCH" and current["category_id"] == data["category_id"]
            category = await binding.prepare("""SELECT id FROM expense_categories WHERE owner_id=?
                AND id=? AND (archived_at IS NULL OR ?)""").bind(
                    user["id"], data["category_id"], 1 if unchanged_category else 0).first()
            if category is None:
                return _error(422, "INVALID_CATEGORY")
        if effective_user_plan(user, timestamp=now) in PAID_PLAN_IDS:
            assistance = await _automatic_assistance(binding, headers, data, now, suggestion_gateway, item_id, user)
            await ensure_current_jev_authority(binding, headers, now, proof, scope="expenses:write",
                target_kind=data["target_kind"], project_id=data["project_id"])
            if not data["category_id"]:
                data = {**data, "category_id": assistance["suggestions"].get("categoryId")}
                if not data["category_id"]:
                    return 422, {"error": {"code": "CATEGORY_REQUIRED"}, "assistance": assistance}
                assistance["categorySource"] = "jev"
        elif not data["category_id"]:
            return _error(422, "CATEGORY_REQUIRED")
    stamp = _timestamp(now)
    from .cloudflare_ledger_activity import activity_commands
    actor_kind, actor_id = _actor(proof)
    event_id = "evt_" + uuid.uuid4().hex
    if method == "POST":
        expense_id = "exp_" + uuid.uuid4().hex
        record = {**data, "id": expense_id, "owner_id": user["id"], "revision": 1,
                  "created_at": stamp, "updated_at": stamp, "deleted_at": None}
        payload = _dto(record)
        if assistance is not None:
            payload["assistance"] = assistance
        activity = await activity_commands(binding, user, "expense", expense_id, "create", None,
            payload, now, proof=proof, operation_id=event_id)
        values = [record[name] for name in ("id", "owner_id", "target_kind", "project_id", "category_id",
            "occurred_on", "amount_minor", "currency", "purpose", "note", "quantity_decimal", "unit",
            "revision", "created_at", "updated_at", "deleted_at")]
        commands = [_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
              EXISTS(SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
              THEN 1 ELSE 0 END)""").bind(user["id"], data["category_id"]),
            (_target_guard(binding, user["id"], target_evidence) if data["target_kind"] == "project" else binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
              ?='shared' OR EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=? AND id=?
                AND status='active' AND deleted_at IS NULL) THEN 1 ELSE 0 END)""").bind(
                data["target_kind"], user["id"], data["project_id"])),
            binding.prepare("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
              occurred_on,amount_minor,currency,purpose,note,quantity_decimal,unit,revision,
              created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""").bind(*values),
            binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
              action,before_json,after_json,created_at) VALUES(?,?,?,?,?,'create',NULL,?,?)""").bind(
                event_id, expense_id, user["id"], actor_kind, actor_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), stamp),
            binding.prepare("""INSERT INTO expense_create_idempotency(owner_id,idempotency_key,
              request_sha256,expense_id,response_json,created_at) VALUES(?,?,?,?,?,?)""").bind(
                user["id"], key, digest, expense_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), stamp),
            *activity,
            binding.prepare("DELETE FROM d1_command_guard")]
        try:
            await binding.batch(commands)
        except PlanLimitError as error:
            return error.response()
        except Exception:
            try:
                _, replay_restrictions, _, replay_rows = await _snapshot(binding, headers, now, "expenses:write",
                    lambda owner: [binding.prepare("SELECT " + _REPLAY_PROJECTION +
                        " FROM expense_create_idempotency WHERE owner_id=? AND idempotency_key=?").bind(owner, key)])
                if not target_allowed(replay_restrictions, data["target_kind"], data["project_id"]):
                    return _error(403, "TARGET_FORBIDDEN")
                if replay_rows[0] and replay_rows[0][0]["request_sha256"] == digest:
                    if not replay_rows[0][0]["target_visible"]:
                        return _error(404, "NOT_FOUND")
                    return 201, json.loads(replay_rows[0][0]["response_json"])
            except PrincipalAuthError as exc:
                return _error(exc.status, exc.code)
            return _error(409, "EXPENSE_CONFLICT")
        return 201, payload
    before = _dto(current)
    if method == "PATCH":
        record = {**current, **data, "revision": expected + 1, "updated_at": stamp}
        after = _dto(record)
        if assistance is not None:
            after["assistance"] = assistance
        sql = """UPDATE expenses SET target_kind=?,project_id=?,category_id=?,occurred_on=?,
          amount_minor=?,currency=?,purpose=?,note=?,quantity_decimal=?,unit=?,revision=revision+1,
          updated_at=? WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL """ + VISIBLE_EXPENSE_TARGET_SQL
        values = [data[name] for name in ("target_kind", "project_id", "category_id", "occurred_on",
            "amount_minor", "currency", "purpose", "note", "quantity_decimal", "unit")]
        values += [stamp, item_id, user["id"], expected]
    else:
        after = None
        sql = """UPDATE expenses SET deleted_at=?,updated_at=?,revision=revision+1
            WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL """ + VISIBLE_EXPENSE_TARGET_SQL
        values = [stamp, stamp, item_id, user["id"], expected]
    action = "move" if after is not None and before["target"] != after["target"] else (
        "update" if method == "PATCH" else "delete")
    activity = await activity_commands(binding, user, "expense", item_id, action, before,
        after, now, proof=proof, operation_id=event_id)
    commands = [_write_guard(binding, proof, user["id"], now)]
    if method == "PATCH" and (automatic_requested or data["category_id"] != current["category_id"]):
        commands.append(binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
            EXISTS(SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
            THEN 1 ELSE 0 END)""").bind(user["id"], data["category_id"]))
    if method == "PATCH" and new_target and data["target_kind"] == "project":
        commands.append(_target_guard(binding, user["id"], target_evidence))
    commands += [binding.prepare(sql).bind(*values),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
          action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""").bind(
            event_id, item_id, user["id"], actor_kind, actor_id,
            "update" if method == "PATCH" else "delete",
            json.dumps(before, ensure_ascii=False, separators=(",", ":")),
            json.dumps(after, ensure_ascii=False, separators=(",", ":")) if after else None, stamp),
        *activity,
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(412, "PRECONDITION_FAILED")
    return (200, after) if method == "PATCH" else (204, None)


async def _automatic_assistance(binding, headers, data, now, gateway, item_id, user):
    """Use the ordinary expense-write authorization; inference never writes money."""
    if gateway is None or not gateway.enabled or not jev_enabled(user):
        return {"status": "unavailable", "reason": "disabled", "suggestions": {},
            "categorySource": "user" if data["category_id"] else None}
    from .cloudflare_ledger_suggestions import handle_suggestion_request
    status, result = await handle_suggestion_request(binding=binding, method="POST",
        headers=headers, now=now, gateway=gateway, scope="expenses:write",
        exclude_expense_id=item_id, body={"target": {"kind": data["target_kind"],
            **({"projectId": data["project_id"]} if data["project_id"] else {})},
            "purpose": data["purpose"], "note": data["note"] or "",
            "occurredOn": data["occurred_on"], "amount": _decimal_amount(data["amount_minor"], data["currency"]),
            "currency": data["currency"]})
    if status != 200:
        return {"status": "unavailable", "reason": result.get("error", {}).get("code", "unavailable"),
            "suggestions": {}, "categorySource": "user" if data["category_id"] else None}
    return {**result, "categorySource": "user" if data["category_id"] else None}
