"""Exercise closed maintenance through the actual singleton Worker path."""
import asyncio
from types import SimpleNamespace

import pytest

from test_preview_business_clear import fixture, armed, RecordingD1, facts
from test_worker_cost_pause import load_entry
from pullwise_server.cloudflare_preview_business_clear import BUSINESS_TABLES, CLEAR_SQL


@pytest.fixture
def runtime(fixture, monkeypatch):
    database, storage, journal = fixture
    entry = load_entry()
    raw = RecordingD1(database, journal)
    events = []
    async def prepared(*args):
        events.append("prepared")
    class Application:
        def __init__(self, env, binding):
            pass
        async def fetch(self, request):
            events.append("health")
            return entry.Response.json({"ok": True})
    class Meter:
        def __init__(self, *args, **kwargs):
            self.rate_rejection = None
        async def ensure_cardinality(self):
            events.append("cardinality")
        def accounted_outcome(self):
            return True
    monkeypatch.setattr(entry.time, "time", lambda: 10)
    monkeypatch.setattr(entry, "NativeD1", lambda binding: binding)
    monkeypatch.setattr(entry, "initialize_product", prepared)
    monkeypatch.setattr(entry, "migrate_product_state_records", prepared)
    monkeypatch.setattr(entry, "ProductMeteredD1", Meter)
    monkeypatch.setattr(entry, "_Application", Application)
    env = armed(DB=raw, PULLWISE_RECURRING_EXPENSES_ENABLED="1")
    coordinator = entry.ValidationBudget(SimpleNamespace(storage=SimpleNamespace(sql=journal.sql)), env)
    coordinator.journal = journal
    return SimpleNamespace(entry=entry, raw=raw, database=database, storage=storage,
        journal=journal, coordinator=coordinator, events=events)


def fetch(runtime, path, method="GET"):
    return asyncio.run(runtime.coordinator.fetch(SimpleNamespace(method=method,
        url="https://preview.invalid"+path, headers={})))


@pytest.mark.parametrize("path,method", [("/api/v1/projects", "GET"),
    ("/api/v1/expenses", "POST"), ("/health", "POST"), ("/auth/email/request-code", "POST")])
def test_pending_action_denies_business_before_upgrade_ticket_or_d1(runtime, path, method):
    before = runtime.journal.snapshot()
    payload, options = fetch(runtime, path, method)
    assert options["status"] == 503
    assert payload["error"]["code"] == "PREVIEW_BUSINESS_CLEAR_PENDING"
    assert runtime.raw.calls == 0 and runtime.events == []
    assert runtime.journal.snapshot() == before


def test_pending_scheduler_denies_before_clock_ticket_or_database(runtime):
    before = runtime.journal.snapshot()
    assert asyncio.run(runtime.coordinator.runRecurring()) == {
        "ok": False, "error": "PREVIEW_BUSINESS_CLEAR_PENDING"}
    assert runtime.raw.calls == 0 and runtime.events == []
    assert runtime.journal.snapshot() == before


def test_health_clears_once_then_cached_receipt_and_later_health_do_no_more_d1(runtime):
    original = facts(runtime.database)
    payload, options = fetch(runtime, "/health")
    assert payload == {"ok": True} and runtime.raw.calls == 3
    assert runtime.raw.dispatched[1] == CLEAR_SQL
    assert runtime.events == ["prepared", "prepared", "cardinality", "health"]
    assert runtime.journal.snapshot()["active"] is None
    remaining = facts(runtime.database)
    assert all(original[table] and remaining[table] == [] for table in BUSINESS_TABLES)
    before = runtime.journal.snapshot()
    payload, _ = fetch(runtime, "/_preview/budget")
    assert payload["businessClear"]["complete"] is True
    assert payload["businessClear"]["remainingBusinessRows"]["total"] == 0
    assert runtime.raw.calls == 3 and runtime.journal.snapshot() == before
    fetch(runtime, "/health")
    assert runtime.raw.calls == 3 and runtime.journal.snapshot()["stopped"] is None


def test_invalid_arming_and_cached_budget_do_not_run_d1(runtime):
    runtime.coordinator.env.PULLWISE_PREVIEW_BUSINESS_CLEAR_MANIFEST = "wrong"
    before = runtime.journal.snapshot()
    payload, options = fetch(runtime, "/health")
    assert options["status"] == 503
    assert payload["error"]["code"] == "PREVIEW_BUSINESS_CLEAR_UNREVIEWED"
    assert runtime.raw.calls == 0 and runtime.journal.snapshot() == before
    payload, _ = fetch(runtime, "/_preview/budget")
    assert payload["businessClear"] is None and runtime.raw.calls == 0
