from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"


class AgentFirstReleaseDocumentSignaturesTest(unittest.TestCase):
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
