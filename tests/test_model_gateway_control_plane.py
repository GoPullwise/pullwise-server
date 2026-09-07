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
from tests.model_gateway_fakes import LeakyFailingSecretWriter, RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayControlPlaneTest(unittest.TestCase):
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
        self.admin_cookie = "pw_session=ses_admin"

    def test_provider_secret_is_write_only_and_never_persisted_in_server_db(self) -> None:
        sentinel = b"pw-provider-secret-SENTINEL-9d3d4b"
        secret_writer = RecordingSecretWriter()
        service = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=secret_writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_fixed",
        )

        result = service.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider_create",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": sentinel.decode("ascii"),
            },
        )

        self.assertEqual(len(secret_writer.writes), 1)
        secret_ref, written_secret = secret_writer.writes[0]
        self.assertEqual(written_secret, sentinel)
        self.assertTrue(secret_ref.startswith("provider/openai-production/"))
        self.assertEqual(
            result,
            {
                "providerConnectionId": "openai-production",
                "displayName": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpointOrigin": "https://api.openai.com",
                "status": "configured",
                "secretVersion": "version-1",
                "secretFingerprint": "sha256:0123456789ab",
                "validatedModels": [
                    "MiniMax-M2.7",
                    "deepseek-v4-flash",
                    "deepseek-v4-pro",
                    "gpt-5.1",
                    "gpt-5.5",
                    "gpt-5.6",
                    "gpt-5.7",
                ],
                "lastValidatedAt": 1_788_259_200,
                "lastRotatedAt": None,
                "createdAt": 1_788_259_200,
                "updatedAt": 1_788_259_200,
            },
        )

        serialized_result = json.dumps(result, sort_keys=True).encode("utf-8")
        self.assertNotIn(sentinel, serialized_result)

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            for (table_name,) in rows:
                if table_name.startswith("sqlite_"):
                    continue
                for row in connection.execute(f'SELECT * FROM "{table_name}"'):
                    for value in row:
                        if isinstance(value, str):
                            self.assertNotIn(sentinel.decode("ascii"), value)
                        elif isinstance(value, bytes):
                            self.assertNotIn(sentinel, value)
        finally:
            connection.close()

    def test_secret_writer_failure_discards_secret_bearing_exception_cause(self) -> None:
        sentinel = "provider-secret-SENTINEL-exception"
        service = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=LeakyFailingSecretWriter(sentinel),
            clock=lambda: 1_788_259_200,
        )
        with self.assertRaises(RuntimeError) as raised:
            service.create_provider_connection(
                actor_user_id="usr_admin",
                request_id="req_failure",
                payload={
                    "provider_connection_id": "openai-production",
                    "display_name": "OpenAI Production",
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": sentinel,
                },
            )
        self.assertEqual(str(raised.exception), "provider secret write failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(sentinel, repr(raised.exception))

    def test_admin_create_and_list_provider_connections_never_echo_the_secret(self) -> None:
        sentinel = "pw-http-secret-SENTINEL-7c1149"
        secret_writer = RecordingSecretWriter()
        with patch.object(model_gateway_api, "provider_secret_writer", return_value=secret_writer):
            created = RouteHarness(
                "/admin/provider-connections",
                {
                    "provider_connection_id": "openai-production",
                    "display_name": "OpenAI Production",
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": sentinel,
                },
                cookie=self.admin_cookie,
                headers={"X-Request-Id": "req_provider_http"},
            )
            app.PullwiseHandler.route(created, "POST")

        self.assertEqual(created.status, 201, created.payload)
        self.assertEqual(created.payload["providerConnection"]["status"], "configured")
        self.assertNotIn(sentinel, json.dumps(created.payload, sort_keys=True))
        self.assertEqual(secret_writer.writes[0][1], sentinel.encode("utf-8"))

        listed = RouteHarness("/admin/provider-connections", cookie=self.admin_cookie)
        app.PullwiseHandler.route(listed, "GET")
        self.assertEqual(listed.status, 200, listed.payload)
        self.assertEqual(
            listed.payload,
            {"items": [created.payload["providerConnection"]]},
        )
        self.assertNotIn(sentinel, json.dumps(listed.payload, sort_keys=True))

    def test_profile_set_publish_creates_deterministic_non_secret_revision(self) -> None:
        secret_writer = RecordingSecretWriter()
        identifiers = iter(range(10))
        service = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=secret_writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
        )
        service.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider_for_profile",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "provider-secret-not-for-manifest",
            },
        )

        published = service.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_publish",
            payload={
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

        self.assertEqual(published["profileSetId"], "reviewer-production")
        self.assertEqual(published["revision"], 1)
        self.assertEqual(published["status"], "published")
        self.assertRegex(published["manifestDigest"], r"^[0-9a-f]{64}$")
        self.assertEqual(published["manifest"]["routes"][0]["upstream_provider"], "openai")
        self.assertEqual(
            published["manifest"],
            {
                "schema_id": "pullwise-model-profile-set/v1",
                "profile_set_id": "reviewer-production",
                "revision": 1,
                "routes": [
                    {
                        "upstream_provider": "openai",
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
        serialized = json.dumps(published, sort_keys=True)
        self.assertNotIn("provider-secret-not-for-manifest", serialized)
        self.assertNotIn("secret_ref", serialized)
        editable_route = {key: value for key, value in published["manifest"]["routes"][0].items()
                          if key != "upstream_provider"}
        duplicate_alias = {
            "profile_set_id": "reviewer-production",
            "display_name": "Reviewer production",
            "routes": [
                {**editable_route},
                {
                    **editable_route,
                    "route_id": "gpt-secondary",
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "model_alias values must be unique"):
            service.publish_profile_set(
                actor_user_id="usr_admin",
                request_id="req_duplicate_alias",
                payload=duplicate_alias,
            )

    def test_admin_can_publish_profile_set_after_provider_connection_creation(self) -> None:
        secret_writer = RecordingSecretWriter()
        with patch.object(model_gateway_api, "provider_secret_writer", return_value=secret_writer):
            provider = RouteHarness(
                "/admin/provider-connections",
                {
                    "provider_connection_id": "openai-production",
                    "display_name": "OpenAI Production",
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": "admin-provider-secret",
                },
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(provider, "POST")
            self.assertEqual(provider.status, 201, provider.payload)

            profile = RouteHarness(
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
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(profile, "POST")

        self.assertEqual(profile.status, 201, profile.payload)
        self.assertEqual(profile.payload["profileSet"]["revision"], 1)
        self.assertNotIn("admin-provider-secret", json.dumps(profile.payload, sort_keys=True))

    def test_worker_pool_binding_resolves_one_published_desired_profile(self) -> None:
        identifiers = iter(range(20))
        service = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=RecordingSecretWriter(),
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
        )
        service.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "pool-provider-secret",
            },
        )
        profile = service.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile",
            payload={
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
        worker = db.create_worker({"name": "Pool worker", "provider": "unconfigured"})

        pool = service.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": profile["revision"],
            },
        )
        membership = service.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_pool_member",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        desired = service.worker_profile_assignment(worker["worker_id"])

        self.assertEqual(pool["desiredRevision"], 1)
        self.assertEqual(membership["workerPoolId"], "reviewers-primary")
        self.assertEqual(desired["workerId"], worker["worker_id"])
        self.assertEqual(desired["profileSetId"], "reviewer-production")
        self.assertEqual(desired["profileRevision"], 1)
        self.assertEqual(desired["manifestDigest"], profile["manifestDigest"])
        self.assertEqual(desired["routeIds"], ["gpt-primary"])
        self.assertNotIn("pool-provider-secret", json.dumps(desired, sort_keys=True))
