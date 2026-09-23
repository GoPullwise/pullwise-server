"""Synthetic OAuth state is durable and single-use under D1 CAS."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_oauth_state_adapter import D1OAuthStates
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


def test_oauth_state_issue_and_single_use_consume(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1OAuthStates(D1ShapedSQLite(fixture.store))
    record = {"kind": "login", "redirectTo": "dashboard",
              "codeVerifier": "synthetic-verifier", "expiresAt": fixture.now + 300}
    asyncio.run(adapter.issue(state_id="state-synthetic", record=record,
        now=fixture.now))
    saved = asyncio.run(adapter.consume(state_id="state-synthetic",
        expected_kind="login", now=fixture.now))
    assert saved == record
    with pytest.raises(ValueError, match="OAUTH_STATE_INVALID"):
        asyncio.run(adapter.consume(state_id="state-synthetic",
            expected_kind="login", now=fixture.now))


def test_oauth_state_consume_rejects_concurrent_map_change(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1OAuthStates(binding)
    asyncio.run(adapter.issue(state_id="state-synthetic",
        record={"kind": "login", "redirectTo": "dashboard",
                "expiresAt": fixture.now + 300}, now=fixture.now))

    def another_state_arrives():
        with fixture.store._immediate() as db:
            states = json.loads(db.execute("SELECT payload FROM app_state WHERE name='githubStates'").fetchone()[0])
            states["other"] = {"kind": "login", "expiresAt": fixture.now + 300}
            db.execute("UPDATE app_state SET payload=? WHERE name='githubStates'",
                (json.dumps(states),))

    binding.before_batch = another_state_arrives
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.consume(state_id="state-synthetic",
            expected_kind="login", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        states = json.loads(db.execute("SELECT payload FROM app_state WHERE name='githubStates'").fetchone()[0])
    assert "other" in states and "state-synthetic" in states


def test_wrong_kind_callback_consumes_state_once(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1OAuthStates(D1ShapedSQLite(fixture.store))
    asyncio.run(adapter.issue(state_id="state-synthetic",
        record={"kind": "login", "redirectTo": "dashboard",
                "expiresAt": fixture.now + 300}, now=fixture.now))
    with pytest.raises(ValueError, match="OAUTH_STATE_INVALID"):
        asyncio.run(adapter.consume(state_id="state-synthetic",
            expected_kind="install_identity", now=fixture.now))
    with pytest.raises(ValueError, match="OAUTH_STATE_INVALID"):
        asyncio.run(adapter.consume(state_id="state-synthetic",
            expected_kind="login", now=fixture.now))
