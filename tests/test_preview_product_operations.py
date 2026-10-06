"""Normal preview traffic keeps audited bounds without lifetime test quotas."""
import asyncio
import io
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from test_d1_validation_budget import LocalSql, RawD1, plan
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, initial_data
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal


def legacy_state(journal, *, stopped=None, active=None):
    state = journal.snapshot()
    state.update(schema_ready=True, requests=321, reserved_read=200_000,
        reserved_written=2_000, actual_read=7_000, actual_written=600,
        active=active, deadline=30 if active else None, stopped=stopped,
        cases={"product-schema": 1, "product": 320},
        evidence=[{"request": 321, "operation": 1, "rows_read": 7_000,
                   "rows_written": 600}])
    journal._save(state)
    return state


@pytest.mark.parametrize("reason,ticket", [(None, None), ("BUDGET_EXHAUSTED", None),
                                          ("BUDGET_EXHAUSTED", 321)])
def test_product_policy_retires_lifetime_limits_without_reset_or_refund(reason, ticket):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = legacy_state(BudgetJournal(sql), stopped=reason, active=ticket)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        after = journal.snapshot()
        assert after["stopped"] is None and after["active"] is None
        for key in ("scope", "requests", "reserved_read", "reserved_written",
                    "actual_read", "actual_written", "cases", "evidence"):
            assert after[key] == before[key]
        marker = after["preview_product_operation_policy"]
        assert marker["reserved_written_at_transition"] == 2_000
        assert marker["previous_stop"] == reason and marker["previous_ticket"] == ticket
        current = journal.begin_product(now=10)
        journal.reserve_operation(current, reads=100_001, writes=1_001, now=11)
        journal.finish(current, now=12)
        snapshot = journal.snapshot()
        assert snapshot["reserved_read"] == 300_001 and snapshot["reserved_written"] == 3_001
        assert BudgetJournal(sql, preview_product=True, product_operations=True).snapshot() == snapshot


@pytest.mark.parametrize("reason", ["D1_OUTCOME_UNKNOWN", "METERING_MISSING", "TIMEOUT",
    "INCOMPLETE_REQUEST", "MANUAL_STOP", "USAGE_EXCEEDED_BOUND", "PREVIEW_SCHEMA_MISMATCH"])
def test_product_policy_never_resumes_an_accounting_or_operator_stop(reason):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = legacy_state(BudgetJournal(sql), stopped=reason)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        assert journal.snapshot() == before
        with pytest.raises(BudgetError, match=reason):
            journal.begin_product(now=10)


@pytest.mark.parametrize("change", [{"schema_ready": False}, {"actual_written": 2_001},
    {"actual_read": 200_001}, {"active": 322},
    {"evidence": [{"rows_read": 7_000, "rows_written": 600, "complete": False}]},
    {"evidence": []}])
def test_product_budget_stop_recovery_requires_matching_complete_evidence(change):
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql)
        before = legacy_state(journal, stopped="BUDGET_EXHAUSTED")
        before.update(change)
        journal._save(before)
        assert BudgetJournal(sql, preview_product=True, product_operations=True).snapshot() == before


def test_new_operation_evidence_is_append_only_bounded_and_keeps_legacy_history():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        before = legacy_state(BudgetJournal(sql))
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        ticket = journal.begin_product(now=10)
        for operation in range(130):
            journal.reserve_operation(ticket, reads=4, writes=2, now=11)
            journal.record(ticket, operation, 1, 1)
            journal.settle_product_reads(ticket, operation, 4, now=12)
        journal.finish(ticket, now=13)
        state = journal.snapshot()
        assert state["evidence"] == before["evidence"]
        assert state["actual_read"] == 7_130 and state["actual_written"] == 730
        assert state["reserved_read"] == 200_130 and state["reserved_written"] == 2_260
        assert state["product_evidence_rows"] == 130
        assert state["read_margin_released"] == 390
        audit = journal.evidence_snapshot()
        assert len(audit["product_evidence"]) == 128 and audit["product_evidence_truncated"]
        assert audit["product_evidence"][0]["operation"] == 2
        assert audit["product_evidence"][-1]["read_margin_released"] == 3
        assert connection.execute("SELECT count(*) FROM preview_operation_evidence").fetchone()[0] == 130
        assert connection.execute("SELECT count(*) FROM preview_read_settlements").fetchone()[0] == 130
        assert BudgetJournal(sql, preview_product=True, product_operations=True).snapshot() == state


@pytest.mark.parametrize("attempts,reserved", [(1, 2), (2, 90), (None, 90)])
def test_native_retry_metadata_keeps_margin_without_stopping_product(attempts, reserved):
    class Raw(RawD1):
        async def batch(self, statements):
            results = await super().batch(statements)
            for result in results:
                result.meta.total_attempts = attempts
            return results

    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=10)
        meter = ProductMeteredD1(Raw(reads=2, writes=0), journal, ticket, clock=lambda: 11)
        asyncio.run(meter._operation([meter.prepare("SELECT 1")], 90, 0))
        journal.finish(ticket, now=12)
        assert journal.snapshot()["reserved_read"] == reserved
        assert journal.snapshot()["actual_read"] == 2 and journal.snapshot()["stopped"] is None
        second = journal.begin_product(now=13)
        journal.finish(second, now=14)


def test_oversized_input_rejects_before_dispatch_and_keeps_other_users_healthy():
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=10)
        raw = RawD1(reads=1, writes=0)
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 11)
        with pytest.raises(ValueError, match="input bound"):
            asyncio.run(meter.batch([meter.prepare("SELECT ?").bind("x" * 8193)]))
        assert raw.calls == 0 and journal.snapshot()["stopped"] is None
        journal.finish(ticket, now=12)
        second = journal.begin_product(now=13)
        meter = ProductMeteredD1(raw, journal, second, clock=lambda: 14)
        asyncio.run(meter.batch([meter.prepare("SELECT ?").bind("valid")]))
        journal.finish(second, now=15)
        assert raw.calls == 1 and journal.snapshot()["stopped"] is None


def test_finite_validation_keeps_its_original_ceilings_in_product_runtime():
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        first = journal.begin(plan("one", reads=6_000, writes=600), now=10)
        journal.finish(first, now=11)
        with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
            journal.begin(plan("two", reads=5_000, writes=500), now=12)


@pytest.mark.parametrize("status", [403, 404, 409, 413, 422, 429, 502, 503])
def test_expected_product_http_errors_finish_ticket_and_allow_next_request(monkeypatch, status):
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["schema_ready"] = True
        journal._save(state)
        seen = []
        async def noop(*args):
            pass
        class Binding:
            def __init__(self, *args, **kwargs):
                self.rate_rejection = None
            async def ensure_cardinality(self):
                pass
        class Application:
            def __init__(self, *args):
                pass
            async def fetch(self, request):
                seen.append(request.url)
                return SimpleNamespace(status=status if len(seen) == 1 else 200)
        monkeypatch.setattr(entry, "reconcile_schema_reads", lambda _: None)
        monkeypatch.setattr(entry, "initialize_product", noop)
        monkeypatch.setattr(entry, "migrate_product_state_records", noop)
        monkeypatch.setattr(entry, "NativeD1", lambda _: None)
        monkeypatch.setattr(entry, "ProductMeteredD1", Binding)
        monkeypatch.setattr(entry, "_Application", Application)
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1", DB=None)
        coordinator = entry.ValidationBudget(SimpleNamespace(storage=SimpleNamespace(sql=journal.sql)), env)
        coordinator.journal = journal
        request = SimpleNamespace(method="GET", url="https://preview.invalid/api/v1/projects")
        async def run():
            assert (await coordinator.fetch(request)).status == status
            assert journal.snapshot()["active"] is None and journal.snapshot()["stopped"] is None
            assert (await coordinator.fetch(request)).status == 200
        asyncio.run(run())


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("cardinality_verified,native_stop", [(True, None), (False, None),
                                                             (True, "D1_OUTCOME_UNKNOWN")])
def test_only_fully_accounted_application_failure_isolated(monkeypatch, timeout,
                                                           cardinality_verified, native_stop):
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["schema_ready"] = True
        journal._save(state)
        seen = []
        async def noop(*args):
            pass
        class Binding:
            def __init__(self, *args, **kwargs):
                self.rate_rejection = None
            async def ensure_cardinality(self):
                pass
            def accounted_outcome(self):
                return cardinality_verified and journal.snapshot()["stopped"] is None
        class Application:
            def __init__(self, *args):
                pass
            async def fetch(self, request):
                seen.append(request.url)
                if len(seen) == 1:
                    if native_stop:
                        journal.stop(native_stop)
                    if timeout:
                        await asyncio.Event().wait()
                    raise RuntimeError("private provider credentials must not appear")
                return SimpleNamespace(status=200)
        monkeypatch.setattr(entry, "REQUEST_SECONDS", .01)
        monkeypatch.setattr(entry, "reconcile_schema_reads", lambda _: None)
        monkeypatch.setattr(entry, "initialize_product", noop)
        monkeypatch.setattr(entry, "migrate_product_state_records", noop)
        monkeypatch.setattr(entry, "NativeD1", lambda _: None)
        monkeypatch.setattr(entry, "ProductMeteredD1", Binding)
        monkeypatch.setattr(entry, "_Application", Application)
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1", DB=None)
        coordinator = entry.ValidationBudget(SimpleNamespace(storage=SimpleNamespace(sql=journal.sql)), env)
        coordinator.journal = journal
        request = SimpleNamespace(method="GET", url="https://preview.invalid/api/v1/projects")
        async def run():
            response = await coordinator.fetch(request)
            assert response[1]["status"] == 503 and "private" not in str(response)
            state = journal.snapshot()
            if cardinality_verified and native_stop is None:
                assert state["stopped"] is None and state["active"] is None
                assert state["product_closed_failures"] == 1
                assert connection.execute("SELECT kind FROM preview_request_failures").fetchone()[0] == (1 if timeout else 2)
                assert (await coordinator.fetch(request)).status == 200
            else:
                assert state["stopped"] == (native_stop or ("TIMEOUT" if timeout else "REQUEST_OUTCOME_UNKNOWN"))
                assert len(seen) == 1
        asyncio.run(run())


@pytest.mark.parametrize("mode,enabled,selected", [("preview", "1", True),
    ("preview", "0", False), ("production", "1", False), ("local", "1", False)])
def test_operation_mode_is_selected_only_for_enabled_preview(mode, enabled, selected):
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        ctx = SimpleNamespace(storage=SimpleNamespace(sql=LocalSql(connection)))
        env = SimpleNamespace(PULLWISE_MODE=mode, PULLWISE_PREVIEW_PRODUCT_ENABLED=enabled)
        assert entry.ValidationBudget(ctx, env)._journal().product_operations is selected


def test_budget_status_distinguishes_retired_ceilings_without_d1_access():
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        ctx = SimpleNamespace(storage=SimpleNamespace(sql=LocalSql(connection)))
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1")
        coordinator = entry.ValidationBudget(ctx, env)
        payload, options = asyncio.run(coordinator.fetch(SimpleNamespace(method="GET",
            url="https://preview.invalid/_preview/budget")))
        assert payload["productOperationMode"] is True
        assert payload["limits"] == {"rowsRead": None, "rowsWritten": None}
        assert payload["historicalCeilings"] == {"rowsRead": 100_000, "rowsWritten": 1_000}


def test_verified_cardinality_avoids_repeated_full_scans_and_survives_restart():
    from test_preview_schema_upgrade import SQLiteD1
    from pullwise_server.cloudflare_preview_budget import initialize_product
    with closing(sqlite3.connect(":memory:")) as storage, closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        sql = LocalSql(storage)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        raw = SQLiteD1(database, journal)
        async def run():
            await initialize_product(raw, journal, clock=lambda: 10)
            assert journal.snapshot()["product_data_verified"] is False
            first = journal.begin_product(now=11)
            meter = ProductMeteredD1(raw, journal, first, clock=lambda: 12)
            initial_calls = raw.calls
            await meter.ensure_cardinality()
            assert raw.calls == initial_calls + 1
            await meter.ensure_cardinality()
            assert raw.calls == initial_calls + 1
            before_write = raw.calls
            await meter.batch([meter.prepare("""INSERT INTO expense_categories
                (id,owner_id,name,created_at,updated_at) VALUES(?,?,?,?,?)""").bind(
                    "cat_new", "owner", "Hosting", "local", "local")])
            assert raw.calls == before_write + 2  # mutation plus post-write verification
            assert journal.snapshot()["product_data"]["rows"]["expense_categories"] == 1
            journal.finish(first, now=13)
            state = journal.snapshot()
            restarted = BudgetJournal(sql, preview_product=True, product_operations=True)
            assert restarted.snapshot() == state
            second = restarted.begin_product(now=14)
            meter = ProductMeteredD1(raw, restarted, second, clock=lambda: 15)
            before_ensure = raw.calls
            await meter.ensure_cardinality()
            assert raw.calls == before_ensure
            assert meter.accounted_outcome()
            restarted.finish(second, now=16)
        asyncio.run(run())


@pytest.mark.parametrize("size,status", [(2 * 1024 * 1024, 200), (25 * 1024 * 1024, 413)])
def test_preview_csv_export_accepts_useful_size_and_returns_bounded_request_error(size, status):
    from test_worker_application_security import application, request
    from pullwise_server.cloudflare_ledger_reports import CsvExport
    app, _ = application()
    app.bounded_exports = True
    app.fetch.__func__.__globals__["io"] = io
    async def chunks():
        # Real Unicode exercises UTF-8 accounting independently of characters.
        chunk = "汉字🙂\n" * 10_000
        emitted = 0
        while emitted < size:
            emitted += len(chunk.encode("utf-8"))
            yield chunk
    export = CsvExport([], None)
    export.chunks = chunks
    async def ledger(**kwargs):
        return 200, export
    app.fetch.__func__.__globals__["handle_ledger_request"] = ledger
    response = asyncio.run(app.fetch(request("/api/v1/expenses/export", method="GET")))
    assert response.status == status
    if status == 200:
        assert isinstance(response.payload, bytes) and len(response.payload) >= size
        assert response.payload.decode("utf-8").startswith("汉字🙂\n")
    else:
        assert response.payload["error"]["code"] == "EXPORT_TOO_LARGE"
