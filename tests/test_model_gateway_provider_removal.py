from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_pool_store import ModelGatewayPoolStore
from pullwise_server.model_gateway_provider_removal import (
    ProviderDependencyError,
    ProviderRemovalService,
)
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter


class ModelGatewayProviderRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_removal_blocks_dependencies_then_requires_upstream_revoke_confirmation(self) -> None:
        identifiers = iter(range(30))
        writer = RecordingSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_{next(identifiers)}",
        )
        for connection_id, name in (("openai-old", "OpenAI old"), ("openai-new", "OpenAI new")):
            control.create_provider_connection(
                actor_user_id="usr_admin",
                request_id=f"req_{connection_id}",
                payload={
                    "provider_connection_id": connection_id,
                    "display_name": name,
                    "provider": "openai",
                    "adapter": "openai-completions",
                    "endpoint_origin": "https://api.openai.com",
                    "secret": f"{connection_id}-secret",
                },
            )
        base_route = {
            "route_id": "gpt-primary",
            "provider": "pullwise-gateway",
            "model_alias": "gpt-reviewer",
            "upstream_model": "gpt-5.5",
            "api": "openai-completions",
            "enabled": True,
        }
        first = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_1",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{**base_route, "provider_connection_id": "openai-old"}],
            },
        )
        second = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile_2",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{**base_route, "provider_connection_id": "openai-new"}],
            },
        )
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
        removal = ProviderRemovalService(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
            audit_id_factory=lambda: f"audit_{next(identifiers)}",
        )

        with self.assertRaises(ProviderDependencyError) as blocked:
            removal.prepare(
                provider_connection_id="openai-old",
                actor_user_id="usr_admin",
                request_id="req_remove_blocked",
            )
        self.assertIn("worker_pool:reviewers-primary", blocked.exception.dependencies)

        ModelGatewayPoolStore(db.connect).set_desired_revision(
            worker_pool_id="reviewers-primary",
            profile_revision=second["revision"],
            actor_user_id="usr_admin",
            request_id="req_migrate",
            timestamp=1_788_259_300,
        )
        prepared = removal.prepare(
            provider_connection_id="openai-old",
            actor_user_id="usr_admin",
            request_id="req_prepare",
        )
        self.assertEqual(prepared["status"], "removal_prepared")
        with self.assertRaisesRegex(ValueError, "upstream revocation confirmation"):
            removal.finalize(
                provider_connection_id="openai-old",
                actor_user_id="usr_admin",
                request_id="req_finalize_unconfirmed",
                upstream_revoked=False,
            )
        removed = removal.finalize(
            provider_connection_id="openai-old",
            actor_user_id="usr_admin",
            request_id="req_finalize",
            upstream_revoked=True,
        )
        self.assertEqual(removed["status"], "removed")
        self.assertEqual(writer.retirements[-1][1], "version-1")


if __name__ == "__main__":
    unittest.main()
