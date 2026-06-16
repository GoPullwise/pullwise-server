from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db, system_config


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


def database_config(**overrides: object) -> dict:
    config = system_config.default_config()
    for path, value in overrides.items():
        current = config
        parts = path.split("__")
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = value
    return config


def quota_config(*, free_limit: int = 5) -> dict:
    return database_config(
        plans__free__userReviewLimit=free_limit,
        plans__free__repositoryReviewLimit=free_limit,
    )


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
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config_patcher = patch("pullwise_server.system_config.config", return_value=quota_config(free_limit=1))
        self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)

    def test_same_github_repo_id_shares_repository_quota_across_users(self) -> None:
        first_cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        second_cookie = seed_user("usr_b", "ses_b", installation_id="222", repo_id="123")

        first = RouteHarness({"repo": "acme/api", "requestId": "req_a"}, cookie=first_cookie)
        second = RouteHarness({"repo": "acme/api", "requestId": "req_b"}, cookie=second_cookie)

        app.PullwiseHandler.route(first, "POST")
        app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.PAYMENT_REQUIRED)
        self.assertEqual(second.payload["code"], "QUOTA_EXCEEDED_REPOSITORY")
        self.assertEqual(first.payload["githubRepoId"], "123")
        self.assertEqual(first.payload["repoUsage"]["used"], 0)
        self.assertEqual(first.payload["repoUsage"]["reserved"], 1)
        self.assertEqual(first.payload["repoUsage"]["remaining"], 0)
        self.assertEqual(first.payload["billingUsage"]["period"], first.payload["repoUsage"]["period"])
        self.assertEqual(first.payload["billingUsage"]["resetAt"], first.payload["repoUsage"]["resetAt"])

    def test_same_installation_shares_user_quota_across_repos(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "user-quota.sqlite3")}, clear=True),
            patch("pullwise_server.system_config.config", return_value=quota_config(free_limit=1)),
        ):
            first_cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
            second_cookie = seed_user("usr_b", "ses_b", installation_id="111", repo_id="456")
            app.USERS["usr_b"]["githubRepositoryAccess"]["repositories"] = ["acme/other"]
            app.USERS["usr_b"]["githubRepositoryAccess"]["repositoryItems"][0].update(
                {"id": "456", "githubRepoId": "456", "name": "other", "fullName": "acme/other"}
            )

            first = RouteHarness({"repo": "acme/api", "requestId": "req_a"}, cookie=first_cookie)
            second = RouteHarness({"repo": "acme/other", "requestId": "req_b"}, cookie=second_cookie)

            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.CREATED)

    def test_default_user_quota_allows_five_distinct_repos_per_month(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "default-user-quota.sqlite3")}, clear=True),
            patch("pullwise_server.system_config.config", return_value=quota_config(free_limit=5)),
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
            for handler in handlers:
                app.PullwiseHandler.route(handler, "POST")
            repositories = RouteHarness(cookie=cookie, path="/repositories")
            app.PullwiseHandler.route(repositories, "GET")

        self.assertEqual([handler.status for handler in handlers], [HTTPStatus.CREATED] * 5 + [HTTPStatus.PAYMENT_REQUIRED])
        self.assertEqual(handlers[-1].payload["code"], "QUOTA_EXCEEDED_USER")
        self.assertEqual(handlers[-2].payload["billingUsage"]["used"], 0)
        self.assertEqual(handlers[-2].payload["billingUsage"]["reserved"], 5)
        self.assertEqual(handlers[-2].payload["billingUsage"]["limit"], 5)
        self.assertEqual(handlers[-2].payload["billingUsage"]["remaining"], 0)
        self.assertEqual(repositories.status, HTTPStatus.OK)
        self.assertEqual(repositories.payload["userQuota"]["used"], 0)
        self.assertEqual(repositories.payload["userQuota"]["reserved"], 5)
        self.assertEqual(repositories.payload["userQuota"]["remaining"], 0)
        self.assertEqual(len(app.SCANS), 5)

    def test_scan_preflight_reports_user_quota_without_creating_scans(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "preflight-user-quota.sqlite3")}, clear=True),
            patch("pullwise_server.system_config.config", return_value=quota_config(free_limit=3)),
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
        self.assertEqual(preflight.payload["userQuota"]["used"], 0)
        self.assertEqual(preflight.payload["userQuota"]["reserved"], 1)
        self.assertEqual(preflight.payload["userQuota"]["remaining"], 2)
        self.assertEqual(len(preflight.payload["repositories"]), 4)
        self.assertEqual(len(app.SCANS), 1)

    def test_repository_branches_route_lists_authorized_repo_branches(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        handler = RouteHarness(cookie=cookie, path="/repositories/123/branches")

        with (
            patch.object(app, "installation_token", return_value="ghs_installation") as token,
            patch.object(
                app.github_auth,
                "list_repository_branches",
                return_value=["main", "release/1.0"],
            ) as list_branches,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["repoId"], "123")
        self.assertEqual(handler.payload["repo"], "acme/api")
        self.assertEqual(handler.payload["defaultBranch"], "main")
        self.assertEqual(handler.payload["branches"], ["main", "release/1.0"])
        token.assert_called_once_with("111")
        list_branches.assert_called_once_with("ghs_installation", "acme/api")

    def test_scan_start_rejects_branch_not_returned_by_github(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        handler = RouteHarness(
            {"repoId": "123", "branch": "feature/not-authorized", "requestId": "req_bad_branch"},
            cookie=cookie,
        )

        with (
            patch.object(app, "installation_token", return_value="ghs_installation"),
            patch.object(app.github_auth, "list_repository_branches", return_value=["main", "develop"]),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["code"], "BRANCH_NOT_AVAILABLE")
        self.assertEqual(handler.payload["message"], "Selected branch is not available for this repository.")
        self.assertEqual(app.SCANS, [])

    def test_same_request_id_does_not_consume_quota_twice(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        first = RouteHarness({"repoId": "123", "requestId": "req_same"}, cookie=cookie)
        second = RouteHarness({"repoId": "123", "requestId": "req_same"}, cookie=cookie)

        app.PullwiseHandler.route(first, "POST")
        app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertEqual(first.payload["id"], second.payload["id"])
        self.assertEqual(first.payload["billingUsage"]["used"], 0)
        self.assertEqual(first.payload["billingUsage"]["reserved"], 1)
        self.assertEqual(first.payload["quotaState"], "reserved")
        self.assertIn("jobId", first.payload)
        job = db.get_scan_job(first.payload["jobId"])
        self.assertIsNotNone(job)
        self.assertEqual(job["scan_id"], first.payload["id"])
        self.assertEqual(job["status"], "queued")

    def test_cancel_releases_reserved_quota_and_records_billing_activity(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        first = RouteHarness({"repoId": "123", "requestId": "req_cancel"}, cookie=cookie)
        app.PullwiseHandler.route(first, "POST")
        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(first.payload["billingUsage"]["used"], 0)
        self.assertEqual(first.payload["billingUsage"]["reserved"], 1)

        cancel = RouteHarness({}, cookie=cookie, path=f"/scans/{first.payload['id']}/cancel")
        app.PullwiseHandler.route(cancel, "POST")

        self.assertEqual(cancel.status, HTTPStatus.OK)
        self.assertEqual(cancel.payload["status"], "cancelled")
        self.assertEqual(cancel.payload["quotaState"], "released")
        self.assertEqual(cancel.payload["billingUsage"]["used"], 0)
        self.assertEqual(cancel.payload["billingUsage"]["reserved"], 0)
        self.assertEqual(cancel.payload["billingUsage"]["remaining"], 1)
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            used, reserved = connection.execute(
                "SELECT COALESCE(SUM(used), 0), COALESCE(SUM(reserved), 0) FROM quota_buckets"
            ).fetchone()
        self.assertEqual(used, 0)
        self.assertEqual(reserved, 0)

        billing_plan = RouteHarness(cookie=cookie, path="/billing/plan")
        app.PullwiseHandler.route(billing_plan, "GET")
        self.assertEqual(billing_plan.status, HTTPStatus.OK)
        activity = billing_plan.payload["account"]["quotaActivity"]
        activity_keys = {(item["scanId"], item["action"]) for item in activity}
        self.assertIn((first.payload["id"], "reserved"), activity_keys)
        self.assertIn((first.payload["id"], "released"), activity_keys)

    def test_scan_start_rolls_back_quota_when_job_creation_fails(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        first = RouteHarness({"repoId": "123", "requestId": "req_job_failure"}, cookie=cookie)

        with patch.object(app, "create_scan_job_for_scan", side_effect=RuntimeError("boom")):
            app.PullwiseHandler.route(first, "POST")

        self.assertEqual(first.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(app.SCANS, [])
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            ledger_count = connection.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0]
            used_total = connection.execute("SELECT COALESCE(SUM(used), 0) FROM quota_buckets").fetchone()[0]
        self.assertEqual(ledger_count, 0)
        self.assertEqual(used_total, 0)

        retry = RouteHarness({"repoId": "123", "requestId": "req_job_failure"}, cookie=cookie)
        app.PullwiseHandler.route(retry, "POST")

        self.assertEqual(retry.status, HTTPStatus.CREATED)
        self.assertEqual(len(app.SCANS), 1)
        self.assertEqual(retry.payload["billingUsage"]["used"], 0)
        self.assertEqual(retry.payload["billingUsage"]["reserved"], 1)

    def test_repo_without_stable_id_requires_repository_sync(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        item = app.USERS["usr_a"]["githubRepositoryAccess"]["repositoryItems"][0]
        item.pop("id")
        item.pop("githubRepoId")
        first = RouteHarness({"repo": "acme/api", "requestId": "req_missing_id"}, cookie=cookie)

        app.PullwiseHandler.route(first, "POST")

        self.assertEqual(first.status, HTTPStatus.CONFLICT)
        self.assertEqual(first.payload["code"], "REPOSITORY_SYNC_REQUIRED")

    def test_body_workspace_id_is_ignored_for_quota_authority(self) -> None:
        cookie = seed_user("usr_a", "ses_a", installation_id="111", repo_id="123")
        handler = RouteHarness(
            {"repo": "acme/api", "workspaceId": "ws_attacker", "requestId": "req_workspace_spoof"},
            cookie=cookie,
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertNotIn("workspaceId", handler.payload)

    def test_repo_id_must_belong_to_current_authorized_repositories(self) -> None:
        seed_user("usr_owner", "ses_owner", installation_id="111", repo_id="123")
        other_cookie = seed_user("usr_other", "ses_other", installation_id="222", repo_id="456")
        handler = RouteHarness({"repoId": "123", "requestId": "req_foreign_repo"}, cookie=other_cookie)

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(handler.payload["code"], "REPOSITORY_NOT_AUTHORIZED")


if __name__ == "__main__":
    unittest.main()
