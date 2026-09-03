from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from pullwise_server import model_gateway_api
from pullwise_server.model_gateway_secret_broker import (
    GatewaySecretBroker,
    HttpSecretWriter,
)
from pullwise_server.model_gateway_secrets import EncryptedFileSecretStore


class ModelGatewaySecretBrokerTest(unittest.TestCase):
    def test_server_selects_http_writer_only_when_broker_environment_is_complete(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_MODEL_GATEWAY_BROKER_URL": "https://gateway.internal/internal/provider-secrets",
                "PULLWISE_MODEL_GATEWAY_BROKER_TOKEN": "broker-write-token",
            },
            clear=False,
        ):
            writer = model_gateway_api.provider_secret_writer()
        self.assertIsInstance(writer, HttpSecretWriter)

    def test_server_writer_can_only_write_and_receives_non_secret_metadata(self) -> None:
        sentinel = b"provider-secret-SENTINEL-broker"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EncryptedFileSecretStore(
                root=Path(temp_dir) / "secrets",
                master_key=bytes(range(32)),
                version_factory=lambda: "version-1",
            )
            validations: list[tuple[str, str, str]] = []

            class Validator:
                def validate(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> list[str]:
                    self._provider(provider)
                    self._record("validate", endpoint_origin, adapter, secret)
                    return ["gpt-5.5"]

                def canary(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> None:
                    self._provider(provider)
                    self._record("canary", endpoint_origin, adapter, secret)

                @staticmethod
                def _provider(provider: str) -> None:
                    if provider != "openai":
                        raise AssertionError("validator received the wrong provider")

                @staticmethod
                def _record(kind: str, endpoint_origin: str, adapter: str, secret: bytes) -> None:
                    if secret != sentinel:
                        raise AssertionError("validator received the wrong secret")
                    validations.append((kind, endpoint_origin, adapter))

            broker = GatewaySecretBroker(
                secret_store=store,
                write_token="broker-write-token",
                validator=Validator(),
            )
            calls: list[dict[str, object]] = []

            def transport(
                url: str,
                *,
                headers: dict[str, str],
                payload: dict[str, object],
                timeout_seconds: float,
            ) -> dict[str, object]:
                calls.append(
                    {
                        "url": url,
                        "headers": headers,
                        "payload": payload,
                        "timeout_seconds": timeout_seconds,
                    }
                )
                if url.endswith("/validate"):
                    return broker.validate_candidate(authorization=headers["Authorization"], payload=payload)
                if url.endswith("/canary"):
                    return broker.canary_candidate(authorization=headers["Authorization"], payload=payload)
                if url.endswith("/retire"):
                    return broker.retire_version(authorization=headers["Authorization"], payload=payload)
                return broker.put_candidate(authorization=headers["Authorization"], payload=payload)

            writer = HttpSecretWriter(
                broker_url="https://gateway.internal/internal/provider-secrets",
                write_token="broker-write-token",
                transport=transport,
                timeout_seconds=3.0,
            )
            stored = writer.put_candidate(
                "provider/openai-production/secret_1",
                sentinel,
            )

            self.assertEqual(stored.version, "version-1")
            self.assertRegex(stored.fingerprint, r"^sha256:[0-9a-f]{12}$")
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0]["url"],
                "https://gateway.internal/internal/provider-secrets",
            )
            self.assertEqual(
                store.read_version("provider/openai-production/secret_1", "version-1"),
                sentinel,
            )
            writer.validate_candidate(
                "provider/openai-production/secret_1",
                "version-1",
                provider="openai",
                adapter="openai-completions",
                endpoint_origin="https://api.openai.com",
            )
            writer.canary_candidate(
                "provider/openai-production/secret_1",
                "version-1",
                provider="openai",
                adapter="openai-completions",
                endpoint_origin="https://api.openai.com",
            )
            self.assertEqual(
                validations,
                [
                    ("validate", "https://api.openai.com", "openai-completions"),
                    ("canary", "https://api.openai.com", "openai-completions"),
                ],
            )
            writer.retire_version("provider/openai-production/secret_1", "version-1")
            writer.retire_version("provider/openai-production/secret_1", "version-1")
            with self.assertRaisesRegex(RuntimeError, "secret version"):
                store.read_version("provider/openai-production/secret_1", "version-1")
            response_text = repr(stored)
            self.assertNotIn(sentinel.decode("ascii"), response_text)
            disk_bytes = b"".join(
                path.read_bytes()
                for path in Path(temp_dir).rglob("*")
                if path.is_file()
            )
            self.assertNotIn(sentinel, disk_bytes)


if __name__ == "__main__":
    unittest.main()
