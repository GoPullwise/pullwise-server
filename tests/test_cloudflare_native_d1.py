"""Keep reviewed Python integers exact at the Workers JavaScript D1 boundary."""
import asyncio
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import (
    BudgetError, BudgetJournal, MeteredD1, OperationBound, ParameterBound, RequestPlan,
)
from test_d1_validation_budget import LocalSql


MAX_SAFE = 9007199254740991


class NativeStatement:
    def __init__(self, binding, sql, params=()):
        self.binding, self.sql, self.params = binding, sql, params

    def bind(self, *params):
        # The actual Python Worker rejects this boundary integer while accepting
        # its exactly representable float. Ordinary SQLite fixtures miss it.
        if any(type(value) is int and abs(value) == MAX_SAFE for value in params):
            raise RuntimeError("native integer boundary")
        self.binding.bound.append(params)
        return NativeStatement(self.binding, self.sql, params)

    async def all(self):
        return self.binding.execute(self)

    async def run(self):
        return await self.all()

    async def first(self, column=None):
        rows = (await self.all())["results"]
        row = rows[0] if rows else None
        return row[column] if row is not None and column is not None else row


class NativeBinding:
    def __init__(self, db):
        self.db, self.bound, self.batches = db, [], []

    def prepare(self, sql):
        return NativeStatement(self, sql)

    def execute(self, statement):
        rows = self.db.execute(statement.sql, statement.params).fetchall()
        return {"success": True, "results": [dict(row) for row in rows],
                "meta": {"rows_read": len(rows), "rows_written": 0}}

    async def batch(self, statements):
        statements = list(statements)
        assert all(isinstance(statement, NativeStatement) for statement in statements)
        self.batches.append(statements)
        with self.db:
            self.last_results = [self.execute(statement) for statement in statements]
        return self.last_results


@pytest.fixture
def raw():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE money(amount_minor INTEGER NOT NULL)")
        yield NativeBinding(db)


@pytest.mark.parametrize("value", [0, 2147483648, MAX_SAFE - 1, MAX_SAFE, -MAX_SAFE])
def test_integer_affinity_preserves_every_safe_integer_at_native_binding(raw, value):
    async def run():
        binding = NativeD1(raw)
        await binding.prepare("INSERT INTO money VALUES (?)").bind(value).run()
        row = await binding.prepare("SELECT amount_minor,typeof(amount_minor) AS storage FROM money").first()
        assert row == {"amount_minor": value, "storage": "integer"}
        assert type(raw.bound[0][0]) is float
        assert int(raw.bound[0][0]) == value
    asyncio.run(run())


def test_all_first_column_and_atomic_batch_keep_native_results_and_metadata(raw):
    async def run():
        binding = NativeD1(raw)
        statements = [binding.prepare("INSERT INTO money VALUES (?)").bind(MAX_SAFE),
                      binding.prepare("SELECT amount_minor FROM money")]
        result = await binding.batch(iter(statements))
        assert result is raw.last_results
        assert len(raw.batches) == 1 and len(raw.batches[0]) == 2
        assert result[1]["results"] == [{"amount_minor": MAX_SAFE}]
        assert result[1]["meta"] == {"rows_read": 1, "rows_written": 0}
        assert await binding.prepare("SELECT amount_minor FROM money").first("amount_minor") == MAX_SAFE
        assert (await binding.prepare("SELECT amount_minor FROM money").all())["results"] == result[1]["results"]
    asyncio.run(run())


def test_non_integer_parameters_are_forwarded_without_reinterpretation(raw):
    values = (None, "9007199254740991", b"bytes", True, 1.5)
    NativeD1(raw).prepare("SELECT 1").bind(*values)
    assert raw.bound == [values]
    assert all(type(actual) is type(expected) for actual, expected in zip(raw.bound[0], values))


def test_native_statements_preserve_logical_sql_and_integer_parameters_for_quota_adapters(raw):
    binding = NativeD1(raw)
    unbound = binding.prepare("SELECT ?")
    bound = unbound.bind(MAX_SAFE)
    assert unbound.sql == bound.sql == "SELECT ?"
    assert unbound.params == () and bound.params == (MAX_SAFE,)
    assert type(bound.params[0]) is int and type(bound.native.params[0]) is float


@pytest.mark.parametrize("value", [MAX_SAFE + 1, -MAX_SAFE - 1])
def test_unsafe_integer_is_rejected_before_native_bind_without_rounding(raw, value):
    with pytest.raises(ValueError, match="safe integer"):
        NativeD1(raw).prepare("SELECT ?").bind(value)
    assert not raw.bound and not raw.batches


def test_meter_validates_integer_envelopes_before_native_conversion(raw):
    with closing(sqlite3.connect(":memory:")) as journal_db:
        journal = BudgetJournal(LocalSql(journal_db))
        plan = RequestPlan("native-max", "GET", "/case", 1,
            (OperationBound(("SELECT CAST(? AS INTEGER) AS value",), 1, 0,
                parameters=((ParameterBound("integer", minimum=MAX_SAFE, maximum=MAX_SAFE),),)),))
        ticket = journal.begin(plan, now=10)
        meter = MeteredD1(NativeD1(raw), journal, ticket, plan, clock=lambda: 11)
        statement = meter.prepare(plan.operations[0].sql[0]).bind(MAX_SAFE)
        assert type(statement.params[0]) is int
        assert asyncio.run(statement.first()) == {"value": MAX_SAFE}
        assert type(raw.bound[0][0]) is float
        assert journal.snapshot()["actual_read"] == 1
        assert journal.snapshot()["actual_written"] == 0


def test_float_cannot_bypass_logical_integer_envelopes(raw):
    with closing(sqlite3.connect(":memory:")) as journal_db:
        journal = BudgetJournal(LocalSql(journal_db))
        plan = RequestPlan("native-float", "GET", "/case", 1,
            (OperationBound(("SELECT ?",), 1, 0,
                parameters=((ParameterBound("integer", minimum=0, maximum=MAX_SAFE),),)),))
        ticket = journal.begin(plan, now=10)
        meter = MeteredD1(NativeD1(raw), journal, ticket, plan, clock=lambda: 11)
        with pytest.raises(BudgetError, match="UNREVIEWED_PARAMETERS"):
            asyncio.run(meter.prepare("SELECT ?").bind(float(MAX_SAFE)).first())
        assert not raw.bound and not raw.batches
