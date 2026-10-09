"""Synthetic OAuth state is durable and single-use under D1 CAS."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_oauth_state_adapter import D1OAuthStates
from ledger_d1_fixture import D1ShapedSQLite, seed
from pullwise_server.cloudflare_state_records import record_name, encode_record


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


def test_oauth_state_consume_isolated_from_unrelated_state(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1OAuthStates(binding)
    asyncio.run(adapter.issue(state_id="state-synthetic",
        record={"kind": "login", "redirectTo": "dashboard",
                "expiresAt": fixture.now + 300}, now=fixture.now))

    def another_state_arrives():
        with fixture.store._immediate() as db:
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("githubStates", "other"), encode_record("githubStates", "other",
                    {"kind": "login", "expiresAt": fixture.now + 300}), fixture.now))

    binding.before_batch = another_state_arrives
    assert asyncio.run(adapter.consume(state_id="state-synthetic",
        expected_kind="login", now=fixture.now))["kind"] == "login"
    with closing(fixture.store.connect()) as db:
        states = [row[0] for row in db.execute("SELECT name FROM app_state WHERE name GLOB 'record:githubStates:*'")]
    assert states == [record_name("githubStates", "other")]


def test_oauth_state_consume_rejects_same_record_race(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1OAuthStates(binding)
    asyncio.run(adapter.issue(state_id="state-synthetic",
        record={"kind": "login", "expiresAt": fixture.now + 300}, now=fixture.now))
    def replace():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload=? WHERE name=?", (
                encode_record("githubStates", "state-synthetic", {"kind": "install", "expiresAt": fixture.now + 300}),
                record_name("githubStates", "state-synthetic")))
    binding.before_batch = replace
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.consume(state_id="state-synthetic", expected_kind="login", now=fixture.now))


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


@pytest.mark.parametrize("binding_fields", [{}, {"userId": "usr_email_local"}, {"sessionId": "ses-local"}])
def test_link_oauth_state_requires_both_current_identity_and_session(tmp_path, binding_fields):
    fixture, _, _ = seed(tmp_path / "link-state.db")
    adapter = D1OAuthStates(D1ShapedSQLite(fixture.store))
    with pytest.raises(ValueError, match="OAuth link state"):
        asyncio.run(adapter.issue(state_id="link-state", record={"kind": "login", "intent": "link",
            "expiresAt": fixture.now + 300, **binding_fields}, now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT name FROM app_state WHERE name=?",
            (record_name("githubStates", "link-state"),)).fetchone() is None


def test_link_oauth_state_keeps_cookie_binding_until_single_use_consumption(tmp_path):
    fixture, _, _ = seed(tmp_path / "bound-link.db")
    adapter = D1OAuthStates(D1ShapedSQLite(fixture.store))
    record = {"kind": "login", "intent": "link", "userId": "usr_email_local", "sessionId": "ses-local",
              "expiresAt": fixture.now + 300, "codeVerifier": "synthetic-verifier"}
    asyncio.run(adapter.issue(state_id="link-state", record=record, now=fixture.now))
    assert asyncio.run(adapter.consume(state_id="link-state", expected_kind="login", now=fixture.now)) == record
    with pytest.raises(ValueError, match="OAUTH_STATE_INVALID"):
        asyncio.run(adapter.consume(state_id="link-state", expected_kind="login", now=fixture.now))
