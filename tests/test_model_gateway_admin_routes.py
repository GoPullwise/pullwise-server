from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, db, model_gateway_api
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayAdminRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": self.db_path,
                "PULLWISE_ADMIN_USER_IDS": "usr_admin",
                "PULLWISE_ADMIN_EMAILS": "admin@example.com",
                "PULLWISE_SERVER_URL": "http://localhost:8080",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)
        self.admin_cookie = "pw_session=ses_admin"

    def post(self, path: str, body: dict) -> RouteHarness:
        handler = RouteHarness(path, body, cookie=self.admin_cookie)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def test_admin_creates_pool_binds_worker_and_reads_gateway_snapshot(self) -> None:
        sentinel = "admin-route-provider-secret"
        secret_writer = RecordingSecretWriter()
        with patch.object(
            model_gateway_api,
            "provider_secret_writer",
            return_value=secret_writer,
        ):
            provider = self.post(
                "/admin/provider-connections",
                {
                    "provider_connection_id": "openai-production",
                    "display_name": "OpenAI Production",
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": sentinel,
                },
            )
            self.assertEqual(provider.status, 201, provider.payload)
            profile = self.post(
                "/admin/profile-sets",
                {
                    "profile_set_id": "reviewer-production",
                    "display_name": "Reviewer production",
                    "routes": [
                        {
                            "route_id": "gpt-primary",
                            "provider_connection_id": "openai-production",
                            "provider": "pullwise-gateway",
                            "model_alias": "gpt-reviewer",
                            "upstream_model": "gpt-5.5",
                            "api": "openai-completions",
                            "enabled": True,
                        }
                    ],
                },
            )
            self.assertEqual(profile.status, 201, profile.payload)
        with patch.object(model_gateway_api, "provider_secret_writer", return_value=secret_writer):
            staged = self.post(
                "/admin/provider-connections/openai-production/rotation/stage",
                {"secret": "rotated-provider-secret-SENTINEL"},
            )
            self.assertEqual(staged.status, 200, staged.payload)
            self.assertEqual(staged.payload["providerConnection"]["status"], "rotation_staged")
            self.assertNotIn("rotated-provider-secret-SENTINEL", json.dumps(staged.payload))
            promoted = self.post(
                "/admin/provider-connections/openai-production/rotation/promote",
                {},
            )
            self.assertEqual(promoted.status, 200, promoted.payload)
            unconfirmed_retirement = self.post(
                "/admin/provider-connections/openai-production/rotation/retire",
                {},
            )
            self.assertEqual(unconfirmed_retirement.status, 400)
            retired = self.post(
                "/admin/provider-connections/openai-production/rotation/retire",
                {"upstream_revoked": True},
            )
            self.assertEqual(retired.status, 200, retired.payload)
            self.assertEqual(retired.payload["providerConnection"]["status"], "configured")
        worker = self.post("/admin/workers", {"name": "Pool member"})
        self.assertEqual(worker.status, 201, worker.payload)
        worker_id = worker.payload["worker_id"]
        pool = self.post(
            "/admin/worker-pools",
            {
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": 1,
            },
        )
        self.assertEqual(pool.status, 201, pool.payload)
        bound = self.post(
            "/admin/worker-pools/reviewers-primary/members",
            {"worker_id": worker_id},
        )
        self.assertEqual(bound.status, 200, bound.payload)

        snapshot = RouteHarness("/admin/model-gateway", cookie=self.admin_cookie)
        app.PullwiseHandler.route(snapshot, "GET")

        self.assertEqual(snapshot.status, 200, snapshot.payload)
        self.assertEqual(len(snapshot.payload["providerConnections"]), 1)
        self.assertEqual(snapshot.payload["profileSets"][0]["activeRevision"], 1)
        self.assertEqual(snapshot.payload["workerPools"][0]["workerPoolId"], "reviewers-primary")
        self.assertEqual(snapshot.payload["workerPools"][0]["memberCount"], 1)
        self.assertEqual(snapshot.payload["workerPools"][0]["readyMemberCount"], 0)
        self.assertNotIn(sentinel, json.dumps(snapshot.payload, sort_keys=True))

        blocked_removal = self.post(
            "/admin/provider-connections/openai-production/removal/prepare",
            {},
        )
        self.assertEqual(blocked_removal.status, 409, blocked_removal.payload)
        self.assertIn("profile_set:reviewer-production@1", blocked_removal.payload["dependencies"])
        self.assertIn("worker_pool:reviewers-primary", blocked_removal.payload["dependencies"])

        emergency = self.post(
            "/admin/provider-connections/openai-production/emergency-revoke",
            {},
        )
        self.assertEqual(emergency.status, 200, emergency.payload)
        self.assertEqual(
            emergency.payload["providerConnection"]["status"],
            "emergency_revoked",
        )

        rotated = self.post(
            "/admin/worker-pools/reviewers-primary/rotate-gateway-tokens",
            {},
        )
        self.assertEqual(rotated.status, 200, rotated.payload)
        self.assertEqual(rotated.payload["workerPool"]["gatewayTokenGeneration"], 2)

    def test_admin_can_retry_durable_secret_cleanup_without_exposing_references(self) -> None:
        with patch.object(
            model_gateway_api,
            "retry_pending_secret_cleanup",
            return_value={"attempted": 2, "retired": 1, "pending": 1},
        ) as retry:
            response = self.post("/admin/model-gateway/secret-cleanup/retry", {})

        self.assertEqual(response.status, 200, response.payload)
        self.assertEqual(
            response.payload,
            {"secretCleanup": {"attempted": 2, "retired": 1, "pending": 1}},
        )
        retry.assert_called_once_with(connect_factory=db.connect)
        self.assertNotIn("secret_ref", json.dumps(response.payload))


if __name__ == "__main__":
    unittest.main()
