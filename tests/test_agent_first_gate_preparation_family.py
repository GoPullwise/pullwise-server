from __future__ import annotations

from pathlib import Path
import re
import unittest

from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)
from tests.agent_first_task_evidence_support import (
    ordered_unique,
    sealed,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/gate-preparation.json"
)
SCHEMA_IDS = (
    "debug-redaction-plan/v1",
    "publication-content-manifest/v1",
    "terminalization-fact/v1",
)
DEBUG_INPUT_SCHEMA_IDS = {
    "execution-state-manifest/v1",
    "local-tool-receipt/v1",
    "observation/v1",
    "stable-error/v1",
}
PUBLICATION_SOURCE_SCHEMA_IDS = {
    "change-set/v1",
    "completion-proposal/v1",
    "observation/v1",
    "source-content/v1",
    "task-report/v1",
    "task-request/v1",
    "verification-attestation/v1",
    "verifier-work-report/v1",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AgentFirstGatePreparationFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = load_family(FAMILY_PATH, "gate-preparation", {}, set())
        cls.schemas = {
            schema["$id"]: schema for schema in cls.family["schemas"]
        }
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for fixture in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return self.fixtures[fixture_id]["document"]

    @staticmethod
    def ref_key(ref: dict[str, object]) -> tuple[object, ...]:
        return (
            ref["content_schema_id"],
            ref["artifact_id"],
            ref["sha256"],
        )

    def valid_debug_plan(self, document: dict[str, object]) -> bool:
        return (
            set(document)
            == set(self.schemas["debug-redaction-plan/v1"]["required"])
            and sealed(document, self.schemas["debug-redaction-plan/v1"])
            and document["allowed_json_pointers"]
            == sorted(set(document["allowed_json_pointers"]))
            and document["rule_ids"] == sorted(set(document["rule_ids"]))
            and ordered_unique(document["debug_input_refs"], self.ref_key)
            and all(
                valid_content_ref(ref, DEBUG_INPUT_SCHEMA_IDS)
                for ref in document["debug_input_refs"]
            )
        )

    def valid_publication_manifest(self, document: dict[str, object]) -> bool:
        entries = document["entries"]
        if not (
            set(document)
            == set(
                self.schemas["publication-content-manifest/v1"]["required"]
            )
            and sealed(
                document,
                self.schemas["publication-content-manifest/v1"],
            )
            and document["entry_count"] == len(entries)
            and ordered_unique(entries, lambda item: item["json_pointer"])
        ):
            return False
        for entry in entries:
            source_ref = entry["source_ref"]
            inline_digest = entry["inline_digest"]
            if entry["content_kind"] == "artifact_bytes":
                source_valid = (
                    inline_digest is None
                    and valid_content_ref(
                        source_ref,
                        PUBLICATION_SOURCE_SCHEMA_IDS,
                    )
                )
                original_sha256 = (
                    source_ref["sha256"] if source_valid else None
                )
            else:
                source_valid = (
                    source_ref is None
                    and isinstance(inline_digest, str)
                    and HEX64.fullmatch(inline_digest) is not None
                )
                original_sha256 = inline_digest if source_valid else None
            receipt = entry["redaction_receipt"]
            if not (
                source_valid
                and set(receipt)
                == {
                    "policy_digest",
                    "original_sha256",
                    "redacted_sha256",
                    "status",
                    "receipt_digest",
                }
                and receipt["policy_digest"]
                == document["redaction_policy_digest"]
                and receipt["original_sha256"] == original_sha256
                and receipt["status"] == "passed"
                and HEX64.fullmatch(receipt["redacted_sha256"]) is not None
                and HEX64.fullmatch(receipt["receipt_digest"]) is not None
            ):
                return False
        return True

    def test_public_source_loader_accepts_the_bounded_family(self) -> None:
        self.assertEqual(
            list(SCHEMA_IDS),
            [schema["$id"] for schema in self.family["schemas"]],
        )

    def test_debug_plan_golden_and_retry_are_sealed_and_output_free(self) -> None:
        golden = self.document("gate_preparation_golden_debug_plan")
        retry = self.document("gate_preparation_idempotency_debug_plan")

        self.assertTrue(self.valid_debug_plan(golden))
        self.assertNotEqual("0" * 64, golden["plan_digest"])
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
        self.assertNotIn(
            "worker-debug-fragment/v1",
            {ref["content_schema_id"] for ref in golden["debug_input_refs"]},
        )

    def test_publication_manifest_golden_and_retry_bind_scanned_content(self) -> None:
        golden = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        retry = self.document(
            "gate_preparation_idempotency_publication_manifest"
        )

        self.assertTrue(self.valid_publication_manifest(golden))
        self.assertNotEqual("0" * 64, golden["manifest_digest"])
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))


if __name__ == "__main__":
    unittest.main()
