import itertools
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pullwise_server import db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_pool_store import ModelGatewayPoolStore
from pullwise_server.model_gateway_provider_rotation import ProviderRotationService
from pullwise_server.model_gateway_route_availability import worker_assignment_routes_available
from pullwise_server.model_gateway_wave_rollout import WorkerPoolWaveRollout
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter

NOW = 1_788_259_200


class NarrowingWriter(RecordingSecretWriter):
    def validate_candidate(self, secret_ref, version, **metadata):
        super().validate_candidate(secret_ref, version, **metadata)
        return ["gpt-5.5", "gpt-5.6"] if len(self.validations) == 1 else ["gpt-5.6"]


class GatewayStateBoundaryTest(unittest.TestCase):
    def setUp(self):
        start_fast_sqlite_connections(self)
        temporary = tempfile.TemporaryDirectory(prefix="gateway-state-")
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {"PULLWISE_DB_PATH": str(Path(temporary.name) / "test.sqlite3")})
        env.start()
        self.addCleanup(env.stop)
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
        identifiers = itertools.count()
        self.writer = NarrowingWriter()
        self.control = ModelGatewayControlPlane(connect_factory=db.connect,
            secret_writer=self.writer, clock=lambda: NOW,
            id_factory=lambda prefix: f"{prefix}_test_{next(identifiers)}")
        self.control.create_provider_connection(actor_user_id="admin", request_id="create",
            payload={"provider_connection_id": "provider", "display_name": "Test provider",
                "provider": "openai", "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com", "secret": "synthetic"})
        for model in ("gpt-5.5", "gpt-5.6"):
            self.control.publish_profile_set(actor_user_id="admin", request_id="publish",
                payload={"profile_set_id": "profile", "display_name": "Test profile", "routes": [{
                    "route_id": "primary", "provider_connection_id": "provider",
                    "provider": "pullwise-gateway", "model_alias": "reviewer",
                    "upstream_model": model, "api": "openai-completions", "enabled": True}]})
        self.pool = ModelGatewayPoolStore(db.connect)

    def members(self, revision, count=2):
        self.control.create_worker_pool(actor_user_id="admin", request_id="pool",
            payload={"worker_pool_id": "pool", "display_name": "Test pool",
                "profile_set_id": "profile", "profile_revision": revision})
        workers = []
        for number in range(count):
            worker = db.create_worker({"name": f"Worker {number}", "provider": "unconfigured"})["worker_id"]
            workers.append(worker)
            self.control.bind_worker_to_pool(actor_user_id="admin", request_id="bind",
                worker_id=worker, worker_pool_id="pool")
        return workers

    def test_immediate_after_wave_converges_all_members_and_then_is_idempotent(self):
        workers = self.members(1)
        wave = WorkerPoolWaveRollout(db.connect).apply(worker_pool_id="pool", profile_revision=2,
            worker_ids=[workers[0]], actor_user_id="admin", request_id="wave", timestamp=NOW + 1)
        immediate = self.pool.set_desired_revision(worker_pool_id="pool", profile_revision=2,
            actor_user_id="admin", request_id="immediate", timestamp=NOW + 2)
        self.assertEqual([self.pool.worker_profile_assignment(worker)["desired_revision"] for worker in workers], [2, 2])
        self.assertEqual(immediate["gateway_token_generation"], wave["gateway_token_generation"] + 1)
        repeated = self.pool.set_desired_revision(worker_pool_id="pool", profile_revision=2,
            actor_user_id="admin", request_id="again", timestamp=NOW + 3)
        self.assertEqual(repeated["gateway_token_generation"], immediate["gateway_token_generation"])
        rollback = self.pool.set_desired_revision(worker_pool_id="pool", profile_revision=1,
            actor_user_id="admin", request_id="rollback", timestamp=NOW + 4)
        self.assertEqual([self.pool.worker_profile_assignment(worker)["desired_revision"] for worker in workers], [1, 1])
        self.assertEqual(rollback["gateway_token_generation"], immediate["gateway_token_generation"] + 1)

    def _assert_promote_rechecks_dependencies(self, change):
        workers = self.members(2)
        audit_ids = itertools.count()
        rotation = ProviderRotationService(connect_factory=db.connect, secret_writer=self.writer,
            clock=lambda: NOW + 10, audit_id_factory=lambda: f"rotation_{next(audit_ids)}")
        staged = rotation.stage(provider_connection_id="provider", actor_user_id="admin",
            request_id="stage", secret="synthetic-replacement")
        if change == "immediate":
            self.pool.set_desired_revision(worker_pool_id="pool", profile_revision=1,
                actor_user_id="admin", request_id="rollback", timestamp=NOW + 11)
        elif change == "wave":
            WorkerPoolWaveRollout(db.connect).apply(worker_pool_id="pool", profile_revision=1,
                worker_ids=[workers[0]], actor_user_id="admin", request_id="rollback-wave", timestamp=NOW + 11)
        else:
            self.control.create_worker_pool(actor_user_id="admin", request_id="old-pool",
                payload={"worker_pool_id": "old-pool", "display_name": "Old revision pool",
                    "profile_set_id": "profile", "profile_revision": 1})
        self.assertTrue(all(worker_assignment_routes_available(db.connect, worker) for worker in workers))
        with self.assertRaisesRegex(ValueError, "active route models"):
            rotation.promote(provider_connection_id="provider", actor_user_id="admin", request_id="promote")
        self.assertTrue(all(worker_assignment_routes_available(db.connect, worker) for worker in workers))
        with closing(db.connect()) as connection:
            row = connection.execute("SELECT status, secret_version, candidate_secret_version FROM provider_connections WHERE provider_connection_id = 'provider'").fetchone()
            versions = connection.execute("SELECT state FROM provider_secret_versions WHERE provider_connection_id = 'provider' ORDER BY created_at").fetchall()
        self.assertEqual(tuple(row), ("rotation_staged", staged["secretVersion"], staged["candidateSecretVersion"]))
        self.assertEqual([entry[0] for entry in versions], ["active", "canary_passed"])

    def test_promote_rechecks_immediate_rollback(self):
        self._assert_promote_rechecks_dependencies("immediate")

    def test_promote_rechecks_wave_rollback(self):
        self._assert_promote_rechecks_dependencies("wave")

    def test_promote_rechecks_new_pool_on_older_revision(self):
        self._assert_promote_rechecks_dependencies("new-pool")
