from __future__ import annotations

from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    digest,
    ordered_unique,
    sealed,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-evidence.json"
SCHEMAS = ("evidence-closure-manifest/v1",)
HELPERS = {
    "evidence-closure-manifest/v1": ["verify_evidence_closure_context"],
}
FORBIDDEN = {
    "evidence-closure-manifest/v1",
    "task-result-core/v1",
    "task-result/v1",
    "worker-debug-fragment/v1",
}


class TaskEvidenceFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_entry(self, document: dict[str, object]) -> bool:
        return valid_content_ref(document) and document["content_schema_id"] not in FORBIDDEN

    def valid_manifest(self, document: dict[str, object]) -> bool:
        entries = document["entries"]
        additions = {
            (
                document[key]["content_schema_id"],
                document[key]["artifact_id"],
                document[key]["sha256"],
            )
            for key in (
                "pre_gate_evidence_closure_ref",
                "input_snapshot_ref",
                "gate_decision_ref",
            )
        }
        entry_keys = {
            (item["content_schema_id"], item["artifact_id"], item["sha256"])
            for item in entries
        }
        artifact_digests: dict[str, set[str]] = {}
        for item in entries:
            artifact_digests.setdefault(item["artifact_id"], set()).add(
                item["sha256"]
            )
        return (
            sealed(document, self.schemas["evidence-closure-manifest/v1"])
            and valid_content_ref(
                document["pre_gate_evidence_closure_ref"],
                {"pre-gate-evidence-closure-manifest/v1"},
            )
            and valid_content_ref(
                document["input_snapshot_ref"],
                {"gate-input-snapshot/v1", "terminalization-input-snapshot/v1"},
            )
            and valid_content_ref(document["gate_decision_ref"], {"gate-decision/v1"})
            and document["entry_count"] == len(entries)
            and ordered_unique(
                entries,
                lambda item: (
                    item["content_schema_id"],
                    item["artifact_id"],
                    item["sha256"],
                ),
            )
            and all(self.valid_entry(item) for item in entries)
            and all(len(digests) == 1 for digests in artifact_digests.values())
            and additions.issubset(entry_keys)
            and document["evidence_closure_digest"] == digest(entries)
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-evidence", SCHEMAS, HELPERS)

    def test_entries_use_native_content_refs_without_an_alias_schema(self) -> None:
        schema = self.schemas["evidence-closure-manifest/v1"]
        self.assertEqual(
            {"$ref": "content-ref/v1"},
            schema["properties"]["entries"]["items"],
        )

    def test_final_closure_has_exact_typed_gate_edges(self) -> None:
        props = self.schemas["evidence-closure-manifest/v1"]["properties"]
        self.assertEqual(
            "pre-gate-evidence-closure-manifest/v1",
            props["pre_gate_evidence_closure_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            ["gate-input-snapshot/v1", "terminalization-input-snapshot/v1"],
            props["input_snapshot_ref"]["x-pullwise-content-schema-ids"],
        )
        self.assertEqual(
            "gate-decision/v1",
            props["gate_decision_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual("content-ref/v1", props["entries"]["items"]["$ref"])

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {
                "evidence-closure-manifest/v1": self.valid_manifest,
            }
        )

    def test_negative_same_artifact_cannot_claim_two_digests(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        entries = fixtures[
            "task_evidence_negative_artifact_digest_conflict"
        ]["document"]["entries"]
        by_artifact: dict[str, set[str]] = {}
        for entry in entries:
            by_artifact.setdefault(entry["artifact_id"], set()).add(
                entry["sha256"]
            )
        self.assertIn(2, {len(digests) for digests in by_artifact.values()})

    def test_negative_closure_cannot_point_back_to_result(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        bad = fixtures["task_evidence_negative_result_back_edge"]["document"]
        self.assertIn("task-result/v1", {item["content_schema_id"] for item in bad["entries"]})


if __name__ == "__main__":
    unittest.main()
