from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter


class RestrictedModelWriter(RecordingSecretWriter):
    def validate_candidate(self, secret_ref: str, version: str, **metadata: object) -> list[str]:
        super().validate_candidate(secret_ref, version, **metadata)
        return ["gpt-5.5"]


class ModelGatewayModelDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3")},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])

    def test_profile_route_must_intersect_gateway_validated_models(self) -> None:
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=RestrictedModelWriter(),
            clock=lambda: 1_788_259_200,
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
                "secret": "model-discovery-secret",
            },
        )
        with self.assertRaisesRegex(ValueError, "upstream model is unavailable"):
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
                        "upstream_model": "gpt-not-available",
                        "api": "openai-completions",
                        "enabled": True,
                    }],
                },
            )


if __name__ == "__main__":
    unittest.main()
