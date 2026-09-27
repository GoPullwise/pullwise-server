from __future__ import annotations

import tempfile
import unittest
import asyncio
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pullwise_server.github_sources import resolve_upstream_repository
from pullwise_server.product_store import ProductStore
from pullwise_server.cloudflare_watch_adapter import D1WatchTransactions


class _Prepared:
    def __init__(self, binding, sql):
        self.binding, self.sql, self.params = binding, sql, ()

    def bind(self, *params):
        self.params = params
        return self

    async def first(self):
        with closing(self.binding.store.connect()) as connection:
            row = connection.execute(self.sql, self.params).fetchone()
            return dict(row) if row is not None else None


class _D1ShapedSQLite:
    def __init__(self, store):
        self.store = store

    def prepare(self, sql):
        return _Prepared(self, sql)

    async def batch(self, statements):
        with self.store._immediate() as connection:
            results = []
            for statement in statements:
                cursor = connection.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(SimpleNamespace(success=True, results=rows))
            return results


class UpstreamRepositoryIdentityTest(unittest.TestCase):
    def test_only_verified_numeric_id_and_matching_canonical_name_are_accepted(self) -> None:
        for repository in (
            {"id": True, "full_name": "acme/toolkit", "private": False},
            {"id": "123", "full_name": "acme/toolkit", "private": False},
            {"id": 0, "full_name": "acme/toolkit", "private": False},
            {"id": 123, "private": False},
            {"id": 123, "full_name": "other/toolkit", "private": False},
        ):
            response = Mock(status_code=200)
            response.json.return_value = repository
            with self.subTest(repository=repository), patch(
                "pullwise_server.github_sources.requests.get", return_value=response
            ), self.assertRaisesRegex(ValueError, "GITHUB_REPOSITORY_INVALID_RESPONSE"):
                resolve_upstream_repository("acme", "toolkit")

    def test_redirect_is_not_a_repository_identity_proof(self) -> None:
        response = Mock(status_code=302)
        response.json.return_value = {"id": 123, "full_name": "acme/toolkit", "private": False}
        with patch("pullwise_server.github_sources.requests.get", return_value=response), \
             self.assertRaisesRegex(ValueError, "GITHUB_REPOSITORY_UNAVAILABLE"):
            resolve_upstream_repository("acme", "toolkit")

    def test_verified_name_is_persisted_with_watch_and_available_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "product.sqlite3"
            store = ProductStore(path)
            store.initialize()
            created = store.create_watch(
                owner_id="owner", target_repository_id=None,
                upstream_repository_id="github:123", upstream_full_name="Acme/Toolkit",
                billing_owner_id="owner", interests=["OAuth"], enabled=True,
                analysis_enabled=False,
            )
            self.assertEqual(created["upstream"], "Acme/Toolkit")
            reopened = ProductStore(path)
            self.assertEqual(reopened.get_watch(created["id"])["upstream"], "Acme/Toolkit")
            with self.assertRaisesRegex(ValueError, "invalid upstream_full_name"):
                reopened.create_watch(
                    owner_id="owner", target_repository_id=None,
                    upstream_repository_id="github:124", upstream_full_name="evil/../name",
                    billing_owner_id="owner", interests=["OAuth"], enabled=True,
                    analysis_enabled=False,
                )

    def test_candidate_public_watch_persists_the_proven_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(Path(directory) / "candidate.sqlite3")
            store.initialize()
            now = 1_800_000_000
            with store._immediate() as connection:
                connection.execute("CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL)")
                connection.execute("CREATE TABLE d1_command_guard(ok INTEGER CHECK(ok=1))")
                connection.execute("INSERT INTO app_state VALUES('users',?,?)",
                                   (json.dumps({"owner": {"id": "owner"}}), now))
                connection.execute("""INSERT INTO public_upstream_proofs VALUES
                    ('acme/toolkit','github:123','Acme/Toolkit',1,0,1,?,?)""", (now, now + 300))
            created = asyncio.run(D1WatchTransactions(_D1ShapedSQLite(store)).create_public_watch(
                owner_id="owner", resolved_public_repository_id="github:123",
                interests=["OAuth"], enabled=True, analysis_enabled=False, now=now,
                public_resolution={"lookup_key": "acme/toolkit", "full_name": "Acme/Toolkit",
                                   "source_revision": 1},
            ))
            self.assertEqual(created["upstream"], "Acme/Toolkit")
            self.assertEqual(store.get_watch(created["id"])["upstream"], "Acme/Toolkit")


if __name__ == "__main__":
    unittest.main()
