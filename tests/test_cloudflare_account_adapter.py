"""Async account commands against the actual Server tables through a D1-shaped binding."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
from test_cloudflare_server_mapping import seed


class Prepared:
    def __init__(self, binding, sql):
        self.binding = binding
        self.sql = sql
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self

    async def first(self):
        with closing(self.binding.store.connect()) as connection:
            cursor = connection.execute(self.sql, self.params)
            row = cursor.fetchone()
            return dict(zip((column[0] for column in cursor.description), row)) if row else None

    async def all(self):
        with closing(self.binding.store.connect()) as connection:
            cursor = connection.execute(self.sql, self.params)
            columns = [column[0] for column in cursor.description]
            return type("Rows", (), {"results": [dict(zip(columns, row)) for row in cursor.fetchall()]})()


class D1ShapedSQLite:
    def __init__(self, store):
        self.store = store
        self.batch_count = 0
        self.before_batch = None

    def prepare(self, sql):
        return Prepared(self, sql)

    async def batch(self, statements):
        self.batch_count += 1
        if self.before_batch:
            self.before_batch()
        with self.store._immediate() as connection:
            for statement in statements:
                connection.execute(statement.sql, statement.params)
        return [{"success": True}]


def test_async_account_adapter_dirties_and_refreshes_persisted_owner(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
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
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AccountTransactions(binding)
    changed = json.dumps({**json.loads(frozen), "githubLogin": "changed"}, separators=(",", ":"))

    def concurrent_change():
        with fixture.store._immediate() as connection:
            connection.execute("UPDATE app_state SET payload='{}' WHERE name='users'")

    binding.before_batch = concurrent_change
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.stage_account_write(owner_id="owner", expected_revision=1,
            next_account_json=changed, now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (1, 0)


def test_async_pending_reconciliation_uses_current_durable_snapshots(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AccountTransactions(binding)
    pending = '[{"eventId":"later","customerId":"customer"}]'
    asyncio.run(adapter.stage_pending_billing_updates(next_pending_json=pending, now=fixture.now))
    updated = json.dumps({**json.loads(frozen), "githubLogin": "known"}, separators=(",", ":"))
    events = '{"event_fixture":{"status":"processed"},"later":{"applied":true}}'
    asyncio.run(adapter.stage_billing_reconciliation(owner_id="owner", expected_revision=1,
        next_account_json=updated, next_events_json=events,
        next_pending_json='[]', now=fixture.now))
    with closing(fixture.store.connect()) as connection:
        assert tuple(connection.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert connection.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == events
        assert connection.execute("SELECT payload FROM app_state WHERE name='billingPendingUpdates'").fetchone()[0] == '[]'
    assert binding.batch_count == 2
