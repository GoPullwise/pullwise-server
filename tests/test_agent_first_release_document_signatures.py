from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
from types import ModuleType
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server.agent_first_contract_bundle_registry import (
    validate_supported_schema,
)
from pullwise_server.agent_first_contract_bundle_source import ContractBundleError


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"


class AgentFirstReleaseDocumentSignaturesTest(unittest.TestCase):
    def test_source_registry_rejects_a_cross_schema_signature_contract(self) -> None:
        family = json.loads(
            (FAMILY_ROOT / "benchmark-bundle.json").read_text(encoding="utf-8")
        )
        schema = deepcopy(family["schemas"][0])
        schema["x-pullwise-semantics"]["signature_contract"] = {
            "algorithm": "Ed25519",
            "domain": "pullwise-release-principal/v1",
            "domain_separator": "NUL",
            "encoding": "base64url_no_padding",
            "signed_projection": "document_without_signature_and_digest",
        }

        with self.assertRaises(ContractBundleError):
            validate_supported_schema(schema, ContractBundleError)

    def test_golden_release_authority_signatures_are_cryptographically_valid(
        self,
    ) -> None:
        built = build_bundle(FAMILY_ROOT.parent)
        facade = ModuleType("_release_signature_verification_facade")
        exec(built.python_wrapper, facade.__dict__)
        benchmark_public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(
                "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
            )
        )
        release_public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(
                "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"
            )
        )
        cases = (
            (
                "benchmark_bundle_golden_current",
                "benchmark-bundle/v1",
                benchmark_public_key,
            ),
            (
                "release_gate_policy_golden_bootstrap",
                "release-gate-policy/v1",
                release_public_key,
            ),
            (
                "release_gate_attestation_golden_bootstrap_pass",
                "release-gate-attestation/v1",
                release_public_key,
            ),
        )
        for fixture_id, schema_id, public_key in cases:
            with self.subTest(schema_id=schema_id):
                document = facade.fixture(fixture_id)["document"]
                public_key.verify(
                    base64.urlsafe_b64decode(document["signature"] + "=="),
                    facade.signature_message(schema_id, document),
                )

    def test_release_context_rejects_cross_organization_binding(self) -> None:
        built = build_bundle(FAMILY_ROOT.parent)
        facade = ModuleType("_release_organization_context_facade")
        exec(built.python_wrapper, facade.__dict__)
        benchmark = deepcopy(
            facade.fixture("benchmark_bundle_golden_current")["document"]
        )
        policy = deepcopy(
            facade.fixture("release_gate_policy_golden_bootstrap")["document"]
        )
        report = deepcopy(
            facade.fixture("release_gate_report_golden_bootstrap_pass")[
                "document"
            ]
        )
        attestation = deepcopy(
            facade.fixture("release_gate_attestation_golden_bootstrap_pass")[
                "document"
            ]
        )
        policy["organization_id"] = "org_other"
        policy.pop("policy_digest")
        policy = facade.seal_document("release-gate-policy/v1", policy)

        with self.assertRaises(facade.ContractValidationError) as policy_error:
            facade.verify_release_gate_policy_context(policy, benchmark)

        self.assertEqual(
            "RELEASE_POLICY_ORGANIZATION_MISMATCH",
            policy_error.exception.detail,
        )
        policy = deepcopy(
            facade.fixture("release_gate_policy_golden_bootstrap")["document"]
        )
        attestation["organization_id"] = "org_other"
        attestation.pop("attestation_digest")
        attestation = facade.seal_document(
            "release-gate-attestation/v1", attestation
        )

        with self.assertRaises(facade.ContractValidationError) as attestation_error:
            facade.verify_release_gate_attestation_context(
                attestation,
                policy,
                report,
            )

        self.assertEqual(
            "RELEASE_ATTESTATION_ORGANIZATION_MISMATCH",
            attestation_error.exception.detail,
        )

    def test_signed_release_source_fixtures_have_semantic_closure(self) -> None:
        built = build_bundle(FAMILY_ROOT.parent)
        facade = ModuleType("_signed_release_source_facade")
        exec(built.python_wrapper, facade.__dict__)
        family_ids = {
            "benchmark-bundle",
            "release-gate-policy",
            "release-gate-attestation",
        }
        fixtures = (
            fixture
            for family in built.document["families"]
            if family["family_id"] in family_ids
            for fixture in family["fixtures"]
        )

        for fixture in fixtures:
            with self.subTest(fixture_id=fixture["fixture_id"]):
                try:
                    facade.validate_document(
                        fixture["schema_id"], fixture["document"]
                    )
                except facade.ContractValidationError as error:
                    self.assertEqual(fixture["expected_code"], error.code)
                else:
                    self.assertIsNone(fixture["expected_code"])

    def test_release_authority_documents_are_organization_scoped_and_signed(
        self,
    ) -> None:
        cases = (
            (
                "benchmark-bundle.json",
                "benchmark-bundle/v1",
                "bundle_digest",
                "pullwise-benchmark-bundle/v1",
            ),
            (
                "release-gate-policy.json",
                "release-gate-policy/v1",
                "policy_digest",
                "pullwise-release-gate-policy/v1",
            ),
            (
                "release-gate-attestation.json",
                "release-gate-attestation/v1",
                "attestation_digest",
                "pullwise-release-gate-attestation/v1",
            ),
        )
        for filename, schema_id, digest_field, signature_domain in cases:
            with self.subTest(schema_id=schema_id):
                family = json.loads(
                    (FAMILY_ROOT / filename).read_text(encoding="utf-8")
                )
                schema = next(
                    item for item in family["schemas"] if item["$id"] == schema_id
                )
                self.assertIn("organization_id", schema["required"])
                self.assertEqual(
                    "^org_[a-z0-9_]{1,64}$",
                    schema["properties"]["organization_id"]["pattern"],
                )
                self.assertEqual(
                    "^[A-Za-z0-9_-]{86}$",
                    schema["properties"]["signature"]["pattern"],
                )
                self.assertEqual(
                    {
                        "algorithm": "Ed25519",
                        "domain": signature_domain,
                        "domain_separator": "NUL",
                        "encoding": "base64url_no_padding",
                        "signed_projection": "document_without_signature_and_digest",
                    },
                    schema["x-pullwise-semantics"]["signature_contract"],
                )
                self.assertEqual(
                    digest_field,
                    schema["x-pullwise-digest"]["field"],
                )


if __name__ == "__main__":
    unittest.main()
