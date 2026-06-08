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

    def test_production_save_state_requires_key_before_persisting_github_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path, "PULLWISE_MODE": "production"}, clear=True):
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

    def test_cleanup_operational_records_prunes_only_old_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO worker_commands (
                                id, worker_id, command, status, created_at, updated_at, completed_at
                            )
                            VALUES
                                ('cmd_old_done', 'wk_1', 'stop', 'succeeded', 100, 100, 100),
                                ('cmd_old_pending', 'wk_1', 'stop', 'pending', 100, 100, NULL),
                                ('cmd_recent_done', 'wk_1', 'stop', 'succeeded', 990, 990, 990)
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO worker_audit_events (
                                id, actor_user_id, action, worker_id, changed_fields, created_at
                            )
                            VALUES
                                ('audit_old', 'usr_admin', 'update_worker', 'wk_1', '{}', 100),
                                ('audit_recent', 'usr_admin', 'update_worker', 'wk_1', '{}', 990)
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO scan_jobs (
                                job_id, scan_id, repo, branch, "commit", status,
                                created_at, updated_at, completed_at
                            )
                            VALUES
                                ('job_old_done', 'sc_old_done', 'acme/api', 'main', 'abc', 'done', 100, 100, 100),
                                ('job_old_queued', 'sc_old_queued', 'acme/api', 'main', 'abc', 'queued', 100, 100, NULL),
                                ('job_recent_done', 'sc_recent_done', 'acme/api', 'main', 'abc', 'done', 990, 990, 990)
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO job_results (id, job_id, attempt_id, result_checksum, status, payload, created_at)
                            VALUES ('jr_old', 'job_old_done', 'wk_1-1', 'checksum-old', 'done', '{}', 100)
                            """
                        )

                first_removed = db.cleanup_operational_records(
                    timestamp=1000,
                    worker_command_retention_seconds=100,
                    worker_audit_retention_seconds=100,
                    scan_job_retention_seconds=100,
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    jobs_before_scan_scope = [
                        row[0] for row in connection.execute("SELECT job_id FROM scan_jobs ORDER BY job_id")
                    ]
                    results_before_scan_scope = [
                        row[0] for row in connection.execute("SELECT id FROM job_results ORDER BY id")
                    ]

                removed = db.cleanup_operational_records(
                    timestamp=1000,
                    worker_command_retention_seconds=100,
                    worker_audit_retention_seconds=100,
                    scan_job_retention_seconds=100,
                    removable_scan_ids={"sc_old_done"},
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    commands = [row[0] for row in connection.execute("SELECT id FROM worker_commands ORDER BY id")]
                    audits = [row[0] for row in connection.execute("SELECT id FROM worker_audit_events ORDER BY id")]
                    jobs = [row[0] for row in connection.execute("SELECT job_id FROM scan_jobs ORDER BY job_id")]
                    results = [row[0] for row in connection.execute("SELECT id FROM job_results ORDER BY id")]

        self.assertEqual(first_removed, {"worker_commands": 1, "worker_audit_events": 1, "scan_jobs": 0})
        self.assertEqual(jobs_before_scan_scope, ["job_old_done", "job_old_queued", "job_recent_done"])
        self.assertEqual(results_before_scan_scope, ["jr_old"])
        self.assertEqual(removed, {"worker_commands": 0, "worker_audit_events": 0, "scan_jobs": 1})
        self.assertEqual(commands, ["cmd_old_pending", "cmd_recent_done"])
        self.assertEqual(audits, ["audit_recent"])
        self.assertEqual(jobs, ["job_old_queued", "job_recent_done"])
        self.assertEqual(results, [])

    def test_record_scan_job_result_requires_current_claimed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                db.create_scan_job(
                    {
                        "job_id": "job_lifecycle",
                        "scan_id": "sc_lifecycle",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 100,
                        "user_id": "usr_1",
                    }
                )
                db.create_scan_job(
                    {
                        "job_id": "job_claimed_lifecycle",
                        "scan_id": "sc_claimed_lifecycle",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 99,
                        "user_id": "usr_1",
                    }
                )

                queued_result = db.record_scan_job_result(
                    "job_lifecycle",
                    attempt_id="wk_1-1",
                    status="done",
                    result_checksum="checksum-queued",
                    payload={"status": "done"},
                )
                queued_job = db.get_scan_job("job_lifecycle")

                claimed = db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=3600, timestamp=120)[0]
                stale_attempt_result = db.record_scan_job_result(
                    "job_claimed_lifecycle",
                    attempt_id="wk_1-2",
                    status="done",
                    result_checksum="checksum-stale",
                    payload={"status": "done"},
                )
                wrong_worker_result = db.record_scan_job_result(
                    "job_claimed_lifecycle",
                    attempt_id="wk_2-1",
                    status="done",
                    result_checksum="checksum-wrong-worker",
                    payload={"status": "done"},
                )
                current_job = db.get_scan_job("job_claimed_lifecycle")
                with closing(sqlite3.connect(db_path)) as connection:
                    result_count = connection.execute("SELECT COUNT(*) FROM job_results").fetchone()[0]

        self.assertTrue(queued_result["conflict"])
        self.assertEqual(queued_job["status"], "queued")
        self.assertEqual(claimed["attempt"], 1)
        self.assertEqual(claimed["job_id"], "job_claimed_lifecycle")
        self.assertTrue(stale_attempt_result["conflict"])
        self.assertTrue(wrong_worker_result["conflict"])
        self.assertEqual(current_job["status"], "claimed")
        self.assertEqual(result_count, 0)

    def test_server_resource_cleanup_keeps_paid_scan_results(self) -> None:
        previous = {
            "USERS": app.USERS,
            "SESSIONS": app.SESSIONS,
            "GITHUB_STATES": app.GITHUB_STATES,
            "SCANS": app.SCANS,
            "ISSUES": app.ISSUES,
            "STATE_LOADED": app.STATE_LOADED,
            "STATE_DIRTY": app.STATE_DIRTY,
        }
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "pullwise.sqlite3")
                with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                    db.initialize()
                    app.USERS = {
                        "usr_1": {
                            "id": "usr_1",
                            "githubRepositoryAccessPending": {"state": "expired", "expiresAt": 900},
                        },
                        "usr_2": {
                            "id": "usr_2",
                            "githubRepositoryAccessPending": {"state": "active", "expiresAt": 1100},
                        },
                    }
                    app.SESSIONS = {
                        "expired": {"id": "expired", "userId": "usr_1", "expiresAt": 900},
                        "active": {"id": "active", "userId": "usr_2", "expiresAt": 1100},
                        "malformed": {"id": "malformed", "userId": "usr_2", "expiresAt": {"bad": 1}},
                    }
                    app.GITHUB_STATES = {
                        "expired_state": {"kind": "login", "expiresAt": 900},
                        "active_state": {"kind": "login", "expiresAt": 1100},
                        "malformed_state": {"kind": "login", "expiresAt": {"bad": 1}},
                    }
                    app.SCANS = [{"id": "sc_paid", "userId": "usr_1", "status": "done", "completedAt": 100}]
                    app.ISSUES = [{"id": "iss_paid", "scanId": "sc_paid", "title": "Paid result"}]
                    app.STATE_LOADED = True
                    app.STATE_DIRTY = False

                    removed = app.cleanup_server_resources(timestamp=1000)

            self.assertEqual(removed["sessions"], 1)
            self.assertEqual(removed["github_states"], 1)
            self.assertEqual(removed["pending_github_authorizations"], 1)
            self.assertEqual(list(app.SESSIONS), ["active", "malformed"])
            self.assertEqual(list(app.GITHUB_STATES), ["active_state", "malformed_state"])
            self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
            self.assertIn("githubRepositoryAccessPending", app.USERS["usr_2"])
            self.assertEqual(app.SCANS, [{"id": "sc_paid", "userId": "usr_1", "status": "done", "completedAt": 100}])
            self.assertEqual(app.ISSUES, [{"id": "iss_paid", "scanId": "sc_paid", "title": "Paid result"}])
            self.assertTrue(app.STATE_DIRTY)
        finally:
            app.USERS = previous["USERS"]
            app.SESSIONS = previous["SESSIONS"]
            app.GITHUB_STATES = previous["GITHUB_STATES"]
            app.SCANS = previous["SCANS"]
            app.ISSUES = previous["ISSUES"]
            app.STATE_LOADED = previous["STATE_LOADED"]
            app.STATE_DIRTY = previous["STATE_DIRTY"]


if __name__ == "__main__":
    unittest.main()
