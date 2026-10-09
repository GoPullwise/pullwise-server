"""Optional Jev exhaustion must leave the persistent preview meter usable.

The SQL and wrapper stack are real; SQLite metadata is synthetic and does not
prove native Cloudflare row costs or transaction behavior.
"""
import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_cloudflare_github_identity_http import GitHubStub, Statement
from test_d1_validation_budget import LocalSql
from test_ledger_automatic_assistance import Provider, expense
import test_ledger_suggestions as suggestion_fixture
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, _USAGE_SQL
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1
from pullwise_server.cloudflare_preview_schema import (
    INDEX_COUNTS, LEGACY_SCHEMA_SQL, UPGRADE_SQL, UPGRADE_V6_SQL, UPGRADE_V7_SQL, UPGRADE_V8_SQL, UPGRADE_V9_SQL, SCHEMA_VERSION, SCHEMA_FINGERPRINT,
)
from pullwise_server.cloudflare_validation_budget import BudgetJournal
from pullwise_server.ledger_plan_policy import default_policy, JEV_RESERVATION_MICROUSD


class MeteredSQLite:
    def __init__(self, store):
        self.store = store

    def prepare(self, sql):
        return Statement(self.store, sql)

    async def batch(self, statements):
        with self.store.connect() as connection:
            result = []
            for statement in statements:
                before = connection.total_changes
                rows = [dict(row) for row in connection.execute(statement.sql, statement.params).fetchall()]
                result.append(SimpleNamespace(success=True, results=rows, meta=SimpleNamespace(
                    rows_read=0, rows_written=connection.total_changes - before, total_attempts=1)))
            return result


@pytest.fixture
def preview():
    fixture = suggestion_fixture.LedgerSuggestionTests()
    fixture.setUp()
    connection = sqlite3.connect(":memory:")
    try:
        with fixture.store.connect() as database:
            for sql in LEGACY_SCHEMA_SQL:
                database.execute(re.sub(r"CREATE (TABLE|(?:UNIQUE )?INDEX) (?!IF NOT EXISTS)",
                    r"CREATE \1 IF NOT EXISTS ", sql, count=1))
            if not database.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_members'").fetchone():
                for sql in UPGRADE_SQL:
                    database.execute(sql)
            if next(row for row in database.execute("PRAGMA table_info(ledger_projects)")
                    if row[1] == "github_repo_id")[3]:
                for sql in UPGRADE_V6_SQL:
                    database.execute(sql)
            if not database.execute("SELECT 1 FROM sqlite_schema WHERE name='expense_recurring_rules'").fetchone():
                for sql in UPGRADE_V7_SQL:
                    database.execute(sql)
            if not database.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_join_requests'").fetchone():
                for sql in UPGRADE_V8_SQL:
                    database.execute(sql)
            if not database.execute("SELECT 1 FROM sqlite_schema WHERE name='ledger_activity_events'").fetchone():
                for sql in UPGRADE_V9_SQL:
                    database.execute(sql)
            database.execute("""INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at)
                VALUES('cat_host','usr_github_77','Hosting','local','local')""")
        yield SimpleNamespace(fixture=fixture, connection=connection, policy=default_policy(),
            now=fixture.now + 3)
    finally:
        connection.close()
        fixture.tearDown()


def reserve_existing(preview, reserved, *, month=None):
    owner, limits = "usr_github_77", preview.policy["max"]
    month = month or datetime.fromtimestamp(preview.now, timezone.utc).strftime("%Y-%m")
    with preview.fixture.store.connect() as database:
        database.execute(_USAGE_SQL, (owner, owner, owner, 0, owner, owner, 0,
            month, 0, preview.now // 60, 0, reserved, limits["projects"], limits["records"],
            limits["writesPerMinute"], limits["writesPerMonth"], 5_000_000,
            0, 0, reserved, month, preview.now // 60))


def save(preview, provider, *, category=True):
    journal = BudgetJournal(LocalSql(preview.connection), preview_product=True)
    with preview.fixture.store.connect() as database:
        counts = {table: database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in INDEX_COUNTS}
    state = journal.snapshot()
    state.update(schema_ready=True, schema_version=SCHEMA_VERSION,
        schema_fingerprint=SCHEMA_FINGERPRINT,
        product_data={"rows": counts, "json": {}, "arrays": 0})
    journal._save(state)

    async def run():
        ticket = journal.begin_product(now=10)
        meter = ProductMeteredD1(MeteredSQLite(preview.fixture.store), journal, ticket, clock=lambda: 11)
        await meter.refresh()
        binding = PlanLimitedD1(meter, policy=preview.policy, now=preview.now)
        result = await handle_ledger_request(binding=binding, gateway=GitHubStub(),
            suggestion_gateway=provider, method="POST", path="/api/v1/expenses", params={},
            body=expense("cat_host" if category else None), now=preview.now,
            headers={**preview.fixture.headers, "Idempotency-Key": "preview-jev-budget"})
        journal.finish(ticket, now=12)
        return result

    result = asyncio.run(run())
    assert journal.snapshot()["stopped"] is None
    return result


@pytest.mark.parametrize("existing", [None, 5_000_000, 5_000_000 - JEV_RESERVATION_MICROUSD + 1])
@pytest.mark.parametrize("category", [True, False])
def test_exhausted_optional_budget_preserves_manual_save_and_preview_journal(preview, existing, category):
    if existing is None:
        preview.policy["max"]["jevMonthlyBudgetUsd"] = "0.000001"
    else:
        reserve_existing(preview, existing)
    provider = Provider()
    status, result = save(preview, provider, category=category)
    assert status == (201 if category else 422)
    assert result["assistance"]["reason"] == "JEV_BUDGET_LIMIT"
    assert provider.calls == []
    if category:
        assert result["categoryId"] == "cat_host"
    else:
        assert result["error"]["code"] == "CATEGORY_REQUIRED"
    with preview.fixture.store.connect() as database:
        assert database.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert database.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
        usage = database.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        if existing is None and not category:
            assert usage is None
        else:
            assert tuple(usage) == (int(category), int(category), existing or 0)


@pytest.mark.parametrize("prior_month", [False, True])
def test_exact_remaining_or_new_month_reserves_atomically_before_provider(preview, prior_month):
    reserved = 5_000_000 if prior_month else 5_000_000 - JEV_RESERVATION_MICROUSD
    reserve_existing(preview, reserved, month="2000-01" if prior_month else None)

    def verify_reservation():
        with preview.fixture.store.connect() as database:
            usage = database.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
            assert tuple(usage) == (0, 0, JEV_RESERVATION_MICROUSD if prior_month else 5_000_000)
            assert database.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1

    provider = Provider(on_call=verify_reservation)
    status, result = save(preview, provider, category=False)
    assert status == 201 and result["categoryId"] == "cat_host"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("failure,category", [(TimeoutError, True), (None, False)])
def test_failed_or_uncertain_model_keeps_reservation_without_stopping_preview(preview, failure, category):
    provider = Provider(failure=failure() if failure else None, confidence=.6)
    status, result = save(preview, provider, category=category)
    assert status == (201 if category else 422)
    assert result["assistance"]["status"] == ("unavailable" if failure else "uncertain")
    assert len(provider.calls) == 1
    with preview.fixture.store.connect() as database:
        usage = database.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(usage) == (int(category), int(category), JEV_RESERVATION_MICROUSD)
        assert database.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 1


@pytest.mark.parametrize("category", [True, False])
def test_daily_cap_keeps_preview_open_without_another_usd_reservation(preview, category):
    reserved = 20 * JEV_RESERVATION_MICROUSD
    reserve_existing(preview, reserved)
    day = datetime.fromtimestamp(preview.now, timezone.utc).date().isoformat()
    with preview.fixture.store.connect() as database:
        database.execute("INSERT INTO expense_suggestion_budget(owner_id,day,attempts) VALUES(?,?,20)",
            ("usr_github_77", day))
    provider = Provider()
    status, result = save(preview, provider, category=category)
    assert status == (201 if category else 422)
    assert result["assistance"]["reason"] == "SUGGESTION_LIMIT"
    assert provider.calls == []
    with preview.fixture.store.connect() as database:
        usage = database.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(usage) == (int(category), int(category), reserved)
        assert database.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 20
        assert database.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
