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
        self.binary_payload = b""
        self.content_type = ""
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.binary_payload = payload
        self.status = status
        self.content_type = content_type
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
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def create_api_key(self) -> tuple[str, str]:
        cookie = seed_session()
        handler = RouteHarness(
            "/api-keys",
            {
                "name": "Automation",
                "scopes": ["repositories:read"],
            },
            cookie=cookie,
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertTrue(handler.payload["key"].startswith("pwk_"))
        return cookie, handler.payload["key"]

    def create_api_key_with_scopes(self, scopes: list[str]) -> tuple[str, str]:
        cookie = seed_session()
        token = f"{app.API_KEY_PREFIX}scoped_test_token"
        db.create_api_key(
            {
                "id": "key_scoped_test",
                "user_id": "usr_1",
                "name": "Scoped automation",
                "key_prefix": app.api_key_prefix(token),
                "key_hash": app.api_key_hash(token),
                "scopes": scopes,
            }
        )
        return cookie, token

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

    def test_api_key_list_and_plaintext_create_responses_are_not_cacheable(self) -> None:
        cookie = seed_session()
        create = RouteHarness("/api-keys", {"name": "Automation"}, cookie=cookie)
        app.PullwiseHandler.route(create, "POST")

        list_handler = RouteHarness("/api-keys", cookie=cookie)
        app.PullwiseHandler.route(list_handler, "GET")

        for handler in (create, list_handler):
            self.assertEqual(handler.headers_out["Cache-Control"], "no-store")
            self.assertEqual(handler.headers_out["Pragma"], "no-cache")
            self.assertIn("Cookie", handler.headers_out["Vary"])
            self.assertIn("Authorization", handler.headers_out["Vary"])
            self.assertIn("X-Pullwise-Api-Key", handler.headers_out["Vary"])
        self.assertEqual(create.status, HTTPStatus.CREATED)
        self.assertTrue(create.payload["key"].startswith("pwk_"))
        self.assertEqual(list_handler.status, HTTPStatus.OK)
        self.assertNotIn("key", list_handler.payload["items"][0])

    def test_invalid_requested_api_key_scopes_are_rejected(self) -> None:
        cookie = seed_session()
        handler = RouteHarness("/api-keys", {"name": "Bad automation", "scopes": ["admin:all"]}, cookie=cookie)

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("scope", handler.payload["message"].lower())

        list_handler = RouteHarness("/api-keys", cookie=cookie)
        app.PullwiseHandler.route(list_handler, "GET")
        self.assertEqual(list_handler.payload["items"], [])

    def test_stored_empty_api_key_scopes_do_not_grant_default_permissions(self) -> None:
        _cookie, key = self.create_api_key_with_scopes([])
        handler = RouteHarness("/api/v1/repositories", headers={"Authorization": f"Bearer {key}"})

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertIn("repositories:read", handler.payload["error"]["message"])

    def test_checkout_session_provider_error_returns_bad_gateway(self) -> None:
        cookie = seed_session()
        handler = RouteHarness(
            "/billing/checkout-sessions",
            {"plan": "pro", "interval": "month"},
            cookie=cookie,
        )

        with patch.object(
            app.billing,
            "create_checkout_session",
            side_effect=app.billing.BillingProviderResponseError(
                "Creem checkout failed (status 400): Product not found. Trace ID: trace_123."
            ),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_GATEWAY)
        self.assertEqual(
            handler.payload["message"],
            "Creem checkout failed (status 400): Product not found. Trace ID: trace_123.",
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
