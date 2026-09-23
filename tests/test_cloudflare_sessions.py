"""Trusted D1 session writes must preserve other sessions and revoke atomically."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_session_adapter import D1SessionTransactions
from pullwise_server.cloudflare_product_read import read_product
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


def test_trusted_session_issue_and_revoke_preserve_other_sessions(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES('sessions',?,?)",
            (json.dumps({"ses-old": {"id": "ses-old", "userId": "owner",
                "createdAt": fixture.now, "expiresAt": fixture.now + 86400}}), fixture.now))
        db.execute("""CREATE TABLE api_keys(key_hash TEXT PRIMARY KEY,user_id TEXT,
            scopes TEXT,expires_at INTEGER,restrictions TEXT,revoked_at INTEGER)""")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1SessionTransactions(binding)
    created = asyncio.run(adapter.issue_session(owner_id="owner", session_id="ses-new",
        now=fixture.now, expires_at=fixture.now + 86400))
    assert created["userId"] == "owner"
    assert asyncio.run(read_product(binding=binding, path="/api/v1/me",
        headers={"Cookie": "pw_session=ses-new"}, now=fixture.now))[0] == 200
    asyncio.run(adapter.revoke_session(owner_id="owner", session_id="ses-new", now=fixture.now))
    assert asyncio.run(read_product(binding=binding, path="/api/v1/me",
        headers={"Cookie": "pw_session=ses-new"}, now=fixture.now))[0] == 401
    assert asyncio.run(read_product(binding=binding, path="/api/v1/me",
        headers={"Cookie": "pw_session=ses-old"}, now=fixture.now))[0] == 200


def test_session_issue_rejects_concurrent_session_map_change_without_loss(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES('sessions','{}',?)",
            (fixture.now,))
    binding = D1ShapedSQLite(fixture.store)

    def another_session_arrives():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload=? WHERE name='sessions'",
                (json.dumps({"ses-other": {"userId": "owner",
                    "expiresAt": fixture.now + 86400}}),))

    binding.before_batch = another_session_arrives
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1SessionTransactions(binding).issue_session(
            owner_id="owner", session_id="ses-new", now=fixture.now,
            expires_at=fixture.now + 86400))
    with closing(fixture.store.connect()) as db:
        saved = json.loads(db.execute("SELECT payload FROM app_state WHERE name='sessions'").fetchone()[0])
    assert "ses-other" in saved and "ses-new" not in saved
