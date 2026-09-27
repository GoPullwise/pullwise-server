from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
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

    def setUp(self) -> None:
        db.reset_initialization_cache()

    def write_state_key(self, temp_dir: str) -> str:
        key_path = os.path.join(temp_dir, "state-encryption-key")
        with open(key_path, "w", encoding="ascii") as key_file:
            key_file.write("01" * 32)
        return key_path

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

    def test_persist_state_excludes_scan_and_issue_business_data(self) -> None:
        with (
            patch.object(app, "STATE_LOADED", True),
            patch.object(app, "STATE_DIRTY", True),
            patch.object(app, "USERS", {"usr_1": {"id": "usr_1"}}),
            patch.object(app, "SESSIONS", {}),
            patch.object(app, "GITHUB_STATES", {}),
            patch.object(app, "SETTINGS", {}),
            patch.object(app, "BILLING_EVENTS", {}),
            patch.object(app, "BILLING_PENDING_UPDATES", []),
            patch.object(app, "SCANS", [{"id": "sc_1"}]),
            patch.object(app, "ISSUES", [{"id": "iss_1"}]),
            patch.object(app.db, "load_state_item", return_value=None),
            patch.object(app.db, "save_state") as save_state,
        ):
            app.persist_state()

        saved = save_state.call_args.args[0]
        self.assertNotIn("scans", saved)
        self.assertNotIn("issues", saved)
        self.assertEqual(saved["users"], {"usr_1": {"id": "usr_1"}})

    def test_save_state_encrypts_github_oauth_tokens_at_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = self.write_state_key(temp_dir)
            state = {
                "users": {
                    "usr_1": {
                        "id": "usr_1",
                        "githubAccessToken": "gho_user_token",
                        "githubIdentities": [
                            {"id": "ghi_1", "accessToken": "gho_identity_token"},
                        ],
                    }
                }
            }

            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path},
                clear=True,
            ):
                db.save_state(state)
                with closing(sqlite3.connect(db_path)) as connection:
                    payload = connection.execute("SELECT payload FROM app_state WHERE name = 'users'").fetchone()[0]
                loaded = db.load_state()

        self.assertNotIn("gho_user_token", payload)
        self.assertNotIn("gho_identity_token", payload)
        self.assertIn("pullwise-state-secret-v1", payload)
        self.assertEqual(loaded, state)
        self.assertEqual(state["users"]["usr_1"]["githubAccessToken"], "gho_user_token")

    def test_load_state_reads_plaintext_tokens_and_migrates_them_to_encrypted_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = self.write_state_key(temp_dir)
            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path},
                clear=True,
            ):
                db.initialize()
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            "INSERT INTO app_state (name, payload) VALUES (?, ?)",
                            (
                                "users",
                                '{"usr_1": {"id": "usr_1", "githubAccessToken": "gho_plain", '
                                '"githubIdentities": [{"id": "ghi_1", "accessToken": "gho_identity"}]}}',
                            ),
                        )

                loaded = db.load_state()
                with closing(sqlite3.connect(db_path)) as connection:
                    payload = connection.execute("SELECT payload FROM app_state WHERE name = 'users'").fetchone()[0]

        self.assertEqual(loaded["users"]["usr_1"]["githubAccessToken"], "gho_plain")
        self.assertEqual(loaded["users"]["usr_1"]["githubIdentities"][0]["accessToken"], "gho_identity")
        self.assertNotIn("gho_plain", payload)
        self.assertNotIn("gho_identity", payload)

    def test_load_state_requires_key_for_encrypted_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = self.write_state_key(temp_dir)
            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path},
                clear=True,
            ):
                db.save_state({"users": {"usr_1": {"id": "usr_1", "githubAccessToken": "gho_user_token"}}})

            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": os.path.join(temp_dir, "missing")},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "PULLWISE_STATE_ENCRYPTION_KEY_PATH"):
                    db.load_state()

    def test_save_state_requires_key_before_persisting_github_tokens_without_production_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = os.path.join(temp_dir, "missing-state-key")
            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "PULLWISE_STATE_ENCRYPTION_KEY_PATH"):
                    db.save_state({"users": {"usr_1": {"id": "usr_1", "githubAccessToken": "gho_user_token"}}})

                with closing(sqlite3.connect(db_path)) as connection:
                    rows = connection.execute("SELECT payload FROM app_state WHERE name = 'users'").fetchall()

        self.assertEqual(rows, [])

    def test_production_save_state_requires_key_before_persisting_github_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = os.path.join(temp_dir, "missing-state-key")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_MODE": "production",
                    "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path,
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "PULLWISE_STATE_ENCRYPTION_KEY_PATH"):
                    db.save_state({"users": {"usr_1": {"id": "usr_1", "githubAccessToken": "gho_user_token"}}})

                with closing(sqlite3.connect(db_path)) as connection:
                    rows = connection.execute("SELECT payload FROM app_state WHERE name = 'users'").fetchall()

        self.assertEqual(rows, [])

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


if __name__ == "__main__":
    unittest.main()
