"""Abuse admission must never write D1 or poison shared request accounting."""
import asyncio
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_preview_rate import PreviewRateLimiter, PreviewRateLimit, request_channel
from pullwise_server.cloudflare_validation_budget import BudgetJournal


def test_oauth_ip_cap_prevents_session_rotation_and_recovers_next_window():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal, limiter = BudgetJournal(sql, preview_product=True, product_operations=True), PreviewRateLimiter(sql)
        before = journal.snapshot()
        for i in range(10):
            limiter.ingress({"cf-connecting-ip": "203.0.113.10", "cookie": f"pw_session=rotated{i}"},
                method="GET", path="/auth/github/authorize", now=120)
        with pytest.raises(PreviewRateLimit) as caught:
            limiter.ingress({"cf-connecting-ip": "203.0.113.10", "cookie": "pw_session=another"},
                method="GET", path="/auth/github/authorize", now=121)
        assert caught.value.retry_after == 59
        assert journal.snapshot() == before
        limiter.ingress({"cf-connecting-ip": "203.0.113.10"}, method="GET",
            path="/auth/github/authorize", now=180)


def test_actor_counters_apply_across_credentials_and_do_not_block_other_people():
    with closing(sqlite3.connect(":memory:")) as connection:
        limiter = PreviewRateLimiter(LocalSql(connection))
        for _ in range(60):
            limiter.actor("actor-one", channel="write", now=120)
        with pytest.raises(PreviewRateLimit):
            limiter.actor("actor-one", channel="write", now=121)
        limiter.actor("actor-two", channel="write", now=121)
        limiter.actor("actor-one", channel="read", now=121)
        limiter.actor("actor-one", channel="security", now=121)
        limiter.actor("actor-one", channel="write", now=180)
        assert all(len(row[0]) == 64 for row in connection.execute("SELECT subject FROM preview_request_rates"))
        assert "actor-one" not in str(list(connection.execute("SELECT * FROM preview_request_rates")))


def test_read_credential_limit_and_ip_limit_both_hold_under_rotation():
    with closing(sqlite3.connect(":memory:")) as connection:
        limiter = PreviewRateLimiter(LocalSql(connection))
        for _ in range(240):
            limiter.ingress({"cf-connecting-ip": "203.0.113.11", "authorization": "Bearer test"},
                method="GET", path="/api/v1/projects", now=120)
        with pytest.raises(PreviewRateLimit):
            limiter.ingress({"cf-connecting-ip": "203.0.113.12", "authorization": "Bearer test"},
                method="GET", path="/api/v1/projects", now=120)
        for i in range(360):
            limiter.ingress({"cf-connecting-ip": "203.0.113.11", "authorization": f"Bearer {i}"},
                method="GET", path="/api/v1/projects", now=120)
        with pytest.raises(PreviewRateLimit):
            limiter.ingress({"cf-connecting-ip": "203.0.113.11", "authorization": "Bearer fresh"},
                method="GET", path="/api/v1/projects", now=120)


def test_ephemeral_subject_capacity_is_bounded_and_expires_without_touching_journal():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        before = journal.snapshot()
        limiter = PreviewRateLimiter(sql)
        limiter.MAX_SUBJECTS = 3
        for actor in ("one", "two", "three"):
            limiter.actor(actor, channel="read", now=120)
        with pytest.raises(PreviewRateLimit):
            limiter.actor("four", channel="read", now=120)
        limiter.actor("four", channel="read", now=240)
        assert connection.execute("SELECT count(*) FROM preview_request_rates").fetchone()[0] == 1
        assert connection.execute("SELECT subjects FROM preview_rate_clock").fetchone()[0] == 1
        assert journal.snapshot() == before


def test_restart_and_late_clock_do_not_reopen_a_window():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        limiter = PreviewRateLimiter(sql)
        for _ in range(120):
            limiter.actor("one", channel="read", now=180)
        for now in (179, 180, 181):
            with pytest.raises(PreviewRateLimit):
                PreviewRateLimiter(sql).actor("one", channel="read", now=now)
        PreviewRateLimiter(sql).actor("one", channel="read", now=240)


def test_principal_observer_reaches_optional_meter_without_changing_authentication():
    from pullwise_server.cloudflare_principal import _observe_authenticated_actor
    seen = []
    meter = SimpleNamespace(observe_authenticated_actor=seen.append)
    _observe_authenticated_actor(SimpleNamespace(binding=meter), "authenticated-id")
    assert seen == ["authenticated-id"]
    _observe_authenticated_actor(SimpleNamespace(), "ignored-in-production")
    assert seen == ["authenticated-id"]


def test_ingress_rejection_returns_429_before_d1_or_budget_ticket(monkeypatch):
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        ctx = SimpleNamespace(storage=SimpleNamespace(sql=LocalSql(connection)))
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1")
        coordinator = entry.ValidationBudget(ctx, env)
        coordinator.rate_limiter = PreviewRateLimiter(ctx.storage.sql)
        limiter = coordinator.rate_limiter
        monkeypatch.setattr(entry.time, "time", lambda: 120)
        headers = {"cf-connecting-ip": "203.0.113.13"}
        for _ in range(10):
            limiter.ingress(headers, method="GET", path="/auth/github/authorize", now=120)
        response, options = asyncio.run(coordinator.fetch(SimpleNamespace(method="GET",
            url="https://preview.invalid/auth/github/authorize", headers=headers)))
        assert options["status"] == 429 and options["headers"]["Retry-After"] == "60"
        assert response["error"]["code"] == "PREVIEW_RATE_LIMIT"
        assert coordinator.journal is None


def test_public_do_only_budget_status_also_has_ingress_protection(monkeypatch):
    from test_worker_cost_pause import load_entry
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        ctx = SimpleNamespace(storage=SimpleNamespace(sql=LocalSql(connection)))
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1")
        coordinator = entry.ValidationBudget(ctx, env)
        coordinator.rate_limiter = PreviewRateLimiter(ctx.storage.sql)
        monkeypatch.setattr(entry.time, "time", lambda: 120)
        headers = {"cf-connecting-ip": "203.0.113.14"}
        for _ in range(600):
            coordinator.rate_limiter.ingress(headers, method="GET", path="/_preview/budget", now=120)
        payload, options = asyncio.run(coordinator.fetch(SimpleNamespace(method="GET",
            url="https://preview.invalid/_preview/budget", headers=headers)))
        assert options["status"] == 429 and options["headers"]["Retry-After"] == "60"
        assert payload["error"]["code"] == "PREVIEW_RATE_LIMIT"
        assert coordinator.journal is None


def test_authenticated_rate_rejection_remains_429_when_route_catches_provider_errors(monkeypatch):
    from test_worker_cost_pause import load_entry
    from test_d1_validation_budget import RawD1
    from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, initial_data
    from pullwise_server.cloudflare_preview_schema import SCHEMA_VERSION, SCHEMA_FINGERPRINT
    from pullwise_server.cloudflare_principal import _observe_authenticated_actor
    entry = load_entry()
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        state = journal.snapshot()
        state.update(schema_ready=True, schema_version=SCHEMA_VERSION,
            schema_fingerprint=SCHEMA_FINGERPRINT, product_data=initial_data(), product_data_verified=True)
        journal._save(state)
        limiter = PreviewRateLimiter(sql)
        for _ in range(60):
            limiter.actor("user-one", channel="write", now=120)
        raw = RawD1(reads=0, writes=0)
        actor = ["user-one"]
        async def noop(*args):
            pass
        class Application:
            def __init__(self, env, binding):
                self.binding = binding
            async def fetch(self, request):
                try:
                    _observe_authenticated_actor(SimpleNamespace(binding=self.binding), actor[0])
                except Exception:
                    return SimpleNamespace(status=503)
                return SimpleNamespace(status=200)
        monkeypatch.setattr(entry.time, "time", lambda: 120)
        monkeypatch.setattr(entry, "initialize_product", noop)
        monkeypatch.setattr(entry, "migrate_product_state_records", noop)
        monkeypatch.setattr(entry, "NativeD1", lambda _: raw)
        monkeypatch.setattr(entry, "ProductMeteredD1", lambda *args, **kwargs:
            ProductMeteredD1(*args, **kwargs, clock=lambda: 120))
        monkeypatch.setattr(entry, "_Application", Application)
        env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
            PULLWISE_PREVIEW_PRODUCT_ENABLED="1", DB=None)
        coordinator = entry.ValidationBudget(SimpleNamespace(storage=SimpleNamespace(sql=sql)), env)
        coordinator.journal, coordinator.rate_limiter = journal, limiter
        request = SimpleNamespace(method="POST", url="https://preview.invalid/api/v1/projects", headers={})
        async def run():
            payload, options = await coordinator.fetch(request)
            assert options["status"] == 429 and options["headers"]["Retry-After"] == "60"
            assert payload["error"]["code"] == "PREVIEW_RATE_LIMIT"
            assert journal.snapshot()["active"] is None and journal.snapshot()["stopped"] is None
            actor[0] = "user-two"
            assert (await coordinator.fetch(request)).status == 200
            assert raw.calls == 0  # admission never generated D1 work
        asyncio.run(run())


@pytest.mark.parametrize("method,path,channel", [("GET", "/api/v1/projects", "read"),
    ("POST", "/billing/checkout-sessions", "write"), ("POST", "/api-keys", "write"),
    ("GET", "/auth/github/callback", "write"), ("DELETE", "/api-keys/key", "security"),
    ("DELETE", "/api/v1/workspaces/workspace/members/person", "security")])
def test_request_channels_cover_identity_platform_and_security_writes(method, path, channel):
    assert request_channel(method, path) == channel
