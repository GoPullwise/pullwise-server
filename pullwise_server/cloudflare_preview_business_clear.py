"""One configured preview-only business clear under the original coordinator.

The caller holds the existing product lock. No HTTP input selects this action,
its targets, SQL or limits. Binding identity is pinned by the operator manifest
and deployment verification; D1 does not expose a runtime database_id proof.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .cloudflare_preview_budget import (
    STATE_RECORD_INTEGRITY_VERSION, _COUNT_SQL, _FK_COUNT_SQL, _RECORD_COUNT_SQL,
    _SCHEMA_QUERY, _STRICT_RECORD_SQL, _record_product_data, _schema_objects,
)
from .cloudflare_preview_schema import INDEX_COUNTS, SCHEMA_FINGERPRINT, SCHEMA_OBJECTS, SCHEMA_VERSION
from .cloudflare_state_records import MAX_SAFE_INTEGER, STATE_KINDS, STATE_STORAGE_VERSION
from .cloudflare_validation_budget import BUDGET_SCOPE, WRITE_CEILING, BudgetError, MeteredD1, OperationBound, _field


ACTION_ID = "preview-ledger-business-clear-2026-10-09-v1"
MARKER_KEY = "preview_business_clear_v1"
BUSINESS_TABLES = (
    "expense_suggestion_events", "ledger_activity_events", "expense_events",
    "expense_create_idempotency", "expense_recurring_occurrences", "expense_recurring_rules",
    "expenses", "ledger_project_repositories", "ledger_projects", "expense_categories",
)
CLEAR_SQL = tuple("DELETE FROM " + table for table in BUSINESS_TABLES) + (
    "UPDATE ledger_plan_usage SET projects=0,records=0,project_delta=0,record_delta=0",
    "SELECT 1 AS maintenance_applied",
)
_FK_ENABLED_SQL = "SELECT foreign_keys AS enabled FROM pragma_foreign_keys"
_CAPACITY_PREDICATE = "projects!=0 OR records!=0 OR project_delta!=0 OR record_delta!=0"
_CAPACITY_PRE_SQL = ("SELECT COUNT(*) AS usageRows,COALESCE(SUM(CASE WHEN " +
    _CAPACITY_PREDICATE + " THEN 1 ELSE 0 END),0) AS nonzeroCapacityRows FROM ledger_plan_usage")
_CAPACITY_POST_SQL = _CAPACITY_PRE_SQL.replace("AS usageRows", "AS remainingUsageRows")
_PRE_SQL = (_SCHEMA_QUERY, _COUNT_SQL, _RECORD_COUNT_SQL, _STRICT_RECORD_SQL,
            _FK_COUNT_SQL, _FK_ENABLED_SQL, _CAPACITY_PRE_SQL)
_POST_SQL = (*_PRE_SQL[:-1], _CAPACITY_POST_SQL)
_INCOMING_FKS = {"expenses": 3, "expense_recurring_rules": 1, "ledger_projects": 3,
                 "expense_categories": 1}
_UPGRADE_MARKERS = ("schema_upgrade", "schema_upgrade_v6", "schema_upgrade_v7",
    "schema_upgrade_v8", "schema_upgrade_v9", "schema_upgrade_v10", "schema_upgrade_v11",
    "state_record_migration")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PLAN_HASH = _digest({"version": 1, "schema": SCHEMA_FINGERPRINT, "sql": (_PRE_SQL, CLEAR_SQL, _POST_SQL),
    "indexes": INDEX_COUNTS, "incoming": _INCOMING_FKS,
    "formula": "verify=3*(384+16*(sum(n)+4*app+128)+8*(usage+1));delete=32+8*n*(2+i+fk);usage=32+8*n*(1+2*i);select=32;write=delete:n*(1+i),usage:n*(1+2*i)"})
EXPECTED_MANIFEST = _digest({"actionId": ACTION_ID, "planHash": PLAN_HASH,
    "account": "84e421f26c708c0cf437e287eed11fa1", "script": "pullwise-server-preview",
    "database": "e9dc3b89-f81f-4fce-87ef-d8797d879fb4",
    "namespace": "0e0ce4946c5e4d5f81c606273ba94b5d", "scope": BUDGET_SCOPE,
    "schemaVersion": SCHEMA_VERSION, "schemaFingerprint": SCHEMA_FINGERPRINT,
    "stateStorageVersion": STATE_STORAGE_VERSION,
    "stateRecordIntegrityVersion": STATE_RECORD_INTEGRITY_VERSION})


def expected_business_clear_manifest():
    """Fixed nonsecret digest for the private operator deployment configuration."""
    return EXPECTED_MANIFEST


def _armed(env, journal):
    action = getattr(env, "PULLWISE_PREVIEW_BUSINESS_CLEAR_ID", "")
    if action in (None, ""):
        return False
    if (action != ACTION_ID or getattr(env, "PULLWISE_MODE", "") != "preview"
            or str(getattr(env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1"
            or str(getattr(env, "PULLWISE_PREVIEW_PRODUCT_ENABLED", "0")) != "1"
            or getattr(env, "PULLWISE_PREVIEW_BUSINESS_CLEAR_MANIFEST", "") != EXPECTED_MANIFEST
            or journal.preview_product is not True or journal.snapshot().get("scope") != BUDGET_SCOPE
            or SCHEMA_VERSION != 11):
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_UNREVIEWED")
    return True


def _valid_completed_marker(marker, state):
    used = state.get("cases", {}).get(ACTION_ID, 0)
    return (isinstance(marker, dict) and marker.get("complete") is True
            and marker.get("action_id") == ACTION_ID and marker.get("manifest") == EXPECTED_MANIFEST
            and marker.get("plan_hash") == PLAN_HASH and marker.get("schema_version") == SCHEMA_VERSION
            and marker.get("schema_fingerprint") == SCHEMA_FINGERPRINT
            and type(used) is int and used == 1
            and type(marker.get("request")) is int and 1 <= marker["request"] <= state["requests"]
            and marker.get("remaining_business_rows") == dict.fromkeys(BUSINESS_TABLES, 0)
            and type(marker.get("nonzero_capacity_rows")) is int and marker["nonzero_capacity_rows"] == 0
            and all(type(marker.get(key)) is int and 0 <= marker[key] <= MAX_SAFE_INTEGER
                    for key in ("reserved_read", "reserved_written", "observed_read", "observed_written"))
            and marker["observed_read"] <= marker["reserved_read"]
            and marker["observed_written"] <= marker["reserved_written"])


def _completed(journal):
    state = journal.snapshot()
    marker = state.get(MARKER_KEY)
    used = state.get("cases", {}).get(ACTION_ID, 0)
    if marker is None and type(used) is int and used == 0:
        return False
    if not _valid_completed_marker(marker, state):
        journal.stop("PREVIEW_BUSINESS_CLEAR_INCOMPLETE")
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_INCOMPLETE")
    return True


def business_clear_pending(env, journal):
    """Called under the coordinator lock before any scheduler/nonhealth work."""
    if not _armed(env, journal):
        return False
    return not _completed(journal)


@dataclass(frozen=True)
class _ClearPlan:
    operations: tuple[OperationBound, ...]

    @property
    def rows_read(self):
        return sum(operation.rows_read for operation in self.operations)

    @property
    def rows_written(self):
        return sum(operation.rows_written for operation in self.operations)


def _compile_plan(state):
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if (state.get("scope") != BUDGET_SCOPE or state.get("schema_ready") is not True
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("schema_fingerprint") != SCHEMA_FINGERPRINT
            or state.get("product_data_verified") is not True
            or state.get("state_storage_version") != STATE_STORAGE_VERSION
            or state.get("state_record_integrity_version") != STATE_RECORD_INTEGRITY_VERSION
            or state.get("cases", {}).get("product-schema") != 1
            or any(marker is not None and (not isinstance(marker, dict) or marker.get("complete") is not True)
                   for marker in (state.get(key) for key in _UPGRADE_MARKERS))
            or not isinstance(rows, dict) or set(rows) != set(INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(records, dict) or set(records) != set(STATE_KINDS)
            or any(type(n) is not int or not 0 <= n <= rows["app_state"] for n in records.values())
            or not 0 <= rows["app_state"] - sum(records.values()) <= 6):
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_UNREVIEWED")
    # Whole-table scans, typed validation, schema catalog and indexed FK probes.
    # Read-only groups retain all three possible native attempts. They never
    # export retained payloads or individual customer quota/spend values.
    verification = 3 * (384 + 16 * (sum(rows.values()) + 4 * rows["app_state"] + 128)
                        + 8 * (rows["ledger_plan_usage"] + 1))
    # Each incoming child table is entirely empty before its parent DELETE.
    # Consequently FK checks are empty seeks rather than repeated child scans.
    reads = sum(32 + 8 * rows[table] * (2 + INDEX_COUNTS[table] + _INCOMING_FKS.get(table, 0))
                for table in BUSINESS_TABLES)
    reads += 32 + 8 * rows["ledger_plan_usage"] * (1 + 2 * INDEX_COUNTS["ledger_plan_usage"]) + 32
    writes = sum(rows[table] * (1 + INDEX_COUNTS[table]) for table in BUSINESS_TABLES)
    writes += rows["ledger_plan_usage"] * (1 + 2 * INDEX_COUNTS["ledger_plan_usage"])
    plan = _ClearPlan((OperationBound(_PRE_SQL, verification, 0),
                       OperationBound(CLEAR_SQL, reads, writes),
                       OperationBound(_POST_SQL, verification, 0)))
    if plan.rows_read > MAX_SAFE_INTEGER or plan.rows_written > MAX_SAFE_INTEGER:
        raise BudgetError("PREVIEW_DATA_BOUND")
    return plan


def _single(result):
    rows = list(_field(result, "results", []))
    if len(rows) != 1:
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_PROOF_INVALID")
    return rows[0]


def _verify(results, upper, expected_rows, expected_records, *, after=False):
    if _schema_objects(results[0]) != SCHEMA_OBJECTS:
        raise BudgetError("PREVIEW_SCHEMA_MISMATCH")
    data = _record_product_data(results[1], results[2], upper, results[3])
    if data["rows"] != expected_rows or data["records"] != expected_records:
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_PROOF_INVALID")
    violations, enabled = _field(_single(results[4]), "violations"), _field(_single(results[5]), "enabled")
    if type(violations) is not int or violations != 0 or type(enabled) is not int or enabled != 1:
        raise BudgetError("PREVIEW_FOREIGN_KEY_MISMATCH")
    capacity = _single(results[6])
    count = _field(capacity, "remainingUsageRows" if after else "usageRows")
    nonzero = _field(capacity, "nonzeroCapacityRows")
    if (type(count) is not int or count != upper["ledger_plan_usage"] or type(nonzero) is not int
            or not 0 <= nonzero <= count or after and nonzero != 0):
        raise BudgetError("PREVIEW_BUSINESS_CLEAR_PROOF_INVALID")
    return data, nonzero


async def clear_preview_business(binding, journal, env, *, clock=time.time):
    """Run the fixed one-use plan; caller holds the original product lock.

    This bypasses no customer quota: it is operator maintenance with no customer
    principal. Its exact source SQL changes only the ten business tables and the
    four capacity fields; all native costs remain in the original global journal.
    """
    if not _armed(env, journal):
        return None
    if _completed(journal):
        return business_clear_receipt(journal)
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if state["active"] is not None:
        raise BudgetError("VALIDATION_BUSY")
    plan = _compile_plan(state)
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=clock())
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=clock())
    saved = journal.check(ticket, now=clock())
    saved["cases"][ACTION_ID] = 1
    saved[MARKER_KEY] = {"action_id": ACTION_ID, "version": 1, "manifest": EXPECTED_MANIFEST,
        "plan_hash": PLAN_HASH, "schema_version": SCHEMA_VERSION, "schema_fingerprint": SCHEMA_FINGERPRINT,
        "request": ticket, "complete": False, "reserved_read": plan.rows_read,
        "reserved_written": plan.rows_written,
        "before_business_rows": {table: state["product_data"]["rows"][table] for table in BUSINESS_TABLES}}
    saved["product_data_verified"] = False
    journal._save(saved)  # One-use case, marker and full reservation precede every native dispatch.
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)
    upper, records = state["product_data"]["rows"], state["product_data"]["records"]
    expected = {**upper, **dict.fromkeys(BUSINESS_TABLES, 0)}
    observed_read = observed_write = 0

    async def execute(index):
        nonlocal observed_read, observed_write
        results = await meter.batch([meter.prepare(sql) for sql in plan.operations[index].sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        if any(value is not None and (type(value) is not int or not 1 <= value <= (1 if index == 1 else 3))
               for value in attempts):
            journal._reject("BUSINESS_CLEAR_ATTEMPTS_UNPROVEN")
        observed_read += sum(_field(_field(result, "meta"), "rows_read") for result in results)
        observed_write += sum(_field(_field(result, "meta"), "rows_written") for result in results)
        return results, attempts

    try:
        before, _ = await execute(0)
        _, nonzero = _verify(before, upper, upper, records)
        mutation, attempts = await execute(1)
        if _field(_single(mutation[-1]), "maintenance_applied") != 1:
            raise BudgetError("PREVIEW_BUSINESS_CLEAR_PROOF_INVALID")
        post, _ = await execute(2)
        data, _ = _verify(post, upper, expected, records, after=True)
        saved = journal.check(ticket, now=clock())
        saved.update(product_data=data, product_data_verified=True)
        saved[MARKER_KEY].update(complete=True, before_nonzero_capacity_rows=nonzero,
            remaining_business_rows=dict.fromkeys(BUSINESS_TABLES, 0), nonzero_capacity_rows=0,
            observed_read=observed_read, observed_written=observed_write,
            write_execution={"native_attempts": attempts, "provenance": "d1-nonretryable-write-contract-v1"})
        # Close only this fully acknowledged ticket. Marker completion and
        # ticket closure share one DO checkpoint, with no recovery/reset path.
        saved["active"] = saved["deadline"] = None
        journal._save(saved)
    except BaseException:
        journal.stop("PREVIEW_BUSINESS_CLEAR_OUTCOME_UNKNOWN")
        raise
    return business_clear_receipt(journal)


def business_clear_receipt(journal):
    """Cached numeric receipt; no D1 IO and no identity/financial payloads."""
    state = journal.snapshot()
    marker = state.get(MARKER_KEY)
    if marker is None:
        return None
    if not isinstance(marker, dict):
        return {"actionId": ACTION_ID, "complete": False}
    complete = _valid_completed_marker(marker, state)
    receipt = {"actionId": ACTION_ID, "complete": complete,
        "schemaVersion": SCHEMA_VERSION,
        "request": marker.get("request") if type(marker.get("request")) is int
                   and 1 <= marker["request"] <= MAX_SAFE_INTEGER else None}
    if complete:
        receipt.update(remainingBusinessRows={"total": 0, "tables": dict.fromkeys(BUSINESS_TABLES, 0)},
            nonzeroCapacityRows=0, reservedRead=marker["reserved_read"], reservedWritten=marker["reserved_written"],
            observedRead=marker["observed_read"], observedWritten=marker["observed_written"])
    return receipt
