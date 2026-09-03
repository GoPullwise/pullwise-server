from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pullwise_server.model_gateway_secrets import EncryptedFileSecretStore


class EncryptedFileSecretStoreTest(unittest.TestCase):
    def test_secret_is_encrypted_at_rest_and_round_trips_only_through_store(self) -> None:
        sentinel = b"pw-upstream-secret-SENTINEL-bff34d"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EncryptedFileSecretStore(
                root=Path(temp_dir) / "gateway-secrets",
                master_key=bytes(range(32)),
                version_factory=lambda: "version-1",
            )

            stored = store.put_candidate(
                "provider/openai-production/secret_fixed",
                sentinel,
            )

            self.assertEqual(stored.version, "version-1")
            self.assertRegex(stored.fingerprint, r"^sha256:[0-9a-f]{12}$")
            self.assertEqual(
                store.read_version(
                    "provider/openai-production/secret_fixed",
                    "version-1",
                ),
                sentinel,
            )
            files = [path for path in Path(temp_dir).rglob("*") if path.is_file()]
            self.assertTrue(files)
            for path in files:
                self.assertNotIn(sentinel, path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
