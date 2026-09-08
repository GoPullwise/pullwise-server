from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server.model_gateway_app import build_application
from pullwise_server.model_gateway_secrets import EncryptedFileSecretStore


class ModelGatewayAppTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "requires POSIX file permissions")
    def test_gateway_fixture_works_under_standard_posix_umask(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            self.test_closed_runtime_config_builds_standalone_gateway_application()
        finally:
            os.umask(previous_umask)

    def test_closed_runtime_config_builds_standalone_gateway_application(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            master_key_path = root / "secret-store.key"
            master_key_path.write_bytes(bytes(range(32)))
            master_key_path.chmod(0o600)
            secret_root = root / "secrets"
            stored = EncryptedFileSecretStore(
                root=secret_root,
                master_key=bytes(range(32)),
                version_factory=lambda: "version-1",
            ).put_candidate("provider/openai-production/secret_1", b"upstream-secret")
            signing_key = Ed25519PrivateKey.generate()
            public_key_path = root / "gateway-signing.pub.pem"
            public_key_path.write_bytes(
                signing_key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            config_path = root / "gateway.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_id": "pullwise-model-gateway-runtime/v1",
                        "issuer": "https://api.pull-wise.com",
                        "public_keys": [
                            {
                                "kid": "gateway-signing-2026-09",
                                "path": str(public_key_path),
                            }
                        ],
                        "introspection_url": "https://api.internal/internal/model-gateway/grants/introspect",
                        "route_url": "https://api.internal/internal/model-gateway/routes/resolve",
                        "secret_root": str(secret_root),
                        "secret_key_path": str(master_key_path),
                        "audit_path": str(root / "gateway-audit.jsonl"),
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_MODEL_GATEWAY_BROKER_TOKEN": "broker-write-token",
                    "PULLWISE_MODEL_GATEWAY_INTROSPECTION_TOKEN": "gateway-introspection-token",
                },
                clear=False,
            ):
                application = build_application(config_path)

            health = application.dispatch(
                method="GET",
                path="/health",
                headers={},
                body=b"",
            )
            self.assertEqual(health.status, 200)
            self.assertEqual(health.payload["service"], "pullwise-model-gateway")


if __name__ == "__main__":
    unittest.main()
