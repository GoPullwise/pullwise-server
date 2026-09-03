from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server import app, db, model_gateway_api
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_tokens import (
    GatewayGrantStatus,
    GatewayTokenAuthority,
    GatewayTokenVerifier,
)
from pullwise_server.model_gateway_worker_profiles import WorkerProfileIssuer
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayWorkerProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_server_builds_profile_issuer_only_from_external_signing_key_config(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        key_path = Path(self.temp_dir.name) / "gateway-signing.pem"
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        with patch.dict(
            os.environ,
            {
                "PULLWISE_MODEL_GATEWAY_SIGNING_KEY_PATH": str(key_path),
                "PULLWISE_MODEL_GATEWAY_SIGNING_KEY_ID": "gateway-signing-2026-09",
                "PULLWISE_MODEL_GATEWAY_TOKEN_ISSUER": "https://api.pull-wise.com",
                "PULLWISE_MODEL_GATEWAY_URL": "https://models.pull-wise.com/v1",
                "PULLWISE_MODEL_GATEWAY_TOKEN_TTL_SECONDS": "300",
            },
            clear=False,
        ):
            issuer = model_gateway_api.worker_profile_issuer(connect_factory=db.connect)
            trust = model_gateway_api.gateway_manifest_trust()
        self.assertIsInstance(issuer, WorkerProfileIssuer)
        self.assertEqual(trust["schema_id"], "pullwise-model-gateway-manifest-trust/v1")
        self.assertEqual(trust["kid"], "gateway-signing-2026-09")
        self.assertEqual(trust["alg"], "Ed25519")
        self.assertIn("BEGIN PUBLIC KEY", trust["public_key_pem"])
        self.assertNotIn("PRIVATE KEY", trust["public_key_pem"])
        self.assertRegex(trust["fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_assigned_worker_receives_signed_manifest_and_distinct_gateway_token(self) -> None:
        identifiers = iter(range(20))
        control_plane = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=RecordingSecretWriter(),
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
        )
        control_plane.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "upstream-secret-must-not-leak",
            },
        )
        profile = control_plane.publish_profile_set(
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
        worker = db.create_worker({"name": "Bound worker", "provider": "unconfigured"})
        control_plane.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": profile["revision"],
            },
        )
        control_plane.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        private_key = Ed25519PrivateKey.generate()
        token_authority = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=private_key,
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: 1_788_259_200,
            jti_factory=lambda: "gtj_worker_profile",
        )

        issuer = WorkerProfileIssuer(
            connect_factory=db.connect,
            token_authority=token_authority,
            manifest_private_key=private_key,
            key_id="gateway-signing-2026-09",
            gateway_base_url="https://models.pull-wise.com/v1",
        )
        issued = issuer.issue(worker["worker_id"])

        self.assertEqual(issued["schema_id"], "pullwise-worker-model-profile/v1")
        self.assertEqual(issued["worker_id"], worker["worker_id"])
        self.assertEqual(issued["profile_set_id"], "reviewer-production")
        self.assertEqual(issued["profile_revision"], 1)
        self.assertEqual(issued["manifest_digest"], profile["manifestDigest"])
        self.assertEqual(
            issued["gateway"]["base_url"],
            f"https://models.pull-wise.com/v1/workers/{worker['worker_id']}"
            "/profiles/reviewer-production/revisions/1",
        )
        self.assertEqual(issued["gateway"]["provider"], "pullwise-gateway")

        manifest_bytes = json.dumps(
            issued["manifest"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signature = issued["manifest_signature"]["value"]
        private_key.public_key().verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            b"pullwise-model-profile-manifest/v1\0" + manifest_bytes,
        )

        access_token = issued["authorization"]["access_token"]
        claims = GatewayTokenVerifier(
            public_keys={"gateway-signing-2026-09": private_key.public_key()},
            issuer="https://api.pull-wise.com",
            clock=lambda: 1_788_259_201,
            grant_status_resolver=lambda _jti, _hash: GatewayGrantStatus(
                active=True,
                worker_enabled=True,
                generation=1,
                desired_profile_revision=1,
            ),
        ).verify(
            access_token,
            worker_id=worker["worker_id"],
            profile_set_id="reviewer-production",
            profile_revision=1,
            manifest_digest=profile["manifestDigest"],
            route_id="gpt-primary",
        )
        self.assertEqual(claims["sub"], worker["worker_id"])
        serialized = json.dumps(issued, sort_keys=True)
        self.assertNotIn("upstream-secret-must-not-leak", serialized)
        self.assertNotIn("secret_ref", serialized)

        model_gateway_api.emergency_revoke_provider_connection(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_emergency_revoke",
            provider_connection_id="openai-production",
        )
        with self.assertRaisesRegex(ValueError, "routes are unavailable"):
            issuer.issue(worker["worker_id"])

    def test_worker_profile_route_requires_matching_control_plane_identity(self) -> None:
        identifiers = iter(range(20))
        control_plane = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=RecordingSecretWriter(),
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
        )
        control_plane.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "route-upstream-secret",
            },
        )
        profile = control_plane.publish_profile_set(
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
        worker = db.create_worker({"name": "Bound worker", "provider": "unconfigured"})
        other_worker = db.create_worker({"name": "Other worker", "provider": "unconfigured"})
        control_plane.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": profile["revision"],
            },
        )
        control_plane.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        private_key = Ed25519PrivateKey.generate()
        issuer = WorkerProfileIssuer(
            connect_factory=db.connect,
            token_authority=GatewayTokenAuthority(
                connect_factory=db.connect,
                private_key=private_key,
                key_id="gateway-signing-2026-09",
                issuer="https://api.pull-wise.com",
                ttl_seconds=300,
                clock=lambda: 1_788_259_200,
                jti_factory=lambda: "gtj_http_profile",
            ),
            manifest_private_key=private_key,
            key_id="gateway-signing-2026-09",
            gateway_base_url="https://models.pull-wise.com/v1",
        )
        trust_payload = {
            "schema_id": "pullwise-model-gateway-manifest-trust/v1",
            "alg": "Ed25519",
            "kid": "gateway-signing-2026-09",
            "public_key_pem": "-----BEGIN PUBLIC KEY-----\npublic\n-----END PUBLIC KEY-----\n",
            "fingerprint": "sha256:" + "a" * 64,
        }
        with (
            patch.object(model_gateway_api, "worker_profile_issuer", return_value=issuer),
            patch.object(model_gateway_api, "gateway_manifest_trust", return_value=trust_payload),
        ):
            accepted = RouteHarness(
                f"/v1/workers/{worker['worker_id']}/model-profile",
                {
                    "schema_id": "pullwise-worker-model-profile-request/v1",
                    "worker_id": worker["worker_id"],
                },
                headers={"Authorization": f"Bearer {worker['worker_token']}"},
            )
            app.PullwiseHandler.route(accepted, "POST")
            rejected = RouteHarness(
                f"/v1/workers/{worker['worker_id']}/model-profile",
                {
                    "schema_id": "pullwise-worker-model-profile-request/v1",
                    "worker_id": worker["worker_id"],
                },
                headers={"Authorization": f"Bearer {other_worker['worker_token']}"},
            )
            app.PullwiseHandler.route(rejected, "POST")
            trust = RouteHarness(
                f"/v1/workers/{worker['worker_id']}/model-profile-trust",
                {
                    "schema_id": "pullwise-model-gateway-manifest-trust-request/v1",
                    "worker_id": worker["worker_id"],
                },
                headers={"Authorization": f"Bearer {worker['worker_token']}"},
            )
            app.PullwiseHandler.route(trust, "POST")

        self.assertEqual(accepted.status, 200, accepted.payload)
        self.assertEqual(accepted.payload["worker_id"], worker["worker_id"])
        self.assertEqual(accepted.headers_out["Cache-Control"], "no-store")
        self.assertEqual(rejected.status, 403, rejected.payload)
        self.assertEqual(trust.status, 200, trust.payload)
        self.assertEqual(trust.payload, trust_payload)
        self.assertEqual(trust.headers_out["Cache-Control"], "no-store")
        self.assertNotIn("route-upstream-secret", json.dumps(accepted.payload, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
