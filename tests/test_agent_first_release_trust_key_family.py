from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)


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
    def test_trust_family_is_part_of_the_current_source_package(self) -> None:
        source_root = FAMILY_PATH.parents[1]

        built = build_bundle(source_root)

        family_ids = {item["family_id"] for item in built.document["families"]}
        self.assertIn("release-trust-authority", family_ids)
        schema_ids = {
            item["schema_id"]
            for item in built.document["root_manifest"]["schema_registry"]
        }
        self.assertTrue(
            {
                "release-key-revocation/v1",
                "release-principal/v1",
                "release-signing-key/v1",
                "release-trust-root/v1",
            }.issubset(schema_ids)
        )

    def test_key_and_revocation_have_signed_semantic_closure(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        fixtures = {item["fixture_id"]: item for item in family["fixtures"]}
        root = fixtures["release_trust_root_golden_external_pin"]["document"]
        verifier = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(root["public_key"] + "=")
        )
        cases = (
            (
                "release_signing_key",
                "release-signing-key/v1",
                "benchmark_owner",
                "pullwise-release-signing-key/v1",
                "pullwise:release-signing-key:v1",
                "signing_key_digest",
            ),
            (
                "release_key_revocation",
                "release-key-revocation/v1",
                "superseded",
                "pullwise-release-key-revocation/v1",
                "pullwise:release-key-revocation:v1",
                "revocation_digest",
            ),
        )
        for prefix, schema_id, suffix, signature_domain, digest_domain, digest_field in cases:
            with self.subTest(schema_id=schema_id):
                golden = fixtures[f"{prefix}_golden_{suffix}"]["document"]
                idempotent = fixtures[f"{prefix}_idempotency_{suffix}"][
                    "document"
                ]
                negative = fixtures[f"{prefix}_negative_time_order"]
                self.assertEqual(
                    canonical_bytes(golden),
                    canonical_bytes(idempotent),
                )
                projection = {
                    key: value
                    for key, value in golden.items()
                    if key not in {"signature", digest_field}
                }
                verifier.verify(
                    base64.urlsafe_b64decode(golden["signature"] + "=="),
                    signature_domain.encode("ascii")
                    + b"\0"
                    + canonical_bytes(projection),
                )
                unsigned = {
                    key: value
                    for key, value in golden.items()
                    if key != digest_field
                }
                self.assertEqual(
                    hashlib.sha256(
                        digest_domain.encode("ascii")
                        + b"\0"
                        + canonical_bytes(unsigned)
                    ).hexdigest(),
                    golden[digest_field],
                )
                self.assertEqual(
                    "CONTRACT_DOCUMENT_INVALID",
                    negative["expected_code"],
                )

    def test_key_revocation_is_root_signed_and_exactly_key_bound(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        revocation = schemas["release-key-revocation/v1"]

        self.assertIs(False, revocation["additionalProperties"])
        self.assertEqual(
            set(revocation["required"]),
            set(revocation["properties"]),
        )
        self.assertEqual(
            {
                "schema_id",
                "revocation_id",
                "organization_id",
                "trust_root_id",
                "trust_root_ref",
                "trust_root_digest",
                "revoked_key_id",
                "revoked_key_ref",
                "revoked_key_digest",
                "revoked_principal_id",
                "reason_code",
                "signer_id",
                "signer_key_id",
                "signature_algorithm",
                "issued_at",
                "effective_at",
                "signature",
                "revocation_digest",
            },
            set(revocation["required"]),
        )
        self.assertEqual(
            "release-trust-root/v1",
            revocation["properties"]["trust_root_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "release-signing-key/v1",
            revocation["properties"]["revoked_key_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            [
                "authorization_withdrawn",
                "compromised",
                "retired",
                "superseded",
            ],
            revocation["properties"]["reason_code"]["enum"],
        )
        self.assertEqual(
            "pullwise-release-key-revocation/v1",
            revocation["x-pullwise-semantics"]["signature_contract"][
                "domain"
            ],
        )

    def test_signing_key_semantics_are_registered(self) -> None:
        loaded = load_family(
            FAMILY_PATH,
            "release-trust-authority",
            {},
            set(),
        )

        self.assertEqual("release-trust-authority", loaded["family_id"])

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
