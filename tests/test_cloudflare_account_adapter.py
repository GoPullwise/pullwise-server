"""Async account commands against the actual Server tables through a D1-shaped binding."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_account_adapter import D1AccountTransactions


from ledger_d1_fixture import D1ShapedSQLite, seed
from state_record_fixtures import normalize_legacy_state


def normalize(fixture):
    with fixture.store._immediate() as db:
        normalize_legacy_state(db, now=fixture.now)


def test_async_account_adapter_dirties_and_refreshes_persisted_owner(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    normalize(fixture)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AccountTransactions(binding)
    changed = json.dumps({**json.loads(frozen), "githubLogin": "changed"}, separators=(",", ":"))
    asyncio.run(adapter.stage_account_write(owner_id="owner", expected_revision=1,
        next_account_json=changed, now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
    asyncio.run(adapter.refresh_account_entitlement(owner_id="owner", expected_revision=2, now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (3, 0)
    assert binding.batch_count == 2


def test_async_account_adapter_rejects_change_between_read_and_batch(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    normalize(fixture)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AccountTransactions(binding)
    changed = json.dumps({**json.loads(frozen), "githubLogin": "changed"}, separators=(",", ":"))

    def concurrent_change():
        with fixture.store._immediate() as connection:
            connection.execute("UPDATE app_state SET payload='{}' WHERE name='record:users:owner'")

    binding.before_batch = concurrent_change
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.stage_account_write(owner_id="owner", expected_revision=1,
            next_account_json=changed, now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (1, 0)


def test_async_account_event_publishes_independent_record_atomically(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    normalize(fixture)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AccountTransactions(binding)
    updated = json.dumps({**json.loads(frozen), "githubLogin": "known"}, separators=(",", ":"))
    asyncio.run(adapter.stage_account_event(owner_id="owner", expected_revision=1,
        next_account_json=updated, event_id="later", event_record_json='{"applied":true}', now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert connection.execute("SELECT payload FROM app_state WHERE name='record:billingEvents:later'").fetchone()[0] == '{"applied":true,"ownerId":"owner"}'
        assert connection.execute("SELECT payload FROM app_state WHERE name='record:billingEvents:event_fixture'").fetchone()[0] == '{"status":"processed"}'
        assert connection.execute("SELECT payload FROM app_state WHERE name='billingPendingUpdates'").fetchone()[0] == '[]'
    assert binding.batch_count == 1
