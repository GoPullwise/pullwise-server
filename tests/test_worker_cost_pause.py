"""The remote cost pause must reject requests before any D1/provider access."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_entry():
    class Response:
        @staticmethod
        def json(payload, **kwargs):
            return payload, kwargs

    spec = importlib.util.spec_from_file_location("cost_pause_entry", ROOT / "cloudflare/server/src/entry.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": SimpleNamespace(Response=Response, WorkerEntrypoint=object,
                                                           DurableObject=object)}):
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("enabled", ["0", "false", "2", None])
def test_disabled_database_access_stops_every_route_before_side_effects(enabled):
    entry = load_entry()

    class PausedEnvironment:
        def __getattr__(self, name):
            if name == "PULLWISE_D1_ACCESS_ENABLED" and enabled is None:
                raise AttributeError(name)
            raise AssertionError(f"Paused Worker accessed {name}")

    worker = entry.Default()
    worker.env = PausedEnvironment()
    if enabled is not None:
        worker.env.PULLWISE_D1_ACCESS_ENABLED = enabled
    for path in ("/health", "/auth/github/authorize", "/auth/github/callback", "/api/v1/me",
                 "/api/v1/expenses", "/api/v1/expenses/export", "/webhooks/creem", "/billing/plan"):
        payload, options = asyncio.run(worker.fetch(SimpleNamespace(url="https://api.example.test" + path)))
        assert options["status"] == 503
        assert options["headers"]["Cache-Control"] == "no-store"
        assert payload["error"]["code"] == "D1_ACCESS_PAUSED"


def test_enabled_remote_worker_without_global_control_still_fails_closed():
    entry = load_entry()

    class Environment:
        PULLWISE_D1_ACCESS_ENABLED = "1"
        PULLWISE_MODE = "preview"
        PULLWISE_PREVIEW_PRODUCT_ENABLED = "0"

        def __getattr__(self, name):
            if name == "VALIDATION_BUDGET":
                raise AttributeError(name)
            raise AssertionError(f"Uncontrolled Worker accessed {name}")

    worker = entry.Default()
    worker.env = Environment()
    payload, options = asyncio.run(worker.fetch(SimpleNamespace(url="https://preview.invalid/webhooks/creem")))
    assert options["status"] == 503
    assert payload["error"]["code"] == "VALIDATION_CONTROL_REQUIRED"


def test_every_preview_route_uses_the_same_singleton_without_direct_d1():
    entry = load_entry()
    names, calls = [], []

    class Namespace:
        def idFromName(self, name):
            names.append(name)
            return "fixed-id"

        def get(self, identity):
            assert identity == "fixed-id"
            async def fetch(request):
                calls.append(request.url)
                return "bounded-response"
            return SimpleNamespace(fetch=fetch)

    class Environment:
        PULLWISE_D1_ACCESS_ENABLED = "1"
        PULLWISE_MODE = "preview"
        VALIDATION_BUDGET = Namespace()

        def __getattr__(self, name):
            raise AssertionError(f"Preview ingress accessed {name}")

    worker = entry.Default()
    worker.env = Environment()
    for path in ("/auth/github/authorize", "/auth/github/callback", "/api-keys",
                 "/auth/session", "/webhooks/creem", "/api/v1/expenses/export", "/unknown"):
        assert asyncio.run(worker.fetch(SimpleNamespace(url="https://preview.invalid" + path))) == "bounded-response"
    assert len(calls) == 7 and set(names) == {"pullwise-s17-s18-2026-09-28"}


def test_production_cannot_enter_preview_validation_even_if_enabled():
    entry = load_entry()
    worker = entry.Default()
    worker.env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED="1", PULLWISE_MODE="production")
    payload, options = asyncio.run(worker.fetch(SimpleNamespace(url="https://api.pull-wise.com/health")))
    assert options["status"] == 503
    assert payload["error"]["code"] == "VALIDATION_CONTROL_REQUIRED"


def test_preview_coordinator_unknown_paths_do_not_touch_storage_d1_or_providers():
    entry = load_entry()

    class Environment:
        PULLWISE_D1_ACCESS_ENABLED = "1"
        PULLWISE_MODE = "preview"
        PULLWISE_PREVIEW_PRODUCT_ENABLED = "0"

        def __getattr__(self, name):
            raise AssertionError(f"Unreviewed case accessed {name}")

    coordinator = entry.ValidationBudget(SimpleNamespace(), Environment())
    for method, path in [("GET", "/auth/github/authorize"), ("GET", "/auth/github/callback"),
                         ("GET", "/auth/session"), ("POST", "/api-keys"),
                         ("POST", "/webhooks/creem"), ("POST", "/migration"),
                         ("DELETE", "/cleanup"), ("GET", "/billing/plan")]:
        payload, options = asyncio.run(coordinator.fetch(SimpleNamespace(
            method=method, url="https://preview.invalid" + path)))
        assert options["status"] == 503
        assert payload["error"]["code"] == "UNREVIEWED_CASE"
    assert coordinator.journal is None


def test_unreviewed_initialization_rpc_cannot_touch_storage_or_d1():
    entry = load_entry()

    class Environment:
        PULLWISE_D1_ACCESS_ENABLED = "1"
        PULLWISE_MODE = "preview"

        def __getattr__(self, name):
            raise AssertionError(f"Unreviewed initialization accessed {name}")

    coordinator = entry.ValidationBudget(SimpleNamespace(), Environment())
    result = asyncio.run(coordinator.initialize())
    assert result == {"initialized": False, "error": "UNREVIEWED_INITIALIZATION"}
    assert coordinator.journal is None


@pytest.mark.parametrize("enabled,mode,reason", [
    ("0", "preview", "D1_ACCESS_PAUSED"),
    ("1", "production", "VALIDATION_CONTROL_REQUIRED"),
    ("1", "local", "VALIDATION_CONTROL_REQUIRED"),
])
def test_initialization_rpc_cannot_bypass_pause_or_environment(enabled, mode, reason):
    entry = load_entry()

    class Environment:
        PULLWISE_D1_ACCESS_ENABLED = enabled
        PULLWISE_MODE = mode

        def __getattr__(self, name):
            raise AssertionError(f"Rejected initialization accessed {name}")

    coordinator = entry.ValidationBudget(SimpleNamespace(), Environment())
    assert asyncio.run(coordinator.initialize()) == {"initialized": False, "error": reason}
    assert coordinator.journal is None
