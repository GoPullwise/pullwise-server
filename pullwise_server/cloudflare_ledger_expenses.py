"""Exact-money ledger expense operations with guarded D1 writes and audit events."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date
from typing import Any, Mapping

from .cloudflare_ledger_api import _error, _live_repos, _param, _revision, _timestamp, _write_guard
from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_product_read import ProductReadAuthError, _header

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


def _input(body):
    fields = {"target", "occurredOn", "amount", "currency", "categoryId", "purpose",
              "note", "quantity", "unit"}
    if not isinstance(body, dict) or set(body) - fields or not fields.difference({"note", "quantity", "unit"}) <= set(body):
        raise ValueError("fields")
    target = body["target"]
    if (not isinstance(target, dict) or target.get("kind") not in {"project", "shared"}
            or set(target) != ({"kind", "projectId"} if target.get("kind") == "project" else {"kind"})
            or (target["kind"] == "project" and (not isinstance(target["projectId"], str)
                or not target["projectId"]))):
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
    if (not isinstance(body["categoryId"], str) or not body["categoryId"]
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
            "category_id": body["categoryId"], "purpose": body["purpose"].strip(),
            "note": body.get("note"), "quantity_decimal": quantity, "unit": body.get("unit")}


def _dto(row):
    currency = row["currency"]
    exponent = EXPONENTS.get(currency, 2)
    minor = row["amount_minor"]
    amount = str(minor) if exponent == 0 else f"{minor // 10**exponent}.{minor % 10**exponent:0{exponent}d}"
    return {"id": row["id"], "target": {"kind": row["target_kind"],
            **({"projectId": row["project_id"]} if row["target_kind"] == "project" else {})},
            "occurredOn": row["occurred_on"], "amount": amount, "amountMinor": minor,
            "currency": currency, "categoryId": row["category_id"], "purpose": row["purpose"],
            "note": row["note"], "quantity": row["quantity_decimal"], "unit": row["unit"],
            "revision": row["revision"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"]}


async def _snapshot(binding, headers, now, scope, queries):
    proof = {}
    user, restrictions, auth, validate = await ledger_principal(
        binding=binding, headers=headers, scope=scope, now=now, proof=proof)
    rows = await binding.batch([*auth, *queries(user["id"])])
    validate([part.results for part in rows[:len(auth)]])
    return user, restrictions, proof, [part.results for part in rows[len(auth):]]


def _actor(proof):
    if proof.get("key") is None:
        return "session", proof["session_id"]
    return "api_key", "sha256:" + hashlib.sha256(proof["token"].encode()).hexdigest()


async def _valid_target(binding, user, restrictions, data, gateway, writing):
    kind, project_id = data["target_kind"], data["project_id"]
    if not target_allowed(restrictions, kind, project_id):
        return _error(403, "TARGET_FORBIDDEN")
    if kind == "project":
        row = await binding.prepare("""SELECT github_repo_id,status FROM ledger_projects
            WHERE owner_id=? AND id=?""").bind(user["id"], project_id).first()
        if row is None:
            return _error(404, "NOT_FOUND")
        if writing and (row["status"] != "active" or row["github_repo_id"] not in await _live_repos(user, gateway)):
            return _error(403, "GITHUB_ACCESS_REQUIRED")
    return None


async def handle_expense_request(*, binding: Any, gateway: Any, method: str, path: str,
                                 headers: Mapping[str, object], params: Mapping[str, object],
                                 body: object, now: int):
    if not path.startswith("/api/v1/expenses") or path not in {"/api/v1/expenses"} and not path.startswith("/api/v1/expenses/"):
        return None
    item_id = path[len("/api/v1/expenses/"):] if path.startswith("/api/v1/expenses/") else None
    if item_id and "/" in item_id:
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
                data = _input(body)
            except ValueError:
                return _error(422, "INVALID_INPUT")
        else:
            data = None
        return await _write(binding, gateway, method, item_id, headers, data, now)
    except ProductReadAuthError as exc:
        return _error(exc.status, exc.code)


async def _read(binding, headers, params, item_id, now):
    if item_id:
        user, restrictions, _, rows = await _snapshot(binding, headers, now, "expenses:read",
            lambda owner: [binding.prepare("SELECT * FROM expenses WHERE id=? AND owner_id=? AND deleted_at IS NULL").bind(item_id, owner)])
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


async def _write(binding, gateway, method, item_id, headers, data, now):
    key = _header(headers, "Idempotency-Key") if method == "POST" else ""
    if method == "POST" and (not 1 <= len(key) <= 128 or any(ord(c) < 33 for c in key)):
        return _error(422, "INVALID_INPUT")
    expected = _revision(headers) if method != "POST" else None
    if method != "POST" and expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if method != "POST" and expected < 0:
        return _error(422, "INVALID_INPUT")
    user, restrictions, proof, rows = await _snapshot(binding, headers, now, "expenses:write",
        lambda owner: [binding.prepare("SELECT * FROM expenses WHERE id=? AND owner_id=?").bind(item_id or "", owner),
            binding.prepare("SELECT * FROM expense_create_idempotency WHERE owner_id=? AND idempotency_key=?").bind(owner, key) if key else binding.prepare("SELECT * FROM expense_create_idempotency WHERE 0")])
    current = rows[0][0] if rows[0] else None
    if item_id and current is None:
        return _error(404, "NOT_FOUND")
    if current and not target_allowed(restrictions, current["target_kind"], current["project_id"]):
        return _error(403, "TARGET_FORBIDDEN")
    if method == "DELETE" and current["deleted_at"] is not None:
        return 204, None
    if method != "POST" and expected != current["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if method == "POST" or method == "PATCH":
        new_target = method == "POST" or (current is not None and (
            data["target_kind"] != current["target_kind"] or data["project_id"] != current["project_id"]))
        target_error = await _valid_target(binding, user, restrictions, data, gateway, new_target)
        if target_error:
            return target_error
        unchanged_category = method == "PATCH" and current["category_id"] == data["category_id"]
        category = await binding.prepare("""SELECT id FROM expense_categories WHERE owner_id=?
            AND id=? AND (archived_at IS NULL OR ?)""").bind(
                user["id"], data["category_id"], 1 if unchanged_category else 0).first()
        if category is None:
            return _error(422, "INVALID_CATEGORY")
    stamp = _timestamp(now)
    actor_kind, actor_id = _actor(proof)
    event_id = "evt_" + uuid.uuid4().hex
    if method == "POST":
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if rows[1]:
            saved = rows[1][0]
            return (201, json.loads(saved["response_json"])) if saved["request_sha256"] == digest else _error(409, "IDEMPOTENCY_CONFLICT")
        expense_id = "exp_" + uuid.uuid4().hex
        record = {**data, "id": expense_id, "owner_id": user["id"], "revision": 1,
                  "created_at": stamp, "updated_at": stamp, "deleted_at": None}
        payload = _dto(record)
        values = [record[name] for name in ("id", "owner_id", "target_kind", "project_id", "category_id",
            "occurred_on", "amount_minor", "currency", "purpose", "note", "quantity_decimal", "unit",
            "revision", "created_at", "updated_at", "deleted_at")]
        commands = [_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
              EXISTS(SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
              THEN 1 ELSE 0 END)""").bind(user["id"], data["category_id"]),
            binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
              ?='shared' OR EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=? AND id=?
                AND status='active') THEN 1 ELSE 0 END)""").bind(
                data["target_kind"], user["id"], data["project_id"]),
            binding.prepare("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
              occurred_on,amount_minor,currency,purpose,note,quantity_decimal,unit,revision,
              created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""").bind(*values),
            binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
              action,before_json,after_json,created_at) VALUES(?,?,?,?,?,'create',NULL,?,?)""").bind(
                event_id, expense_id, user["id"], actor_kind, actor_id,
                json.dumps(payload, separators=(",", ":")), stamp),
            binding.prepare("""INSERT INTO expense_create_idempotency(owner_id,idempotency_key,
              request_sha256,expense_id,response_json,created_at) VALUES(?,?,?,?,?,?)""").bind(
                user["id"], key, digest, expense_id, json.dumps(payload, separators=(",", ":")), stamp),
            binding.prepare("DELETE FROM d1_command_guard")]
        try:
            await binding.batch(commands)
        except Exception:
            try:
                _, _, _, replay_rows = await _snapshot(binding, headers, now, "expenses:write",
                    lambda owner: [binding.prepare("""SELECT request_sha256,response_json
                        FROM expense_create_idempotency WHERE owner_id=? AND idempotency_key=?""").bind(owner, key)])
                if replay_rows[0] and replay_rows[0][0]["request_sha256"] == digest:
                    return 201, json.loads(replay_rows[0][0]["response_json"])
            except ProductReadAuthError as exc:
                return _error(exc.status, exc.code)
            return _error(409, "EXPENSE_CONFLICT")
        return 201, payload
    before = _dto(current)
    if method == "PATCH":
        record = {**current, **data, "revision": expected + 1, "updated_at": stamp}
        after = _dto(record)
        sql = """UPDATE expenses SET target_kind=?,project_id=?,category_id=?,occurred_on=?,
          amount_minor=?,currency=?,purpose=?,note=?,quantity_decimal=?,unit=?,revision=revision+1,
          updated_at=? WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL"""
        values = [data[name] for name in ("target_kind", "project_id", "category_id", "occurred_on",
            "amount_minor", "currency", "purpose", "note", "quantity_decimal", "unit")]
        values += [stamp, item_id, user["id"], expected]
    else:
        after = None
        sql = """UPDATE expenses SET deleted_at=?,updated_at=?,revision=revision+1
            WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL"""
        values = [stamp, stamp, item_id, user["id"], expected]
    commands = [_write_guard(binding, proof, user["id"], now)]
    if method == "PATCH" and data["category_id"] != current["category_id"]:
        commands.append(binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
            EXISTS(SELECT 1 FROM expense_categories WHERE owner_id=? AND id=? AND archived_at IS NULL)
            THEN 1 ELSE 0 END)""").bind(user["id"], data["category_id"]))
    if method == "PATCH" and new_target and data["target_kind"] == "project":
        commands.append(binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
            EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=? AND id=? AND status='active')
            THEN 1 ELSE 0 END)""").bind(user["id"], data["project_id"]))
    commands += [binding.prepare(sql).bind(*values),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
          action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""").bind(
            event_id, item_id, user["id"], actor_kind, actor_id,
            "update" if method == "PATCH" else "delete",
            json.dumps(before, separators=(",", ":")),
            json.dumps(after, separators=(",", ":")) if after else None, stamp),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except Exception:
        return _error(412, "PRECONDITION_FAILED")
    return (200, after) if method == "PATCH" else (204, None)
