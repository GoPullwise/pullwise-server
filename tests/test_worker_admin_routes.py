from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


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
    app.STATE_LOADED = True
    app.STATE_DIRTY = False


class WorkerAdminRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
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
        db.initialize()
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

    def test_admin_can_create_worker_and_token_is_only_returned_once_as_hash(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        stored = db.get_worker(worker_id)
        audit = db.list_worker_audit_events(worker_id)

        self.assertTrue(token.startswith("pww_"))
        self.assertEqual(stored["worker_id"], worker_id)
        self.assertNotEqual(stored["token_hash"], token)
        self.assertEqual(stored["token_hash"], db.worker_token_hash(token))
        self.assertNotIn("worker_token", payload["worker"])
        self.assertEqual(payload["suggested_env"]["PULLWISE_WORKER_TOKEN"], token)
        self.assertEqual(payload["install_url"], "http://localhost:8080/install-worker.sh")
        self.assertIn("read -rsp", payload["install_command"])
        self.assertIn("PULLWISE_WORKER_TOKEN", payload["install_command"])
        self.assertNotIn("--worker-token", payload["install_command"])
        self.assertNotIn(token, payload["install_command"])
        self.assertIn("'US worker'", payload["install_command"])
        self.assertIn("--max-concurrent-jobs 4", payload["install_command"])
        self.assertIn(f"--package '{app.default_worker_package()}'", payload["install_command"])
        self.assertIn("pullwise_worker-0.1.8-py3-none-any.whl", payload["install_command"])
        self.assertEqual(payload["local_server_url"], "http://127.0.0.1:18080")
        self.assertEqual(payload["local_install_url"], "http://127.0.0.1:18080/install-worker.sh")
        self.assertIn("http://127.0.0.1:18080/install-worker.sh", payload["local_install_command"])
        self.assertIn("--server 'http://127.0.0.1:18080'", payload["local_install_command"])
        self.assertIn("--max-concurrent-jobs 4", payload["local_install_command"])
        self.assertIn(f"--package '{app.default_worker_package()}'", payload["local_install_command"])
        self.assertNotIn(token, payload["local_install_command"])
        self.assertEqual(payload["install_commands"]["standard"], payload["install_command"])
        self.assertEqual(payload["install_commands"]["local"], payload["local_install_command"])
        self.assertEqual(payload["suggested_env"]["PULLWISE_MAX_CONCURRENT_JOBS"], "4")
        self.assertEqual(payload["suggested_env"]["PULLWISE_LOCAL_SERVER_URL"], "http://127.0.0.1:18080")
        self.assertEqual(payload["suggested_env"]["PULLWISE_WORKER_PACKAGE"], app.default_worker_package())
        self.assertEqual(payload["suggested_env"]["PULLWISE_CODEX_PACKAGE"], "@openai/codex@0.135.0")
        self.assertEqual(payload["suggested_env"]["PULLWISE_PROVIDER_CHAIN"], "codex")
        self.assertEqual(payload["suggested_env"]["PULLWISE_CODEX_MODEL"], "gpt-5.5")
        self.assertEqual(payload["suggested_env"]["PULLWISE_CODEX_REASONING_EFFORT"], "medium")
        self.assertEqual(payload["suggested_env"]["PULLWISE_OPENCODE_COMMAND"], "opencode")
        self.assertEqual(payload["suggested_env"]["PULLWISE_OPENCODE_MODEL"], "opencode/big-pickle")
        self.assertEqual(payload["suggested_env"]["PULLWISE_OPENCODE_VARIANT"], "medium")
        self.assertEqual(payload["suggested_env"]["PULLWISE_WORKER_MAX_BACKOFF_SECONDS"], "60")
        self.assertEqual(payload["suggested_env"]["PULLWISE_MAX_REPO_FILES"], "2000")
        self.assertEqual(payload["suggested_env"]["PULLWISE_MAX_REPO_BYTES"], "52428800")
        self.assertEqual(audit[0]["action"], "create_worker")
        self.assertEqual(audit[0]["actor_user_id"], "usr_admin")

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")
        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertNotIn("worker_token", json.dumps(detail.payload))

    def test_admin_worker_detail_includes_recent_task_activity(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        timestamp = app.now()
        scan = {
            "id": "sc_worker_activity",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc123",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "installationId": "111",
            "repoId": "repo_123",
            "githubRepoId": "123",
            "cloneUrl": "https://github.com/acme/api.git",
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        worker_auth = {"Authorization": f"Bearer {token}"}

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.1.0",
                "max_concurrent_jobs": 4,
                "running_jobs": 0,
                "free_slots": 4,
                "doctor_status": "ok",
                "codex_ready": True,
            },
            headers=worker_auth,
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers=worker_auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["job"]["job_id"], job["job_id"])

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 70, "message": "reviewing", "started_at": timestamp + 10},
            headers=worker_auth,
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.OK)

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": f"{worker_id}-1",
                **empty_audit_result_fields(),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": 123,
                "result_checksum": "checksum-worker-activity",
            },
            headers=worker_auth,
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(detail.status, HTTPStatus.OK)
        activity = detail.payload["taskActivity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["worker_id"], worker_id)
        self.assertEqual(activity[0]["job_id"], job["job_id"])
        self.assertEqual(activity[0]["scan_id"], scan["id"])
        self.assertEqual(activity[0]["repo"], "acme/api")
        self.assertEqual(activity[0]["status"], "done")
        self.assertIsNotNone(activity[0]["claimed_at"])
        self.assertEqual(activity[0]["started_at"], timestamp + 10)
        self.assertIsNotNone(activity[0]["completed_at"])
        self.assertNotIn("clone_token", json.dumps(activity))
        self.assertNotIn("result_payload", json.dumps(activity))

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
        self.assertIn(f"--package '{expected}'", handler.payload["install_command"])

    def test_admin_worker_defaults_resolve_latest_release_version(self) -> None:
        class ReleaseResponse:
            def __enter__(self) -> "ReleaseResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"tag_name": "v0.2.3"}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=ReleaseResponse()):
            handler = RouteHarness("/admin/workers/defaults", cookie=self.admin_cookie)
            app.PullwiseHandler.route(handler, "GET")

        expected_package = app.worker_release_package("0.2.3")
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["workerVersion"], "0.2.3")
        self.assertEqual(handler.payload["workerPackage"], expected_package)

    def test_public_install_script_contains_deploy_assets_but_no_worker_secrets(self) -> None:
        install = RouteHarness("/install-worker.sh")

        app.PullwiseHandler.route(install, "GET")

        self.assertEqual(install.status, HTTPStatus.OK)
        self.assertIn("text/x-shellscript", install.headers_out["Content-Type"])
        self.assertIn("systemd", install.text_payload)
        self.assertIn("pullwise-worker.service", install.text_payload)
        self.assertIn("logrotate", install.text_payload)
        self.assertIn("doctor", install.text_payload)
        self.assertIn("codex login --device-auth", install.text_payload)
        self.assertIn("PULLWISE_WORKER_PACKAGE", install.text_payload)
        self.assertIn(app.default_worker_package(), install.text_payload)
        self.assertIn("pullwise_worker-0.1.8-py3-none-any.whl", install.text_payload)
        self.assertIn("Python 3.9 or newer", install.text_payload)
        self.assertIn("Node.js 20+ is required", install.text_payload)
        self.assertIn("Node.js 20+ must be available to $SERVICE_USER", install.text_payload)
        self.assertNotIn("pullwise-worker==0.1.0", install.text_payload)
        self.assertIn("PULLWISE_CODEX_PACKAGE", install.text_payload)
        self.assertIn("PULLWISE_PROVIDER_CHAIN", install.text_payload)
        self.assertIn("PULLWISE_CODEX_MODEL", install.text_payload)
        self.assertIn("PULLWISE_CODEX_REASONING_EFFORT", install.text_payload)
        self.assertIn('write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"', install.text_payload)
        self.assertIn(
            'write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"',
            install.text_payload,
        )
        self.assertIn(
            'write_env_value PULLWISE_OPENCODE_MODEL "${PULLWISE_OPENCODE_MODEL:-opencode/big-pickle}"',
            install.text_payload,
        )
        self.assertIn('write_env_value PULLWISE_OPENCODE_VARIANT "${PULLWISE_OPENCODE_VARIANT:-medium}"', install.text_payload)
        self.assertIn('write_env_value PULLWISE_MAX_REPO_FILES "2000"', install.text_payload)
        self.assertIn('write_env_value PULLWISE_MAX_REPO_BYTES "52428800"', install.text_payload)
        self.assertIn("PULLWISE_OPENCODE_COMMAND", install.text_payload)
        self.assertIn("PULLWISE_OPENCODE_MODEL", install.text_payload)
        self.assertIn("PULLWISE_OPENCODE_VARIANT", install.text_payload)
        self.assertIn("PULLWISE_PYTHON_BIN", install.text_payload)
        self.assertIn("run_as_service_user \"$BIN_PATH\" doctor || true", install.text_payload)
        self.assertIn("@openai/codex@0.135.0", install.text_payload)
        self.assertIn("--codex-package", install.text_payload)
        self.assertIn("--provider-chain", install.text_payload)
        self.assertIn("write_env_value()", install.text_payload)
        self.assertIn("environment value for $key must be single-line", install.text_payload)
        self.assertIn("load_worker_env /etc/pullwise-worker/worker.env", install.text_payload)
        self.assertNotIn("PULLWISE_WORKER_TOKEN=$WORKER_TOKEN", install.text_payload)
        self.assertNotIn(". /etc/pullwise-worker/worker.env", install.text_payload)
        self.assertIn("PULLWISE_WORKER_TOKEN", install.text_payload)
        self.assertIn("--worker-token-file", install.text_payload)
        self.assertIn("Restart=on-failure", install.text_payload)
        self.assertNotIn("Restart=always", install.text_payload)
        self.assertNotIn("--worker-token) WORKER_TOKEN", install.text_payload)
        self.assertNotIn("$(dirname \"$0\")", install.text_payload)
        self.assertNotIn("cp \"$(dirname", install.text_payload)
        self.assertNotIn("\r\n", install.text_payload)
        self.assertNotIn("pww_", install.text_payload)
        self.assertNotIn("WORKER_TOKEN=pww_", install.text_payload)

    def test_non_admin_cannot_access_admin_workers(self) -> None:
        denied = RouteHarness("/admin/workers", cookie=self.user_cookie)
        app.PullwiseHandler.route(denied, "GET")

        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

    def test_admin_review_calibration_summary_is_admin_only_and_sanitized(self) -> None:
        event = {
            "protocol": "pullwise-review-decision/0.1",
            "event_id": "evt_admin_calibration",
            "candidate_observation_key": "obs_admin_calibration",
            "scan_id": "sc_admin_calibration",
            "job_id": "job_admin_calibration",
            "attempt_id": "wk_1-1",
            "user_id": "usr_1",
            "repo_id": "repo_123",
            "github_repo_id": "123",
            "repo_full_name": "acme/api",
            "branch": "main",
            "commit_sha": "a" * 40,
            "candidate_id": "candidate-secret",
            "fingerprint": "fp-admin",
            "source": "correctness reviewer",
            "provider": "codex",
            "model": "gpt-5.5",
            "category": "correctness",
            "severity": "high",
            "verification_status": "potential_risk",
            "file_path": "src/app.py",
            "line_start": 12,
            "raw_confidence": 0.91,
            "calibrated_confidence": 0.90,
            "decision_score": 0.84,
            "decision": "reported",
            "decision_reason": "passed_convergence_gate",
            "scoring_protocol": "pullwise-review-score/0.1",
            "score_factors": {
                "scoreKind": "ranking_score",
                "proposedDecision": "reported",
                "rawSnippet": "secret code must never be exposed",
            },
            "created_at": app.now(),
        }
        db.record_review_decision_events([event])
        app.record_manual_review_outcome(
            event_id="evt_admin_calibration",
            candidate_observation_key="obs_admin_calibration",
            outcome_label="valid",
            reviewer_id="usr_admin",
            reason="confirmed during review",
        )

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

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["protocol"], "pullwise-review-calibration/0.2")
        self.assertEqual(handler.payload["scopeKey"], "user:usr_1|repo:repo_123|branch:main")
        self.assertEqual(handler.payload["shadowEvaluation"]["candidateCount"], 1)
        self.assertTrue(handler.payload["enforceGate"]["canConsiderEnforce"])
        self.assertEqual(handler.payload["driftSummary"]["normal"], len(handler.payload["snapshots"]))
        self.assertTrue(handler.payload["snapshots"])
        snapshot = next(
            item
            for item in handler.payload["snapshots"]
            if item["cohortKey"] == "source:correctness reviewer"
        )
        self.assertIn("posteriorMean", snapshot)
        self.assertIn("confidenceBuckets", snapshot)
        serialized = json.dumps(handler.payload)
        self.assertNotIn("candidate-secret", serialized)
        self.assertNotIn("secret code", serialized)
        self.assertNotIn("review_decision_events", serialized)

    def test_admin_can_record_manual_review_outcome_label(self) -> None:
        event = {
            "protocol": "pullwise-review-decision/0.1",
            "event_id": "evt_manual_label",
            "candidate_observation_key": "obs_manual_label",
            "scan_id": "sc_manual_label",
            "job_id": "job_manual_label",
            "attempt_id": "wk_1-1",
            "user_id": "usr_1",
            "repo_id": "repo_123",
            "github_repo_id": "123",
            "repo_full_name": "acme/api",
            "branch": "main",
            "commit_sha": "a" * 40,
            "candidate_id": "candidate-manual",
            "fingerprint": "fp-manual",
            "source": "correctness reviewer",
            "provider": "codex",
            "model": "gpt-5.5",
            "category": "correctness",
            "severity": "medium",
            "verification_status": "potential_risk",
            "file_path": "src/app.py",
            "line_start": 12,
            "normalized_title": "manual review candidate",
            "raw_confidence": 0.9,
            "calibrated_confidence": 0.88,
            "decision_score": 0.83,
            "decision": "reported",
            "decision_reason": "reported",
            "scoring_protocol": "pullwise-review-score/0.1",
            "score_factors": {"scoreKind": "ranking_score", "proposedDecision": "audit_only"},
            "created_at": app.now(),
        }
        db.record_review_decision_events([event])

        denied = RouteHarness(
            "/admin/review-calibration/labels",
            {"candidateObservationKey": "obs_manual_label", "outcomeLabel": "false_positive"},
            cookie=self.user_cookie,
        )
        app.PullwiseHandler.route(denied, "POST")
        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

        invalid = RouteHarness(
            "/admin/review-calibration/labels",
            {"candidateObservationKey": "obs_manual_label", "outcomeLabel": "maybe"},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(invalid, "POST")
        self.assertEqual(invalid.status, HTTPStatus.BAD_REQUEST)

        handler = RouteHarness(
            "/admin/review-calibration/labels",
            {
                "eventId": "evt_manual_label",
                "candidateObservationKey": "obs_manual_label",
                "outcomeLabel": "false_positive",
                "reason": "manual review rejected the sample",
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["label"]["outcomeLabel"], "false_positive")
        self.assertEqual(handler.payload["label"]["labelSource"], "manual_review")
        self.assertEqual(handler.payload["label"]["createdBy"], "usr_admin")
        self.assertEqual(handler.payload["effectiveLabel"]["outcomeLabel"], "false_positive")
        labels = db.list_review_outcome_labels("obs_manual_label")
        self.assertEqual(labels[0]["label_source"], "manual_review")
        self.assertEqual(labels[0]["outcome_label"], "false_positive")
        summary = RouteHarness(
            "/admin/review-calibration?scope_key=user:usr_1|repo:repo_123|branch:main",
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(summary, "GET")
        self.assertEqual(summary.status, HTTPStatus.OK)
        self.assertEqual(summary.payload["shadowEvaluation"]["labeledOutcomeCount"], 1)
        self.assertEqual(summary.payload["shadowEvaluation"]["currentReportedFalsePositiveCount"], 1)
        self.assertTrue(summary.payload["snapshots"])

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
        self.assertEqual(disabled_heartbeat.status, HTTPStatus.OK)
        self.assertEqual(disabled_heartbeat.payload["worker"]["status"], "disabled")
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
        self.assertIn("PULLWISE_WORKER_TOKEN", rotate.payload["install_command"])
        self.assertNotIn("--worker-token", rotate.payload["install_command"])
        self.assertNotIn(new_token, rotate.payload["install_command"])

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
        self.assertEqual(delete.status, HTTPStatus.OK)
        self.assertTrue(delete.payload["deleted"])
        self.assertIsNotNone(db.get_worker(worker_id, include_deleted=True)["deleted_at"])

        actions = [event["action"] for event in db.list_worker_audit_events(worker_id, limit=20)]
        for expected in ["update_worker", "disable_worker", "enable_worker", "rotate_worker_token", "delete_worker"]:
            self.assertIn(expected, actions)

    def test_admin_can_queue_worker_stop_and_uninstall_commands(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

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

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(heartbeat.payload["worker"]["status"], "disabled")
        self.assertEqual(heartbeat.payload["command"]["id"], stop_command["id"])

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

        uninstall_done = RouteHarness(
            f"/worker/commands/{uninstall_command['id']}/status",
            {"worker_id": worker_id, "status": "succeeded"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(uninstall_done, "POST")
        self.assertEqual(uninstall_done.status, HTTPStatus.OK)
        self.assertIsNone(db.get_worker(worker_id))
        self.assertIsNotNone(db.get_worker(worker_id, include_deleted=True)["deleted_at"])

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
        self.assertEqual(worker["max_concurrent_jobs"], 4)
        self.assertEqual(worker["running_jobs"], 2)
        self.assertIsNotNone(worker["last_heartbeat_at"])
        self.assertEqual(worker["doctor_status"], "ok")
        self.assertEqual(worker["codex_ready"], 1)
        self.assertEqual(worker["systemd_active"], 1)
        self.assertEqual(app.computed_worker_status(worker), "idle")

        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 4, "free_slots": 0, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "busy")
        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 0, "free_slots": 4, "doctor_status": "degraded", "codex_ready": 0, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "last_error": "internal stack", "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
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

    def test_status_capacity_increases_with_multiple_online_workers(self) -> None:
        payload_one, token_one = self.create_worker()
        worker_one_id = payload_one["worker_id"]
        payload_two, token_two = self.create_worker()
        worker_two_id = payload_two["worker_id"]

        for worker_id, token, capacity, running in (
            (worker_one_id, token_one, 4, 1),
            (worker_two_id, token_two, 2, 0),
        ):
            heartbeat = RouteHarness(
                "/worker/heartbeat",
                {
                    "worker_id": worker_id,
                    "provider": "codex",
                    "version": "0.1.0",
                    "max_concurrent_jobs": capacity,
                    "running_jobs": running,
                    "free_slots": capacity - running,
                    "doctor_status": "ok",
                    "codex_ready": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            app.PullwiseHandler.route(heartbeat, "POST")
            self.assertEqual(heartbeat.status, HTTPStatus.OK)

        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")

        self.assertEqual(public.status, HTTPStatus.OK)
        self.assertEqual(public.payload["onlineWorkerCount"], 2)
        self.assertEqual(public.payload["totalWorkerCount"], 2)
        self.assertEqual(public.payload["totalCapacity"], 6)
        self.assertEqual(public.payload["availableCapacity"], 5)
        self.assertNotIn("workers", public.payload)

    def test_worker_capacity_is_capped_for_admin_writes_and_heartbeat(self) -> None:
        create = RouteHarness(
            "/admin/workers",
            {"name": "Too large", "provider": "codex", "max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(create, "POST")
        self.assertEqual(create.status, HTTPStatus.BAD_REQUEST)

        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.BAD_REQUEST)

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 99, "running_jobs": 99, "free_slots": 99},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        stored = db.get_worker(worker_id)
        self.assertEqual(stored["max_concurrent_jobs"], 32)
        self.assertEqual(stored["running_jobs"], 32)
        self.assertEqual(stored["free_slots"], 32)
        self.assertEqual(stored["last_error"], "max_concurrent_jobs clamped to 32")

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
        self.assertEqual(stored["last_error"], "disk pressure; max_concurrent_jobs clamped to 32")

    def test_disabling_worker_blocks_job_progress_and_result_mutations(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        scan = {
            "id": "sc_active",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": worker_id,
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        disable = RouteHarness(f"/admin/workers/{worker_id}/disable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(disable, "POST")
        self.assertEqual(disable.status, HTTPStatus.OK)

        new_claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(new_claim, "POST")
        self.assertEqual(new_claim.status, HTTPStatus.UNAUTHORIZED)

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 70},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.UNAUTHORIZED)

        result_body = {
            "status": "done",
            **empty_audit_result_fields(),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "duration_ms": 1000,
            "attempt_id": f"{worker_id}-1",
            "result_checksum": "checksum-disabled-worker-finish",
        }
        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            result_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")

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
