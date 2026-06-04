from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from pullwise_server import app, db


class FakeConnection:
    def __init__(self, rows: list[tuple[str, str]] | None = None) -> None:
        self.rows = rows or []
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def execute(self, *_args, **_kwargs) -> "FakeConnection":
        return self

    def executemany(self, *_args, **_kwargs) -> None:
        return None

    def fetchall(self) -> list[tuple[str, str]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class DatabaseContractsTest(unittest.TestCase):
    def test_initialize_closes_sqlite_connection(self) -> None:
        connection = FakeConnection()

        with patch("pullwise_server.db.connect", return_value=connection):
            db.initialize()

        self.assertTrue(connection.closed)

    def test_load_state_closes_initialize_and_read_connections(self) -> None:
        initialize_connection = FakeConnection()
        read_connection = FakeConnection([("users", "{}")])

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, read_connection]):
            self.assertEqual(db.load_state(), {"users": {}})

        self.assertTrue(initialize_connection.closed)
        self.assertTrue(read_connection.closed)

    def test_load_state_ignores_malformed_json_rows(self) -> None:
        initialize_connection = FakeConnection()
        read_connection = FakeConnection([
            ("users", '{"usr_1": {"id": "usr_1"}}'),
            ("sessions", "{not-json"),
        ])

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, read_connection]):
            self.assertEqual(db.load_state(), {"users": {"usr_1": {"id": "usr_1"}}})

    def test_save_state_closes_initialize_and_write_connections(self) -> None:
        initialize_connection = FakeConnection()
        write_connection = FakeConnection()

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, write_connection]):
            db.save_state({"users": {}})

        self.assertTrue(initialize_connection.closed)
        self.assertTrue(write_connection.closed)

    def test_persist_state_keeps_dirty_when_save_fails(self) -> None:
        with (
            patch.object(app, "STATE_LOADED", True),
            patch.object(app, "STATE_DIRTY", True),
            patch.object(app.db, "save_state", side_effect=RuntimeError("disk full")),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.persist_state()

            self.assertTrue(app.STATE_DIRTY)
            log_exception.assert_called_once()

    def test_rate_limit_resets_malformed_stored_request_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO api_rate_limits
                                (key, subject, route, window_start, request_count, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            ("ip:203.0.113.10:api:120", "ip:203.0.113.10", "api", 120, "not-a-count", 120),
                        )

                result = db.record_rate_limit_hit(
                    "ip:203.0.113.10",
                    limit=5,
                    window_seconds=60,
                    timestamp=120,
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    stored_count = connection.execute(
                        "SELECT request_count FROM api_rate_limits WHERE key = ?",
                        ("ip:203.0.113.10:api:120",),
                    ).fetchone()[0]

        self.assertTrue(result["allowed"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["remaining"], 4)
        self.assertEqual(stored_count, 1)

    def test_initialize_removes_workspace_columns_from_quota_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            CREATE TABLE quota_ledger (
                                id TEXT PRIMARY KEY,
                                workspace_id TEXT NOT NULL,
                                repository_id TEXT NOT NULL,
                                github_repo_id TEXT NOT NULL,
                                scan_id TEXT,
                                requested_by_user_id TEXT NOT NULL,
                                request_id TEXT,
                                bucket_id TEXT NOT NULL,
                                delta INTEGER NOT NULL,
                                reason TEXT NOT NULL,
                                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                                FOREIGN KEY(repository_id) REFERENCES repositories(id),
                                FOREIGN KEY(bucket_id) REFERENCES quota_buckets(id)
                            )
                            """
                        )

                db.initialize()

                with closing(sqlite3.connect(db_path)) as connection:
                    columns = [row[1] for row in connection.execute("PRAGMA table_info(quota_ledger)").fetchall()]
                    foreign_key_tables = [row[2] for row in connection.execute("PRAGMA foreign_key_list(quota_ledger)").fetchall()]

        self.assertNotIn("workspace_id", columns)
        self.assertNotIn("workspaces", foreign_key_tables)

    def test_initialize_removes_workspace_foreign_key_from_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY)")
                        connection.execute("INSERT INTO workspaces (id) VALUES ('ws_1')")
                        connection.execute(
                            """
                            CREATE TABLE api_keys (
                                id TEXT PRIMARY KEY,
                                user_id TEXT NOT NULL,
                                workspace_id TEXT NOT NULL,
                                name TEXT NOT NULL,
                                key_prefix TEXT NOT NULL,
                                key_hash TEXT NOT NULL UNIQUE,
                                scopes TEXT NOT NULL DEFAULT '[]',
                                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                                last_used_at INTEGER,
                                revoked_at INTEGER,
                                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO api_keys (
                                id, user_id, workspace_id, name, key_prefix, key_hash,
                                scopes, created_at, last_used_at, revoked_at
                            )
                            VALUES ('ak_old', 'usr_1', 'ws_1', 'Old key', 'pwk_old', 'hash_old', '[]', 100, NULL, NULL)
                            """
                        )
                        connection.execute("DROP TABLE workspaces")

                db.initialize()
                created = db.create_api_key(
                    {
                        "id": "ak_new",
                        "user_id": "usr_1",
                        "name": "New key",
                        "key_prefix": "pwk_new",
                        "key_hash": "hash_new",
                        "scopes": [],
                    }
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    columns = [row[1] for row in connection.execute("PRAGMA table_info(api_keys)").fetchall()]
                    foreign_key_tables = [row[2] for row in connection.execute("PRAGMA foreign_key_list(api_keys)").fetchall()]
                    rows = connection.execute("SELECT id, name FROM api_keys ORDER BY created_at, id").fetchall()

        self.assertEqual(created["id"], "ak_new")
        self.assertNotIn("workspace_id", columns)
        self.assertNotIn("workspaces", foreign_key_tables)
        self.assertEqual(rows, [("ak_old", "Old key"), ("ak_new", "New key")])


if __name__ == "__main__":
    unittest.main()
