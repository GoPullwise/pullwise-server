from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server import app, db, model_gateway_api
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_tokens import GatewayTokenAuthority
from pullwise_server.model_gateway_snapshot import admin_model_gateway_snapshot
from pullwise_server.worker_profile_state import get_worker_profile_observation
from pullwise_server.worker_profile_state import worker_model_profile_readiness
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class WorkerProfileObservationTest(unittest.TestCase):
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

    def test_heartbeat_persists_strict_de_secreted_profile_observation(self) -> None:
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
                "secret": "observation-upstream-secret",
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
        worker = db.create_worker({"name": "Observed worker", "provider": "unconfigured"})
        control_plane.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": 1,
            },
        )
        control_plane.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=Ed25519PrivateKey.generate(),
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: app.now(),
            jti_factory=lambda: "gtj_observed",
        ).issue(
            worker_id=worker["worker_id"],
            profile_set_id="reviewer-production",
            profile_revision=1,
            manifest_digest=profile["manifestDigest"],
            route_ids=["gpt-primary"],
            generation=1,
        )
        profile_state = {
            "schema_id": "pullwise-worker-profile-state/v1",
            "worker_id": worker["worker_id"],
            "worker_pool_id": "reviewers-primary",
            "profile_set_id": "reviewer-production",
            "desired_revision": 1,
            "applied_revision": 1,
            "manifest_digest": profile["manifestDigest"],
            "catalog_digest": "d" * 64,
            "gateway_token_expires_at": app.now() + 300,
            "gateway_token_id": "gtj_observed",
            "last_apply_result": "succeeded",
            "applied_at": app.now(),
        }
        lease_before_observation = RouteHarness(
            f"/v1/workers/{worker['worker_id']}/lease",
            {
                "protocol_version": "review-worker-protocol/v1",
                "capacity": {
                    "available_job_slots": 1,
                    "active_jobs": 0,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "capabilities": {
                    "full_repo_scan": True,
                    "pi_agent_session": True,
                    "isolated_pi_profiles": True,
                    "progress_events": True,
                    "cancellation": True,
                    "intent_test_validation": True,
                },
            },
            headers={"Authorization": f"Bearer {worker['worker_token']}"},
        )
        app.PullwiseHandler.route(lease_before_observation, "POST")
        self.assertEqual(lease_before_observation.status, 200, lease_before_observation.payload)
        self.assertEqual(
            lease_before_observation.payload["reason"],
            "model_profile_not_ready:profile_state_missing",
        )
        heartbeat = RouteHarness(
            f"/v1/workers/{worker['worker_id']}/heartbeat",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker_id": worker["worker_id"],
                "status": "idle",
                "active_run_id": None,
                "concurrency": {
                    "max_active_jobs": 1,
                    "active_jobs": 0,
                    "available_job_slots": 1,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "agent_session": {
                    "status": "idle",
                    "transport": "embedded",
                    "active_session_id": None,
                },
                "provider": "pullwise-gateway",
                "providerChain": ["pullwise-gateway"],
                "readyProviders": ["pullwise-gateway"],
                "version": "0.10.24",
                "doctor_status": "ok",
                "runtime_catalog": {
                    "schema_id": "pullwise-pi-runtime-catalog/v1",
                    "credentials": [
                        {
                            "credential_id": "gateway-reviewer-production",
                            "label": "reviewer-production",
                            "provider": "pullwise-gateway",
                            "auth_type": "api_key",
                            "models": [{"id": "gpt-reviewer", "name": "gpt-reviewer"}],
                        }
                    ],
                },
                "profile_state": profile_state,
            },
            headers={"Authorization": f"Bearer {worker['worker_token']}"},
        )

        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, 200, heartbeat.payload)
        observed = get_worker_profile_observation(db.connect, worker["worker_id"])
        self.assertEqual(observed["profile_set_id"], "reviewer-production")
        self.assertEqual(observed["desired_revision"], 1)
        self.assertEqual(observed["applied_revision"], 1)
        self.assertEqual(observed["manifest_digest"], profile["manifestDigest"])
        self.assertNotIn("observation-upstream-secret", repr(observed))
        self.assertEqual(
            worker_model_profile_readiness(
                db.connect,
                worker_id=worker["worker_id"],
                timestamp=app.now(),
            ),
            (True, True, "ready"),
        )
        self.assertEqual(admin_model_gateway_snapshot(db.connect, timestamp=app.now())["workerPools"][0]["readyMemberCount"], 1)
        for field, invalid in (
            ("status", "revoked"), ("adapter", "unsupported"), ("secret_ref", ""), ("secret_version", ""),
            ("validated_models_json", "[]"), ("validated_models_json", "not-json"), ("validated_models_json", "{}"),
            ("validated_models_json", '["gpt-5.5", 3]'),
        ):
            with self.subTest(unavailable_field=field, value=invalid):
                with closing(db.connect()) as connection, connection:
                    original = connection.execute(f"SELECT {field} FROM provider_connections WHERE provider_connection_id='openai-production'").fetchone()[0]
                    connection.execute(f"UPDATE provider_connections SET {field}=? WHERE provider_connection_id='openai-production'", (invalid,))
                try:
                    self.assertFalse(worker_model_profile_readiness(db.connect, worker_id=worker["worker_id"], timestamp=app.now())[1])
                    self.assertEqual(admin_model_gateway_snapshot(db.connect, timestamp=app.now())["workerPools"][0]["readyMemberCount"], 0)
                finally:
                    with closing(db.connect()) as connection, connection:
                        connection.execute(f"UPDATE provider_connections SET {field}=? WHERE provider_connection_id='openai-production'", (original,))
        with closing(db.connect()) as connection, connection:
            connection.execute("UPDATE workers SET last_heartbeat_at=? WHERE worker_id=?", (app.now() - 121, worker["worker_id"]))
        self.assertEqual(admin_model_gateway_snapshot(db.connect, timestamp=app.now())["workerPools"][0]["readyMemberCount"], 0)
        with closing(db.connect()) as connection, connection:
            connection.execute("UPDATE workers SET last_heartbeat_at=? WHERE worker_id=?", (app.now(), worker["worker_id"]))
        model_gateway_api.rotate_worker_pool_gateway_tokens(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_rotate",
            worker_pool_id="reviewers-primary",
        )
        self.assertEqual(admin_model_gateway_snapshot(db.connect, timestamp=app.now())["workerPools"][0]["readyMemberCount"], 0)
        self.assertEqual(
            worker_model_profile_readiness(
                db.connect,
                worker_id=worker["worker_id"],
                timestamp=app.now(),
            ),
            (True, False, "gateway_token_stale"),
        )
        model_gateway_api.emergency_revoke_provider_connection(
            connect_factory=db.connect,
            actor_user_id="usr_admin",
            request_id="req_emergency_revoke",
            provider_connection_id="openai-production",
        )
        self.assertEqual(
            worker_model_profile_readiness(
                db.connect,
                worker_id=worker["worker_id"],
                timestamp=app.now(),
            ),
            (True, False, "profile_routes_unavailable"),
        )


if __name__ == "__main__":
    unittest.main()
