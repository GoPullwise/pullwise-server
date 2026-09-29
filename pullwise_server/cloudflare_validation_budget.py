"""One durable, non-resetting validation budget; journal storage is NOT D1.

Only reviewed finite SQL groups may execute. Failed/ambiguous reservations and
all write reservations are retained. Product reads may settle a proven unused
single-attempt margin. Bounds must be proven separately against the exact schema,
fixture cardinalities, indexes and inputs; measuring afterwards cannot prove one.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass


BUDGET_SCOPE = "pullwise-s17-s18-2026-09-28"
READ_CEILING = 10_000
WRITE_CEILING = 1_000
REQUEST_CEILING = 40
REQUEST_SECONDS = 30


class BudgetError(Exception):
    pass


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


@dataclass(frozen=True)
class ParameterBound:
    """Reviewed scalar input envelope, independent of SQL row-bound proof."""
    kind: str
    minimum: int | None = None
    maximum: int | None = None
    max_bytes: int | None = None
    max_items: int | None = None

    def __post_init__(self):
        valid = False
        if self.kind == "integer":
            valid = (type(self.minimum) is int and type(self.maximum) is int
                     and -(2**53 - 1) <= self.minimum <= self.maximum <= 2**53 - 1
                     and self.max_bytes is self.max_items is None)
        elif self.kind in {"text", "json_object"}:
            valid = (_integer(self.max_bytes, 1) and self.max_bytes <= 8192
                     and self.minimum is None and self.maximum is None
                     and (self.max_items is None if self.kind == "text" else
                          _integer(self.max_items) and self.max_items <= 100))
        elif self.kind == "null":
            valid = self.minimum is self.maximum is self.max_bytes is self.max_items is None
        if not valid:
            raise ValueError("invalid parameter bound")

    def accepts(self, value):
        if self.kind == "integer":
            return type(value) is int and self.minimum <= value <= self.maximum
        if self.kind in {"text", "json_object"}:
            if type(value) is not str:
                return False
            try:
                if len(value.encode("utf-8")) > self.max_bytes:
                    return False
                if self.kind == "text":
                    return True
                decoded = json.loads(value, object_pairs_hook=_unique_object)
                # Reject ambiguous/non-finite JSON and escaped invalid Unicode
                # before SQL json_each/json() can interpret it differently.
                json.dumps(decoded, ensure_ascii=False, allow_nan=False).encode("utf-8")
                return isinstance(decoded, dict) and len(decoded) <= self.max_items
            except (ValueError, UnicodeError, RecursionError):
                return False
        return value is None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class OperationBound:
    sql: tuple[str, ...]
    rows_read: int
    rows_written: int
    max_calls: int = 1
    parameters: tuple[tuple[ParameterBound, ...], ...] = ()

    def __post_init__(self):
        if (not isinstance(self.sql, tuple) or not 1 <= len(self.sql) <= 64
                or any(not isinstance(item, str) or not item.strip() for item in self.sql)
                or not _integer(self.rows_read) or not _integer(self.rows_written)
                or not _integer(self.max_calls, 1) or self.max_calls > 100):
            raise ValueError("invalid operation bound")
        if (not isinstance(self.parameters, tuple)
                or self.parameters and len(self.parameters) != len(self.sql)
                or any(not isinstance(group, tuple) or len(group) > 100
                       or any(not isinstance(item, ParameterBound) for item in group)
                       for group in self.parameters)):
            raise ValueError("invalid operation parameters")


@dataclass(frozen=True)
class RequestPlan:
    name: str
    method: str
    path: str
    max_requests: int
    operations: tuple[OperationBound, ...]
    expected_statuses: tuple[int, ...] = (200, 201, 204, 302)

    def __post_init__(self):
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 80
                or self.method not in {"GET", "POST", "PATCH", "DELETE"}
                or not isinstance(self.path, str) or not self.path.startswith("/")
                or not _integer(self.max_requests, 1) or self.max_requests > REQUEST_CEILING
                or not isinstance(self.operations, tuple) or not 1 <= len(self.operations) <= 64
                or any(not isinstance(op, OperationBound) for op in self.operations)
                or sum(op.max_calls for op in self.operations) > 64
                or len({op.sql for op in self.operations}) != len(self.operations)
                or not isinstance(self.expected_statuses, tuple) or not self.expected_statuses
                or any(not _integer(status, 100) or status > 599 for status in self.expected_statuses)
                or self.rows_read > READ_CEILING or self.rows_written > WRITE_CEILING):
            raise ValueError("invalid finite request plan")

    @property
    def rows_read(self):
        return sum(op.rows_read * op.max_calls for op in self.operations)

    @property
    def rows_written(self):
        return sum(op.rows_written * op.max_calls for op in self.operations)


# No remote case has a proven bound yet. Neither env vars nor request headers
# can supply plans. OAuth, webhooks, migrations and cleanup remain unadmitted.
REVIEWED_REMOTE_PLANS: tuple[RequestPlan, ...] = ()
# Initialization is an internal RPC, never an HTTP route. Local observations
# alone cannot populate this plan; remote DDL bounds remain unproven.
REVIEWED_INITIALIZATION_PLAN: RequestPlan | None = None


class BudgetJournal:
    """Synchronous SQLite DO storage: no await between read/reserve/write.

    All users, databases and phases must address ONE fixed DO namespace/name.
    A live ticket excludes overlapping requests. Object restart with a pending
    ticket stops permanently: its outcome is unknown. There is no reset API.
    """
    def __init__(self, sql):
        self.sql = sql
        sql.exec("CREATE TABLE IF NOT EXISTS validation_budget (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        initial = {"scope": BUDGET_SCOPE, "requests": 0, "reserved_read": 0,
                   "reserved_written": 0, "actual_read": 0, "actual_written": 0,
                   "active": None, "deadline": None, "stopped": None,
                   "cases": {}, "evidence": []}
        sql.exec("INSERT OR IGNORE INTO validation_budget(id,payload) VALUES(1,?)", json.dumps(initial))
        state = self.snapshot()
        if state["scope"] != BUDGET_SCOPE:
            self.stop("BUDGET_SCOPE_MISMATCH")
        elif state["active"] is not None:
            self.stop("INCOMPLETE_REQUEST")

    def snapshot(self):
        row = self.sql.exec("SELECT payload FROM validation_budget WHERE id=1").one()
        return json.loads(_field(row, "payload"))

    def _save(self, state):
        self.sql.exec("UPDATE validation_budget SET payload=? WHERE id=1",
                      json.dumps(state, separators=(",", ":")))

    def stop(self, reason):
        state = self.snapshot()
        if not state["stopped"]:
            state["stopped"] = reason
            self._save(state)

    def _reject(self, reason):
        self.stop(reason)
        raise BudgetError(reason)

    def begin(self, plan, *, now):
        if not isinstance(plan, RequestPlan):
            self._reject("UNREVIEWED_CASE")
        state = self.snapshot()
        if state["stopped"]:
            raise BudgetError(state["stopped"])
        if state["active"] is not None:
            raise BudgetError("VALIDATION_BUSY")
        if state["requests"] >= REQUEST_CEILING:
            self._reject("REQUEST_LIMIT")
        if state["cases"].get(plan.name, 0) >= plan.max_requests:
            self._reject("CASE_LIMIT")
        if (state["reserved_read"] + plan.rows_read > READ_CEILING
                or state["reserved_written"] + plan.rows_written > WRITE_CEILING):
            self._reject("BUDGET_EXHAUSTED")
        state["requests"] += 1
        state["cases"][plan.name] = state["cases"].get(plan.name, 0) + 1
        state["reserved_read"] += plan.rows_read
        state["reserved_written"] += plan.rows_written
        state["active"] = state["requests"]
        state["deadline"] = now + REQUEST_SECONDS
        self._save(state)  # Persist before provider calls or D1 dispatch.
        return state["active"]

    def check(self, ticket, *, now):
        state = self.snapshot()
        if state["stopped"]:
            raise BudgetError(state["stopped"])
        if state["active"] != ticket:
            self._reject("REQUEST_CLOSED")
        if now >= state["deadline"]:
            self._reject("TIMEOUT")
        return state

    def begin_product(self, *, now):
        state = self.snapshot()
        if state["stopped"]:
            raise BudgetError(state["stopped"])
        if state["active"] is not None:
            raise BudgetError("VALIDATION_BUSY")
        if state["requests"] >= 200:
            self._reject("REQUEST_LIMIT")
        state["requests"] += 1
        state["cases"]["product"] = state["cases"].get("product", 0) + 1
        state["active"] = state["requests"]
        state["deadline"] = now + REQUEST_SECONDS
        self._save(state)
        return state["active"]

    def reserve_operation(self, ticket, *, reads, writes, now):
        state = self.check(ticket, now=now)
        if not _integer(reads) or not _integer(writes):
            self._reject("UNREVIEWED_BOUND")
        if (state["reserved_read"] + reads > READ_CEILING
                or state["reserved_written"] + writes > WRITE_CEILING):
            self._reject("BUDGET_EXHAUSTED")
        state["reserved_read"] += reads
        state["reserved_written"] += writes
        self._save(state)

    def save_product_state(self, ticket, data, *, now, initialized=False):
        state = self.check(ticket, now=now)
        state["product_data"] = data
        if initialized:
            state["schema_ready"] = True
        self._save(state)

    def settle_product_reads(self, ticket, operation, reserved, *, now):
        state = self.check(ticket, now=now)
        evidence = state["evidence"][-1] if state["evidence"] else {}
        if (evidence.get("request") != ticket or evidence.get("operation") != operation
                or evidence.get("complete", True) is not True
                or "read_margin_released" in evidence or not _integer(reserved)
                or evidence.get("rows_read", reserved + 1) > reserved):
            self._reject("SETTLEMENT_INVALID")
        margin = reserved - evidence["rows_read"]
        if state["reserved_read"] - margin < state["actual_read"]:
            self._reject("SETTLEMENT_INVALID")
        state["reserved_read"] -= margin
        state["read_margin_released"] = state.get("read_margin_released", 0) + margin
        evidence["read_margin_released"] = margin
        self._save(state)

    def record(self, ticket, operation, reads, writes, *, complete=True):
        # An already dispatched operation can finish after stop/timeout. Keep
        # its observed meta without authorizing another operation or refund.
        state = self.snapshot()
        if state["active"] != ticket:
            self._reject("REQUEST_CLOSED")
        state["actual_read"] += reads
        state["actual_written"] += writes
        evidence = {"request": ticket, "operation": operation,
                    "rows_read": reads, "rows_written": writes}
        if not complete:
            evidence["complete"] = False
        state["evidence"].append(evidence)
        self._save(state)

    def finish(self, ticket, *, now):
        state = self.check(ticket, now=now)
        state["active"] = state["deadline"] = None
        self._save(state)


class MeteredD1:
    """D1-shaped adapter preserving batches and capturing metadata for first()."""
    def __init__(self, binding, journal, ticket, plan, *, clock=time.time):
        self._binding, self.journal, self.ticket, self.plan = binding, journal, ticket, plan
        self.clock = clock
        self._calls = [0] * len(plan.operations)

    def prepare(self, sql):
        return _Statement(self, sql, ())

    async def batch(self, statements):
        statements = list(statements)
        self.journal.check(self.ticket, now=self.clock())
        if (not statements or any(not isinstance(item, _Statement) or item.meter is not self
                                  for item in statements)):
            self.journal._reject("UNREVIEWED_SQL")
        group = tuple(item.sql for item in statements)
        index = next((i for i, op in enumerate(self.plan.operations) if op.sql == group), None)
        if index is None:
            self.journal._reject("UNREVIEWED_SQL")
        bound = self.plan.operations[index]
        parameters = bound.parameters or tuple(() for _ in bound.sql)
        for statement, reviewed in zip(statements, parameters):
            if (len(statement.params) != len(reviewed)
                    or any(not check.accepts(value)
                           for check, value in zip(reviewed, statement.params))):
                self.journal._reject("UNREVIEWED_PARAMETERS")
        if self._calls[index] >= bound.max_calls:
            self.journal._reject("OPERATION_LIMIT")
        self._calls[index] += 1  # Consume before yielding; concurrent calls cannot reuse it.
        return await self._dispatch(statements, index, bound)

    async def _dispatch(self, statements, index, bound):
        try:
            raw = [self._binding.prepare(item.sql).bind(*item.params) for item in statements]
            results = list(await asyncio.wait_for(self._binding.batch(raw), timeout=10))
        except BaseException as error:
            self.journal.stop("D1_OUTCOME_UNKNOWN")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BudgetError("D1_OUTCOME_UNKNOWN") from None
        reason = "METERING_MISSING" if len(results) != len(statements) else None
        reads = writes = 0
        for result in results:
            if _field(result, "success") is not True:
                reason = reason or "D1_OUTCOME_UNKNOWN"
            meta = _field(result, "meta")
            if meta is None or _field(meta, "rows_read") is None or _field(meta, "rows_written") is None:
                reason = reason or "METERING_MISSING"
                continue
            read, write = _field(meta, "rows_read"), _field(meta, "rows_written")
            if not _integer(read) or not _integer(write):
                reason = reason or "METERING_INVALID"
                continue
            reads += read
            writes += write
        # Record observable actual usage even when it violates the reviewed bound.
        self.journal.record(self.ticket, index, reads, writes, complete=reason is None)
        if reason:
            self.journal._reject(reason)
        self.journal.check(self.ticket, now=self.clock())
        if reads > bound.rows_read or writes > bound.rows_written:
            self.journal._reject("USAGE_EXCEEDED_BOUND")
        return results


@dataclass(frozen=True)
class _Statement:
    meter: MeteredD1
    sql: str
    params: tuple

    def bind(self, *params):
        return _Statement(self.meter, self.sql, params)

    async def all(self):
        return (await self.meter.batch([self]))[0]

    async def run(self):
        return await self.all()

    async def first(self, column=None):
        result = await self.all()  # Native first() drops meta; keep the full result.
        rows = _field(result, "results")
        row = rows[0] if rows else None
        return _field(row, column) if column is not None and row is not None else row


async def run_initialization(binding, journal, plan, *, clock=time.time):
    """Execute fixed, parameter-free SQL once under the same cumulative journal.

    Caller code owns the reviewed schema/empty-database prerequisites. Partial
    initialization is preserved on failure; there is no retry/reset/cleanup.
    """
    if (not isinstance(plan, RequestPlan) or plan.method != "POST"
            or plan.path != "/_internal/initialize" or plan.max_requests != 1
            or any(op.max_calls != 1 or any(op.parameters) for op in plan.operations)):
        raise BudgetError("UNREVIEWED_INITIALIZATION")
    ticket = journal.begin(plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)

    async def execute():
        for operation in plan.operations:
            await meter.batch([meter.prepare(sql) for sql in operation.sql])
        journal.finish(ticket, now=clock())

    try:
        await asyncio.wait_for(execute(), timeout=REQUEST_SECONDS)
    except BaseException as error:
        journal.stop("TIMEOUT" if isinstance(error, asyncio.TimeoutError)
                     else "INITIALIZATION_OUTCOME_UNKNOWN")
        raise
    return journal.snapshot()
