from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str = "/billing/plan", body: dict | None = None, cookie: str = "") -> None:
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


def seed_workspace_session() -> str:
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
                        "defaultBranch": "main",
                        "cloneUrl": "https://github.com/acme/api.git",
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


class WorkspaceBillingRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_billing_plan_returns_workspace_usage_and_account_alias(self) -> None:
        cookie = seed_workspace_session()
        workspace = db.upsert_workspace(
            {
                "id": db.workspace_id_for_installation("111"),
                "name": "acme",
                "github_app_installation_id": "111",
                "plan": "pro",
                "billing_status": "active",
                "billing_interval": "month",
            }
        )
        db.upsert_workspace_member(workspace["id"], "usr_1", role="admin")
        handler = RouteHarness(cookie=cookie)

        app.PullwiseHandler.handle_get(handler, "/billing/plan", {}, ["billing", "plan"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["workspace"]["id"], workspace["id"])
        self.assertEqual(handler.payload["workspace"]["plan"], "pro")
        self.assertEqual(handler.payload["workspace"]["usage"]["scope"], "workspace")
        self.assertTrue(handler.payload["account"]["deprecated"])

    def test_auth_session_exposes_current_workspace(self) -> None:
        cookie = seed_workspace_session()
        handler = RouteHarness("/auth/session", cookie=cookie)

        app.PullwiseHandler.handle_get(handler, "/auth/session", {}, ["auth", "session"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["currentWorkspace"]["githubAppInstallationId"], "111")
        self.assertEqual(handler.payload["workspaces"][0]["githubAppInstallationId"], "111")

    def test_repositories_payload_includes_resource_ids_and_quota(self) -> None:
        cookie = seed_workspace_session()
        handler = RouteHarness("/repositories", cookie=cookie)

        app.PullwiseHandler.handle_get(handler, "/repositories", {}, ["repositories"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        repo = handler.payload["items"][0]
        self.assertEqual(repo["githubRepoId"], "123")
        self.assertEqual(repo["workspaceId"], db.workspace_id_for_installation("111"))
        self.assertEqual(repo["repoId"], db.repository_id_for_github_repo("123"))
        self.assertEqual(repo["quota"]["scope"], "repository")

    def test_checkout_session_is_created_for_workspace_subject(self) -> None:
        cookie = seed_workspace_session()
        handler = RouteHarness(
            "/billing/checkout-sessions",
            {
                "successUrl": "https://app.pullwise.dev/?screen=billing&billing=success",
                "cancelUrl": "https://app.pullwise.dev/?screen=billing&billing=cancel",
            },
            cookie=cookie,
        )

        with patch(
            "pullwise_server.billing.create_checkout_session",
            return_value={"provider": "stripe", "id": "cs_1", "workspaceId": db.workspace_id_for_installation("111"), "url": "https://checkout.stripe.com/cs/test"},
        ) as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        subject = create.call_args.args[0]
        self.assertEqual(subject["id"], "usr_1")
        self.assertEqual(subject["workspaceId"], db.workspace_id_for_installation("111"))
        self.assertEqual(create.call_args.kwargs["workspace"]["id"], db.workspace_id_for_installation("111"))

    def test_webhook_with_workspace_id_updates_workspace_billing(self) -> None:
        workspace = db.upsert_workspace(
            {
                "id": "ws_123",
                "name": "acme",
                "github_app_installation_id": "111",
                "plan": "free",
            }
        )
        handler = RouteHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "workspaceId": workspace["id"],
                "userId": "usr_1",
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "subscriptionItemId": "si_1",
                "status": "active",
                "plan": "pro",
                "interval": "year",
                "eventType": "customer.subscription.updated",
                "eventId": "evt_workspace",
                "eventCreated": 100,
            },
        )

        updated = db.get_workspace(workspace["id"])
        self.assertEqual(updated["plan"], "pro")
        self.assertEqual(updated["billing_status"], "active")
        self.assertEqual(updated["billing_subscription_id"], "sub_1")

    def test_workspace_billing_update_ignores_unsafe_scalar_fields(self) -> None:
        workspace = db.upsert_workspace(
            {
                "id": "ws_123",
                "name": "acme",
                "github_app_installation_id": "111",
                "plan": "pro",
                "billing_provider": "stripe",
                "billing_customer_id": "cus_existing",
                "billing_subscription_id": "sub_existing",
                "billing_subscription_item_id": "si_existing",
                "billing_status": "active",
                "billing_interval": "month",
            }
        )
        handler = RouteHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "workspaceId": workspace["id"],
                "provider": "stripe\r\nX-Injected: bad",
                "customerId": "cus_bad\r\nX-Injected: bad",
                "subscriptionId": "sub_bad\r\nX-Injected: bad",
                "subscriptionItemId": "si_bad\r\nX-Injected: bad",
                "status": "past_due\r\nX-Injected: bad",
                "plan": "free\r\nX-Injected: bad",
                "interval": "year\r\nX-Injected: bad",
                "eventType": "customer.subscription.updated\r\nX-Injected: bad",
                "eventId": "evt_bad_workspace_scalars",
                "eventCreated": 300,
            },
        )

        updated = db.get_workspace(workspace["id"])
        self.assertEqual(updated["billing_provider"], "stripe")
        self.assertEqual(updated["billing_customer_id"], "cus_existing")
        self.assertEqual(updated["billing_subscription_id"], "sub_existing")
        self.assertEqual(updated["billing_subscription_item_id"], "si_existing")
        self.assertEqual(updated["billing_status"], "active")
        self.assertEqual(updated["plan"], "pro")
        self.assertEqual(updated["billing_interval"], "month")
        self.assertIsNone(app.BILLING_EVENTS["evt_bad_workspace_scalars"]["eventType"])

    def test_legacy_user_billing_usage_migrates_to_workspace_bucket(self) -> None:
        cookie = seed_workspace_session()
        app.USERS["usr_1"]["billingUsage"] = {
            "period": app.current_review_usage_period(),
            "plan": "free",
            "used": 2,
        }
        handler = RouteHarness(cookie=cookie)

        app.PullwiseHandler.handle_get(handler, "/billing/plan", {}, ["billing", "plan"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["workspace"]["usage"]["used"], 2)


if __name__ == "__main__":
    unittest.main()
