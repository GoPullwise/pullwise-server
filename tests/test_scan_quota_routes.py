from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


class RouteHarness(app.PullwiseHandler):
    def __init__(self, body: dict | None = None, cookie: str = "", path: str = "/scans") -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie}
        self.payload = None
        self.status = None
        self.headers_out = {}

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def reset_state() -> None:
    app.USERS = {}
    app.SESSIONS = {}
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.SCANS = []
    app.ISSUES = []
    app.STATE_LOADED = True
    app.STATE_DIRTY = False


def seed_user(user_id: str, session_id: str, *, installation_id: str = "111", repo_id: str = "123") -> str:
    app.USERS[user_id] = {
        "id": user_id,
        "name": user_id,
        "email": f"{user_id}@example.com",
        "createdAt": app.now(),
        "providers": ["github"],
        "githubId": user_id.removeprefix("usr_"),
        "githubLogin": user_id,
        "githubRepositoryAccess": {
            "mode": "github-app",
            "scope": "selected",
            "repositorySelection": "selected",
            "authorizedUserId": user_id,
            "authorizedGithubId": user_id.removeprefix("usr_"),
            "authorizedGithubLogin": user_id,
            "installationId": installation_id,
            "installationIds": [installation_id],
            "installationAccount": "acme",
            "installationAccounts": ["acme"],
            "repositories": ["acme/api"],
            "repositoryItems": [
                {
                    "id": repo_id,
                    "githubRepoId": repo_id,
                    "name": "api",
                    "fullName": "acme/api",
                    "installationId": installation_id,
                    "installationAccount": "acme",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/acme/api.git",
                }
            ],
            "repositoriesNeedSync": False,
        },
    }
    app.SESSIONS[session_id] = {
        "id": session_id,
        "userId": user_id,
        "createdAt": app.now(),
        "expiresAt": app.now() + 3600,
    }
    return f"pw_session={session_id}"


class ScanQuotaRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_REVIEW_PROVIDER": "mock",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "10",
                "PULLWISE_FREE_REPO_REVIEW_LIMIT": "1",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_same_github_repo_id_shares_repository_quota_across_users(self) -> None:
        first_cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        second_cookie = seed_user("usr_b", "ses_b", installation_id="222", repo_id="123")

        first = RouteHarness({"repo": "acme/api", "requestId": "req_a"}, cookie=first_cookie)
        second = RouteHarness({"repo": "acme/api", "requestId": "req_b"}, cookie=second_cookie)

        with patch.object(app.worker, "start_scan"):
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.PAYMENT_REQUIRED)
        self.assertEqual(second.payload["code"], "QUOTA_EXCEEDED_REPOSITORY")
        self.assertEqual(first.payload["githubRepoId"], "123")
        self.assertEqual(first.payload["repoUsage"]["used"], 1)
        self.assertEqual(first.payload["billingUsage"]["period"], first.payload["repoUsage"]["period"])
        self.assertEqual(first.payload["billingUsage"]["resetAt"], first.payload["repoUsage"]["resetAt"])

    def test_same_installation_shares_user_quota_across_repos(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "user-quota.sqlite3"),
                "PULLWISE_REVIEW_PROVIDER": "mock",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "1",
                "PULLWISE_FREE_REPO_REVIEW_LIMIT": "10",
            },
            clear=True,
        ):
            first_cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
            second_cookie = seed_user("usr_b", "ses_b", installation_id="111", repo_id="456")
            app.USERS["usr_b"]["githubRepositoryAccess"]["repositories"] = ["acme/other"]
            app.USERS["usr_b"]["githubRepositoryAccess"]["repositoryItems"][0].update(
                {"id": "456", "githubRepoId": "456", "name": "other", "fullName": "acme/other"}
            )

            first = RouteHarness({"repo": "acme/api", "requestId": "req_a"}, cookie=first_cookie)
            second = RouteHarness({"repo": "acme/other", "requestId": "req_b"}, cookie=second_cookie)

            with patch.object(app.worker, "start_scan"):
                app.PullwiseHandler.route(first, "POST")
                app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.CREATED)

    def test_default_user_quota_allows_one_prior_scan_plus_five_distinct_repos(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "default-user-quota.sqlite3"),
                "PULLWISE_REVIEW_PROVIDER": "mock",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "10",
                "PULLWISE_FREE_REPO_REVIEW_LIMIT": "3",
            },
            clear=True,
        ):
            cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="100")
            github_access = app.USERS["usr_a"]["githubRepositoryAccess"]
            repository_items = []
            for index in range(6):
                repo_id = str(100 + index)
                full_name = f"acme/repo-{index}"
                repository_items.append(
                    {
                        "id": repo_id,
                        "githubRepoId": repo_id,
                        "name": f"repo-{index}",
                        "fullName": full_name,
                        "installationId": "111",
                        "installationAccount": "acme",
                        "defaultBranch": "main",
                        "cloneUrl": f"https://github.com/{full_name}.git",
                    }
                )
            github_access["repositories"] = [item["fullName"] for item in repository_items]
            github_access["repositoryItems"] = repository_items

            handlers = [
                RouteHarness({"repo": item["fullName"], "requestId": f"req_{index}"}, cookie=cookie)
                for index, item in enumerate(repository_items)
            ]
            with patch.object(app.worker, "start_scan"):
                for handler in handlers:
                    app.PullwiseHandler.route(handler, "POST")
            repositories = RouteHarness(cookie=cookie, path="/repositories")
            app.PullwiseHandler.route(repositories, "GET")

        self.assertEqual([handler.status for handler in handlers], [HTTPStatus.CREATED] * 6)
        self.assertEqual(handlers[-1].payload["billingUsage"]["used"], 6)
        self.assertEqual(handlers[-1].payload["billingUsage"]["limit"], 10)
        self.assertEqual(handlers[-1].payload["billingUsage"]["remaining"], 4)
        self.assertEqual(repositories.status, HTTPStatus.OK)
        self.assertEqual(repositories.payload["userQuota"]["used"], 6)
        self.assertEqual(repositories.payload["userQuota"]["remaining"], 4)
        self.assertEqual(len(app.SCANS), 6)

    def test_scan_preflight_reports_user_quota_without_creating_scans(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "preflight-user-quota.sqlite3"),
                "PULLWISE_REVIEW_PROVIDER": "mock",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "3",
                "PULLWISE_FREE_REPO_REVIEW_LIMIT": "3",
            },
            clear=True,
        ):
            cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="100")
            github_access = app.USERS["usr_a"]["githubRepositoryAccess"]
            repository_items = []
            for index in range(5):
                repo_id = str(100 + index)
                full_name = f"acme/repo-{index}"
                repository_items.append(
                    {
                        "id": repo_id,
                        "githubRepoId": repo_id,
                        "name": f"repo-{index}",
                        "fullName": full_name,
                        "installationId": "111",
                        "installationAccount": "acme",
                        "defaultBranch": "main",
                        "cloneUrl": f"https://github.com/{full_name}.git",
                    }
                )
            github_access["repositories"] = [item["fullName"] for item in repository_items]
            github_access["repositoryItems"] = repository_items

            first = RouteHarness({"repo": "acme/repo-0", "requestId": "req_0"}, cookie=cookie)
            with patch.object(app.worker, "start_scan"):
                app.PullwiseHandler.route(first, "POST")
            preflight = RouteHarness(
                {
                    "repositories": [
                        {"repo": item["fullName"], "branch": "main"}
                        for item in repository_items[1:]
                    ]
                },
                cookie=cookie,
                path="/scans/preflight",
            )
            app.PullwiseHandler.route(preflight, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(preflight.status, HTTPStatus.OK)
        self.assertEqual(preflight.payload["requestedCount"], 4)
        self.assertEqual(preflight.payload["allowedCount"], 2)
        self.assertEqual(preflight.payload["userQuota"]["used"], 1)
        self.assertEqual(preflight.payload["userQuota"]["remaining"], 2)
        self.assertEqual(len(preflight.payload["repositories"]), 4)
        self.assertEqual(len(app.SCANS), 1)

    def test_same_request_id_does_not_consume_quota_twice(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        first = RouteHarness({"repoId": "123", "requestId": "req_same"}, cookie=cookie)
        second = RouteHarness({"repoId": "123", "requestId": "req_same"}, cookie=cookie)

        with patch.object(app.worker, "start_scan") as start_scan:
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertEqual(first.payload["id"], second.payload["id"])
        self.assertEqual(first.payload["billingUsage"]["used"], 1)
        self.assertIn("jobId", first.payload)
        job = db.get_scan_job(first.payload["jobId"])
        self.assertIsNotNone(job)
        self.assertEqual(job["scan_id"], first.payload["id"])
        self.assertEqual(job["status"], "queued")
        start_scan.assert_not_called()

    def test_repo_without_stable_id_requires_repository_sync(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        item = app.USERS["usr_a"]["githubRepositoryAccess"]["repositoryItems"][0]
        item.pop("id")
        item.pop("githubRepoId")
        first = RouteHarness({"repo": "acme/api", "requestId": "req_missing_id"}, cookie=cookie)

        with patch.object(app.worker, "start_scan") as start_scan:
            app.PullwiseHandler.route(first, "POST")

        self.assertEqual(first.status, HTTPStatus.CONFLICT)
        self.assertEqual(first.payload["code"], "REPOSITORY_SYNC_REQUIRED")
        start_scan.assert_not_called()

    def test_body_workspace_id_is_ignored_for_quota_authority(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        handler = RouteHarness(
            {"repo": "acme/api", "workspaceId": "ws_attacker", "requestId": "req_workspace_spoof"},
            cookie=cookie,
        )

        with patch.object(app.worker, "start_scan"):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertNotIn("workspaceId", handler.payload)

    def test_repo_id_must_belong_to_current_authorized_repositories(self) -> None:
        seed_user("usr_owner", "ses_owner", installation_id="111", repo_id="123")
        other_cookie = seed_user("usr_other", "ses_other", installation_id="222", repo_id="456")
        handler = RouteHarness({"repoId": "123", "requestId": "req_foreign_repo"}, cookie=other_cookie)

        with patch.object(app.worker, "start_scan") as start_scan:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(handler.payload["code"], "REPOSITORY_NOT_AUTHORIZED")
        start_scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
