from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_provider_rotation import ProviderRotationService
from pullwise_server.model_gateway_secret_cleanup import (
    SecretCleanupCoordinator,
    SecretCleanupPending,
)
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.model_gateway_fakes import RecordingSecretWriter


PROVIDER_PAYLOAD = {
    "provider_connection_id": "openai-production",
    "display_name": "OpenAI Production",
    "provider": "openai",
    "adapter": "openai-completions",
    "endpoint_origin": "https://api.openai.com",
    "secret": "provider-secret-SENTINEL",
}


class InvalidModelsSecretWriter(RecordingSecretWriter):
    def validate_candidate(self, *_args: object, **_kwargs: object) -> list[str]:
        return []


class FailingRotationSecretWriter(RecordingSecretWriter):
    def validate_candidate(self, *_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("validation failed with secret-SENTINEL")


class RetryableRetirementWriter(RecordingSecretWriter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_retirement = True

    def retire_version(self, secret_ref: str, version: str) -> None:
        if self.fail_retirement:
            raise RuntimeError("retirement failed with secret-SENTINEL")
        super().retire_version(secret_ref, version)


class ModelGatewaySecretCompensationTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_create_retires_candidate_when_database_persistence_fails(self) -> None:
        writer = RecordingSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_duplicate",
        )
        control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_first",
            payload=PROVIDER_PAYLOAD,
        )

        with self.assertRaises(Exception):
            control.create_provider_connection(
                actor_user_id="usr_admin",
                request_id="req_duplicate",
                payload={**PROVIDER_PAYLOAD, "secret": "second-secret-SENTINEL"},
            )

        self.assertEqual(writer.retirements, [(writer.writes[1][0], "version-2")])

    def test_failed_compensation_is_durable_and_retryable(self) -> None:
        writer = RetryableRetirementWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_duplicate",
        )
        control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_first",
            payload=PROVIDER_PAYLOAD,
        )

        with self.assertRaisesRegex(SecretCleanupPending, "queued for retry"):
            control.create_provider_connection(
                actor_user_id="usr_admin",
                request_id="req_duplicate",
                payload={**PROVIDER_PAYLOAD, "secret": "second-secret-SENTINEL"},
            )
        connection = db.connect()
        try:
            pending = connection.execute(
                "SELECT secret_ref, secret_version, attempts, status "
                "FROM model_gateway_secret_cleanup_tasks"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(pending, (writer.writes[1][0], "version-2", 1, "pending"))

        writer.fail_retirement = False
        result = SecretCleanupCoordinator(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
        ).retry_pending()

        self.assertEqual(result, {"attempted": 1, "retired": 1, "pending": 0})
        self.assertEqual(writer.retirements, [(writer.writes[1][0], "version-2")])

    def test_create_retires_candidate_when_model_metadata_is_invalid(self) -> None:
        writer = InvalidModelsSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
        )

        with self.assertRaisesRegex(RuntimeError, "invalid model metadata"):
            control.create_provider_connection(
                actor_user_id="usr_admin",
                request_id="req_invalid_models",
                payload=PROVIDER_PAYLOAD,
            )

        self.assertEqual(writer.retirements, [(writer.writes[0][0], "version-1")])

    def test_rotation_retires_candidate_when_database_transaction_fails(self) -> None:
        writer = RecordingSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_duplicate",
        )
        control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_first",
            payload=PROVIDER_PAYLOAD,
        )
        rotation = ProviderRotationService(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
            audit_id_factory=lambda: "audit_duplicate",
        )

        with self.assertRaises(Exception):
            rotation.stage(
                provider_connection_id="openai-production",
                actor_user_id="usr_admin",
                request_id="req_rotation",
                secret="replacement-secret-SENTINEL",
            )

        self.assertEqual(writer.retirements, [(writer.writes[1][0], "version-2")])

    def test_rotation_retires_candidate_when_validation_fails(self) -> None:
        writer = FailingRotationSecretWriter()
        control_writer = RecordingSecretWriter()
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=control_writer,
            clock=lambda: 1_788_259_200,
            id_factory=lambda prefix: f"{prefix}_initial",
        )
        control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_first",
            payload=PROVIDER_PAYLOAD,
        )
        rotation = ProviderRotationService(
            connect_factory=db.connect,
            secret_writer=writer,
            clock=lambda: 1_788_259_300,
            audit_id_factory=lambda: "audit_rotation",
        )

        with self.assertRaisesRegex(RuntimeError, "provider rotation validation failed"):
            rotation.stage(
                provider_connection_id="openai-production",
                actor_user_id="usr_admin",
                request_id="req_rotation",
                secret="replacement-secret-SENTINEL",
            )

        self.assertEqual(writer.retirements, [(writer.writes[0][0], "version-1")])


if __name__ == "__main__":
    unittest.main()
