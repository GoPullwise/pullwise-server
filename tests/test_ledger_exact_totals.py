"""Aggregate valid ledger money without SQLite or JSON integer precision loss."""
import pytest
import sqlite3

import test_ledger_routes as route_fixture
from pullwise_server.ledger_money_totals import AGGREGATE_SQL, aggregate_minor, public_minor


MAX_MINOR = 9007199254740991


@pytest.fixture
def ledger():
    routes = route_fixture.LedgerRoutesTests()
    routes.setUp()
    try:
        yield routes
    finally:
        routes.tearDown()


def make_expenses(ledger, count, *, project_id=None):
    _, category = ledger.call("POST", "/api/v1/categories", {"name": "Exact totals"})
    with ledger.store.connect() as db:
        owner = db.execute("SELECT owner_id FROM expense_categories WHERE id=?",
            (category["id"],)).fetchone()[0]
        db.executemany("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
            occurred_on,amount_minor,currency,purpose,created_at,updated_at)
            VALUES(?,?,?,?,?,'2026-10-01',?,'USD','Allowed maximum',?,?)""",
            [(f"exp_exact_{index}", owner, "project" if project_id else "shared",
              project_id, category["id"], MAX_MINOR, "2026-10-01T00:00:00Z",
              "2026-10-01T00:00:00Z") for index in range(count)])


@pytest.mark.parametrize("report", ["summary", "timeseries", "categories"])
@pytest.mark.parametrize("count", [2, 1025])
def test_reports_preserve_totals_above_json_and_sqlite_integer_limits(ledger, report, count):
    make_expenses(ledger, count)
    status, payload = ledger.call("GET", "/api/v1/reports/" + report)
    assert status == 200
    assert payload["groups"]
    assert all(group["amountMinor"] == str(MAX_MINOR * count) for group in payload["groups"])


@pytest.mark.parametrize("count", [2, 1025])
def test_project_list_detail_and_update_return_exact_totals(ledger, count):
    _, project = ledger.call("POST", "/api/v1/projects", {"githubRepoId": 202})
    make_expenses(ledger, count, project_id=project["id"])
    expected = [{"currency": "USD", "amountMinor": str(MAX_MINOR * count)}]
    status, page = ledger.call("GET", "/api/v1/projects")
    assert status == 200
    assert page["items"][0]["totals"] == expected
    status, detail = ledger.call("GET", "/api/v1/projects/" + project["id"])
    assert status == 200
    assert detail["totals"] == expected
    status, updated = ledger.call("PATCH", "/api/v1/projects/" + project["id"],
        {"description": "Renamed"}, {**ledger.headers, "If-Match": '"1"'})
    assert status == 200
    assert updated["totals"] == expected


def test_safe_totals_keep_numbers_and_do_not_combine_currencies(ledger):
    make_expenses(ledger, 1)
    with ledger.store.connect() as db:
        db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at)
            SELECT 'exp_jpy',owner_id,target_kind,category_id,occurred_on,7,'JPY',
                purpose,created_at,updated_at FROM expenses LIMIT 1""")
    status, payload = ledger.call("GET", "/api/v1/reports/summary")
    assert status == 200
    amounts = {group["currency"]: group["amountMinor"] for group in payload["groups"]
               if group["target"] == "account"}
    assert amounts == {"USD": MAX_MINOR, "JPY": 7}
    assert all(type(amount) is int for amount in amounts.values())


def test_project_update_preserves_existing_small_totals(ledger):
    _, project = ledger.call("POST", "/api/v1/projects", {"githubRepoId": 202})
    make_expenses(ledger, 1, project_id=project["id"])
    status, updated = ledger.call("PATCH", "/api/v1/projects/" + project["id"],
        {"description": "Renamed"}, {**ledger.headers, "If-Match": '"1"'})
    assert status == 200
    assert updated["totals"] == [{"currency": "USD", "amountMinor": MAX_MINOR}]


def test_split_totals_stay_native_safe_at_maximum_operator_record_allowance():
    # Exercise the actual SQLite arithmetic at the maximum configurable cap,
    # without allocating a million persisted expense or audit records.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(f"""WITH RECURSIVE records(n) AS (
            VALUES(1) UNION ALL SELECT n+1 FROM records WHERE n<1000000)
            SELECT {AGGREGATE_SQL} FROM (SELECT ? AS amount_minor FROM records)""",
            (MAX_MINOR,)).fetchone()
        assert all(type(value) is int and 0 <= value <= MAX_MINOR for value in row)
        assert public_minor(aggregate_minor(row)) == str(MAX_MINOR * 1000000)
    finally:
        db.close()
