from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_provider_rotation import ProviderRotationService
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter


class ModelDroppingRotationWriter(RecordingSecretWriter):
    def validate_candidate(self, secret_ref: str, version: str, **metadata: object) -> list[str]:
        super().validate_candidate(secret_ref, version, **metadata)
        return ["gpt-5.5"] if len(self.validations) == 1 else ["gpt-other"]


class ModelGatewayProviderRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_rotation_validates_canaries_promotes_then_retires_old_version(self) -> None:
        writer = RecordingSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_rotation",
        )
        created = control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_create",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "old-provider-secret",
            },
        )
        audit_ids = iter(["audit_rotation_stage", "audit_rotation_promote", "audit_rotation_retire"])
        rotation = ProviderRotationService(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
            audit_id_factory=lambda: next(audit_ids),
        )

        staged = rotation.stage(
            provider_connection_id="openai-production",
            actor_user_id="usr_admin",
            request_id="req_stage",
            secret="new-provider-secret-SENTINEL",
        )
        self.assertEqual(staged["status"], "rotation_staged")
        self.assertEqual(staged["secretVersion"], created["secretVersion"])
        self.assertEqual(staged["candidateSecretVersion"], "version-2")
        self.assertEqual(
            writer.validations,
            [(writer.writes[0][0], "version-1"), (writer.writes[0][0], "version-2")],
        )
        self.assertEqual(writer.canaries, [(writer.writes[0][0], "version-2")])

        promoted = rotation.promote(
            provider_connection_id="openai-production",
            actor_user_id="usr_admin",
            request_id="req_promote",
        )
        self.assertEqual(promoted["status"], "rotation_promoted")
        self.assertEqual(promoted["secretVersion"], "version-2")
        self.assertEqual(promoted["previousSecretVersion"], "version-1")

        with self.assertRaisesRegex(ValueError, "upstream revocation confirmation"):
            rotation.retire_previous(
                provider_connection_id="openai-production",
                actor_user_id="usr_admin",
                request_id="req_retire_unconfirmed",
                upstream_revoked=False,
            )
        self.assertEqual(writer.retirements, [])
        retired = rotation.retire_previous(
            provider_connection_id="openai-production",
            actor_user_id="usr_admin",
            request_id="req_retire",
            upstream_revoked=True,
        )
        self.assertEqual(retired["status"], "configured")
        self.assertIsNone(retired["previousSecretVersion"])
        self.assertEqual(writer.retirements, [(writer.writes[0][0], "version-1")])

        connection = sqlite3.connect(self.db_path)
        try:
            states = connection.execute(
                "SELECT secret_version, state FROM provider_secret_versions "
                "WHERE provider_connection_id = 'openai-production' ORDER BY secret_version"
            ).fetchall()
            database_bytes = open(self.db_path, "rb").read()
        finally:
            connection.close()
        self.assertEqual(states, [("version-1", "retired"), ("version-2", "active")])
        self.assertNotIn(b"new-provider-secret-SENTINEL", database_bytes)

    def test_rotation_rejects_candidate_that_drops_an_active_route_model(self) -> None:
        writer = ModelDroppingRotationWriter()
        identifiers = iter(range(10))
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_model_drop_{next(identifiers)}",
        )
        control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_create",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": "old-provider-secret",
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
        rotation = ProviderRotationService(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
            audit_id_factory=lambda: "audit_model_drop",
        )

        with self.assertRaisesRegex(ValueError, "active route models"):
            rotation.stage(
                provider_connection_id="openai-production",
                actor_user_id="usr_admin",
                request_id="req_stage",
                secret="new-provider-secret",
            )

        self.assertEqual(writer.retirements, [(writer.writes[0][0], "version-2")])


if __name__ == "__main__":
    unittest.main()
