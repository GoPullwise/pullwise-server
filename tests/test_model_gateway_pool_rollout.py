from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_pool_store import ModelGatewayPoolStore
from pullwise_server.model_gateway_wave_rollout import WorkerPoolWaveRollout
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter
from tests.test_worker_admin_routes import RouteHarness, reset_state


class ModelGatewayPoolRolloutTest(unittest.TestCase):
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

    def test_pool_switches_to_published_revision_and_rotates_worker_grants(self) -> None:
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
                "secret": "pool-rollout-secret",
            },
        )
        route = {
            "route_id": "gpt-primary",
            "provider_connection_id": "openai-production",
            "provider": "pullwise-gateway",
            "model_alias": "gpt-reviewer",
            "upstream_model": "gpt-5.5",
            "api": "openai-completions",
            "enabled": True,
        }
        first = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_1",
            payload={"profile_set_id": "reviewer-production", "display_name": "Reviewer production", "routes": [route]},
        )
        second = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_2",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{**route, "upstream_model": "gpt-5.6"}],
            },
        )
        third = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_3",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{**route, "upstream_model": "gpt-5.7"}],
            },
        )
        worker = db.create_worker({"name": "Rollout worker", "provider": "unconfigured"})
        control.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": first["revision"],
            },
        )
        control.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )

        updated = ModelGatewayPoolStore(db.connect).set_desired_revision(
            worker_pool_id="reviewers-primary",
            profile_revision=second["revision"],
            actor_user_id="usr_admin",
            request_id="req_rollout",
            timestamp=1_788_259_300,
        )

        self.assertEqual(updated["desired_revision"], 2)
        self.assertEqual(updated["gateway_token_generation"], 2)
        assignment = ModelGatewayPoolStore(db.connect).worker_profile_assignment(worker["worker_id"])
        self.assertEqual(assignment["desired_revision"], 2)
        self.assertEqual(assignment["manifest_digest"], second["manifestDigest"])

        rollout = RouteHarness(
            "/admin/worker-pools/reviewers-primary/desired-revision",
            {"profile_revision": third["revision"]},
            cookie="pw_session=ses_admin",
        )
        app.PullwiseHandler.route(rollout, "POST")
        self.assertEqual(rollout.status, 200, rollout.payload)
        self.assertEqual(rollout.payload["workerPool"]["desiredRevision"], 3)
        self.assertEqual(rollout.payload["workerPool"]["gatewayTokenGeneration"], 3)

        fourth = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_4",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{**route, "upstream_model": "gpt-5.5"}],
            },
        )
        second_worker = db.create_worker({"name": "Second rollout worker", "provider": "unconfigured"})
        control.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind_second",
            worker_id=second_worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )
        wave = WorkerPoolWaveRollout(db.connect)
        first_wave = wave.apply(
            worker_pool_id="reviewers-primary",
            profile_revision=fourth["revision"],
            worker_ids=[worker["worker_id"]],
            actor_user_id="usr_admin",
            request_id="req_wave_1",
            timestamp=1_788_259_400,
        )
        self.assertEqual(first_wave["updated_worker_ids"], [worker["worker_id"]])
        self.assertEqual(
            ModelGatewayPoolStore(db.connect).worker_profile_assignment(worker["worker_id"])["desired_revision"],
            4,
        )
        self.assertEqual(
            ModelGatewayPoolStore(db.connect).worker_profile_assignment(second_worker["worker_id"])["desired_revision"],
            3,
        )
        second_wave = RouteHarness(
            "/admin/worker-pools/reviewers-primary/rollout-wave",
            {
                "profile_revision": fourth["revision"],
                "worker_ids": [second_worker["worker_id"]],
            },
            cookie="pw_session=ses_admin",
        )
        app.PullwiseHandler.route(second_wave, "POST")
        self.assertEqual(second_wave.status, 200, second_wave.payload)
        self.assertEqual(second_wave.payload["rollout"]["updatedWorkerIds"], [second_worker["worker_id"]])
        self.assertEqual(
            ModelGatewayPoolStore(db.connect).worker_profile_assignment(second_worker["worker_id"])["desired_revision"],
            4,
        )


if __name__ == "__main__":
    unittest.main()
