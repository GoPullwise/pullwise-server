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

    def test_initialize_preserves_existing_workspace_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY)")
                        connection.execute("CREATE TABLE workspace_members (id TEXT PRIMARY KEY)")
                        connection.execute("CREATE TABLE workspace_repositories (id TEXT PRIMARY KEY)")
                        connection.execute("INSERT INTO workspaces (id) VALUES ('ws_1')")

                db.initialize()

                with closing(sqlite3.connect(db_path)) as connection:
                    workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
                    member_exists = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workspace_members'"
                    ).fetchone()
                    repository_exists = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workspace_repositories'"
                    ).fetchone()

        self.assertEqual(workspace_count, 1)
        self.assertIsNotNone(member_exists)
        self.assertIsNotNone(repository_exists)


if __name__ == "__main__":
    unittest.main()
