"""Synthetic target D1 tables and account facts for Worker HTTP tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

TOKEN = "pwk_synthetic_ledger_profile"


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _immediate(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class Prepared:
    def __init__(self, binding, sql):
        self.binding, self.sql, self.params = binding, sql, ()

    def bind(self, *params):
        self.params = params
        return self

    async def first(self):
        with self.binding.store.connect() as db:
            row = db.execute(self.sql, self.params).fetchone()
            return dict(row) if row else None

    async def all(self):
        with self.binding.store.connect() as db:
            return SimpleNamespace(results=[dict(row) for row in db.execute(self.sql, self.params)])


class D1ShapedSQLite:
    def __init__(self, store):
        self.store, self.batch_count, self.before_batch = store, 0, None

    def prepare(self, sql):
        return Prepared(self, sql)

    async def batch(self, statements):
        self.batch_count += 1
        if self.before_batch:
            self.before_batch()
        with self.store._immediate() as db:
            results = []
            for statement in statements:
                cursor = db.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(SimpleNamespace(success=True, results=rows))
            return results


def seed(path, *, now=1_800_000_000):
    root = Path(__file__).resolve().parents[1]
    store = Store(path)
    with store.connect() as db:
        for migration in ("0001_ledger.sql", "0002_identity_billing_keys.sql"):
            db.executescript((root / "cloudflare/server/migrations" / migration).read_text())
        account = {"id": "owner", "createdAt": now - 864000,
            "billing": {"plan": "pro", "status": "active", "subscriptionId": "sub_fixture",
                "currentPeriodStart": now - 864000, "currentPeriodEnd": now + 864000},
            "githubId": "author", "githubAccessToken": "sealed:synthetic-user-token",
            "githubIdentities": [{"id": "synthetic_identity", "accessToken": "sealed:synthetic-identity-token"}]}
        frozen = json.dumps(account, separators=(",", ":"))
        db.execute("UPDATE app_state SET payload=?,updated_at=? WHERE name='users'",
                   (json.dumps({"owner": account}, separators=(",", ":")), now))
        db.execute("UPDATE app_state SET payload=?,updated_at=? WHERE name='billingEvents'",
                   ('{"event_fixture":{"status":"processed"}}', now))
        db.execute("INSERT INTO account_entitlement_authority VALUES(?,?,?,?,?,?,?)",
                   ("owner", 1, "pro", "2026-09", now - 864000, now + 864000, 0))
    return SimpleNamespace(store=store, now=now), None, frozen


def seed_auth(fixture, *, scopes=("profile:read",),
              session_expires=None, key_expires=None, restrictions="{}"):
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=?,updated_at=? WHERE name='sessions'",
            (json.dumps({"session-local": {"userId": "owner",
                "expiresAt": fixture.now + 3600 if session_expires is None else session_expires}}), fixture.now))
        db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("key-local", "owner", "Synthetic", TOKEN[:16],
             hashlib.sha256(TOKEN.encode()).hexdigest(), json.dumps(scopes),
             key_expires, restrictions, fixture.now, None, None))
