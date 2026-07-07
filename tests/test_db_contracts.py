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

    def test_ensure_initialized_skips_schema_work_after_first_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                with patch(
                    "pullwise_server.db.reconcile_worker_uninstall_deletes",
                    wraps=db.reconcile_worker_uninstall_deletes,
                ) as reconcile:
                    db.ensure_initialized()
                    db.ensure_initialized()

        self.assertEqual(reconcile.call_count, 1)

    def test_initialize_migrates_legacy_worker_capacity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            CREATE TABLE workers (
                                worker_id TEXT PRIMARY KEY,
                                name TEXT,
                                token_hash TEXT UNIQUE,
                                version TEXT,
                                provider TEXT,
                                provider_chain TEXT,
                                enabled INTEGER NOT NULL DEFAULT 1,
                                max_concurrent_jobs INTEGER NOT NULL DEFAULT 4,
                                running_jobs INTEGER NOT NULL DEFAULT 2,
                                free_slots INTEGER NOT NULL DEFAULT 2,
                                hostname TEXT,
                                region TEXT,
                                last_error TEXT,
                                status TEXT NOT NULL DEFAULT 'online',
                                first_seen_at INTEGER NOT NULL DEFAULT 100,
                                last_heartbeat_at INTEGER,
                                token_last_used_at INTEGER,
                                disabled_at INTEGER,
                                deleted_at INTEGER
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO workers (
                                worker_id, name, token_hash, version, provider, provider_chain,
                                running_jobs, hostname, region, status, first_seen_at,
                                last_heartbeat_at
                            )
                            VALUES (
                                'wk_legacy', 'Legacy worker', 'hash_legacy', '0.1.0',
                                'codex', '["codex"]', 3, 'host-1', 'us-east',
                                'online', 100, 110
                            )
                            """
                        )

                db.initialize()
                with closing(sqlite3.connect(db_path)) as connection:
                    columns = [row[1] for row in connection.execute("PRAGMA table_info(workers)").fetchall()]
                    row = connection.execute("SELECT * FROM workers WHERE worker_id = 'wk_legacy'").fetchone()

        self.assertNotIn("max_concurrent_jobs", columns)
        self.assertNotIn("free_slots", columns)
        self.assertIn("running_jobs", columns)
        self.assertIn("created_at", columns)
        self.assertIn("updated_at", columns)
        self.assertIsNotNone(row)
        self.assertEqual(row[columns.index("running_jobs")], 1)
        self.assertIsNotNone(row[columns.index("created_at")])
        self.assertIsNotNone(row[columns.index("updated_at")])

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

    def test_save_state_item_encrypts_system_config_alert_smtp_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = self.write_state_key(temp_dir)
            config = {
                "alerts": {
                    "email": {
                        "smtpHost": "smtp.example.com",
                        "smtpPassword": "smtp-secret",
                    }
                }
            }
            with patch.dict(
                os.environ,
                {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path},
                clear=True,
            ):
                db.save_state_item("system_config", config)
                with closing(sqlite3.connect(db_path)) as connection:
                    payload = connection.execute("SELECT payload FROM app_state WHERE name = 'system_config'").fetchone()[0]
                loaded = db.load_state_item("system_config")

        self.assertNotIn("smtp-secret", payload)
        self.assertIn("pullwise-state-secret-v1", payload)
        self.assertEqual(loaded, config)

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

    def test_initialize_adds_last_attempt_id_to_existing_scan_jobs_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            CREATE TABLE scan_jobs (
                                job_id TEXT PRIMARY KEY,
                                scan_id TEXT NOT NULL UNIQUE,
                                repo TEXT NOT NULL,
                                branch TEXT NOT NULL,
                                "commit" TEXT NOT NULL,
                                status TEXT NOT NULL,
                                attempt INTEGER NOT NULL DEFAULT 0,
                                claimed_by_worker_id TEXT,
                                claimed_at INTEGER,
                                started_at INTEGER,
                                completed_at INTEGER,
                                timeout_at INTEGER,
                                error TEXT,
                                result_checksum TEXT,
                                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                                updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                                user_id TEXT,
                                repo_id TEXT,
                                github_repo_id TEXT,
                                installation_id TEXT,
                                clone_url TEXT,
                                progress_phase TEXT,
                                progress INTEGER NOT NULL DEFAULT 0,
                                progress_message TEXT,
                                logs_summary TEXT,
                                max_attempts INTEGER NOT NULL DEFAULT 3,
                                review_output_language TEXT
                            )
                            """
                        )

                db.initialize()

                with closing(sqlite3.connect(db_path)) as connection:
                    columns = [row[1] for row in connection.execute("PRAGMA table_info(scan_jobs)").fetchall()]
                results = db.list_completed_scan_job_results()

        self.assertIn("last_attempt_id", columns)
        self.assertEqual(results, [])

    def test_set_worker_enabled_records_disable_timestamp_and_clears_on_enable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                worker = db.create_worker({"worker_id": "wk_disable_timestamp", "name": "Timestamp worker"})

                with patch("pullwise_server.db.time.time", return_value=1234):
                    disabled = db.set_worker_enabled(worker["worker_id"], False)
                with patch("pullwise_server.db.time.time", return_value=1300):
                    enabled = db.set_worker_enabled(worker["worker_id"], True)

        self.assertEqual(disabled["disabled_at"], 1234)
        self.assertEqual(disabled["enabled"], 0)
        self.assertIsNone(enabled["disabled_at"])
        self.assertEqual(enabled["enabled"], 1)

    def test_initialize_does_not_restore_deleted_environment_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            env = {
                "PULLWISE_DB_PATH": db_path,
                "PULLWISE_WORKER_TOKEN": "env-token",
                "PULLWISE_WORKER_ID": "env_worker",
            }
            with patch.dict(os.environ, env, clear=True):
                db.initialize()
                self.assertEqual([worker["worker_id"] for worker in db.list_workers()], ["env_worker"])

                deleted = db.soft_delete_worker("env_worker")
                self.assertIsNotNone(deleted)
                self.assertEqual(db.list_workers(), [])

                db.initialize()

                self.assertEqual(db.list_workers(), [])
                self.assertIsNotNone(db.get_worker("env_worker", include_deleted=True)["deleted_at"])

    def test_initialize_keeps_legacy_pending_uninstall_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                db.create_worker({"worker_id": "wk_legacy_uninstall", "name": "Legacy uninstall worker"})
                db.upsert_worker_heartbeat(
                    {
                        "worker_id": "wk_legacy_uninstall",
                        "provider": "codex",
                        "version": "0.4.18",
                        "running_jobs": 0,
                        "doctor_status": "ok",
                        "codex_ready": 1,
                        "ready_providers": ["codex"],
                        "timestamp": 120,
                    }
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO worker_commands (
                                id, worker_id, command, status, created_at, updated_at
                            )
                            VALUES ('cmd_legacy_uninstall', 'wk_legacy_uninstall', 'uninstall', 'pending', 123, 123)
                            """
                        )

                db.initialize()

                worker = db.get_worker("wk_legacy_uninstall")

        self.assertIsNotNone(worker)
        self.assertEqual(worker["enabled"], 0)
        self.assertEqual(worker["disabled_at"], 123)
        self.assertIsNone(worker["deleted_at"])

    def test_initialize_keeps_uncontacted_pending_uninstall_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                db.create_worker({"worker_id": "wk_uncontacted_uninstall", "name": "Uncontacted uninstall worker"})
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO worker_commands (
                                id, worker_id, command, status, created_at, updated_at
                            )
                            VALUES ('cmd_uncontacted_uninstall', 'wk_uncontacted_uninstall', 'uninstall', 'pending', 123, 123)
                            """
                        )

                db.initialize()

                worker = db.get_worker("wk_uncontacted_uninstall")
                command = db.get_worker_command(
                    "cmd_uncontacted_uninstall", worker_id="wk_uncontacted_uninstall"
                )

        self.assertIsNotNone(worker)
        self.assertEqual(worker["enabled"], 0)
        self.assertEqual(worker["disabled_at"], 123)
        self.assertIsNone(worker["deleted_at"])
        self.assertEqual(command["status"], "pending")
        self.assertIsNone(command["completed_at"])

    def test_initialize_soft_deletes_legacy_succeeded_uninstall_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                db.initialize()
                db.create_worker({"worker_id": "wk_legacy_uninstall", "name": "Legacy uninstall worker"})
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO worker_commands (
                                id, worker_id, command, status, created_at, completed_at, updated_at
                            )
                            VALUES ('cmd_legacy_uninstall', 'wk_legacy_uninstall', 'uninstall', 'succeeded', 123, 456, 456)
                            """
                        )

                db.initialize()

                self.assertIsNone(db.get_worker("wk_legacy_uninstall"))
                deleted = db.get_worker("wk_legacy_uninstall", include_deleted=True)

        self.assertIsNotNone(deleted)
        self.assertEqual(deleted["enabled"], 0)
        self.assertEqual(deleted["deleted_at"], 456)
        self.assertEqual(deleted["disabled_at"], 123)

    def test_cleanup_stale_worker_uninstall_commands_keeps_timed_out_cleanup_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                for worker_id in ("wk_old_pending", "wk_recent_pending", "wk_old_running", "wk_stop_pending"):
                    db.create_worker({"worker_id": worker_id, "name": worker_id})
                    db.upsert_worker_heartbeat(
                        {
                            "worker_id": worker_id,
                            "provider": "codex",
                            "version": "0.4.18",
                            "running_jobs": 0,
                            "timestamp": 90,
                        }
                    )
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO worker_commands (
                                id, worker_id, command, status, created_at, started_at, updated_at
                            )
                            VALUES
                                ('cmd_old_pending', 'wk_old_pending', 'uninstall', 'pending', 100, NULL, 100),
                                ('cmd_recent_pending', 'wk_recent_pending', 'uninstall', 'pending', 950, NULL, 950),
                                ('cmd_old_running', 'wk_old_running', 'uninstall', 'running', 100, 110, 110),
                                ('cmd_stop_pending', 'wk_stop_pending', 'stop', 'pending', 100, NULL, 100)
                            """
                        )

                removed = db.cleanup_stale_worker_uninstall_commands(
                    timestamp=1000,
                    pending_timeout_seconds=864,
                )
                old_worker = db.get_worker("wk_old_pending", include_deleted=True)
                old_command = db.get_worker_command("cmd_old_pending", worker_id="wk_old_pending")
                old_visible = db.get_worker("wk_old_pending")
                recent_visible = db.get_worker("wk_recent_pending")
                running_visible = db.get_worker("wk_old_running")
                stop_visible = db.get_worker("wk_stop_pending")

        self.assertEqual(removed, 1)
        self.assertIsNotNone(old_worker)
        self.assertEqual(old_worker["enabled"], 0)
        self.assertIsNone(old_worker["deleted_at"])
        self.assertEqual(old_worker["disabled_at"], 1000)
        self.assertEqual(old_command["status"], "cancelled")
        self.assertIn("host cleanup was not confirmed", old_command["error"])
        self.assertEqual(old_command["completed_at"], 1000)
        self.assertIsNotNone(old_visible)
        self.assertIsNotNone(recent_visible)
        self.assertIsNotNone(running_visible)
        self.assertIsNotNone(stop_visible)

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

    def test_cleanup_user_scan_issue_records_prunes_only_old_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                scans = [
                    {"id": "sc_old_done", "userId": "usr_1", "repo": "acme/api", "status": "done", "createdAt": 100},
                    {
                        "id": "sc_old_queued",
                        "userId": "usr_1",
                        "repo": "acme/api",
                        "status": "queued",
                        "createdAt": 100,
                    },
                    {
                        "id": "sc_old_running",
                        "userId": "usr_1",
                        "repo": "acme/api",
                        "status": "running",
                        "createdAt": 100,
                    },
                    {
                        "id": "sc_recent_done",
                        "userId": "usr_1",
                        "repo": "acme/api",
                        "status": "done",
                        "createdAt": 950,
                    },
                ]
                issues = [
                    {
                        "id": "iss_old_done",
                        "userId": "usr_1",
                        "scanId": "sc_old_done",
                        "repo": "acme/api",
                        "status": "open",
                        "createdAt": 100,
                    },
                    {
                        "id": "iss_old_queued",
                        "userId": "usr_1",
                        "scanId": "sc_old_queued",
                        "repo": "acme/api",
                        "status": "open",
                        "createdAt": 100,
                    },
                    {
                        "id": "iss_old_running",
                        "userId": "usr_1",
                        "scanId": "sc_old_running",
                        "repo": "acme/api",
                        "status": "open",
                        "createdAt": 100,
                    },
                    {
                        "id": "iss_recent_done",
                        "userId": "usr_1",
                        "scanId": "sc_recent_done",
                        "repo": "acme/api",
                        "status": "open",
                        "createdAt": 950,
                    },
                    {
                        "id": "iss_old_orphan",
                        "userId": "usr_1",
                        "repo": "acme/api",
                        "status": "open",
                        "createdAt": 100,
                    },
                ]
                for scan in scans:
                    db.upsert_scan(scan)
                for issue in issues:
                    db.upsert_issue(issue)
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.executemany(
                            """
                            INSERT INTO scan_jobs (
                                job_id, scan_id, repo, branch, "commit", status,
                                created_at, updated_at, completed_at, user_id
                            )
                            VALUES (?, ?, 'acme/api', 'main', 'abc', ?, ?, ?, ?, 'usr_1')
                            """,
                            [
                                ("job_old_done", "sc_old_done", "done", 100, 100, 100),
                                ("job_old_queued", "sc_old_queued", "queued", 100, 100, None),
                                ("job_old_running", "sc_old_running", "running", 100, 100, None),
                                ("job_recent_done", "sc_recent_done", "done", 950, 950, 950),
                            ],
                        )

                removed = db.cleanup_user_scan_issue_records(timestamp=1000, retention_seconds=500)

                with closing(sqlite3.connect(db_path)) as connection:
                    remaining_scans = [
                        row[0] for row in connection.execute("SELECT scan_id FROM scans ORDER BY scan_id")
                    ]
                    remaining_issues = [
                        row[0] for row in connection.execute("SELECT issue_id FROM issues ORDER BY issue_id")
                    ]
                    remaining_jobs = [
                        row[0] for row in connection.execute("SELECT scan_id FROM scan_jobs ORDER BY scan_id")
                    ]

        self.assertEqual(removed, {"issues": 2, "scans": 1, "scan_jobs": 1})
        self.assertEqual(remaining_scans, ["sc_old_queued", "sc_old_running", "sc_recent_done"])
        self.assertEqual(remaining_issues, ["iss_old_queued", "iss_old_running", "iss_recent_done"])
        self.assertEqual(remaining_jobs, ["sc_old_queued", "sc_old_running", "sc_recent_done"])

    def test_scan_job_defaults_to_single_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                job = db.create_scan_job(
                    {
                        "job_id": "job_default_single_attempt",
                        "scan_id": "sc_default_single_attempt",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 100,
                        "user_id": "usr_1",
                    }
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    max_attempts_default = next(
                        row[4] for row in connection.execute("PRAGMA table_info(scan_jobs)") if row[1] == "max_attempts"
                    )

        self.assertEqual(job["max_attempts"], 1)
        self.assertEqual(max_attempts_default, "1")

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

                claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=120)
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

    def test_failed_scan_job_result_does_not_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'pullwise.sqlite3')
            with patch.dict(os.environ, {'PULLWISE_DB_PATH': db_path}, clear=False):
                db.initialize()
                db.create_scan_job(
                    {
                        'job_id': 'job_no_retry_worker_failure',
                        'scan_id': 'sc_no_retry_worker_failure',
                        'repo': 'acme/api',
                        'branch': 'main',
                        'commit': 'pending',
                        'status': 'queued',
                        'created_at': 100,
                        'user_id': 'usr_1',
                        'max_attempts': 3,
                    }
                )
                claimed = db.claim_next_scan_job('wk_single', lease_seconds=3600, timestamp=120)
                failed = db.record_scan_job_result(
                    'job_no_retry_worker_failure',
                    attempt_id='wk_single-1',
                    status='failed',
                    result_checksum='checksum-no-retry',
                    payload={'status': 'failed', 'error': 'worker_result_failed'},
                )
                second_claim = db.claim_next_scan_job('wk_single', lease_seconds=3600, timestamp=130)
                stored = db.get_scan_job('job_no_retry_worker_failure')

        self.assertEqual(claimed['attempt'], 1)
        self.assertTrue(failed['accepted'])
        self.assertIsNone(second_claim)
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['attempt'], 1)
    def test_exhausted_queued_scan_job_fails_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                db.create_scan_job(
                    {
                        "job_id": "job_exhausted",
                        "scan_id": "sc_exhausted",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 100,
                        "user_id": "usr_1",
                        "max_attempts": 2,
                    }
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute(
                            "UPDATE scan_jobs SET attempt = 2 WHERE job_id = ?",
                            ("job_exhausted",),
                        )

                recovered = db.recover_expired_scan_jobs(120)
                claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=121)
                stored = db.get_scan_job("job_exhausted")

        self.assertEqual(claimed, None)
        self.assertEqual(recovered[0]["status"], "failed")
        self.assertEqual(recovered[0]["reason"], "scan_attempts_exhausted")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "scan_attempts_exhausted")

    def test_record_scan_job_result_stores_large_payload_as_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                db.create_scan_job(
                    {
                        "job_id": "job_artifact",
                        "scan_id": "sc_artifact",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 100,
                        "user_id": "usr_1",
                    }
                )
                claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=120)
                payload = {
                    "status": "done",
                    "summary": {"high": 1},
                    "reviewWorkerProtocol": {
                        "version": "review-worker-protocol/v1",
                        "runId": "run_artifact",
                        "scanMode": "full-strict",
                        "confirmedCount": 1,
                        "finalJson": {"confirmed": [{"candidate": {"issue_id": "iss_1"}}]},
                        "debugText": "x" * 1000,
                    },
                }
                result = db.record_scan_job_result(
                    claimed["job_id"],
                    attempt_id="wk_1-1",
                    status="done",
                    result_checksum="checksum-artifact",
                    payload=payload,
                )
                restored = db.get_completed_scan_job_result(claimed["job_id"])
                with closing(sqlite3.connect(db_path)) as connection:
                    row = connection.execute(
                        "SELECT payload, payload_artifact_id FROM job_results WHERE job_id = ?",
                        (claimed["job_id"],),
                    ).fetchone()
                    artifact_count = connection.execute(
                        "SELECT COUNT(*) FROM job_result_artifacts WHERE job_id = ?",
                        (claimed["job_id"],),
                    ).fetchone()[0]

        self.assertTrue(result["accepted"])
        self.assertEqual(artifact_count, 1)
        self.assertIsNotNone(row[1])
        self.assertIn("artifactId", row[0])
        self.assertNotIn("debugText", row[0])
        self.assertEqual(restored["result_payload"]["reviewWorkerProtocol"]["debugText"], "x" * 1000)

    def test_record_scan_job_result_rolls_back_when_artifact_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=False):
                db.initialize()
                db.create_scan_job(
                    {
                        "job_id": "job_artifact_failure",
                        "scan_id": "sc_artifact_failure",
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "status": "queued",
                        "created_at": 100,
                        "user_id": "usr_1",
                    }
                )
                claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=120)
                payload = {
                    "status": "done",
                    "summary": {"high": 1},
                    "reviewWorkerProtocol": {
                        "version": "review-worker-protocol/v1",
                        "runId": "run_artifact_failure",
                        "scanMode": "full-strict",
                        "confirmedCount": 1,
                        "finalJson": {"confirmed": [{"candidate": {"issue_id": "iss_1"}}]},
                        "debugText": "x" * 1000,
                    },
                }

                with patch.object(
                    db,
                    "_store_scan_job_result_artifact_locked",
                    side_effect=RuntimeError("artifact write failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "artifact write failed"):
                        db.record_scan_job_result(
                            claimed["job_id"],
                            attempt_id="wk_1-1",
                            status="done",
                            result_checksum="checksum-artifact-failure",
                            payload=payload,
                        )

                with closing(sqlite3.connect(db_path)) as connection:
                    job_status = connection.execute(
                        "SELECT status FROM scan_jobs WHERE job_id = ?",
                        (claimed["job_id"],),
                    ).fetchone()[0]
                    result_count = connection.execute(
                        "SELECT COUNT(*) FROM job_results WHERE job_id = ?",
                        (claimed["job_id"],),
                    ).fetchone()[0]
                    artifact_count = connection.execute(
                        "SELECT COUNT(*) FROM job_result_artifacts WHERE job_id = ?",
                        (claimed["job_id"],),
                    ).fetchone()[0]

        self.assertEqual(job_status, "claimed")
        self.assertEqual(result_count, 0)
        self.assertEqual(artifact_count, 0)

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

    def test_server_resource_cleanup_prunes_expired_memory_scan_issue_records(self) -> None:
        previous = {
            "USERS": app.USERS,
            "SESSIONS": app.SESSIONS,
            "GITHUB_STATES": app.GITHUB_STATES,
            "SCANS": app.SCANS,
            "SCAN_BY_ID": app.SCAN_BY_ID,
            "ISSUES": app.ISSUES,
            "STATE_LOADED": app.STATE_LOADED,
            "STATE_DIRTY": app.STATE_DIRTY,
        }
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "pullwise.sqlite3")
                with patch.dict(
                    os.environ,
                    {
                        "PULLWISE_DB_PATH": db_path,
                        "PULLWISE_SCAN_ISSUE_RETENTION_SECONDS": "500",
                        "PULLWISE_SCAN_JOB_RETENTION_SECONDS": "1",
                    },
                    clear=False,
                ):
                    db.initialize()
                    app.USERS = {}
                    app.SESSIONS = {}
                    app.GITHUB_STATES = {}
                    app.SCANS = [
                        {"id": "sc_old_done", "userId": "usr_1", "status": "done", "createdAt": 100},
                        {"id": "sc_old_queued", "userId": "usr_1", "status": "queued", "createdAt": 100},
                        {"id": "sc_old_running", "userId": "usr_1", "status": "running", "createdAt": 100},
                        {"id": "sc_recent_done", "userId": "usr_1", "status": "done", "createdAt": 950},
                    ]
                    app.SCAN_BY_ID = {
                        str(scan["id"]): scan for scan in app.SCANS
                    }
                    app.ISSUES = [
                        {"id": "iss_old_done", "scanId": "sc_old_done", "createdAt": 100},
                        {"id": "iss_old_queued", "scanId": "sc_old_queued", "createdAt": 100},
                        {"id": "iss_old_running", "scanId": "sc_old_running", "createdAt": 100},
                        {"id": "iss_recent_done", "scanId": "sc_recent_done", "createdAt": 950},
                        {"id": "iss_old_orphan", "createdAt": 100},
                    ]
                    app.STATE_LOADED = True
                    app.STATE_DIRTY = False
                    with closing(sqlite3.connect(db_path)) as connection:
                        with connection:
                            connection.execute(
                                """
                                INSERT INTO scan_jobs (
                                    job_id, scan_id, repo, branch, "commit", status,
                                    created_at, updated_at, completed_at, user_id
                                )
                                VALUES (
                                    'job_recent_done', 'sc_recent_done', 'acme/api', 'main',
                                    'abc', 'done', 950, 950, 950, 'usr_1'
                                )
                                """
                            )

                    removed = app.cleanup_server_resources(timestamp=1000)
                    recent_job = db.get_scan_job_for_scan("sc_recent_done")

            self.assertEqual(removed["memory_scans"], 1)
            self.assertEqual(removed["memory_issues"], 2)
            self.assertEqual([scan["id"] for scan in app.SCANS], ["sc_old_queued", "sc_old_running", "sc_recent_done"])
            self.assertEqual(
                [issue["id"] for issue in app.ISSUES],
                ["iss_old_queued", "iss_old_running", "iss_recent_done"],
            )
            self.assertNotIn("sc_old_done", app.SCAN_BY_ID)
            self.assertIsNotNone(recent_job)
            self.assertTrue(app.STATE_DIRTY)
        finally:
            app.USERS = previous["USERS"]
            app.SESSIONS = previous["SESSIONS"]
            app.GITHUB_STATES = previous["GITHUB_STATES"]
            app.SCANS = previous["SCANS"]
            app.SCAN_BY_ID = previous["SCAN_BY_ID"]
            app.ISSUES = previous["ISSUES"]
            app.STATE_LOADED = previous["STATE_LOADED"]
            app.STATE_DIRTY = previous["STATE_DIRTY"]


if __name__ == "__main__":
    unittest.main()
