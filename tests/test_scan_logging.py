from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, scan_logging


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, cookie: str = "") -> None:
        self.path = path
        self._body = body or {}
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class ScanLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patcher = patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3")}, clear=False)
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.ISSUES = []
        app.SCANS = [self._running_scan("sc_trace")]

    def test_log_event_sanitizes_non_json_serializable_fields(self) -> None:
        with self.assertLogs("pullwise_server.scan", level="INFO") as logs:
            try:
                scan_logging.log_event(
                    "metadata_seen",
                    labels={"beta", "alpha"},
                    token=b"\xfftoken",
                    metrics={"duration": float("nan"), "limit": float("inf")},
                    rows=[{"value": {"nested"}}],
                )
            except TypeError as exc:
                self.fail(f"log_event should sanitize non-JSON fields: {exc}")

        payload = json.loads(logs.output[0].split("scan_review ", 1)[1])
        self.assertEqual(payload["event"], "metadata_seen")
        self.assertEqual(payload["labels"], ["alpha", "beta"])
        self.assertEqual(payload["token"], "\ufffdtoken")
        self.assertEqual(payload["metrics"], {"duration": None, "limit": None})
        self.assertEqual(payload["rows"], [{"value": ["nested"]}])

    def test_scan_creation_logs_queued_scan(self) -> None:
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "createdAt": app.now(),
                "providers": ["github"],
                "githubRepositoryAccess": {
                    "mode": "github-app",
                    "scope": "selected",
                    "authorizedUserId": "usr_1",
                    "authorizedGithubId": "1",
                    "authorizedGithubLogin": "octocat",
                    "installationId": "111",
                    "repositories": ["owner/repo"],
                    "repositoryItems": [
                        {
                            "id": "repo_1",
                            "name": "repo",
                            "fullName": "owner/repo",
                            "installationId": "111",
                            "defaultBranch": "main",
                            "cloneUrl": "https://github.com/owner/repo.git",
                        },
                    ],
                    "repositoriesNeedSync": False,
                },
            }
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        app.SCANS = []
        handler = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_1"},
            cookie="pw_session=ses_1",
        )

        with self.assertLogs("pullwise_server.scan", level="INFO") as logs:
            app.PullwiseHandler.route(handler, "POST")

        output = "\n".join(logs.output)
        self.assertEqual(HTTPStatus.CREATED, handler.status)
        self.assertIn('"event":"scan_queued"', output)
        self.assertIn(f'"scanId":"{handler.payload["id"]}"', output)
        self.assertIn('"repo":"owner/repo"', output)
        self.assertIn('"requestId":"scan_req_1"', output)
        self.assertIn('"provider":"worker"', output)

    def _running_scan(self, scan_id: str) -> dict:
        return {
            "id": scan_id,
            "userId": "usr_1",
            "repo": "owner/repo",
            "branch": "main",
            "commit": "pending",
            "status": "running",
            "createdAt": 100,
            "startedAt": 100,
            "repoPath": None,
        }

    def _snapshot(self, scan_id: str) -> dict:
        return {
            "id": scan_id,
            "userId": "usr_1",
            "repo": "owner/repo",
            "branch": "main",
            "commit": "pending",
            "installationId": "123",
            "cloneUrl": "https://github.com/owner/repo.git",
            "repoPath": None,
            "startedAt": 100,
        }


if __name__ == "__main__":
    unittest.main()
