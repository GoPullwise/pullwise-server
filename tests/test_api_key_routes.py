from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


class RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        *,
        cookie: str = "",
        headers: dict | None = None,
    ) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
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

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def seed_session() -> str:
    app.USERS = {
        "usr_1": {
            "id": "usr_1",
            "name": "Dev",
            "email": "dev@example.com",
            "createdAt": app.now(),
            "providers": ["github"],
            "githubId": "1",
            "githubLogin": "dev",
            "githubRepositoryAccess": {
                "mode": "github-app",
                "scope": "selected",
                "repositorySelection": "selected",
                "authorizedUserId": "usr_1",
                "authorizedGithubId": "1",
                "authorizedGithubLogin": "dev",
                "installationId": "111",
                "installationIds": ["111"],
                "installationAccount": "acme",
                "installationAccounts": ["acme"],
                "repositories": ["acme/api"],
                "repositoryItems": [
                    {
                        "id": "123",
                        "githubRepoId": "123",
                        "name": "api",
                        "fullName": "acme/api",
                        "installationId": "111",
                        "installationAccount": "acme",
                        "repositorySelection": "selected",
                        "defaultBranch": "main",
                        "cloneUrl": "https://github.com/acme/api.git",
                        "permissions": {"pull": True},
                    }
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
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.SCANS = []
    app.ISSUES = []
    app.STATE_LOADED = True
    app.STATE_DIRTY = False
    return "pw_session=ses_1"


class ApiKeyRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
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
                "PULLWISE_RATE_LIMIT_ENABLED": "false",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def create_api_key(self) -> tuple[str, str]:
        cookie = seed_session()
        handler = RouteHarness("/api-keys", {"name": "Automation"}, cookie=cookie)

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["workspaceId"], db.workspace_id_for_installation("111"))
        self.assertTrue(handler.payload["key"].startswith("pwk_"))
        return cookie, handler.payload["key"]

    def test_session_user_can_create_list_and_revoke_workspace_api_keys(self) -> None:
        cookie, key = self.create_api_key()
        list_handler = RouteHarness("/api-keys", cookie=cookie)

        app.PullwiseHandler.route(list_handler, "GET")

        self.assertEqual(list_handler.status, HTTPStatus.OK)
        self.assertEqual(list_handler.payload["items"][0]["name"], "Automation")
        self.assertNotIn("key", list_handler.payload["items"][0])

        key_id = list_handler.payload["items"][0]["id"]
        revoke = RouteHarness(f"/api-keys/{key_id}", cookie=cookie)
        app.PullwiseHandler.route(revoke, "DELETE")
        self.assertEqual(revoke.status, HTTPStatus.OK)

        denied = RouteHarness("/api/v1/repositories", headers={"Authorization": f"Bearer {key}"})
        app.PullwiseHandler.route(denied, "GET")
        self.assertEqual(denied.status, HTTPStatus.UNAUTHORIZED)

    def test_api_key_lists_repositories_and_controls_scan_by_repo_id(self) -> None:
        _cookie, key = self.create_api_key()
        auth = {"Authorization": f"Bearer {key}"}
        repositories = RouteHarness("/api/v1/repositories", headers=auth)

        app.PullwiseHandler.route(repositories, "GET")

        self.assertEqual(repositories.status, HTTPStatus.OK)
        repo = repositories.payload["items"][0]
        self.assertEqual(repo["repoId"], db.repository_id_for_github_repo("123"))
        self.assertEqual(repo["fullName"], "acme/api")
        self.assertEqual(repo["quota"]["scope"], "repository")

        with patch.object(app.worker, "start_scan") as start_scan:
            start = RouteHarness(
                f"/api/v1/repositories/{repo['repoId']}/scans",
                {"requestId": "req_api", "branch": "main"},
                headers=auth,
            )
            app.PullwiseHandler.route(start, "POST")

        self.assertEqual(start.status, HTTPStatus.CREATED)
        self.assertEqual(start.payload["repoId"], repo["repoId"])
        self.assertEqual(start.payload["workspaceId"], db.workspace_id_for_installation("111"))
        self.assertEqual(app.SCANS[0]["apiKeyId"], repositories.payload["apiKey"]["id"])
        start_scan.assert_called_once_with(start.payload["id"])

        status = RouteHarness(f"/api/v1/repositories/{repo['repoId']}/scans/current", headers=auth)
        app.PullwiseHandler.route(status, "GET")
        self.assertEqual(status.status, HTTPStatus.OK)
        self.assertEqual(status.payload["status"], "queued")
        self.assertEqual(status.payload["scan"]["id"], start.payload["id"])

        quota_handler = RouteHarness(f"/api/v1/repositories/{repo['repoId']}/quota", headers=auth)
        app.PullwiseHandler.route(quota_handler, "GET")
        self.assertEqual(quota_handler.status, HTTPStatus.OK)
        self.assertEqual(quota_handler.payload["repository"]["remaining"], 2)

        stop = RouteHarness(f"/api/v1/repositories/{repo['repoId']}/scans/stop", headers=auth)
        app.PullwiseHandler.route(stop, "POST")
        self.assertEqual(stop.status, HTTPStatus.OK)
        self.assertEqual(stop.payload["status"], "cancelled")

    def test_user_can_create_manual_workspace_for_dashboard_switcher(self) -> None:
        cookie = seed_session()
        handler = RouteHarness("/workspaces", {"name": "Platform"}, cookie=cookie)

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["name"], "Platform")
        self.assertEqual(handler.payload["role"], "owner")
        self.assertTrue(db.user_is_workspace_member(handler.payload["id"], "usr_1"))

    def test_pricing_api_docs_and_dashboard_overview_contracts_are_available(self) -> None:
        cookie = seed_session()
        pricing = RouteHarness("/pricing", cookie=cookie)
        docs = RouteHarness("/api-docs")
        overview = RouteHarness("/dashboard/overview", cookie=cookie)

        app.PullwiseHandler.route(pricing, "GET")
        app.PullwiseHandler.route(docs, "GET")
        app.PullwiseHandler.route(overview, "GET")

        self.assertEqual(pricing.status, HTTPStatus.OK)
        self.assertEqual(pricing.payload["page"]["checkoutAction"]["href"], "/billing/checkout-sessions")
        self.assertEqual(docs.status, HTTPStatus.OK)
        self.assertIn("/api/v1/repositories/{repoId}/quota", [item["path"] for item in docs.payload["endpoints"]])
        self.assertEqual(overview.status, HTTPStatus.OK)
        self.assertIsNone(overview.payload["scope"]["repoId"])
        self.assertEqual(overview.payload["authorizedRepositories"]["href"], "/repositories")
        self.assertEqual(
            overview.payload["authorizedRepositories"]["items"][0]["href"],
            f"/repositories/{db.repository_id_for_github_repo('123')}",
        )

    def test_billing_page_points_subscription_action_to_pricing(self) -> None:
        cookie = seed_session()
        billing = RouteHarness("/billing", cookie=cookie)

        app.PullwiseHandler.route(billing, "GET")

        self.assertEqual(billing.status, HTTPStatus.OK)
        self.assertEqual(billing.payload["page"]["subscriptionAction"]["href"], "/pricing")
        self.assertIsNone(billing.payload["page"]["checkoutAction"])
        self.assertEqual(billing.payload["workspace"]["id"], db.workspace_id_for_installation("111"))


if __name__ == "__main__":
    unittest.main()
