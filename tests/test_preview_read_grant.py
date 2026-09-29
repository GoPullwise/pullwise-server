import sqlite3
from contextlib import closing

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal


def stopped_state(journal, reason="BUDGET_EXHAUSTED"):
    state = journal.snapshot()
    state.update(schema_ready=True, requests=25, reserved_read=9898,
                 reserved_written=218, actual_read=2856, actual_written=131,
                 active=25, deadline=30, stopped=reason,
                 cases={"product-schema": 1, "product": 24},
                 evidence=[{"request": 25, "operation": 1,
                            "rows_read": 2856, "rows_written": 131}])
    journal._save(state)
    return state


def test_preview_grant_recovers_read_stop_without_resetting_any_usage():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped_state(BudgetJournal(sql))
        journal = BudgetJournal(sql, preview_product=True)
        after = journal.snapshot()
        assert journal.read_ceiling == 100000
        assert after["stopped"] is None and after["active"] is None
        for key in ("scope", "requests", "reserved_read", "reserved_written",
                    "actual_read", "actual_written", "cases", "evidence"):
            assert after[key] == before[key]
        assert after["preview_read_grant"]["from"] == 10000
        assert after["preview_read_grant"]["to"] == 100000
        assert BudgetJournal(sql, preview_product=True).snapshot() == after


def test_default_budget_is_unchanged_and_cannot_recover_the_preview_stop():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped_state(BudgetJournal(sql))
        journal = BudgetJournal(sql)
        assert journal.read_ceiling == 10000
        assert journal.snapshot() == before


@pytest.mark.parametrize("reason", ["MANUAL_STOP", "TIMEOUT", "D1_OUTCOME_UNKNOWN", "INCOMPLETE_REQUEST", "REQUEST_LIMIT"])
def test_preview_grant_never_recovers_other_stop_reasons(reason):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = stopped_state(BudgetJournal(sql), reason)
        assert BudgetJournal(sql, preview_product=True).snapshot() == before


@pytest.mark.parametrize("change", [
    {"reserved_written": 1001}, {"actual_written": 219},
    {"reserved_read": 10001}, {"actual_read": 9899},
    {"schema_ready": False}, {"scope": "another-budget"},
    {"evidence": [{"request": 25, "operation": 1, "rows_read": 2856,
                   "rows_written": 131, "complete": False}]},
])
def test_preview_grant_rejects_unproven_or_over_budget_state(change):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql)
        before = stopped_state(journal)
        before.update(change)
        journal._save(before)
        assert BudgetJournal(sql, preview_product=True).snapshot() == before


def test_new_read_ceiling_and_original_write_ceiling_are_enforced_without_repeat_recovery():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True)
        ticket = journal.begin_product(now=10)
        journal.reserve_operation(ticket, reads=100000, writes=1000, now=11)
        with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
            journal.reserve_operation(ticket, reads=1, writes=0, now=12)
        state = journal.snapshot()
        assert BudgetJournal(sql, preview_product=True).snapshot() == state
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True)
        ticket = journal.begin_product(now=10)
        journal.reserve_operation(ticket, reads=0, writes=1000, now=11)
        with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
            journal.reserve_operation(ticket, reads=0, writes=1, now=12)


@pytest.mark.parametrize("mode,enabled,limit", [("preview", "1", 100000),
    ("preview", "0", 10000), ("production", "1", 10000), ("local", "1", 10000)])
def test_worker_selects_expansion_only_for_enabled_preview_product(mode, enabled, limit):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    source = Path("cloudflare/server/src/entry.py").read_text(encoding="utf-8")
    worker = next(node for node in ast.parse(source).body
                  if isinstance(node, ast.ClassDef) and node.name == "ValidationBudget")
    method = next(node for node in worker.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_journal")
    namespace = {"BudgetJournal": BudgetJournal}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "entry.py", "exec"), namespace)
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        stopped_state(BudgetJournal(sql))
        holder = SimpleNamespace(journal=None, ctx=SimpleNamespace(storage=SimpleNamespace(sql=sql)),
            env=SimpleNamespace(PULLWISE_MODE=mode, PULLWISE_PREVIEW_PRODUCT_ENABLED=enabled))
        journal = namespace["_journal"](holder)
        assert journal.read_ceiling == limit
        assert (journal.snapshot()["stopped"] is None) == (limit == 100000)
