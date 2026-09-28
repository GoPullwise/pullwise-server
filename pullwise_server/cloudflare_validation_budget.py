"""One durable, non-resetting validation budget; journal storage is NOT D1.

Only reviewed finite SQL groups may execute. Reservations are never refunded,
including on failure. Bounds must be proven separately against the exact schema,
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
class OperationBound:
    sql: tuple[str, ...]
    rows_read: int
    rows_written: int
    max_calls: int = 1

    def __post_init__(self):
        if (not isinstance(self.sql, tuple) or not 1 <= len(self.sql) <= 64
                or any(not isinstance(item, str) or not item.strip() for item in self.sql)
                or not _integer(self.rows_read) or not _integer(self.rows_written)
                or not _integer(self.max_calls, 1) or self.max_calls > 100):
            raise ValueError("invalid operation bound")


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
        if self._calls[index] >= bound.max_calls:
            self.journal._reject("OPERATION_LIMIT")
        self._calls[index] += 1  # Consume before yielding; concurrent calls cannot reuse it.
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
