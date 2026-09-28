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
    with patch.dict(sys.modules, {"workers": SimpleNamespace(Response=Response, WorkerEntrypoint=object)}):
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
