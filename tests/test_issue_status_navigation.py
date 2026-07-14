from __future__ import annotations

import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests import test_worker_pull_routes as worker_routes


class IssueStatusNavigationTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_WORKER_TOKEN": "worker-secret",
                "PULLWISE_WORKER_ID": "wk_1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        app.USERS = {}
        app.SESSIONS = {}
        app.SETTINGS = {}
        app.BILLING_EVENTS = {}
        app.BILLING_PENDING_UPDATES = []
        app.SCANS = []
        app.ISSUES = []
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        install_initialized_db_template(
            os.environ["PULLWISE_DB_PATH"], worker_token="worker-secret", worker_id="wk_1"
        )
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": app.now(),
            }
        )
        self.worker_auth = {"Authorization": "Bearer worker-secret"}

    def test_mark_all_fixed_survives_overview_scan_reconciliation(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_overview_status",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": 100,
            "queuedAt": 100,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        lease = worker_routes.RouteHarness(
            "/v1/workers/wk_1/lease",
            worker_routes.v1_worker_lease_payload(),
            headers=self.worker_auth,
        )
        app.PullwiseHandler.route(lease, "POST")
        self.assertEqual(lease.status, HTTPStatus.OK)
        job = lease.payload["job"]
        result_body = {
            "status": "done",
            "attempt_id": f"wk_1-{job['attempt']}",
            **worker_routes.audit_result_fields(
                [
                    worker_routes.audit_issue_card(
                        "Validate overview redirects",
                        issue_id="iss_overview_status",
                        severity="P1",
                        file="src/auth.py",
                        line=42,
                    )
                ]
            ),
            "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        }
        result = worker_routes.RouteHarness(
            f"/v1/review-runs/{job['run_id']}/result",
            result_body,
            headers=self.worker_auth,
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        cookie = {"Cookie": "pw_session=ses_owner"}
        issues_before = worker_routes.RouteHarness(
            "/issues?status=all&limit=50&sort=severity", headers=cookie
        )
        app.PullwiseHandler.route(issues_before, "GET")
        self.assertEqual(issues_before.payload["total"], 1)
        issue = issues_before.payload["items"][0]

        mark_all_fixed = worker_routes.RouteHarness(
            "/issues/status",
            {
                "updates": [
                    {
                        "id": issue["id"],
                        "status": "fixed",
                        **{
                            field: issue[field]
                            for field in app.ISSUE_STATUS_IDENTITY_FIELDS
                            if field in issue
                        },
                    }
                ]
            },
            headers=cookie,
        )
        app.PullwiseHandler.route(mark_all_fixed, "PATCH")
        self.assertEqual(mark_all_fixed.payload["items"][0]["status"], "fixed")

        overview_issues = worker_routes.RouteHarness(
            "/issues?status=open&limit=50&sort=severity", headers=cookie
        )
        app.PullwiseHandler.route(overview_issues, "GET")
        self.assertEqual(overview_issues.payload["total"], 0)

        overview_scans = worker_routes.RouteHarness("/scans?limit=50", headers=cookie)
        app.PullwiseHandler.route(overview_scans, "GET")
        self.assertEqual(overview_scans.status, HTTPStatus.OK)

        issues_after = worker_routes.RouteHarness(
            "/issues?status=all&limit=50&sort=severity", headers=cookie
        )
        app.PullwiseHandler.route(issues_after, "GET")
        self.assertEqual(issues_after.payload["items"][0]["status"], "fixed")

        overview_after = worker_routes.RouteHarness(
            "/issues?status=open&limit=50&sort=severity", headers=cookie
        )
        app.PullwiseHandler.route(overview_after, "GET")
        self.assertEqual(overview_after.payload["total"], 0)


if __name__ == "__main__":
    unittest.main()
