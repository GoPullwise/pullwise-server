from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server import app, db, model_gateway_api
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_tokens import GatewayTokenAuthority
from pullwise_server.model_gateway_introspection_client import (
    HttpGatewayRouteResolver,
    HttpGrantStatusResolver,
)
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayIntrospectionTest(unittest.TestCase):
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
                "PULLWISE_MODEL_GATEWAY_INTROSPECTION_TOKEN": "gateway-introspection-token",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_internal_introspection_resolves_current_worker_pool_generation(self) -> None:
        identifiers = iter(range(20))
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=RecordingSecretWriter(),
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
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
                "secret": "introspection-provider-secret",
            },
        )
        profile = control.publish_profile_set(
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
        worker = db.create_worker({"name": "Introspection worker", "provider": "unconfigured"})
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
        control.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        grant = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=Ed25519PrivateKey.generate(),
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: app.now(),
            jti_factory=lambda: "gtj_introspection",
        ).issue(
            worker_id=worker["worker_id"],
            profile_set_id="reviewer-production",
            profile_revision=1,
            manifest_digest=profile["manifestDigest"],
            route_ids=["gpt-primary"],
            generation=1,
        )
        handler = RouteHarness(
            "/internal/model-gateway/grants/introspect",
            {"jti": grant.jti, "token_hash": grant.token_hash},
            headers={"Authorization": "Bearer gateway-introspection-token"},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, 200, handler.payload)
        self.assertEqual(
            handler.payload,
            {
                "active": True,
                "worker_enabled": True,
                "generation": 1,
                "desired_profile_revision": 1,
            },
        )
        self.assertNotIn("introspection-provider-secret", repr(handler.payload))

        route = RouteHarness(
            "/internal/model-gateway/routes/resolve",
            {
                "profile_set_id": "reviewer-production",
                "profile_revision": 1,
                "model_alias": "gpt-reviewer",
            },
            headers={"Authorization": "Bearer gateway-introspection-token"},
        )
        app.PullwiseHandler.route(route, "POST")
        self.assertEqual(route.status, 200, route.payload)
        self.assertEqual(route.payload["route_id"], "gpt-primary")
        self.assertEqual(route.payload["manifest_digest"], profile["manifestDigest"])
        self.assertEqual(route.payload["secret_version"], "version-1")
        self.assertEqual(route.payload["endpoint_origin"], "https://api.openai.com")
        self.assertNotIn("introspection-provider-secret", repr(route.payload))

        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE provider_connections SET validated_models_json = ? "
                    "WHERE provider_connection_id = ?",
                    ('["gpt-no-longer-available"]', "openai-production"),
                )
        finally:
            connection.close()
        app.PullwiseHandler.route(route, "POST")
        self.assertEqual(route.status, 404)
        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE provider_connections SET validated_models_json = ? "
                    "WHERE provider_connection_id = ?",
                    ('["gpt-5.5"]', "openai-production"),
                )
        finally:
            connection.close()

        revoked = model_gateway_api.emergency_revoke_provider_connection(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_emergency_revoke",
            provider_connection_id="openai-production",
        )
        self.assertEqual(revoked["status"], "emergency_revoked")
        route_after_revoke = RouteHarness(
            "/internal/model-gateway/routes/resolve",
            {
                "profile_set_id": "reviewer-production",
                "profile_revision": 1,
                "model_alias": "gpt-reviewer",
            },
            headers={"Authorization": "Bearer gateway-introspection-token"},
        )
        app.PullwiseHandler.route(route_after_revoke, "POST")
        self.assertEqual(route_after_revoke.status, 404)
        after_revoke = RouteHarness(
            "/internal/model-gateway/grants/introspect",
            {"jti": grant.jti, "token_hash": grant.token_hash},
            headers={"Authorization": "Bearer gateway-introspection-token"},
        )
        app.PullwiseHandler.route(after_revoke, "POST")
        self.assertEqual(after_revoke.status, 200)
        self.assertFalse(after_revoke.payload["active"])

        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE gateway_token_grants SET revoked_at = NULL WHERE jti = ?",
                    (grant.jti,),
                )
        finally:
            connection.close()
        model_gateway_api.emergency_revoke_provider_connection(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_emergency_revoke_repeat",
            provider_connection_id="openai-production",
        )
        app.PullwiseHandler.route(after_revoke, "POST")
        self.assertFalse(after_revoke.payload["active"])
        connection = db.connect()
        try:
            revoked_at = connection.execute(
                "SELECT revoked_at FROM gateway_token_grants WHERE jti = ?",
                (grant.jti,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNotNone(revoked_at)

        rotated = model_gateway_api.rotate_worker_pool_gateway_tokens(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_rotate_gateway_tokens",
            worker_pool_id="reviewers-primary",
        )
        self.assertEqual(rotated["gatewayTokenGeneration"], 2)
        after_rotation = RouteHarness(
            "/internal/model-gateway/grants/introspect",
            {"jti": grant.jti, "token_hash": grant.token_hash},
            headers={"Authorization": "Bearer gateway-introspection-token"},
        )
        app.PullwiseHandler.route(after_rotation, "POST")
        self.assertEqual(after_rotation.status, 200)
        self.assertFalse(after_rotation.payload["active"])
        self.assertEqual(after_rotation.payload["generation"], 2)

    def test_gateway_introspection_client_uses_fixed_url_and_closed_response(self) -> None:
        calls: list[dict[str, object]] = []

        def transport(
            url: str,
            *,
            headers: dict[str, str],
            payload: dict[str, object],
            timeout_seconds: float,
        ) -> dict[str, object]:
            calls.append({
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            })
            return {
                "active": True,
                "worker_enabled": True,
                "generation": 4,
                "desired_profile_revision": 13,
            }

        resolver = HttpGrantStatusResolver(
            introspection_url="https://api.internal/internal/model-gateway/grants/introspect",
            introspection_token="gateway-introspection-token",
            transport=transport,
            timeout_seconds=2.0,
        )
        status = resolver("gtj_client", "e" * 64)

        self.assertTrue(status.active)
        self.assertEqual(status.generation, 4)
        self.assertEqual(status.desired_profile_revision, 13)
        self.assertEqual(
            calls[0]["url"],
            "https://api.internal/internal/model-gateway/grants/introspect",
        )
        self.assertNotIn("gateway-introspection-token", calls[0]["url"])
        self.assertNotIn("e" * 64, calls[0]["url"])

    def test_gateway_route_client_returns_closed_route_without_arbitrary_url_input(self) -> None:
        calls: list[dict[str, object]] = []

        def transport(url: str, **kwargs: object) -> dict[str, object]:
            calls.append({"url": url, **kwargs})
            return {
                "route_id": "gpt-primary",
                "profile_set_id": "reviewer-production",
                "profile_revision": 12,
                "manifest_digest": "a" * 64,
                "model_alias": "gpt-reviewer",
                "provider_connection_id": "openai-production",
                "upstream_provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "upstream_model": "gpt-5.5",
                "secret_ref": "provider/openai-production/secret_1",
                "secret_version": "version-1",
            }

        resolver = HttpGatewayRouteResolver(
            route_url="https://api.internal/internal/model-gateway/routes/resolve",
            introspection_token="gateway-introspection-token",
            transport=transport,
            timeout_seconds=2.0,
        )
        route = resolver.resolve("reviewer-production", 12, "gpt-reviewer")

        self.assertEqual(route.route_id, "gpt-primary")
        self.assertEqual(route.endpoint_origin, "https://api.openai.com")
        self.assertEqual(calls[0]["url"], "https://api.internal/internal/model-gateway/routes/resolve")
        self.assertEqual(
            calls[0]["payload"],
            {
                "profile_set_id": "reviewer-production",
                "profile_revision": 12,
                "model_alias": "gpt-reviewer",
            },
        )


if __name__ == "__main__":
    unittest.main()
