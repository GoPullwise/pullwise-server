from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "release-trust-authority.json"
)


class AgentFirstReleaseTrustKeyFamilyTest(unittest.TestCase):
    def test_signing_key_is_root_signed_and_principal_bound(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        signing_key = schemas["release-signing-key/v1"]

        self.assertIs(False, signing_key["additionalProperties"])
        self.assertEqual(
            set(signing_key["required"]),
            set(signing_key["properties"]),
        )
        self.assertEqual(
            {
                "schema_id",
                "key_id",
                "organization_id",
                "principal_id",
                "principal_ref",
                "principal_digest",
                "key_purpose",
                "trust_root_id",
                "trust_root_digest",
                "signer_id",
                "signer_key_id",
                "signature_algorithm",
                "public_key",
                "issued_at",
                "expires_at",
                "signature",
                "signing_key_digest",
            },
            set(signing_key["required"]),
        )
        self.assertEqual(
            ["benchmark_signing", "release_signing"],
            signing_key["properties"]["key_purpose"]["enum"],
        )
        self.assertEqual(
            "release-principal/v1",
            signing_key["properties"]["principal_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "^[A-Za-z0-9_-]{43}$",
            signing_key["properties"]["public_key"]["pattern"],
        )
        self.assertEqual(
            {
                "algorithm": "Ed25519",
                "domain": "pullwise-release-signing-key/v1",
                "domain_separator": "NUL",
                "encoding": "base64url_no_padding",
                "signed_projection": "document_without_signature_and_digest",
            },
            signing_key["x-pullwise-semantics"]["signature_contract"],
        )


if __name__ == "__main__":
    unittest.main()
