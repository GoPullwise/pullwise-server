"""Persistent reservations must precede D1 and survive ambiguous outcomes."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from pullwise_server.cloudflare_validation_budget import (
    BudgetError, BudgetJournal, MeteredD1, OperationBound, RequestPlan,
)


class LocalSql:
    """Only the DO journal API; this is not a billed-D1 emulator."""
    def __init__(self, connection):
        self.connection = connection

    def exec(self, sql, *params):
        cursor = self.connection.execute(sql, params)
        names = [column[0] for column in cursor.description or []]
        rows = [SimpleNamespace(**dict(zip(names, row))) for row in cursor.fetchall()]
        self.connection.commit()
        return SimpleNamespace(one=lambda: rows[0])


@pytest.fixture
def sql():
    connection = sqlite3.connect(":memory:")
    try:
        yield LocalSql(connection)
    finally:
        connection.close()


def plan(name="case", reads=10, writes=5, calls=1, requests=1):
    return RequestPlan(name, "POST", "/case", requests,
                       (OperationBound(("SELECT ?",), reads, writes, calls),))


class RawD1:
    def __init__(self, *, reads=2, writes=1, meta=True, failure=None):
        self.reads, self.writes, self.meta, self.failure = reads, writes, meta, failure
        self.calls = 0
        self.entered = None
        self.release = None

    def prepare(self, sql):
        return SimpleNamespace(bind=lambda *args: SimpleNamespace(sql=sql, args=args))

    async def batch(self, statements):
        self.calls += 1
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        result = SimpleNamespace(success=True, results=[{"value": 7}])
        if self.meta:
            result.meta = SimpleNamespace(rows_read=self.reads, rows_written=self.writes)
        return [result for _ in statements]


def test_reservations_are_cumulative_across_cases_and_restarts(sql):
    journal = BudgetJournal(sql)
    ticket = journal.begin(plan(reads=6000, writes=600), now=10)
    journal.finish(ticket, now=11)
    journal = BudgetJournal(sql)
    assert journal.snapshot()["reserved_read"] == 6000
    assert journal.snapshot()["reserved_written"] == 600
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        journal.begin(plan("another", reads=5000, writes=500), now=12)
    assert journal.snapshot()["requests"] == 1
    assert journal.snapshot()["stopped"] == "BUDGET_EXHAUSTED"


def test_request_cap_and_case_cap_do_not_reset(sql):
    journal = BudgetJournal(sql)
    for index in range(40):
        ticket = journal.begin(plan(str(index)), now=10)
        journal.finish(ticket, now=11)
    with pytest.raises(BudgetError, match="REQUEST_LIMIT"):
        BudgetJournal(sql).begin(plan("extra"), now=12)


def test_case_replay_is_rejected_before_reserving_again(sql):
    journal = BudgetJournal(sql)
    ticket = journal.begin(plan(), now=10)
    journal.finish(ticket, now=11)
    with pytest.raises(BudgetError, match="CASE_LIMIT"):
        journal.begin(plan(), now=12)
    assert journal.snapshot()["reserved_written"] == 5


def test_interrupted_request_stops_after_object_restart(sql):
    journal = BudgetJournal(sql)
    journal.begin(plan(), now=10)
    restarted = BudgetJournal(sql)
    assert restarted.snapshot()["stopped"] == "INCOMPLETE_REQUEST"
    assert restarted.snapshot()["reserved_written"] == 5
    with pytest.raises(BudgetError):
        restarted.begin(plan("next"), now=12)


def test_manual_stop_and_expired_ticket_are_permanent(sql):
    journal = BudgetJournal(sql)
    ticket = journal.begin(plan(), now=10)
    with pytest.raises(BudgetError, match="TIMEOUT"):
        journal.check(ticket, now=41)
    journal.stop("MANUAL_STOP")
    assert BudgetJournal(sql).snapshot()["stopped"] == "TIMEOUT"


def test_concurrent_requests_and_operations_cannot_duplicate_allowances(sql):
    async def run():
        journal = BudgetJournal(sql)
        ticket = journal.begin(plan(), now=10)
        raw = RawD1()
        raw.entered, raw.release = asyncio.Event(), asyncio.Event()
        meter = MeteredD1(raw, journal, ticket, plan(), clock=lambda: 11)
        task = asyncio.create_task(meter.prepare("SELECT ?").bind(7).first())
        await raw.entered.wait()
        with pytest.raises(BudgetError, match="VALIDATION_BUSY"):
            journal.begin(plan("second"), now=11)
        with pytest.raises(BudgetError, match="OPERATION_LIMIT"):
            await meter.prepare("SELECT ?").bind(7).first()
        raw.release.set()
        with pytest.raises(BudgetError):
            await task
        assert raw.calls == 1
        assert journal.snapshot()["reserved_written"] == 5
    asyncio.run(run())


@pytest.mark.parametrize("kwargs,reason", [
    ({"meta": False}, "METERING_MISSING"),
    ({"reads": -1}, "METERING_INVALID"),
    ({"writes": True}, "METERING_INVALID"),
    ({"reads": 2.5}, "METERING_INVALID"),
    ({"writes": 6}, "USAGE_EXCEEDED_BOUND"),
    ({"failure": TimeoutError()}, "D1_OUTCOME_UNKNOWN"),
])
def test_uncertain_or_excess_usage_stops_without_retry(sql, kwargs, reason):
    async def run():
        journal = BudgetJournal(sql)
        ticket = journal.begin(plan(), now=10)
        raw = RawD1(**kwargs)
        meter = MeteredD1(raw, journal, ticket, plan(), clock=lambda: 11)
        with pytest.raises(BudgetError, match=reason):
            await meter.prepare("SELECT ?").bind(7).first()
        with pytest.raises(BudgetError):
            await meter.prepare("SELECT ?").bind(7).all()
        assert raw.calls == 1
        assert journal.snapshot()["reserved_written"] == 5
        assert journal.snapshot()["stopped"] == reason
    asyncio.run(run())


def test_first_retains_meta_and_records_only_numeric_evidence(sql):
    async def run():
        journal = BudgetJournal(sql)
        ticket = journal.begin(plan(), now=10)
        raw = RawD1()
        meter = MeteredD1(raw, journal, ticket, plan(), clock=lambda: 11)
        assert await meter.prepare("SELECT ?").bind("sensitive").first("value") == 7
        journal.finish(ticket, now=12)
        state = journal.snapshot()
        assert state["actual_read"] == 2 and state["actual_written"] == 1
        assert state["reserved_written"] == 5  # Never refund a reservation.
        assert state["evidence"] == [{"request": 1, "operation": 0,
                                      "rows_read": 2, "rows_written": 1}]
        assert "sensitive" not in str(state)
        with pytest.raises(BudgetError):
            await meter.prepare("SELECT ?").first()
        assert raw.calls == 1
    asyncio.run(run())


def test_unknown_sql_and_foreign_statements_never_reach_d1(sql):
    async def run():
        journal = BudgetJournal(sql)
        ticket = journal.begin(plan(), now=10)
        raw = RawD1()
        meter = MeteredD1(raw, journal, ticket, plan(), clock=lambda: 11)
        with pytest.raises(BudgetError, match="UNREVIEWED_SQL"):
            await meter.prepare("DELETE FROM expenses").run()
        assert raw.calls == 0
    asyncio.run(run())


def test_invalid_bounds_are_never_treated_as_zero(sql):
    for reads, writes, calls in [(None, 1, 1), (1, -1, 1), (True, 1, 1), (1, 1, 0)]:
        with pytest.raises(ValueError):
            plan(reads=reads, writes=writes, calls=calls)


def test_partial_batch_meta_is_saved_before_stopping(sql):
    async def run():
        case = RequestPlan("partial", "POST", "/case", 1,
                           (OperationBound(("SELECT ?", "SELECT ?"), 10, 5),))
        journal = BudgetJournal(sql)
        ticket = journal.begin(case, now=10)

        class PartialD1(RawD1):
            async def batch(self, statements):
                self.calls += 1
                return [SimpleNamespace(success=True, results=[], meta=SimpleNamespace(
                    rows_read=3, rows_written=2)), SimpleNamespace(success=True, results=[])]

        raw = PartialD1()
        meter = MeteredD1(raw, journal, ticket, case, clock=lambda: 11)
        with pytest.raises(BudgetError, match="METERING_MISSING"):
            await meter.batch([meter.prepare("SELECT ?"), meter.prepare("SELECT ?")])
        state = journal.snapshot()
        assert state["actual_read"] == 3 and state["actual_written"] == 2
        assert state["evidence"][-1]["complete"] is False
        assert state["reserved_written"] == 5 and raw.calls == 1
    asyncio.run(run())


def test_manual_stop_during_dispatch_keeps_returned_meta(sql):
    async def run():
        journal = BudgetJournal(sql)
        ticket = journal.begin(plan(), now=10)
        raw = RawD1()
        raw.entered, raw.release = asyncio.Event(), asyncio.Event()
        meter = MeteredD1(raw, journal, ticket, plan(), clock=lambda: 11)
        task = asyncio.create_task(meter.prepare("SELECT ?").first())
        await raw.entered.wait()
        journal.stop("MANUAL_STOP")
        raw.release.set()
        with pytest.raises(BudgetError, match="MANUAL_STOP"):
            await task
        state = journal.snapshot()
        assert state["actual_read"] == 2 and state["actual_written"] == 1
        assert state["reserved_written"] == 5 and raw.calls == 1
    asyncio.run(run())
