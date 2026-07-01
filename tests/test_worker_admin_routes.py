from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, *, cookie: str = "", headers: dict | None = None) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.text_payload = None
        self.status = None
        self.headers_out = {}
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def text(self, payload: str, status: int = HTTPStatus.OK, *, content_type: str = "text/plain; charset=utf-8") -> None:
        self.text_payload = payload
        self.status = status
        self.headers_out = {"Content-Type": content_type}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def empty_audit_result_fields() -> dict:
    return {
        "audit_protocol": "audit-swarm/0.1",
        "issue_cards": [],
        "verification_results": [],
    }


def reset_state() -> None:
    app.USERS = {
        "usr_admin": {"id": "usr_admin", "email": "admin@example.com", "name": "Admin"},
        "usr_user": {"id": "usr_user", "email": "user@example.com", "name": "User"},
    }
    app.SESSIONS = {
        "ses_admin": {"id": "ses_admin", "userId": "usr_admin", "createdAt": app.now(), "expiresAt": app.now() + 3600},
        "ses_user": {"id": "ses_user", "userId": "usr_user", "createdAt": app.now(), "expiresAt": app.now() + 3600},
    }
    app.SCANS = []
    app.ISSUES = []
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.LATEST_WORKER_RELEASE_CACHE.update({"version": "", "checked_at": 0.0})
    app.LOG_STREAM_SESSIONS.clear()
    app.SCAN_SYSTEM_STATUS_CACHE.clear()
    app.STATE_LOADED = True
    app.STATE_DIRTY = False


class WorkerAdminRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_ADMIN_USER_IDS": "usr_admin",
                "PULLWISE_ADMIN_EMAILS": "admin@example.com",
                "PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "120",
                "PULLWISE_SERVER_URL": "http://localhost:8080",
                "PULLWISE_API_BASE_URL": "",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
        self.admin_cookie = "pw_session=ses_admin"
        self.user_cookie = "pw_session=ses_user"

    def create_worker(self) -> tuple[dict, str]:
        handler = RouteHarness(
            "/admin/workers",
            {"name": "US worker", "provider": "codex", "region": "us-east", "max_concurrent_jobs": 4},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_create"},
        )
        app.PullwiseHandler.route(handler, "POST")
        self.assertEqual(handler.status, HTTPStatus.CREATED)
        return handler.payload, handler.payload["worker_token"]

    def test_admin_workers_list_is_paginated_and_uses_aggregate_counts(self) -> None:
        first, _first_token = self.create_worker()
        second, _second_token = self.create_worker()
        for worker in (first, second):
            db.upsert_worker_heartbeat(
                {
                    "worker_id": worker["worker_id"],
                    "provider": "codex",
                    "version": "0.4.18",
                    "running_jobs": 0,
                    "doctor_status": "ok",
                    "codex_ready": 1,
                    "ready_providers": ["codex"],
                    "timestamp": app.now(),
                }
            )

        with (
            patch.object(db, "count_worker_running_scan_jobs", side_effect=AssertionError("worker list should batch counts")),
            patch.object(db, "get_latest_worker_command", side_effect=AssertionError("worker list should batch latest commands")),
        ):
            handler = RouteHarness("/admin/workers?limit=1&offset=0", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["total"], 2)
        self.assertEqual(handler.payload["limit"], 1)
        self.assertTrue(handler.payload["hasMore"])
        self.assertEqual(len(handler.payload["workers"]), 1)

    def test_admin_workers_hide_created_worker_until_first_heartbeat(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        self.assertIsNone(db.get_worker(worker_id)["last_heartbeat_at"])

        hidden = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(hidden, "GET")
        self.assertEqual(hidden.status, HTTPStatus.OK)
        self.assertEqual(hidden.payload["workers"], [])
        self.assertEqual(hidden.payload["total"], 0)

        status = RouteHarness("/admin/status", cookie=self.admin_cookie)
        app.PullwiseHandler.route(status, "GET")
        self.assertEqual(status.status, HTTPStatus.OK)
        self.assertEqual(status.payload["workers"], [])
        self.assertEqual(status.payload["totalWorkerCount"], 0)
        self.assertEqual(status.payload["offlineWorkerCount"], 0)

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "running_jobs": 0,
                "doctor_status": "ok",
                "codex_ready": True,
                "readyProviders": ["codex"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        visible = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(visible, "GET")
        self.assertEqual(visible.status, HTTPStatus.OK)
        self.assertEqual(visible.payload["total"], 1)
        self.assertEqual(visible.payload["workers"][0]["worker_id"], worker_id)
        self.assertEqual(visible.payload["workers"][0]["status"], "idle")

    def test_admin_access_can_match_verified_github_email_alias(self) -> None:
        app.USERS["usr_admin"].update(
            {
                "email": "primary@example.com",
                "githubId": "123456",
                "githubVerifiedEmails": ["primary@example.com", "Admin@Example.com"],
            }
        )

        with patch.dict(
            os.environ,
            {"PULLWISE_ADMIN_USER_IDS": "", "PULLWISE_ADMIN_EMAILS": "admin@example.com"},
            clear=False,
        ):
            handler = RouteHarness("/admin/status", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)

    def test_admin_access_can_match_github_user_id(self) -> None:
        app.USERS["usr_admin"].update({"email": "user@example.com", "githubId": "123456"})

        with patch.dict(
            os.environ,
            {"PULLWISE_ADMIN_USER_IDS": "123456", "PULLWISE_ADMIN_EMAILS": ""},
            clear=False,
        ):
            handler = RouteHarness("/admin/status", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)

    def test_admin_can_read_server_machine_metrics(self) -> None:
        expected = {
            "ok": True,
            "collectedAt": 1781200000,
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"totalBytes": 8589934592, "availableBytes": 4294967296, "usedBytes": 4294967296, "usedPercent": 50.0},
            "storage": {"totalBytes": 107374182400, "freeBytes": 64424509440, "usedBytes": 42949672960, "usedPercent": 40.0},
            "server": {"hostname": "api-1"},
        }
        with (
            patch.object(app, "now", return_value=1781200000),
            patch.object(app.system_metrics, "server_metrics_payload", return_value=expected) as collect,
        ):
            handler = RouteHarness("/admin/server-metrics", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["collectedAt"], expected["collectedAt"])
        self.assertEqual(handler.payload["cpu"], expected["cpu"])
        self.assertEqual(handler.payload["memory"], expected["memory"])
        self.assertEqual(handler.payload["storage"], expected["storage"])
        self.assertEqual(handler.payload["server"], expected["server"])
        self.assertEqual(handler.payload["history"][0]["collectedAt"], expected["collectedAt"])
        self.assertEqual(handler.payload["history"][0]["memory"]["usedPercent"], 50.0)
        self.assertEqual(handler.payload["history"][0]["storage"]["usedPercent"], 40.0)
        self.assertNotIn("usagePercent", json.dumps(handler.payload))
        self.assertEqual(collect.call_args.kwargs["timestamp"], 1781200000)
        self.assertEqual(collect.call_args.kwargs["storage_path"], os.path.dirname(db.database_path()))

    def test_admin_server_metrics_bypasses_user_rate_limit(self) -> None:
        expected = {
            "ok": True,
            "collectedAt": 1781200000,
            "cpu": {"logicalCount": 8, "loadAverage": None},
            "memory": {"totalBytes": 8589934592, "availableBytes": 4294967296, "usedBytes": 4294967296, "usedPercent": 50.0},
            "storage": {"totalBytes": 107374182400, "freeBytes": 64424509440, "usedBytes": 42949672960, "usedPercent": 40.0},
            "server": {"hostname": "api-1"},
        }
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_RATE_LIMIT_ENABLED": "true",
                    "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                    "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
                },
                clear=False,
            ),
            patch.object(app, "now", return_value=1781200000),
            patch.object(app.system_metrics, "server_metrics_payload", return_value=expected),
        ):
            first = RouteHarness("/admin/server-metrics", cookie=self.admin_cookie)
            second = RouteHarness("/admin/server-metrics", cookie=self.admin_cookie)

            app.PullwiseHandler.route(first, "GET")
            app.PullwiseHandler.route(second, "GET")

        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual(second.status, HTTPStatus.OK)

    def test_non_admin_server_metrics_bypasses_user_rate_limit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_RATE_LIMIT_ENABLED": "true",
                "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
            },
            clear=False,
        ):
            first = RouteHarness("/admin/server-metrics", cookie=self.user_cookie)
            second = RouteHarness("/admin/server-metrics", cookie=self.user_cookie)

            app.PullwiseHandler.route(first, "GET")
            app.PullwiseHandler.route(second, "GET")

        self.assertEqual(first.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(second.status, HTTPStatus.FORBIDDEN)

    def test_admin_can_start_pullwise_server_restart(self) -> None:
        fake_process = type("FakeProcess", (), {"pid": 4321})()
        with (
            patch.dict(os.environ, {"PULLWISE_ADMIN_RESTART_MODE": "launcher"}, clear=False),
            patch.object(app, "now", return_value=1781200000),
            patch.object(app.subprocess, "Popen", return_value=fake_process) as popen,
        ):
            handler = RouteHarness("/admin/server/restart", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.ACCEPTED)
        self.assertEqual(handler.payload["command"], "bash launcher.sh restart")
        self.assertEqual(handler.payload["pid"], 4321)
        self.assertEqual(handler.payload["startedAt"], 1781200000)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["bash", "launcher.sh", "restart"])
        self.assertEqual(kwargs["cwd"], app.project_root())
        self.assertIs(kwargs["stdin"], app.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], app.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], app.subprocess.DEVNULL)

    def test_admin_server_restart_under_systemd_schedules_self_restart(self) -> None:
        timers = []

        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False
                self.started = False

            def start(self):
                self.started = True

        def fake_timer(delay, callback):
            timer = FakeTimer(delay, callback)
            timers.append(timer)
            return timer

        with (
            patch.dict(os.environ, {"INVOCATION_ID": "systemd-run"}, clear=False),
            patch.object(app, "now", return_value=1781200001),
            patch.object(app.threading, "Timer", side_effect=fake_timer),
            patch.object(app.subprocess, "Popen") as popen,
        ):
            handler = RouteHarness("/admin/server/restart", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.ACCEPTED)
        self.assertEqual(handler.payload["command"], "self SIGTERM for systemd restart")
        self.assertEqual(handler.payload["startedAt"], 1781200001)
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].daemon)
        self.assertTrue(timers[0].started)
        popen.assert_not_called()

    def test_non_admin_cannot_start_pullwise_server_restart(self) -> None:
        with patch.object(app.subprocess, "Popen") as popen:
            handler = RouteHarness("/admin/server/restart", cookie=self.user_cookie)
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        popen.assert_not_called()

    def test_admin_server_log_stream_reads_only_lines_after_enable(self) -> None:
        log_path = os.path.join(self.temp_dir.name, "pullwise.log")
        with open(log_path, "w", encoding="utf-8") as stream:
            stream.write("old line\n")
        with patch.object(app, "server_log_path_for_timestamp", return_value=log_path):
            start = RouteHarness("/admin/log-streams", {"source": "server"}, cookie=self.admin_cookie)
            app.PullwiseHandler.route(start, "POST")
            self.assertEqual(start.status, HTTPStatus.CREATED)
            session_id = start.payload["session"]["id"]
            with open(log_path, "a", encoding="utf-8") as stream:
                stream.write("new line\n")

            lines = RouteHarness(f"/admin/log-streams/{session_id}/lines?after=0&limit=10", cookie=self.admin_cookie)
            app.PullwiseHandler.route(lines, "GET")

        self.assertEqual(lines.status, HTTPStatus.OK)
        self.assertEqual([line["line"] for line in lines.payload["lines"]], ["new line"])
        self.assertEqual(lines.payload["lines"][0]["sequence"], 1)

    def test_log_stream_cleanup_bounds_session_count(self) -> None:
        app.LOG_STREAM_SESSIONS.update(
            {
                "old_paused": {"id": "old_paused", "status": "paused", "updated_at": 980, "created_at": 900},
                "active": {"id": "active", "status": "active", "updated_at": 970, "created_at": 910},
                "new_paused": {"id": "new_paused", "status": "paused", "updated_at": 990, "created_at": 920},
            }
        )

        with patch.dict(os.environ, {"PULLWISE_LOG_STREAM_MAX_SESSIONS": "2"}, clear=False):
            removed = app.log_stream_cleanup_expired(timestamp=1000)

        self.assertEqual(removed, 1)
        self.assertEqual(set(app.LOG_STREAM_SESSIONS), {"active", "new_paused"})

    def test_server_resource_cleanup_removes_expired_log_stream_sessions(self) -> None:
        app.LOG_STREAM_SESSIONS.update(
            {
                "expired": {"id": "expired", "status": "paused", "updated_at": 900, "created_at": 890},
                "active": {"id": "active", "status": "active", "updated_at": 990, "created_at": 980, "expires_at": 1100},
            }
        )

        removed = app.cleanup_server_resources(timestamp=1000)

        self.assertEqual(removed["log_stream_sessions"], 1)
        self.assertEqual(set(app.LOG_STREAM_SESSIONS), {"active"})

    def test_admin_log_stream_creation_does_not_consume_rate_limit(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]

        with patch.dict(
            os.environ,
            {
                "PULLWISE_RATE_LIMIT_ENABLED": "true",
                "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
            },
            clear=False,
        ):
            server_stream = RouteHarness("/admin/log-streams", {"source": "server"}, cookie=self.admin_cookie)
            worker_stream = RouteHarness(
                "/admin/log-streams",
                {"source": "worker", "worker_id": worker_id},
                cookie=self.admin_cookie,
            )

            app.PullwiseHandler.route(server_stream, "POST")
            app.PullwiseHandler.route(worker_stream, "POST")

        self.assertEqual(server_stream.status, HTTPStatus.CREATED)
        self.assertEqual(worker_stream.status, HTTPStatus.CREATED)

    def test_admin_log_stream_pause_does_not_consume_rate_limit(self) -> None:
        first_start = RouteHarness("/admin/log-streams", {"source": "server"}, cookie=self.admin_cookie)
        second_start = RouteHarness("/admin/log-streams", {"source": "server"}, cookie=self.admin_cookie)
        app.PullwiseHandler.route(first_start, "POST")
        app.PullwiseHandler.route(second_start, "POST")
        self.assertEqual(first_start.status, HTTPStatus.CREATED)
        self.assertEqual(second_start.status, HTTPStatus.CREATED)

        first_session_id = first_start.payload["session"]["id"]
        second_session_id = second_start.payload["session"]["id"]
        with patch.dict(
            os.environ,
            {
                "PULLWISE_RATE_LIMIT_ENABLED": "true",
                "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
            },
            clear=False,
        ):
            first_pause = RouteHarness(f"/admin/log-streams/{first_session_id}/pause", cookie=self.admin_cookie)
            second_pause = RouteHarness(f"/admin/log-streams/{second_session_id}/pause", cookie=self.admin_cookie)

            app.PullwiseHandler.route(first_pause, "POST")
            app.PullwiseHandler.route(second_pause, "POST")

        self.assertEqual(first_pause.status, HTTPStatus.OK)
        self.assertEqual(second_pause.status, HTTPStatus.OK)

    def test_admin_missing_log_stream_lines_returns_paused_payload(self) -> None:
        lines = RouteHarness("/admin/log-streams/log_missing/lines?after=0&limit=10", cookie=self.admin_cookie)

        app.PullwiseHandler.route(lines, "GET")

        self.assertEqual(lines.status, HTTPStatus.OK)
        self.assertEqual(lines.payload["lines"], [])
        self.assertEqual(lines.payload["session"]["id"], "log_missing")
        self.assertEqual(lines.payload["session"]["status"], "paused")
        self.assertNotIn("message", lines.payload)

    def test_admin_missing_log_stream_pause_returns_paused_payload(self) -> None:
        pause = RouteHarness("/admin/log-streams/log_missing/pause", cookie=self.admin_cookie)

        app.PullwiseHandler.route(pause, "POST")

        self.assertEqual(pause.status, HTTPStatus.OK)
        self.assertEqual(pause.payload["session"]["id"], "log_missing")
        self.assertEqual(pause.payload["session"]["status"], "paused")
        self.assertNotIn("message", pause.payload)

    def test_worker_log_stream_is_polled_uploaded_and_paused(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        worker_auth = {"Authorization": f"Bearer {token}"}
        start = RouteHarness(
            "/admin/log-streams",
            {"source": "worker", "worker_id": worker_id},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(start, "POST")
        self.assertEqual(start.status, HTTPStatus.CREATED)
        session_id = start.payload["session"]["id"]

        poll = RouteHarness("/worker/commands/poll", {"worker_id": worker_id}, headers=worker_auth)
        app.PullwiseHandler.route(poll, "POST")
        self.assertEqual(poll.status, HTTPStatus.OK)
        self.assertEqual(poll.payload["logSession"]["id"], session_id)

        upload = RouteHarness(
            f"/worker/log-streams/{session_id}/lines",
            {
                "worker_id": worker_id,
                "lines": [
                    {
                        "source": "worker",
                        "stream": "journal",
                        "timestamp": 1781200000,
                        "line": "claimed job with Bearer secret_token",
                    }
                ],
            },
            headers=worker_auth,
        )
        app.PullwiseHandler.route(upload, "POST")
        self.assertEqual(upload.status, HTTPStatus.OK)
        self.assertTrue(upload.payload["accepted"])
        self.assertEqual(upload.payload["appended"], 1)

        lines = RouteHarness(f"/admin/log-streams/{session_id}/lines?after=0", cookie=self.admin_cookie)
        app.PullwiseHandler.route(lines, "GET")
        self.assertEqual(lines.status, HTTPStatus.OK)
        self.assertEqual(lines.payload["lines"][0]["sequence"], 1)
        self.assertIn("Bearer [redacted]", lines.payload["lines"][0]["line"])
        self.assertNotIn("secret_token", lines.payload["lines"][0]["line"])

        pause = RouteHarness(f"/admin/log-streams/{session_id}/pause", cookie=self.admin_cookie)
        app.PullwiseHandler.route(pause, "POST")
        self.assertEqual(pause.status, HTTPStatus.OK)
        self.assertEqual(pause.payload["session"]["status"], "paused")

        poll_after_pause = RouteHarness("/worker/commands/poll", {"worker_id": worker_id}, headers=worker_auth)
        app.PullwiseHandler.route(poll_after_pause, "POST")
        self.assertEqual(poll_after_pause.status, HTTPStatus.OK)
        self.assertIsNone(poll_after_pause.payload["logSession"])

    def test_worker_heartbeat_includes_active_log_session(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        worker_auth = {"Authorization": f"Bearer {token}"}
        start = RouteHarness(
            "/admin/log-streams",
            {"source": "worker", "worker_id": worker_id},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(start, "POST")
        self.assertEqual(start.status, HTTPStatus.CREATED)
        session_id = start.payload["session"]["id"]

        heartbeat = RouteHarness("/worker/heartbeat", {"worker_id": worker_id}, headers=worker_auth)
        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(heartbeat.payload["logSession"]["id"], session_id)

    def test_worker_alert_email_is_sent_once_per_active_problem(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        headers = {"Authorization": "Bearer " + token}
        degraded = {
            "worker_id": worker_id,
            "doctor_status": "degraded",
            "codex_ready": False,
            "readyProviders": [],
            "last_error": "codex_quota_exhausted: credits exhausted",
        }
        ready = {
            "worker_id": worker_id,
            "doctor_status": "ok",
            "codex_ready": True,
            "readyProviders": ["codex"],
        }

        with patch.object(app.alerts, "send_alert_email", return_value=True) as send_alert:
            first = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(first, "POST")
            second = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(second, "POST")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(second.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 1)
            state = db.load_state_item("alert_notifications")
            self.assertIn(f"worker:{worker_id}:degraded", state["active"])

            recovered = RouteHarness("/worker/heartbeat", ready, headers=headers)
            app.PullwiseHandler.route(recovered, "POST")
            self.assertEqual(recovered.status, HTTPStatus.OK)
            state = db.load_state_item("alert_notifications")
            self.assertNotIn(f"worker:{worker_id}:degraded", state.get("active", {}))

            repeated = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(repeated, "POST")
            self.assertEqual(repeated.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 2)


    def test_worker_codex_quota_alert_email_is_sent_once_and_cleared(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        headers = {"Authorization": "Bearer " + token}
        low_quota = {
            "provider": "codex",
            "limitId": "codex",
            "planType": "pro",
            "status": "low",
            "ready": False,
            "thresholdPercent": 5,
            "remainingPercent": 4,
            "checkedAt": 1782900000,
            "rateLimitResetCredits": {"availableCount": 1},
            "windows": [
                {"windowKind": "five_hour", "label": "5 hour", "usedPercent": 96, "remainingPercent": 4, "windowDurationMins": 300, "resetsAt": 1782918371},
                {"windowKind": "weekly", "label": "weekly", "usedPercent": 22, "remainingPercent": 78, "windowDurationMins": 10080, "resetsAt": 1783419385},
            ],
            "blockedWindows": [
                {"windowKind": "five_hour", "label": "5 hour", "usedPercent": 96, "remainingPercent": 4, "windowDurationMins": 300, "resetsAt": 1782918371}
            ],
        }
        degraded = {
            "worker_id": worker_id,
            "doctor_status": "degraded",
            "codex_ready": False,
            "readyProviders": [],
            "last_error": "codex quota low",
            "codexQuota": low_quota,
        }
        ready = {
            "worker_id": worker_id,
            "doctor_status": "ok",
            "codex_ready": True,
            "readyProviders": ["codex"],
            "codexQuota": {
                **low_quota,
                "status": "ok",
                "ready": True,
                "remainingPercent": 78,
                "windows": [
                    {"windowKind": "five_hour", "label": "5 hour", "usedPercent": 8, "remainingPercent": 92, "windowDurationMins": 300, "resetsAt": 1782918371},
                    {"windowKind": "weekly", "label": "weekly", "usedPercent": 22, "remainingPercent": 78, "windowDurationMins": 10080, "resetsAt": 1783419385},
                ],
                "blockedWindows": [],
            },
        }

        with patch.object(app.alerts, "send_alert_email", return_value=True) as send_alert:
            first = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(first, "POST")
            second = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(second, "POST")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(second.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 1)
            subject, body = send_alert.call_args.args
            self.assertIn("Codex quota low", subject)
            self.assertIn("5 hour", body)
            self.assertIn("Reset credits: 1", body)
            state = db.load_state_item("alert_notifications")
            self.assertIn(f"worker:{worker_id}:codex-quota:low", state["active"])
            self.assertNotIn(f"worker:{worker_id}:degraded", state["active"])

            recovered = RouteHarness("/worker/heartbeat", ready, headers=headers)
            app.PullwiseHandler.route(recovered, "POST")
            self.assertEqual(recovered.status, HTTPStatus.OK)
            state = db.load_state_item("alert_notifications")
            self.assertNotIn(f"worker:{worker_id}:codex-quota:low", state.get("active", {}))

            repeated = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(repeated, "POST")
            self.assertEqual(repeated.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 2)
    def test_worker_alert_email_failure_is_retried_instead_of_deduped(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        headers = {"Authorization": "Bearer " + token}
        degraded = {
            "worker_id": worker_id,
            "doctor_status": "degraded",
            "codex_ready": False,
            "readyProviders": [],
            "last_error": "codex_quota_exhausted: credits exhausted",
        }

        with patch.object(app.alerts, "send_alert_email", side_effect=[False, True]) as send_alert:
            first = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(first, "POST")
            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 1)
            state = db.load_state_item("alert_notifications") or {}
            self.assertNotIn(f"worker:{worker_id}:degraded", state.get("active", {}))

            second = RouteHarness("/worker/heartbeat", degraded, headers=headers)
            app.PullwiseHandler.route(second, "POST")
            self.assertEqual(second.status, HTTPStatus.OK)
            self.assertEqual(send_alert.call_count, 2)
            state = db.load_state_item("alert_notifications")
            self.assertIn(f"worker:{worker_id}:degraded", state["active"])

    def test_scan_system_alert_email_is_sent_once_per_active_problem(self) -> None:
        with patch.object(app.alerts, "send_alert_email", return_value=True) as send_alert:
            first = RouteHarness("/status/system")
            app.PullwiseHandler.route(first, "GET")
            second = RouteHarness("/status/system")
            app.PullwiseHandler.route(second, "GET")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(second.status, HTTPStatus.OK)
            self.assertEqual(first.payload["scanSystemStatus"], "down")
            self.assertEqual(send_alert.call_count, 1)
            state = db.load_state_item("alert_notifications")
            self.assertIn("server:scan-system:down", state["active"])

            payload, token = self.create_worker()
            worker_id = payload["worker_id"]
            ready = {
                "worker_id": worker_id,
                "doctor_status": "ok",
                "codex_ready": True,
                "readyProviders": ["codex"],
            }
            heartbeat = RouteHarness(
                "/worker/heartbeat",
                ready,
                headers={"Authorization": "Bearer " + token},
            )
            app.PullwiseHandler.route(heartbeat, "POST")
            app.SCAN_SYSTEM_STATUS_CACHE.clear()
            recovered = RouteHarness("/status/system")
            app.PullwiseHandler.route(recovered, "GET")

            self.assertEqual(heartbeat.status, HTTPStatus.OK)
            self.assertEqual(recovered.status, HTTPStatus.OK)
            self.assertEqual(recovered.payload["scanSystemStatus"], "ok")
            state = db.load_state_item("alert_notifications")
            self.assertNotIn("server:scan-system:down", state.get("active", {}))
            self.assertEqual(send_alert.call_count, 1)


    def test_alert_email_uses_admin_system_config_smtp_settings(self) -> None:
        config = app.system_config.default_config()
        config["alerts"]["email"].update(
            {
                "enabled": True,
                "to": ["ops@example.com", "admin@example.com"],
                "from": "pullwise@example.com",
                "smtpHost": "smtp.admin.example.com",
                "smtpPort": 465,
                "smtpUsername": "mailer",
                "smtpPassword": "smtp-secret",
                "smtpSsl": True,
                "smtpStarttls": False,
            }
        )
        with (
            patch.object(app.system_config, "config", return_value=config),
            patch.object(app.alerts.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            smtp = smtp_ssl.return_value.__enter__.return_value
            attempted = app.alerts.send_alert_email("Pullwise problem", "Worker degraded")

        self.assertTrue(attempted)
        smtp_ssl.assert_called_once_with("smtp.admin.example.com", 465, timeout=15)
        smtp.login.assert_called_once_with("mailer", "smtp-secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["From"], "pullwise@example.com")
        self.assertEqual(message["To"], "ops@example.com, admin@example.com")
        self.assertEqual(message["Subject"], "Pullwise problem")


    def test_alert_email_returns_false_when_smtp_delivery_fails(self) -> None:
        config = app.system_config.default_config()
        config["alerts"]["email"].update(
            {
                "enabled": True,
                "to": ["ops@example.com"],
                "from": "pullwise@example.com",
                "smtpHost": "smtp.admin.example.com",
                "smtpPort": 465,
                "smtpUsername": "mailer",
                "smtpPassword": "smtp-secret",
                "smtpSsl": True,
                "smtpStarttls": False,
            }
        )
        with (
            patch.object(app.system_config, "config", return_value=config),
            patch.object(app.alerts.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.side_effect = OSError("smtp down")
            attempted = app.alerts.send_alert_email("Pullwise problem", "Worker degraded")

        self.assertFalse(attempted)

    def test_admin_worker_create_rejects_empty_provider_chain(self) -> None:
        handler = RouteHarness(
            "/admin/workers",
            {"name": "No provider", "providerChain": ["bad"]},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("providerChain", handler.payload["message"])

    def test_worker_heartbeat_persists_machine_metrics_for_admin_detail(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        worker_auth = {"Authorization": f"Bearer {token}"}
        machine_metrics = {
            "ok": True,
            "collectedAt": 1781200000,
            "worker": {
                "hostname": "worker-host",
                "platform": "Linux-6.8",
                "system": "Linux",
                "release": "6.8",
                "machine": "x86_64",
                "pythonVersion": "3.10.12",
                "processId": 321,
            },
            "cpu": {
                "logicalCount": 8,
                "loadAverage": {"oneMinute": 1.25, "fiveMinute": 0.75, "fifteenMinute": 0.5},
            },
            "memory": {
                "totalBytes": 8589934592,
                "availableBytes": 3221225472,
                "usedBytes": 5368709120,
                "usedPercent": 62.5,
            },
            "storage": {
                "path": "/var/lib/pullwise-worker/checkouts",
                "measuredPath": "/var/lib/pullwise-worker",
                "totalBytes": 107374182400,
                "freeBytes": 64424509440,
                "usedBytes": 42949672960,
                "usedPercent": 40.0,
            },
        }

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "max_concurrent_jobs": 4,
                "running_jobs": 1,
                "free_slots": 3,
                "doctor_status": "ok",
                "codex_ready": True,
                "machine_metrics": machine_metrics,
            },
            headers=worker_auth,
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(detail.status, HTTPStatus.OK)
        metrics = detail.payload["worker"]["machineMetrics"]
        self.assertEqual(metrics["worker"]["hostname"], "worker-host")
        self.assertEqual(metrics["memory"]["usedPercent"], 62.5)
        self.assertEqual(metrics["storage"]["usedPercent"], 40.0)
        self.assertEqual(metrics["history"][0]["collectedAt"], 1781200000)
        self.assertEqual(metrics["history"][0]["memory"]["usedPercent"], 62.5)
        self.assertEqual(metrics["history"][0]["storage"]["usedPercent"], 40.0)
        self.assertNotIn("usagePercent", json.dumps(metrics))

    def test_worker_heartbeat_persists_codex_quota_for_admin_detail(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        worker_auth = {"Authorization": f"Bearer {token}"}
        codex_quota = {
            "provider": "codex",
            "limitId": "codex",
            "limitName": None,
            "planType": "pro",
            "status": "low",
            "ready": False,
            "reason": "codex_quota_low",
            "thresholdPercent": 5,
            "usedPercent": 96,
            "remainingPercent": 4,
            "checkedAt": 1782900000,
            "nextCheckAt": 1782900300,
            "rateLimitReachedType": None,
            "rateLimitResetCredits": {"availableCount": 1},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "windows": [
                {"name": "primary", "windowKind": "five_hour", "label": "5 hour", "usedPercent": 96, "remainingPercent": 4, "windowDurationMins": 300, "resetsAt": 1782918371},
                {"name": "secondary", "windowKind": "weekly", "label": "weekly", "usedPercent": 22, "remainingPercent": 78, "windowDurationMins": 10080, "resetsAt": 1783419385},
            ],
            "blockedWindows": [
                {"name": "primary", "windowKind": "five_hour", "label": "5 hour", "usedPercent": 96, "remainingPercent": 4, "windowDurationMins": 300, "resetsAt": 1782918371}
            ],
            "secret": "do-not-store",
        }

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "running_jobs": 0,
                "doctor_status": "degraded",
                "codex_ready": False,
                "readyProviders": [],
                "codexQuota": codex_quota,
            },
            headers=worker_auth,
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(app.computed_worker_status(worker), "degraded")
        stored_quota = json.loads(worker["codex_quota"])
        self.assertEqual(stored_quota["planType"], "pro")
        self.assertEqual(stored_quota["windows"][0]["windowKind"], "five_hour")
        self.assertNotIn("secret", stored_quota)

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(detail.status, HTTPStatus.OK)
        quota = detail.payload["worker"]["codexQuota"]
        self.assertEqual(quota["status"], "low")
        self.assertFalse(quota["ready"])
        self.assertEqual(quota["planType"], "pro")
        self.assertEqual(quota["rateLimitResetCredits"]["availableCount"], 1)
        self.assertEqual([window["windowKind"] for window in quota["windows"]], ["five_hour", "weekly"])
        self.assertEqual(detail.payload["worker"]["readyProviders"], [])
    def test_admin_worker_activity_uses_latest_activity_timestamp(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]
        timestamp = app.now()
        scan = {
            "id": "sc_worker_active_today",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        claimed = db.claim_next_scan_job(worker_id, lease_seconds=3600, timestamp=timestamp)
        db.update_scan_job_progress(
            claimed["job_id"],
            {"phase": "ai", "progress": 50, "message": "reviewing", "started_at": timestamp + 10},
        )
        db.renew_worker_scan_job_leases(worker_id, [job["job_id"]], timestamp=timestamp + 3700)

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(detail.status, HTTPStatus.OK)
        activity = detail.payload["taskActivity"][0]
        self.assertEqual(activity["status"], "running")
        self.assertEqual(activity["started_at"], timestamp + 10)
        self.assertEqual(activity["last_activity_at"], timestamp + 3700)

    def test_admin_worker_running_jobs_counts_only_running_scan_jobs(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        timestamp = app.now()
        scans = [
            {
                "id": "sc_worker_running",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_user",
                "createdAt": timestamp,
                "queuedAt": timestamp,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            {
                "id": "sc_worker_claimed",
                "repo": "acme/web",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_other",
                "createdAt": timestamp + 1,
                "queuedAt": timestamp + 1,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        ]
        app.USERS["usr_other"] = {"id": "usr_other", "email": "other@example.com", "name": "Other"}
        app.SCANS = scans
        for scan in scans:
            app.create_scan_job_for_scan(scan)
        claimed = db.claim_next_scan_job(worker_id, lease_seconds=3600, timestamp=timestamp)
        self.assertIsNotNone(claimed)
        self.assertIsNone(db.claim_next_scan_job(worker_id, lease_seconds=3600, timestamp=timestamp + 1))
        db.update_scan_job_progress(
            claimed["job_id"],
            {"phase": "ai", "progress": 50, "message": "reviewing", "started_at": timestamp + 10},
        )

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "max_concurrent_jobs": 2,
                "running_jobs": 2,
                "free_slots": 0,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [claimed["job_id"]],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        admin_workers = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers, "GET")
        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(admin_workers.status, HTTPStatus.OK)
        self.assertEqual(admin_workers.payload["workers"][0]["running_jobs"], 1)
        self.assertEqual(admin_workers.payload["workers"][0]["status"], "busy")
        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertEqual(detail.payload["worker"]["running_jobs"], 1)
        self.assertEqual(detail.payload["worker"]["status"], "busy")
        self.assertEqual(
            [activity["status"] for activity in detail.payload["taskActivity"]],
            ["running"],
        )

    def test_ready_worker_running_jobs_are_not_degraded_by_doctor_warning(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "max_concurrent_jobs": 1,
                "running_jobs": 1,
                "free_slots": 0,
                "last_error": "previous transient claim error",
                "doctor_status": "degraded",
                "codex_ready": True,
                "readyProviders": ["codex"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(worker["doctor_status"], "degraded")
        self.assertEqual(app.computed_worker_status(worker), "busy")

    def test_running_worker_with_deferred_codex_readiness_is_busy(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "max_concurrent_jobs": 4,
                "running_jobs": 4,
                "free_slots": 0,
                "last_error": "worker not ready: codex_ready: ready check deferred while codex is running",
                "doctor_status": "degraded",
                "codex_ready": False,
                "readyProviders": [],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(worker["doctor_status"], "degraded")
        self.assertEqual(worker["codex_ready"], 0)
        self.assertEqual(app.computed_worker_status(worker), "busy")

        db.upsert_worker_heartbeat(
            {
                **worker,
                "worker_id": worker_id,
                "running_jobs": 1,
                "free_slots": 3,
                "timestamp": app.now(),
            }
        )
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "busy")

        db.upsert_worker_heartbeat(
            {
                **worker,
                "worker_id": worker_id,
                "running_jobs": 0,
                "free_slots": 4,
                "timestamp": app.now(),
            }
        )
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")

    def test_admin_worker_version_controls_release_package_in_install_command(self) -> None:
        handler = RouteHarness(
            "/admin/workers",
            {"name": "Versioned worker", "provider": "codex", "version": "0.1.1", "max_concurrent_jobs": 2},
            cookie=self.admin_cookie,
        )

        app.PullwiseHandler.route(handler, "POST")

        expected = app.worker_release_package("0.1.1")
        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["suggested_env"]["PULLWISE_WORKER_PACKAGE"], expected)
        self.assertIn(f"--package '{expected}'", handler.payload["install_commands"]["standard"])

    def test_default_worker_package_version_matches_checked_in_worker_package(self) -> None:
        worker_init = Path(__file__).resolve().parents[2] / "pullwise-worker" / "pullwise_worker" / "__init__.py"
        if not worker_init.is_file():
            self.skipTest("pullwise-worker checkout is not available beside pullwise-server")
        match = re.search(r'^__version__ = "([^"]+)"', worker_init.read_text(encoding="utf-8"), flags=re.MULTILINE)
        self.assertIsNotNone(match)
        worker_version = match.group(1)

        self.assertEqual(app.DEFAULT_WORKER_PACKAGE_VERSION, worker_version)
        self.assertEqual(app.DEFAULT_WORKER_PACKAGE, app.worker_release_package(worker_version))

    def test_admin_worker_create_includes_configured_codex_timeout_env(self) -> None:
        with patch.object(app.system_config, "worker_codex_timeout_seconds", return_value=900):
            handler = RouteHarness(
                "/admin/workers",
                {"name": "Timeout worker", "provider": "codex", "max_concurrent_jobs": 2},
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["suggested_env"]["PULLWISE_CODEX_TIMEOUT_SECONDS"], "900")
        self.assertEqual(handler.payload["suggested_env"]["PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS"], "1800")
        self.assertEqual(handler.payload["suggested_env"]["PULLWISE_CODEX_APP_SERVER_MAX_TURNS"], "8")

    def test_install_worker_script_writes_configured_codex_timeout_env(self) -> None:
        with patch.object(app.system_config, "worker_codex_timeout_seconds", return_value=900):
            script = app.worker_install_script()

        self.assertIn(
            'write_env_value PULLWISE_CODEX_TIMEOUT_SECONDS "${PULLWISE_CODEX_TIMEOUT_SECONDS:-900}"',
            script,
        )
        self.assertIn(
            'write_env_value PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS "${PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS:-1800}"',
            script,
        )
        self.assertIn(
            'write_env_value PULLWISE_CODEX_APP_SERVER_MAX_TURNS "${PULLWISE_CODEX_APP_SERVER_MAX_TURNS:-8}"',
            script,
        )

    def test_worker_minimum_version_uses_numeric_components(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_MIN_WORKER_VERSION": "0.9.0"}, clear=False):
            self.assertTrue(app.worker_version_compatible({"version": "0.10.0"}))
            self.assertTrue(app.worker_version_compatible({"version": "v0.9.0"}))
            self.assertFalse(app.worker_version_compatible({"version": "0.8.9"}))
            self.assertFalse(app.worker_version_compatible({"version": "not-a-version"}))
            self.assertFalse(app.worker_version_compatible({"version": "0.10.0-beta"}))
            self.assertFalse(app.worker_version_compatible({"version": ""}))

    def test_admin_worker_defaults_recovers_from_invalid_plan_agent_state(self) -> None:
        db.save_state_item(
            app.billing.REVIEW_AGENT_CONFIG_STATE_KEY,
            {
                "version": 1,
                "plans": {
                    "free": {"provider": "bad"},
                    "pro": {"provider": ""},
                    "max": {"provider": None},
                },
            },
        )

        with patch("urllib.request.urlopen", side_effect=OSError("network unavailable")):
            handler = RouteHarness("/admin/workers/defaults", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["providerChain"], ["codex"])
        self.assertEqual(handler.payload["defaults"]["providerChain"], ["codex"])
        self.assertEqual(handler.payload["workerVersion"], app.DEFAULT_WORKER_PACKAGE_VERSION)

    def test_admin_worker_defaults_refresh_bypasses_cached_latest_release(self) -> None:
        class ReleaseResponse:
            def __enter__(self) -> "ReleaseResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"tag_name": "v0.5.5"}).encode("utf-8")

        app.LATEST_WORKER_RELEASE_CACHE.update({"version": "0.5.4", "checked_at": app.now()})
        cached = RouteHarness("/admin/workers/defaults", cookie=self.admin_cookie)
        app.PullwiseHandler.route(cached, "GET")

        with patch("urllib.request.urlopen", return_value=ReleaseResponse()) as urlopen:
            refreshed = RouteHarness("/admin/workers/defaults?refresh=1", cookie=self.admin_cookie)
            app.PullwiseHandler.route(refreshed, "GET")

        self.assertEqual(cached.status, HTTPStatus.OK)
        self.assertEqual(cached.payload["latestWorkerVersion"], "0.5.4")
        self.assertEqual(refreshed.status, HTTPStatus.OK)
        self.assertEqual(refreshed.payload["latestWorkerVersion"], "0.5.5")
        self.assertEqual(refreshed.payload["workerVersion"], "0.5.5")
        self.assertEqual(urlopen.call_count, 2)

    def test_admin_worker_defaults_prefers_highest_release_list_version(self) -> None:
        class ReleaseResponse:
            def __init__(self, payload: object) -> None:
                self.payload = payload

            def __enter__(self) -> "ReleaseResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        responses = [
            ReleaseResponse({"tag_name": "v0.5.4"}),
            ReleaseResponse(
                [
                    {"tag_name": "v0.5.4", "draft": False, "prerelease": False},
                    {"tag_name": "v0.5.5", "draft": False, "prerelease": False},
                    {"tag_name": "v0.6.0-beta", "draft": False, "prerelease": True},
                ]
            ),
        ]

        with (
            patch.dict(os.environ, {"PULLWISE_WORKER_RELEASE_TOKEN": "ghp_release"}, clear=False),
            patch("urllib.request.urlopen", side_effect=responses) as urlopen,
        ):
            handler = RouteHarness("/admin/workers/defaults?refresh=1", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["latestWorkerVersion"], "0.5.5")
        self.assertEqual(handler.payload["workerVersion"], "0.5.5")
        self.assertEqual(urlopen.call_count, 2)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertIn("/releases/latest", requests[0].full_url)
        self.assertIn("/releases?per_page=50", requests[1].full_url)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer ghp_release")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer ghp_release")

    def test_admin_worker_defaults_exposes_latest_release_when_default_version_is_pinned(self) -> None:
        class ReleaseResponse:
            def __enter__(self) -> "ReleaseResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"tag_name": "v0.5.5"}).encode("utf-8")

        with (
            patch.object(app.system_config, "worker_default_version", return_value="0.5.4"),
            patch("urllib.request.urlopen", return_value=ReleaseResponse()),
        ):
            handler = RouteHarness("/admin/workers/defaults?refresh=1", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["workerVersion"], "0.5.4")
        self.assertEqual(handler.payload["configuredWorkerVersion"], "0.5.4")
        self.assertEqual(handler.payload["defaults"]["source"], "configured")
        self.assertEqual(handler.payload["latestWorkerVersion"], "0.5.5")
        self.assertEqual(handler.payload["release"]["latestVersion"], "0.5.5")

    def test_admin_can_dispatch_worker_release_workflow(self) -> None:
        captured: dict[str, object] = {}

        class DispatchResponse:
            status = 204

            def __enter__(self) -> "DispatchResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return 204

        def fake_urlopen(request, timeout: int):
            captured["request"] = request
            captured["timeout"] = timeout
            return DispatchResponse()

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_WORKER_RELEASE_TOKEN": "ghp_release",
                    "PULLWISE_WORKER_RELEASE_REPOSITORY": "GoPullwise/pullwise-worker",
                    "PULLWISE_WORKER_RELEASE_WORKFLOW": "release.yml",
                    "PULLWISE_WORKER_RELEASE_REF": "main",
                    "PULLWISE_GITHUB_API_URL": "https://api.github.test",
                    "PULLWISE_GITHUB_TIMEOUT_SECONDS": "7",
                },
                clear=False,
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen) as urlopen,
        ):
            handler = RouteHarness(
                "/admin/workers/releases",
                {"version": "v0.4.3"},
                cookie=self.admin_cookie,
                headers={"X-Request-Id": "req_release"},
            )
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.ACCEPTED)
        self.assertEqual(handler.payload["version"], "0.4.3")
        self.assertEqual(handler.payload["tag"], "v0.4.3")
        self.assertEqual(handler.payload["repository"], "GoPullwise/pullwise-worker")
        self.assertEqual(handler.payload["workflow"], "release.yml")
        self.assertEqual(handler.payload["workflowDispatch"]["inputs"], {"version": "0.4.3"})
        self.assertNotIn("ghp_release", json.dumps(handler.payload))
        self.assertEqual(captured["timeout"], 7)
        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.github.test/repos/GoPullwise/pullwise-worker/actions/workflows/release.yml/dispatches",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer ghp_release")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"ref": "main", "inputs": {"version": "0.4.3"}})
        self.assertEqual(urlopen.call_count, 1)
        audit = db.list_worker_audit_events(limit=1)[0]
        self.assertEqual(audit["action"], "release_worker")
        self.assertEqual(audit["actor_user_id"], "usr_admin")
        self.assertEqual(audit["request_id"], "req_release")
        self.assertEqual(json.loads(audit["changed_fields"])["tag"], "v0.4.3")

    def test_admin_worker_release_rejects_invalid_version(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            handler = RouteHarness(
                "/admin/workers/releases",
                {"version": "0.4.3-beta"},
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("x.y.z", handler.payload["message"])
        urlopen.assert_not_called()
        audit = db.list_worker_audit_events(limit=1)[0]
        self.assertEqual(audit["action"], "release_worker")
        self.assertEqual(audit["success"], 0)

    def test_non_admin_cannot_dispatch_worker_release_workflow(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            handler = RouteHarness(
                "/admin/workers/releases",
                {"version": "0.4.3"},
                cookie=self.user_cookie,
            )
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        urlopen.assert_not_called()

    def test_non_admin_cannot_access_admin_workers(self) -> None:
        denied = RouteHarness("/admin/workers", cookie=self.user_cookie)
        app.PullwiseHandler.route(denied, "GET")

        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

    def test_admin_can_list_authorized_users(self) -> None:
        app.USERS["usr_user"]["billing"] = {
            "provider": "creem",
            "customerId": "cust_user",
            "customerEmail": "user@example.com",
            "subscriptionId": "sub_user",
            "subscriptionItemId": "item_user",
            "status": "active",
            "plan": "pro",
            "interval": "year",
            "currentPeriodStart": 1710000000,
            "currentPeriodEnd": 4102444800,
            "cancelAtPeriodEnd": False,
            "lastEventType": "subscription.active",
            "lastEventCreated": 1710000123,
            "updatedAt": 1710000130,
        }
        app.SCANS = [
            {"id": "sc_user", "userId": "usr_user", "repo": "owner/repo", "status": "done"},
            {"id": "sc_admin", "userId": "usr_admin", "repo": "owner/repo", "status": "done"},
        ]
        app.ISSUES = [
            {"id": "issue_user", "scanId": "sc_user", "title": "User issue"},
            {"id": "issue_admin", "scanId": "sc_admin", "title": "Admin issue"},
        ]

        handler = RouteHarness("/admin/users", cookie=self.admin_cookie)
        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        users = {user["id"]: user for user in handler.payload["users"]}
        self.assertEqual(set(users), {"usr_admin", "usr_user"})
        self.assertTrue(users["usr_admin"]["admin"])
        self.assertTrue(users["usr_admin"]["current"])
        self.assertFalse(users["usr_user"]["admin"])
        self.assertEqual(users["usr_user"]["scanCount"], 1)
        self.assertEqual(users["usr_user"]["issueCount"], 1)
        self.assertEqual(
            users["usr_user"]["subscription"],
            {
                "provider": "creem",
                "status": "active",
                "plan": "pro",
                "effectivePlan": "pro",
                "interval": "year",
                "customerId": "cust_user",
                "customerEmail": "user@example.com",
                "subscriptionId": "sub_user",
                "subscriptionItemId": "item_user",
                "currentPeriodStart": 1710000000,
                "currentPeriodEnd": 4102444800,
                "cancelAtPeriodEnd": False,
                "canceledAt": None,
                "lastEventType": "subscription.active",
                "lastEventCreated": 1710000123,
                "updatedAt": 1710000130,
            },
        )

    def test_admin_delete_user_removes_sessions_state_and_database_records(self) -> None:
        app.USERS["usr_user"]["githubRepositoryAccess"] = {
            "repositoryItems": [{"id": "repo_123", "fullName": "owner/repo"}],
        }
        app.SETTINGS["usr_user"] = {"profile": {"name": "User"}}
        app.GITHUB_STATES["state_user"] = {"kind": "install", "userId": "usr_user", "expiresAt": app.now() + 600}
        app.SCANS = [
            {
                "id": "sc_user",
                "userId": "usr_user",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "abc1234",
                "status": "queued",
            },
            {"id": "sc_admin", "userId": "usr_admin", "repo": "owner/repo", "status": "done"},
        ]
        app.ISSUES = [
            {"id": "issue_user", "scanId": "sc_user", "title": "User issue"},
            {"id": "issue_admin", "scanId": "sc_admin", "title": "Admin issue"},
        ]
        db.create_api_key(
            {
                "id": "ak_user",
                "user_id": "usr_user",
                "name": "User key",
                "key_prefix": "pwk_user",
                "key_hash": "hash_user",
                "scopes": ["scans:read"],
            }
        )
        job = app.create_scan_job_for_scan(app.SCANS[0])

        handler = RouteHarness("/admin/users/usr_user", cookie=self.admin_cookie)
        app.PullwiseHandler.route(handler, "DELETE")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["deleted"])
        self.assertNotIn("usr_user", app.USERS)
        self.assertNotIn("ses_user", app.SESSIONS)
        self.assertNotIn("usr_user", app.SETTINGS)
        self.assertNotIn("state_user", app.GITHUB_STATES)
        self.assertEqual([scan["id"] for scan in app.SCANS], ["sc_admin"])
        self.assertEqual([issue["id"] for issue in app.ISSUES], ["issue_admin"])
        self.assertEqual(db.list_api_keys_for_user("usr_user"), [])
        self.assertIsNone(db.get_scan_job(job["job_id"]))
        self.assertEqual(handler.payload["removed"]["sessions"], 1)
        self.assertEqual(handler.payload["removed"]["scans"], 1)
        self.assertEqual(handler.payload["removed"]["issues"], 1)

    def test_admin_cannot_delete_current_user(self) -> None:
        handler = RouteHarness("/admin/users/usr_admin", cookie=self.admin_cookie)
        app.PullwiseHandler.route(handler, "DELETE")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("cannot delete", handler.payload["message"])
        self.assertIn("usr_admin", app.USERS)

    def test_non_admin_cannot_access_admin_users(self) -> None:
        denied = RouteHarness("/admin/users", cookie=self.user_cookie)
        app.PullwiseHandler.route(denied, "GET")

        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

    def test_admin_plan_agent_config_rejects_invalid_values(self) -> None:
        update = RouteHarness(
            "/admin/subscription-plans/agent-configs/pro",
            {"provider": "bad", "codex": {"reasoningEffort": "extreme"}},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")

        self.assertEqual(update.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("provider", update.payload["message"])

    def test_admin_plan_agent_config_updates_review_worker_policy(self) -> None:
        update = RouteHarness(
            "/admin/subscription-plans/agent-configs/pro",
            {
                "provider": "codex",
                "reviewWorker": {
                    "reviewerMaxTurnsPerScan": 4,
                    "turnTimeoutSeconds": 1800,
                    "scanDeadlineSeconds": 12000,
                },
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")

        defaults = app.billing.default_review_agent_review_worker_config("pro")
        self.assertEqual(defaults["reviewerMaxTurnsPerScan"], 2)
        self.assertEqual(defaults["turnTimeoutSeconds"], 3600)
        self.assertEqual(defaults["scanDeadlineSeconds"], 14400)
        expected_pro_policy = {
            **app.billing.default_review_agent_review_worker_config("pro"),
            "reviewerMaxTurnsPerScan": 4,
            "turnTimeoutSeconds": 1800,
            "scanDeadlineSeconds": 12000,
        }
        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(
            update.payload["agentConfig"]["reviewWorker"],
            expected_pro_policy,
        )

        admin = RouteHarness("/admin/subscription-plans/agent-configs", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin, "GET")

        self.assertEqual(admin.status, HTTPStatus.OK)
        self.assertEqual(
            admin.payload["agentConfigs"]["pro"]["reviewWorker"],
            expected_pro_policy,
        )
        self.assertEqual(
            admin.payload["agentConfigs"]["free"]["reviewWorker"],
            app.billing.default_review_agent_review_worker_config("free"),
        )
    def test_plan_agent_config_reads_repair_invalid_persisted_provider(self) -> None:
        db.save_state_item(
            app.billing.REVIEW_AGENT_CONFIG_STATE_KEY,
            {
                "version": 1,
                "plans": {
                    "free": {
                        "provider": "bad",
                        "codex": {"cli": "codex", "model": "gpt-free", "reasoningEffort": "high"},
                    },
                    "pro": {"provider": ""},
                    "max": {"provider": None},
                },
            },
        )

        admin = RouteHarness("/admin/subscription-plans/agent-configs", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin, "GET")
        docs = RouteHarness("/docs/subscription-plans")
        app.PullwiseHandler.route(docs, "GET")

        for handler in (admin, docs):
            self.assertEqual(handler.status, HTTPStatus.OK)
            self.assertEqual(handler.payload["agentConfigs"]["free"]["provider"], "codex")
            self.assertEqual(handler.payload["agentConfigs"]["pro"]["provider"], "codex")
            self.assertEqual(handler.payload["agentConfigs"]["max"]["provider"], "codex")
            self.assertNotIn("providerChain", handler.payload["agentConfigs"]["free"])
            self.assertEqual(handler.payload["agentConfigs"]["free"]["codex"]["model"], "gpt-free")

        stored = db.load_state_item(app.billing.REVIEW_AGENT_CONFIG_STATE_KEY)
        self.assertEqual(stored["plans"]["free"]["provider"], "codex")
        self.assertEqual(stored["plans"]["pro"]["provider"], "codex")
        self.assertEqual(stored["plans"]["max"]["provider"], "codex")

    def test_admin_review_calibration_routes_are_retired(self) -> None:
        denied = RouteHarness(
            "/admin/review-calibration?scope_key=user:usr_1|repo:repo_123|branch:main",
            cookie=self.user_cookie,
        )
        app.PullwiseHandler.route(denied, "GET")
        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

        handler = RouteHarness(
            "/admin/review-calibration?scope_key=user:usr_1|repo:repo_123|branch:main",
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.GONE)
        self.assertIn("Graph-Verified", handler.payload["message"])

        label_denied = RouteHarness(
            "/admin/review-calibration/labels",
            {"candidateObservationKey": "obs_manual_label", "outcomeLabel": "false_positive"},
            cookie=self.user_cookie,
        )
        app.PullwiseHandler.route(label_denied, "POST")
        self.assertEqual(label_denied.status, HTTPStatus.FORBIDDEN)

        label_handler = RouteHarness(
            "/admin/review-calibration/labels",
            {
                "eventId": "evt_manual_label",
                "candidateObservationKey": "obs_manual_label",
                "outcomeLabel": "false_positive",
                "reason": "manual review rejected the sample",
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(label_handler, "POST")
        self.assertEqual(label_handler.status, HTTPStatus.GONE)
        self.assertIn("retired", label_handler.payload["message"])

    def test_admin_can_update_enable_disable_delete_and_rotate_worker(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        update = RouteHarness(f"/admin/workers/{worker_id}", {"name": "EU worker", "region": "eu"}, cookie=self.admin_cookie)
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(update.payload["worker"]["name"], "EU worker")
        self.assertEqual(update.payload["worker"]["region"], "eu")

        disable = RouteHarness(f"/admin/workers/{worker_id}/disable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(disable, "POST")
        self.assertEqual(disable.status, HTTPStatus.OK)
        self.assertFalse(disable.payload["worker"]["enabled"])
        self.assertEqual(disable.payload["worker"]["status"], "disabled")

        disabled_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(disabled_heartbeat, "POST")
        self.assertEqual(disabled_heartbeat.status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(db.get_worker(worker_id)["region"], "eu")

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.UNAUTHORIZED)

        enable = RouteHarness(f"/admin/workers/{worker_id}/enable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(enable, "POST")
        self.assertEqual(enable.status, HTTPStatus.OK)
        self.assertTrue(enable.payload["worker"]["enabled"])

        rotate = RouteHarness(f"/admin/workers/{worker_id}/rotate-token", cookie=self.admin_cookie)
        app.PullwiseHandler.route(rotate, "POST")
        self.assertEqual(rotate.status, HTTPStatus.OK)
        new_token = rotate.payload["worker_token"]
        self.assertNotEqual(new_token, token)
        self.assertEqual(db.get_worker(worker_id)["token_hash"], db.worker_token_hash(new_token))
        self.assertIn("PULLWISE_WORKER_TOKEN", rotate.payload["install_commands"]["standard"])
        self.assertNotIn("--worker-token", rotate.payload["install_commands"]["standard"])
        self.assertNotIn(new_token, rotate.payload["install_commands"]["standard"])

        old_token_claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(old_token_claim, "POST")
        self.assertEqual(old_token_claim.status, HTTPStatus.UNAUTHORIZED)

        new_token_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        app.PullwiseHandler.route(new_token_heartbeat, "POST")
        self.assertEqual(new_token_heartbeat.status, HTTPStatus.OK)
        self.assertEqual(new_token_heartbeat.payload["worker"]["worker_id"], worker_id)
        self.assertEqual(db.get_worker(worker_id)["region"], "eu")

        admin_workers = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers, "GET")
        self.assertEqual(admin_workers.status, HTTPStatus.OK)
        self.assertEqual(admin_workers.payload["workers"][0]["region"], "eu")

        delete = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(delete, "DELETE")
        self.assertEqual(delete.status, HTTPStatus.ACCEPTED)
        self.assertTrue(delete.payload["deleteQueued"])
        self.assertFalse(delete.payload["deleted"])
        self.assertEqual(delete.payload["command"]["command"], "uninstall")
        self.assertIsNone(db.get_worker(worker_id)["deleted_at"])

        actions = [event["action"] for event in db.list_worker_audit_events(worker_id, limit=20)]
        for expected in ["update_worker", "disable_worker", "enable_worker", "rotate_worker_token", "delete_worker"]:
            self.assertIn(expected, actions)

    def test_admin_delete_worker_queues_uninstall_without_server_host_cleanup(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        with patch.object(app.subprocess, "run", side_effect=AssertionError("admin delete must not run host cleanup")):
            delete = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
            app.PullwiseHandler.route(delete, "DELETE")

        self.assertEqual(delete.status, HTTPStatus.ACCEPTED)
        self.assertTrue(delete.payload["deleteQueued"])
        self.assertFalse(delete.payload["deleted"])
        command = delete.payload["command"]
        self.assertEqual(command["command"], "uninstall")
        self.assertEqual(command["status"], "pending")
        self.assertIsNone(db.get_worker(worker_id)["deleted_at"])

        poll = RouteHarness(
            "/worker/commands/poll",
            {"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(poll, "POST")
        self.assertEqual(poll.status, HTTPStatus.OK)
        self.assertEqual(poll.payload["command"]["id"], command["id"])
        self.assertEqual(poll.payload["command"]["command"], "uninstall")

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.UNAUTHORIZED)

    def test_admin_can_queue_worker_stop_and_uninstall_commands(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        initial_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(initial_heartbeat, "POST")
        self.assertEqual(initial_heartbeat.status, HTTPStatus.OK)
        stop = RouteHarness(
            f"/admin/workers/{worker_id}/commands",
            {"command": "stop"},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_stop"},
        )
        app.PullwiseHandler.route(stop, "POST")
        self.assertEqual(stop.status, HTTPStatus.ACCEPTED)
        stop_command = stop.payload["command"]
        self.assertEqual(stop_command["command"], "stop")
        self.assertEqual(stop_command["status"], "pending")
        self.assertFalse(db.get_worker(worker_id)["enabled"])
        self.assertEqual(stop.payload["worker"]["latest_command"]["id"], stop_command["id"])

        duplicate = RouteHarness(
            f"/admin/workers/{worker_id}/commands",
            {"command": "uninstall"},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(duplicate, "POST")
        self.assertEqual(duplicate.status, HTTPStatus.CONFLICT)

        poll_stop = RouteHarness(
            "/worker/commands/poll",
            {"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(poll_stop, "POST")
        self.assertEqual(poll_stop.status, HTTPStatus.OK)
        self.assertEqual(poll_stop.payload["worker"]["status"], "disabled")
        self.assertEqual(poll_stop.payload["command"]["id"], stop_command["id"])
        self.assertIsNone(poll_stop.payload["logSession"])

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.UNAUTHORIZED)

        running = RouteHarness(
            f"/worker/commands/{stop_command['id']}/status",
            {"worker_id": worker_id, "status": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(running, "POST")
        self.assertEqual(running.status, HTTPStatus.OK)
        self.assertEqual(running.payload["command"]["status"], "running")

        succeeded = RouteHarness(
            f"/worker/commands/{stop_command['id']}/status",
            {"worker_id": worker_id, "status": "succeeded"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(succeeded, "POST")
        self.assertEqual(succeeded.status, HTTPStatus.OK)
        self.assertEqual(db.get_latest_worker_command(worker_id)["status"], "succeeded")

        admin_workers_after_stop = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers_after_stop, "GET")
        self.assertEqual(admin_workers_after_stop.status, HTTPStatus.OK)
        self.assertEqual(admin_workers_after_stop.payload["workers"][0]["worker_id"], worker_id)
        self.assertFalse(admin_workers_after_stop.payload["workers"][0]["enabled"])

        uninstall = RouteHarness(
            f"/admin/workers/{worker_id}/commands",
            {"command": "uninstall"},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_uninstall"},
        )
        app.PullwiseHandler.route(uninstall, "POST")
        self.assertEqual(uninstall.status, HTTPStatus.ACCEPTED)
        uninstall_command = uninstall.payload["command"]
        self.assertEqual(uninstall_command["command"], "uninstall")
        self.assertTrue(uninstall.payload["deleteQueued"])
        self.assertFalse(uninstall.payload["deleted"])
        worker_after_uninstall_request = db.get_worker(worker_id, include_deleted=True)
        self.assertIsNotNone(worker_after_uninstall_request)
        self.assertFalse(worker_after_uninstall_request["enabled"])
        self.assertIsNone(worker_after_uninstall_request["deleted_at"])
        self.assertIsNone(uninstall.payload["worker"]["deleted_at"])

        admin_workers = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers, "GET")
        self.assertEqual(admin_workers.status, HTTPStatus.OK)
        self.assertEqual(admin_workers.payload["workers"][0]["worker_id"], worker_id)
        self.assertEqual(admin_workers.payload["workers"][0]["latest_command"]["status"], "pending")

        duplicate_delete = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(duplicate_delete, "DELETE")
        self.assertEqual(duplicate_delete.status, HTTPStatus.ACCEPTED)
        self.assertTrue(duplicate_delete.payload["deleteQueued"])
        self.assertTrue(duplicate_delete.payload["alreadyQueued"])
        self.assertEqual(duplicate_delete.payload["command"]["id"], uninstall_command["id"])

        uninstall_poll = RouteHarness(
            "/worker/commands/poll",
            {"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(uninstall_poll, "POST")
        self.assertEqual(uninstall_poll.status, HTTPStatus.OK)
        self.assertEqual(uninstall_poll.payload["command"]["id"], uninstall_command["id"])
        self.assertIsNone(uninstall_poll.payload["logSession"])

        uninstall_done = RouteHarness(
            f"/worker/commands/{uninstall_command['id']}/status",
            {"worker_id": worker_id, "status": "succeeded"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(uninstall_done, "POST")
        self.assertEqual(uninstall_done.status, HTTPStatus.OK)
        self.assertIsNone(db.get_worker(worker_id))
        self.assertIsNotNone(db.get_worker(worker_id, include_deleted=True)["deleted_at"])

        deleted_poll = RouteHarness(
            "/worker/commands/poll",
            {"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(deleted_poll, "POST")
        self.assertEqual(deleted_poll.status, HTTPStatus.UNAUTHORIZED)

    def test_uninstall_command_keeps_degraded_worker_visible_until_cleanup_finishes(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "max_concurrent_jobs": 4,
                "running_jobs": 0,
                "free_slots": 4,
                "doctor_status": "degraded",
                "codex_ready": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")

        uninstall = RouteHarness(
            f"/admin/workers/{worker_id}/commands",
            {"command": "uninstall"},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(uninstall, "POST")

        self.assertEqual(uninstall.status, HTTPStatus.ACCEPTED)
        self.assertEqual(uninstall.payload["command"]["command"], "uninstall")
        uninstall_command = uninstall.payload["command"]
        self.assertIsNone(db.get_worker(worker_id)["deleted_at"])

        admin_workers = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers, "GET")
        self.assertEqual(admin_workers.status, HTTPStatus.OK)
        self.assertEqual(admin_workers.payload["workers"][0]["worker_id"], worker_id)
        self.assertEqual(admin_workers.payload["workers"][0]["latest_command"]["status"], "pending")

        uninstall_failed = RouteHarness(
            f"/worker/commands/{uninstall_command['id']}/status",
            {"worker_id": worker_id, "status": "failed", "error": "watcher cleanup failed"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(uninstall_failed, "POST")
        self.assertEqual(uninstall_failed.status, HTTPStatus.OK)
        self.assertIsNone(db.get_worker(worker_id)["deleted_at"])

        admin_workers_after_failure = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers_after_failure, "GET")
        self.assertEqual(admin_workers_after_failure.status, HTTPStatus.OK)
        self.assertEqual(admin_workers_after_failure.payload["workers"][0]["worker_id"], worker_id)
        self.assertEqual(admin_workers_after_failure.payload["workers"][0]["latest_command"]["status"], "failed")

    def test_worker_can_unregister_itself_from_registry(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        unregister = RouteHarness(
            "/worker/registry",
            headers={"Authorization": f"Bearer {token}", "X-Request-Id": "req_worker_unregister"},
        )
        app.PullwiseHandler.route(unregister, "DELETE")

        self.assertEqual(unregister.status, HTTPStatus.OK)
        self.assertTrue(unregister.payload["deleted"])
        self.assertIsNone(db.get_worker(worker_id))
        self.assertIsNotNone(db.get_worker(worker_id, include_deleted=True)["deleted_at"])

        admin_workers = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_workers, "GET")
        self.assertEqual(admin_workers.status, HTTPStatus.OK)
        self.assertEqual(admin_workers.payload["workers"], [])

        claim = RouteHarness(
            "/worker/jobs/claim",
            {"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.UNAUTHORIZED)

        actions = [event["action"] for event in db.list_worker_audit_events(worker_id, limit=10)]
        self.assertIn("worker_self_unregister", actions)

    def test_disabled_or_deleted_worker_token_cannot_unregister_from_registry(self) -> None:
        disabled_payload, disabled_token = self.create_worker()
        disabled_worker_id = disabled_payload["worker_id"]
        db.set_worker_enabled(disabled_worker_id, False)

        disabled_unregister = RouteHarness(
            "/worker/registry",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        app.PullwiseHandler.route(disabled_unregister, "DELETE")

        self.assertEqual(disabled_unregister.status, HTTPStatus.UNAUTHORIZED)
        self.assertIsNone(db.get_worker(disabled_worker_id, include_deleted=True)["deleted_at"])

        deleted_payload, deleted_token = self.create_worker()
        deleted_worker_id = deleted_payload["worker_id"]
        db.soft_delete_worker(deleted_worker_id)

        deleted_unregister = RouteHarness(
            "/worker/registry",
            headers={"Authorization": f"Bearer {deleted_token}"},
        )
        app.PullwiseHandler.route(deleted_unregister, "DELETE")

        self.assertEqual(deleted_unregister.status, HTTPStatus.UNAUTHORIZED)
        self.assertIsNotNone(db.get_worker(deleted_worker_id, include_deleted=True)["deleted_at"])

    def test_heartbeat_status_public_and_admin_status_payloads(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.1.0",
                "max_concurrent_jobs": 4,
                "running_jobs": 2,
                "free_slots": 2,
                "hostname": "secret-host",
                "region": "us-east",
                "last_error": "",
                "doctor_status": "ok",
                "codex_ready": True,
                "systemd_active": True,
                "doctor_checked_at": app.now(),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(worker["running_jobs"], 1)
        self.assertNotIn("max_concurrent_jobs", worker)
        self.assertNotIn("free_slots", worker)
        self.assertIsNotNone(worker["last_heartbeat_at"])
        self.assertEqual(worker["doctor_status"], "ok")
        self.assertEqual(worker["codex_ready"], 1)
        self.assertEqual(worker["systemd_active"], 1)
        self.assertEqual(app.computed_worker_status(worker), "busy")

        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 0, "free_slots": 4, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "idle")
        db.upsert_worker_heartbeat(
            {
                **worker,
                "worker_id": worker_id,
                "running_jobs": 0,
                "free_slots": 4,
                "doctor_status": "degraded",
                "codex_ready": 0,
                "ready_providers": [],
                "timestamp": app.now(),
            }
        )
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
        db.upsert_worker_heartbeat(
            {
                **worker,
                "worker_id": worker_id,
                "running_jobs": 0,
                "free_slots": 4,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "last_error": "internal stack",
                "timestamp": app.now(),
            }
        )
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "idle")
        with patch("pullwise_server.app.now", return_value=app.now() + 1000):
            self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "offline")

        app.SCANS = [{"id": "sc_queued", "status": "queued"}, {"id": "sc_running", "status": "running"}]
        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")
        self.assertEqual(public.status, HTTPStatus.OK)
        public_text = json.dumps(public.payload)
        self.assertIn(public.payload["scanSystemStatus"], {"ok", "degraded", "down"})
        self.assertEqual(public.payload["queuedJobs"], 1)
        self.assertEqual(public.payload["runningJobs"], 1)
        self.assertNotIn("workers", public.payload)
        self.assertNotIn("US worker", public_text)
        self.assertNotIn("us-east", public_text)
        self.assertNotIn("0.1.0", public_text)
        self.assertNotIn("secret-host", public_text)
        self.assertNotIn("internal stack", public_text)
        self.assertNotIn("doctor_status", public_text)
        self.assertNotIn("systemd_active", public_text)
        self.assertNotIn("codex_ready", public_text)
        self.assertNotIn("auditEvents", public_text)
        self.assertNotIn("worker_token", public_text)
        self.assertNotIn("token_hash", public_text)
        self.assertNotIn(token, public_text)

        admin = RouteHarness("/admin/status", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin, "GET")
        self.assertEqual(admin.status, HTTPStatus.OK)
        self.assertEqual(admin.payload["workers"][0]["worker_id"], worker_id)
        self.assertIn("hostname", admin.payload["workers"][0])
        self.assertIn("last_error", admin.payload["workers"][0])
        self.assertEqual(admin.payload["workers"][0]["doctor_status"], "ok")
        self.assertTrue(admin.payload["workers"][0]["codex_ready"])
        self.assertTrue(admin.payload["workers"][0]["systemd_active"])

    def test_status_reports_worker_counts_without_capacity_fields(self) -> None:
        payload_one, token_one = self.create_worker()
        worker_one_id = payload_one["worker_id"]
        payload_two, token_two = self.create_worker()
        worker_two_id = payload_two["worker_id"]

        for worker_id, token, running in (
            (worker_one_id, token_one, 1),
            (worker_two_id, token_two, 0),
        ):
            heartbeat = RouteHarness(
                "/worker/heartbeat",
                {
                    "worker_id": worker_id,
                    "provider": "codex",
                    "version": "0.1.0",
                    "max_concurrent_jobs": 99,
                    "running_jobs": running,
                    "free_slots": 99,
                    "doctor_status": "ok",
                    "codex_ready": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            app.PullwiseHandler.route(heartbeat, "POST")
            self.assertEqual(heartbeat.status, HTTPStatus.OK)

        scan = {
            "id": "sc_busy_worker",
            "repo": "acme/busy",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        db.claim_next_scan_job(worker_one_id)

        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")

        self.assertEqual(public.status, HTTPStatus.OK)
        self.assertEqual(public.payload["onlineWorkerCount"], 2)
        self.assertEqual(public.payload["totalWorkerCount"], 2)
        self.assertEqual(public.payload["busyWorkerCount"], 1)
        self.assertEqual(public.payload["idleWorkerCount"], 1)
        self.assertNotIn("totalCapacity", public.payload)
        self.assertNotIn("availableCapacity", public.payload)
        self.assertNotIn("workers", public.payload)

    def test_public_worker_status_and_admin_routes_exclude_private_workers(self) -> None:
        private_worker = db.create_worker(
            {
                "name": "User private worker",
                "worker_scope": db.WORKER_SCOPE_PRIVATE,
                "owner_user_id": "usr_user",
                "provider": "codex",
                "region": "private",
            }
        )
        private_worker_id = private_worker["worker_id"]
        db.upsert_worker_heartbeat(
            {
                "worker_id": private_worker_id,
                "provider": "codex",
                "version": "0.4.18",
                "running_jobs": 0,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": app.now(),
            }
        )
        db.create_scan_job(
            {
                "job_id": "job_private_status",
                "scan_id": "sc_private_status",
                "repo": "acme/private",
                "branch": "main",
                "commit": "abc1234",
                "status": "queued",
                "created_at": app.now(),
                "user_id": "usr_user",
                "worker_scope": db.WORKER_SCOPE_PRIVATE,
                "worker_owner_user_id": "usr_user",
            }
        )
        app.SCANS = [
            {
                "id": "sc_private_memory",
                "status": "queued",
                "workerScope": db.WORKER_SCOPE_PRIVATE,
            }
        ]

        public_status = RouteHarness("/status/system")
        app.PullwiseHandler.route(public_status, "GET")
        admin_status = RouteHarness("/admin/status", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_status, "GET")
        admin_list = RouteHarness("/admin/workers", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_list, "GET")

        self.assertEqual(public_status.status, HTTPStatus.OK)
        self.assertEqual(public_status.payload["scanSystemStatus"], "down")
        self.assertEqual(public_status.payload["totalWorkerCount"], 0)
        self.assertEqual(public_status.payload["onlineWorkerCount"], 0)
        self.assertEqual(public_status.payload["queuedJobs"], 0)
        self.assertEqual(admin_status.status, HTTPStatus.OK)
        self.assertEqual(admin_status.payload["workers"], [])
        self.assertEqual(admin_status.payload["totalWorkerCount"], 0)
        self.assertEqual(admin_list.status, HTTPStatus.OK)
        self.assertEqual(admin_list.payload["workers"], [])
        self.assertEqual(admin_list.payload["total"], 0)

        admin_detail = RouteHarness(f"/admin/workers/{private_worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_detail, "GET")
        admin_disable = RouteHarness(f"/admin/workers/{private_worker_id}/disable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_disable, "POST")
        admin_update = RouteHarness(f"/admin/workers/{private_worker_id}", {"name": "Renamed"}, cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_update, "PATCH")
        admin_command = RouteHarness(
            f"/admin/workers/{private_worker_id}/commands",
            {"command": "uninstall"},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(admin_command, "POST")
        admin_delete = RouteHarness(f"/admin/workers/{private_worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin_delete, "DELETE")

        self.assertEqual(admin_detail.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(admin_disable.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(admin_update.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(admin_command.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(admin_delete.status, HTTPStatus.NOT_FOUND)

    def test_worker_capacity_inputs_are_ignored_for_admin_writes_and_heartbeat(self) -> None:
        create = RouteHarness(
            "/admin/workers",
            {"name": "Too large", "provider": "codex", "max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(create, "POST")
        self.assertEqual(create.status, HTTPStatus.CREATED)
        self.assertNotIn("max_concurrent_jobs", create.payload["worker"])
        self.assertNotIn("free_slots", create.payload["worker"])
        self.assertNotIn("PULLWISE_MAX_CONCURRENT_JOBS", create.payload["suggested_env"])
        self.assertNotIn("--max-concurrent-jobs", create.payload["install_commands"]["standard"])

        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertNotIn("max_concurrent_jobs", update.payload["worker"])
        self.assertNotIn("free_slots", update.payload["worker"])

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 99, "running_jobs": 99, "free_slots": 99},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        stored = db.get_worker(worker_id)
        self.assertEqual(stored["running_jobs"], 1)
        self.assertNotIn("max_concurrent_jobs", stored)
        self.assertNotIn("free_slots", stored)
        self.assertEqual(stored["last_error"], "")

        heartbeat_with_error = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "max_concurrent_jobs": 99,
                "running_jobs": 1,
                "free_slots": 1,
                "last_error": "disk pressure",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat_with_error, "POST")

        self.assertEqual(heartbeat_with_error.status, HTTPStatus.OK)
        stored = db.get_worker(worker_id)
        self.assertEqual(stored["last_error"], "disk pressure")

    def test_worker_test_records_audit(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]

        test = RouteHarness(f"/admin/workers/{worker_id}/test", cookie=self.admin_cookie)
        app.PullwiseHandler.route(test, "POST")

        self.assertEqual(test.status, HTTPStatus.OK)
        self.assertIn("checks", test.payload["result"])
        self.assertIn("test_worker", [event["action"] for event in db.list_worker_audit_events(worker_id)])

    def test_worker_audit_records_required_fields_for_success_and_failure(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"name": "Audit worker", "region": "eu-west"},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_update"},
        )
        app.PullwiseHandler.route(update, "PATCH")
        missing = RouteHarness(
            "/admin/workers/missing_worker/disable",
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_missing"},
        )
        app.PullwiseHandler.route(missing, "POST")

        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(missing.status, HTTPStatus.NOT_FOUND)
        events = db.list_worker_audit_events(limit=20)
        update_event = next(event for event in events if event["action"] == "update_worker")
        failure_event = next(event for event in events if event["action"] == "disable_worker" and event["success"] == 0)

        self.assertEqual(update_event["actor_user_id"], "usr_admin")
        self.assertEqual(update_event["worker_id"], worker_id)
        self.assertEqual(update_event["request_id"], "req_update")
        self.assertEqual(update_event["success"], 1)
        self.assertEqual(json.loads(update_event["changed_fields"]), {"name": "Audit worker", "region": "eu-west"})
        self.assertIsNotNone(update_event["created_at"])

        self.assertEqual(failure_event["actor_user_id"], "usr_admin")
        self.assertEqual(failure_event["worker_id"], "missing_worker")
        self.assertEqual(failure_event["request_id"], "req_missing")
        self.assertEqual(failure_event["error"], "Worker not found.")


if __name__ == "__main__":
    unittest.main()

