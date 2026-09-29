import sqlite3
from contextlib import closing

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal


def stopped(journal, reason="REQUEST_LIMIT", active=None):
    state = journal.snapshot()
    state.update(requests=200, cases={"product-schema": 1, "product": 199},
        schema_ready=True, stopped=reason, active=active, deadline=None,
        reserved_read=11605, reserved_written=243, actual_read=4563,
        actual_written=150, preview_read_grant={"from": 10000, "to": 100000},
        evidence=[{"request": 200, "operation": 1, "rows_read": 4563, "rows_written": 150}])
    journal._save(state)
    return state


def test_enabled_preview_accepts_requests_after_200_and_preserves_count():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True)
        for _ in range(205):
            ticket = journal.begin_product(now=10)
            journal.finish(ticket, now=11)
        assert journal.snapshot()["requests"] == 205
        assert journal.snapshot()["cases"]["product"] == 205


def test_current_request_stop_recovers_without_refunding_or_resetting_any_counter():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped(BudgetJournal(sql))
        journal = BudgetJournal(sql, preview_product=True)
        after = journal.snapshot()
        assert after["stopped"] is None
        for key in ("scope", "requests", "cases", "reserved_read", "reserved_written",
                    "actual_read", "actual_written", "evidence", "preview_read_grant"):
            assert after[key] == before[key]
        ticket = journal.begin_product(now=10)
        assert ticket == 201
        journal.finish(ticket, now=11)
        assert BudgetJournal(sql, preview_product=True).snapshot() == journal.snapshot()


@pytest.mark.parametrize("reason,active", [("TIMEOUT", None), ("D1_OUTCOME_UNKNOWN", None),
    ("MANUAL_STOP", None), ("BUDGET_EXHAUSTED", None), ("REQUEST_LIMIT", 200)])
def test_request_limit_change_never_recovers_other_stops_or_active_tickets(reason, active):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped(BudgetJournal(sql), reason, active)
        assert BudgetJournal(sql, preview_product=True).snapshot() == before


def test_non_preview_product_keeps_its_original_request_limit():
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection))
        for _ in range(200):
            ticket = journal.begin_product(now=10)
            journal.finish(ticket, now=11)
        with pytest.raises(BudgetError, match="REQUEST_LIMIT"):
            journal.begin_product(now=12)


@pytest.mark.parametrize("change", [
    {"reserved_read": 100001}, {"reserved_written": 1001},
    {"actual_read": 11606}, {"actual_written": 244},
    {"schema_ready": False}, {"requests": 199},
    {"evidence": [{"request": 200, "rows_read": 4563,
                   "rows_written": 150, "complete": False}]},
])
def test_request_stop_recovery_requires_complete_in_budget_state(change):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql)
        before = stopped(journal)
        before.update(change)
        journal._save(before)
        assert BudgetJournal(sql, preview_product=True).snapshot() == before


def test_non_preview_constructor_never_recovers_a_request_stop():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped(BudgetJournal(sql))
        assert BudgetJournal(sql).snapshot() == before


@pytest.mark.parametrize("reads,writes", [(100001, 0), (0, 1001)])
def test_removing_request_limit_does_not_remove_either_row_limit(reads, writes):
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True)
        ticket = journal.begin_product(now=10)
        with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
            journal.reserve_operation(ticket, reads=reads, writes=writes, now=11)
