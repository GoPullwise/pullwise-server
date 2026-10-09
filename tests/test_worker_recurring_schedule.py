"""Exercise real Worker scheduled/RPC entrypoints with local journal storage."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_validation_budget import BUDGET_SCOPE, BudgetError, BudgetJournal
from test_d1_validation_budget import LocalSql
from test_worker_production_runtime import load_entry


NOW = 1_800_000_000
CRON = "0 * * * *"
ENABLED = {"PULLWISE_MODE": "preview", "PULLWISE_D1_ACCESS_ENABLED": "1",
    "PULLWISE_PREVIEW_PRODUCT_ENABLED": "1", "PULLWISE_RECURRING_EXPENSES_ENABLED": "1"}
DISABLED = [
    ("PULLWISE_MODE", "production"), ("PULLWISE_MODE", "local"), ("PULLWISE_MODE", "Preview"),
    ("PULLWISE_MODE", "unknown"), ("PULLWISE_MODE", None),
    ("PULLWISE_D1_ACCESS_ENABLED", "0"), ("PULLWISE_D1_ACCESS_ENABLED", "true"),
    ("PULLWISE_D1_ACCESS_ENABLED", None),
    ("PULLWISE_PREVIEW_PRODUCT_ENABLED", "0"), ("PULLWISE_PREVIEW_PRODUCT_ENABLED", None),
    ("PULLWISE_RECURRING_EXPENSES_ENABLED", "0"), ("PULLWISE_RECURRING_EXPENSES_ENABLED", None),
]


class NoSideEffects(SimpleNamespace):
    def __getattr__(self, name):
        raise AssertionError("Rejected scheduler touched " + name)


def controller(**fields):
    return SimpleNamespace(cron=CRON, scheduledTime=NOW * 1000, **fields)


@pytest.mark.parametrize("field,value", DISABLED)
def test_scheduled_disabled_environments_never_read_clock_namespace_or_database(field, value):
    entry = load_entry()
    settings = {**ENABLED, field: value}
    if value is None:
        settings.pop(field)
    worker = entry.Default()
    worker.env = NoSideEffects(**settings)
    # Missing flags use getattr defaults; any other access is forbidden.
    if value is None:
        worker.env = SimpleNamespace(**settings)
    assert asyncio.run(worker.scheduled(NoSideEffects(), worker.env, NoSideEffects())) is None


@pytest.mark.parametrize("field,value", DISABLED)
def test_recurring_rpc_disabled_environments_never_open_journal_or_database(field, value):
    entry = load_entry()
    settings = {**ENABLED, field: value}
    if value is None:
        settings.pop(field)
    env = SimpleNamespace(**settings) if value is None else NoSideEffects(**settings)
    coordinator = entry.ValidationBudget(NoSideEffects(), env)
    assert asyncio.run(coordinator.runRecurring()) == {"ok": False, "error": "RECURRING_DISABLED"}
    assert coordinator.journal is None and coordinator._waiting == 0


@pytest.mark.parametrize("result", [{"ok": True}, SimpleNamespace(ok=True)])
def test_scheduled_awaits_fixed_singleton_rpc_without_caller_clock_or_direct_d1(result):
    entry = load_entry()
    calls = []
    class Namespace:
        def idFromName(self, name):
            calls.append(("name", name))
            return "fixed-coordinator"
        def get(self, identity):
            assert identity == "fixed-coordinator"
            async def runRecurring(*args, **kwargs):
                assert args == () and kwargs == {}
                calls.append(("rpc",))
                await asyncio.sleep(0)
                calls.append(("complete",))
                return result
            return SimpleNamespace(runRecurring=runRecurring)
    worker = entry.Default()
    worker.env = NoSideEffects(**ENABLED, VALIDATION_BUDGET=Namespace())
    # Supplied event environment cannot redirect scheduling to another binding.
    assert asyncio.run(worker.scheduled(controller(), NoSideEffects(), NoSideEffects())) is None
    assert calls == [("name", BUDGET_SCOPE), ("rpc",), ("complete",)]
    assert BUDGET_SCOPE == "pullwise-s17-s18-2026-09-28"


@pytest.mark.parametrize("cron,scheduled", [
    ("* * * * *", NOW * 1000), ("0 0 * * *", NOW * 1000), ("", NOW * 1000),
    (CRON, -1), (CRON, float("inf")), (CRON, float("-inf")), (CRON, float("nan")),
    (CRON, 9007199254740992), (CRON, "not-a-time"), (CRON, None),
])
def test_invalid_scheduled_trigger_never_resolves_a_coordinator(cron, scheduled):
    entry = load_entry()
    worker = entry.Default()
    worker.env = NoSideEffects(**ENABLED)
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        asyncio.run(worker.scheduled(SimpleNamespace(cron=cron, scheduledTime=scheduled), worker.env, None))


def test_scheduled_missing_binding_fails_closed_and_rpc_rejection_is_not_retried():
    entry = load_entry()
    worker = entry.Default()
    worker.env = SimpleNamespace(**ENABLED)
    with pytest.raises(RuntimeError, match="VALIDATION_CONTROL_REQUIRED"):
        asyncio.run(worker.scheduled(controller(), worker.env, None))
    calls = []
    async def rejected():
        calls.append("rpc")
        return {"ok": False, "error": "SCHEMA_UPGRADE_REQUIRED"}
    worker.env.VALIDATION_BUDGET = SimpleNamespace(idFromName=lambda _: "fixed",
        get=lambda _: SimpleNamespace(runRecurring=rejected))
    with pytest.raises(RuntimeError, match="RECURRING_EXECUTION_UNAVAILABLE"):
        asyncio.run(worker.scheduled(controller(), worker.env, None))
    assert calls == ["rpc"]


@pytest.fixture
def runtime(monkeypatch):
    entry = load_entry()
    monkeypatch.setattr(entry.time, "time", lambda: NOW)
    connection = sqlite3.connect(":memory:")
    sql = LocalSql(connection)
    journal = BudgetJournal(sql, preview_product=True, product_operations=True)
    state = journal.snapshot()
    state.update(schema_ready=True, schema_version=7)
    journal._save(state)
    events, meters = [], []
    native = object()
    def native_d1(binding):
        assert binding is native
        events.append("native")
        return native
    class Meter:
        def __init__(self, binding, selected_journal, ticket):
            assert binding is native and selected_journal is journal
            assert journal.snapshot()["active"] == ticket
            self.ticket, self.accounted = ticket, True
            meters.append(self)
            events.append("meter")
        async def ensure_cardinality(self):
            events.append("cardinality")
        def accounted_outcome(self):
            return self.accounted
    async def forbidden(*args, **kwargs):
        raise AssertionError("Scheduler initialized or migrated the database")
    async def due(**kwargs):
        assert isinstance(kwargs["binding"], PlanLimitedD1)
        assert kwargs["binding"].binding is meters[-1]
        assert kwargs["binding"].now == NOW
        assert kwargs["maintenance_binding"] is meters[-1]
        assert kwargs["now"] == NOW and kwargs["rule_limit"] == kwargs["occurrence_limit"] == 10
        events.append("due")
        return {"scanned": 2, "created": 1, "blocked": 1, "replayed": 0}
    monkeypatch.setattr(entry, "NativeD1", native_d1)
    monkeypatch.setattr(entry, "ProductMeteredD1", Meter)
    monkeypatch.setattr(entry, "initialize_product", forbidden)
    monkeypatch.setattr(entry, "migrate_product_state_records", forbidden)
    monkeypatch.setattr(entry, "upgrade_product_schema_v7", forbidden)
    monkeypatch.setattr(entry, "run_due_recurring", due)
    monkeypatch.setattr(entry, "WorkerGitHubGateway", lambda _: "actor-specific-gateway")
    env = SimpleNamespace(**ENABLED, DB=native)
    coordinator = entry.ValidationBudget(SimpleNamespace(storage=SimpleNamespace(sql=sql)), env)
    coordinator.journal = journal
    try:
        yield SimpleNamespace(entry=entry, coordinator=coordinator, journal=journal,
            events=events, meters=meters, monkeypatch=monkeypatch, sql=sql)
    finally:
        connection.close()


@pytest.mark.parametrize("schema_version", [7, 8])
def test_rpc_runs_bounded_due_work_under_commercial_and_global_meters(runtime, schema_version):
    state = runtime.journal.snapshot()
    state["schema_version"] = schema_version
    runtime.journal._save(state)
    result = asyncio.run(runtime.coordinator.runRecurring())
    assert result == {"ok": True, "scanned": 2, "created": 1, "blocked": 1, "replayed": 0}
    assert runtime.events == ["native", "meter", "cardinality", "due"]
    state = runtime.journal.snapshot()
    assert state["requests"] == 1 and state["active"] is None and state["stopped"] is None
    assert runtime.coordinator._waiting == 0


@pytest.mark.parametrize("ready,version", [(False, 7), (False, 8), (True, 6), (True, 5), (True, 9), (True, None)])
def test_rpc_requires_known_recurring_schema_without_auto_initializing_or_upgrading(runtime, ready, version):
    state = runtime.journal.snapshot()
    state.update(schema_ready=ready, schema_version=version)
    runtime.journal._save(state)
    assert asyncio.run(runtime.coordinator.runRecurring()) == {"ok": False, "error": "SCHEMA_UPGRADE_REQUIRED"}
    assert runtime.events == [] and runtime.journal.snapshot() == state
    assert runtime.coordinator._waiting == 0


def test_rpc_busy_cap_never_reads_journal_database_or_clock():
    entry = load_entry()
    coordinator = entry.ValidationBudget(NoSideEffects(), NoSideEffects(**ENABLED))
    coordinator._waiting = 16
    assert asyncio.run(coordinator.runRecurring()) == {"ok": False, "error": "VALIDATION_BUSY"}
    assert coordinator.journal is None and coordinator._waiting == 16


@pytest.mark.parametrize("error", [RuntimeError("known application failure"), asyncio.TimeoutError()])
def test_fully_accounted_failure_closes_only_its_ticket_and_retains_reservations(runtime, error):
    async def failed(**kwargs):
        ticket = runtime.meters[-1].ticket
        runtime.journal.reserve_operation(ticket, reads=5, writes=3, now=NOW)
        runtime.journal.record(ticket, 0, 2, 1)
        raise error
    runtime.monkeypatch.setattr(runtime.entry, "run_due_recurring", failed)
    assert asyncio.run(runtime.coordinator.runRecurring()) == {"ok": False, "error": "RECURRING_EXECUTION_UNAVAILABLE"}
    state = runtime.journal.snapshot()
    assert state["active"] is None and state["stopped"] is None
    assert state["reserved_read"] == 5 and state["reserved_written"] == 3
    assert state["actual_read"] == 2 and state["actual_written"] == 1
    assert runtime.coordinator._waiting == 0


@pytest.mark.parametrize("error,code", [(RuntimeError("unknown outcome"), "REQUEST_OUTCOME_UNKNOWN"),
    (asyncio.TimeoutError(), "TIMEOUT"), (BudgetError("D1_OUTCOME_UNKNOWN"), "D1_OUTCOME_UNKNOWN")])
def test_unknown_failure_keeps_ticket_reservations_and_persistent_stop(runtime, error, code):
    async def failed(**kwargs):
        meter = runtime.meters[-1]
        meter.accounted = False
        runtime.journal.reserve_operation(meter.ticket, reads=5, writes=3, now=NOW)
        raise error
    runtime.monkeypatch.setattr(runtime.entry, "run_due_recurring", failed)
    assert asyncio.run(runtime.coordinator.runRecurring()) == {"ok": False, "error": code}
    state = runtime.journal.snapshot()
    assert state["active"] == 1 and state["stopped"] == code
    assert state["reserved_read"] == 5 and state["reserved_written"] == 3
    before = list(runtime.events)
    assert asyncio.run(runtime.coordinator.runRecurring()) == {"ok": False, "error": code}
    assert runtime.events == before and runtime.journal.snapshot() == state
    assert runtime.coordinator._waiting == 0


def test_cancellation_preserves_unknown_outcome_and_propagates(runtime):
    async def canceled(**kwargs):
        raise asyncio.CancelledError()
    runtime.monkeypatch.setattr(runtime.entry, "run_due_recurring", canceled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.coordinator.runRecurring())
    assert runtime.journal.snapshot()["active"] == 1
    assert runtime.journal.snapshot()["stopped"] == "REQUEST_OUTCOME_UNKNOWN"
    assert runtime.coordinator._waiting == 0


def test_recurring_rpc_serializes_with_existing_product_lock(runtime):
    async def run():
        async with runtime.coordinator._product_lock:
            pending = asyncio.create_task(runtime.coordinator.runRecurring())
            await asyncio.sleep(0)
            assert runtime.coordinator._waiting == 1 and runtime.events == []
        assert (await pending)["ok"] is True
    asyncio.run(run())
    assert runtime.events == ["native", "meter", "cardinality", "due"]
    assert runtime.coordinator._waiting == 0


def test_binding_rpc_accepts_no_caller_supplied_clock(runtime):
    with pytest.raises(TypeError):
        asyncio.run(runtime.coordinator.runRecurring(NOW + 100000))
    with pytest.raises(TypeError):
        asyncio.run(runtime.coordinator.runRecurring(now=NOW + 100000))
    assert runtime.events == [] and runtime.journal.snapshot()["requests"] == 0


@pytest.mark.parametrize("path", ["/_preview/recurring/tick", "/_preview/runRecurring", "/scheduled"])
def test_public_tick_guesses_never_open_a_journal_or_dispatch_due_work(runtime, path):
    runtime.coordinator.journal = None
    runtime.coordinator.rate_limiter = SimpleNamespace(ingress=lambda *args, **kwargs: None)
    response = asyncio.run(runtime.coordinator.fetch(SimpleNamespace(method="POST",
        url="https://preview.invalid" + path, headers={})))
    assert response.status == 404
    assert runtime.coordinator.journal is None and runtime.events == []
