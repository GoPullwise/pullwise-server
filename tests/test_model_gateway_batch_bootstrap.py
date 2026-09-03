from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, db, model_gateway_api
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayBatchBootstrapTest(unittest.TestCase):
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
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_batch_workers_exchange_distinct_single_use_bootstrap_credentials(self) -> None:
        with patch.object(model_gateway_api, "provider_secret_writer", return_value=RecordingSecretWriter()):
            control = ModelGatewayControlPlane(
                connect_factory=db.connect,
                secret_writer=model_gateway_api.provider_secret_writer(),
                clock=lambda: app.now(),
            )
            control.create_provider_connection(
                actor_user_id="usr_admin",
                request_id="req_provider",
                payload={
                    "provider_connection_id": "openai-production",
                    "display_name": "OpenAI Production",
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": "batch-provider-secret",
                },
            )
        control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{
                    "route_id": "gpt-primary",
                    "provider_connection_id": "openai-production",
                    "provider": "pullwise-gateway",
                    "model_alias": "gpt-reviewer",
                    "upstream_model": "gpt-5.5",
                    "api": "openai-completions",
                    "enabled": True,
                }],
            },
        )
        control.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": 1,
            },
        )
        batch = RouteHarness(
            "/admin/worker-pools/reviewers-primary/workers/batch",
            {"count": 2, "name_prefix": "Reviewer", "region": "cn-east", "version": "0.10.24"},
            cookie="pw_session=ses_admin",
        )
        app.PullwiseHandler.route(batch, "POST")

        self.assertEqual(batch.status, 201, batch.payload)
        self.assertEqual(len(batch.payload["items"]), 2)
        first, second = batch.payload["items"]
        self.assertNotEqual(first["workerId"], second["workerId"])
        self.assertNotEqual(first["bootstrapToken"], second["bootstrapToken"])
        self.assertNotIn("worker_token", json.dumps(batch.payload))
        connection = sqlite3.connect(self.db_path)
        try:
            token_hashes = connection.execute(
                "SELECT token_hash FROM workers ORDER BY worker_id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(token_hashes, [(None,), (None,)])
        self.assertNotIn(
            first["bootstrapToken"].encode("utf-8"),
            PathLikeDatabase(self.db_path).all_bytes(),
        )

        exchange = RouteHarness(
            "/v1/workers/bootstrap",
            {
                "schema_id": "pullwise-worker-bootstrap-exchange/v1",
                "worker_id": first["workerId"],
            },
            headers={"Authorization": f"Bearer {first['bootstrapToken']}"},
        )
        app.PullwiseHandler.route(exchange, "POST")
        self.assertEqual(exchange.status, 200, exchange.payload)
        self.assertEqual(exchange.payload["worker_id"], first["workerId"])
        self.assertTrue(exchange.payload["worker_token"].startswith("pww_"))

        replay = RouteHarness(
            "/v1/workers/bootstrap",
            {
                "schema_id": "pullwise-worker-bootstrap-exchange/v1",
                "worker_id": first["workerId"],
            },
            headers={"Authorization": f"Bearer {first['bootstrapToken']}"},
        )
        app.PullwiseHandler.route(replay, "POST")
        self.assertEqual(replay.status, 401, replay.payload)


class PathLikeDatabase:
    def __init__(self, path: str) -> None:
        self.path = path

    def all_bytes(self) -> bytes:
        with open(self.path, "rb") as database:
            return database.read()


if __name__ == "__main__":
    unittest.main()
