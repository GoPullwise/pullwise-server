from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
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


class AgentFirstReleaseTrustFamilyTest(unittest.TestCase):
    def test_external_root_is_closed_and_organization_scoped(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        root = schemas["release-trust-root/v1"]

        self.assertEqual("release-trust-authority", family["family_id"])
        self.assertIs(False, root["additionalProperties"])
        self.assertEqual(set(root["required"]), set(root["properties"]))
        self.assertEqual(
            {
                "schema_id",
                "trust_root_id",
                "organization_id",
                "root_principal_id",
                "root_key_id",
                "signature_algorithm",
                "public_key",
                "issued_at",
                "expires_at",
                "root_digest",
            },
            set(root["required"]),
        )
        self.assertNotIn("signature", root["properties"])
        self.assertEqual(
            "^[A-Za-z0-9_-]{43}$",
            root["properties"]["public_key"]["pattern"],
        )
        self.assertEqual(
            {
                "field": "root_digest",
                "domain": "pullwise:release-trust-root:v1",
            },
            root["x-pullwise-digest"],
        )

    def test_external_root_has_exact_golden_and_negative_fixtures(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        loaded = load_family(
            FAMILY_PATH,
            "release-trust-authority",
            {},
            set(),
        )
        fixtures = {
            item["fixture_id"]: item for item in loaded["fixtures"]
        }

        self.assertEqual(
            {
                "release_trust_root_golden_external_pin",
                "release_trust_root_idempotency_external_pin",
                "release_trust_root_negative_public_key",
            },
            set(fixtures),
        )
        golden = fixtures["release_trust_root_golden_external_pin"]["document"]
        unsigned = {
            key: value for key, value in golden.items() if key != "root_digest"
        }
        self.assertEqual(
            hashlib.sha256(
                b"pullwise:release-trust-root:v1\0"
                + canonical_bytes(unsigned)
            ).hexdigest(),
            golden["root_digest"],
        )
        self.assertEqual(43, len(golden["public_key"]))
        negative = fixtures["release_trust_root_negative_public_key"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertNotEqual(43, len(negative["document"]["public_key"]))
        self.assertEqual(family["fixtures"], loaded["fixtures"])

    def test_external_root_canonical_bytes_are_idempotent(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        fixtures = {item["fixture_id"]: item for item in family["fixtures"]}
        core = json.loads(
            (FAMILY_PATH.parent / "core.json").read_text(encoding="utf-8")
        )
        error_family = json.loads(
            (FAMILY_PATH.parent / "receipt-error.json").read_text(
                encoding="utf-8"
            )
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical_bundle = canonical_bytes(
            {
                "root_manifest": {
                    "schema_registry": [
                        {
                            "schema_id": schema["$id"],
                            "role": "public_document",
                        }
                        for schema in core["schemas"] + family["schemas"]
                    ]
                },
                "families": [
                    core,
                    family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ],
            }
        )
        wrapper_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"release-trust-root-test").hexdigest(),
            hashlib.sha256(canonical_bundle).hexdigest(),
            canonical_bundle,
        )
        wrapper = ModuleType("_release_trust_root_wrapper")
        exec(wrapper_bytes, wrapper.__dict__)
        golden = fixtures["release_trust_root_golden_external_pin"]["document"]
        idempotent = fixtures[
            "release_trust_root_idempotency_external_pin"
        ]["document"]

        self.assertEqual(
            wrapper.canonical_validated_bytes("release-trust-root/v1", golden),
            wrapper.canonical_validated_bytes(
                "release-trust-root/v1", idempotent
            ),
        )

    def test_principal_is_root_signed_and_has_one_organization_role(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        principal = schemas["release-principal/v1"]

        self.assertIs(False, principal["additionalProperties"])
        self.assertEqual(set(principal["required"]), set(principal["properties"]))
        self.assertEqual(
            ["benchmark_owner", "release_operator"],
            principal["properties"]["role"]["enum"],
        )
        self.assertEqual(
            "release-trust-root/v1",
            principal["properties"]["trust_root_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "^[A-Za-z0-9_-]{86}$",
            principal["properties"]["signature"]["pattern"],
        )
        self.assertEqual(
            {
                "algorithm": "Ed25519",
                "domain": "pullwise-release-principal/v1",
                "domain_separator": "NUL",
                "encoding": "base64url_no_padding",
                "signed_projection": "document_without_signature_and_digest",
            },
            principal["x-pullwise-semantics"]["signature_contract"],
        )


if __name__ == "__main__":
    unittest.main()
