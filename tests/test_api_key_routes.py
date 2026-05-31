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
        self.assertTrue(handler.payload["key"].startswith("pwk_"))
        return cookie, handler.payload["key"]

    def test_session_user_can_create_list_and_revoke_api_keys(self) -> None:
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

        list_after_revoke = RouteHarness("/api-keys", cookie=cookie)
        app.PullwiseHandler.route(list_after_revoke, "GET")
        self.assertEqual(list_after_revoke.status, HTTPStatus.OK)
        self.assertEqual(list_after_revoke.payload["items"], [])

    def test_invalid_requested_api_key_scopes_are_rejected(self) -> None:
        cookie = seed_session()
        handler = RouteHarness("/api-keys", {"name": "Bad automation", "scopes": ["admin:all"]}, cookie=cookie)

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("scope", handler.payload["message"].lower())

        list_handler = RouteHarness("/api-keys", cookie=cookie)
        app.PullwiseHandler.route(list_handler, "GET")
        self.assertEqual(list_handler.payload["items"], [])

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
                {"requestId": "req_bad\r\nX-Test: bad", "idempotencyKey": "req_api", "branch": "main"},
                headers=auth,
            )
            app.PullwiseHandler.route(start, "POST")

        self.assertEqual(start.status, HTTPStatus.CREATED)
        self.assertEqual(start.payload["repoId"], repo["repoId"])
        self.assertNotIn("workspaceId", start.payload)
        self.assertEqual(app.SCANS[0]["apiKeyId"], repositories.payload["apiKey"]["id"])
        self.assertEqual(app.SCANS[0]["requestId"], "req_api")
        start_scan.assert_not_called()

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

    def test_api_key_cannot_scan_repository_record_outside_authorized_access(self) -> None:
        _cookie, key = self.create_api_key()
        victim = db.upsert_repository(
            {
                "id": db.repository_id_for_github_repo("456"),
                "github_repo_id": "456",
                "full_name": "victim/secret",
                "owner_login": "victim",
                "default_branch": "main",
                "private": True,
                "clone_url": "https://github.com/victim/secret.git",
            }
        )

        start = RouteHarness(
            f"/api/v1/repositories/{victim['id']}/scans",
            {"requestId": "req_victim"},
            headers={"Authorization": f"Bearer {key}"},
        )
        app.PullwiseHandler.route(start, "POST")

        self.assertEqual(start.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(start.payload["message"], "Repository is not authorized for this account.")
        self.assertEqual(app.SCANS, [])

    def test_api_key_rejects_request_id_reuse_for_different_repo(self) -> None:
        _cookie, key = self.create_api_key()
        other_repo = db.upsert_repository(
            {
                "id": db.repository_id_for_github_repo("456"),
                "github_repo_id": "456",
                "full_name": "acme/other",
                "owner_login": "acme",
                "default_branch": "main",
                "clone_url": "https://github.com/acme/other.git",
            }
        )
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositories"].append("acme/other")
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"].append(
            {
                "id": "456",
                "githubRepoId": "456",
                "name": "other",
                "fullName": "acme/other",
                "installationId": "111",
                "installationAccount": "acme",
                "repositorySelection": "selected",
                "defaultBranch": "main",
                "cloneUrl": "https://github.com/acme/other.git",
                "permissions": {"pull": True},
            }
        )
        auth = {"Authorization": f"Bearer {key}"}

        repositories = RouteHarness("/api/v1/repositories", headers=auth)
        app.PullwiseHandler.route(repositories, "GET")
        repo_ids = {item["fullName"]: item["repoId"] for item in repositories.payload["items"]}

        with patch.object(app.worker, "start_scan") as start_scan:
            first = RouteHarness(
                f"/api/v1/repositories/{repo_ids['acme/api']}/scans",
                {"requestId": "req_shared"},
                headers=auth,
            )
            second = RouteHarness(
                f"/api/v1/repositories/{repo_ids['acme/other']}/scans",
                {"requestId": "req_shared"},
                headers=auth,
            )
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.CONFLICT)
        self.assertEqual(second.payload["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(second.payload["repoId"], first.payload["repoId"])
        self.assertEqual(len([scan for scan in app.SCANS if scan.get("requestId") == "req_shared"]), 1)
        start_scan.assert_not_called()

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
        self.assertEqual(billing.payload["account"]["plan"], "free")


if __name__ == "__main__":
    unittest.main()
