from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    canonical_bytes,
    digest,
    ordered_unique,
    sealed,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-evidence.json"
PRE_GATE_FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/pre-gate.json"
)
SCHEMAS = ("evidence-closure-manifest/v1",)
HELPERS = {
    "evidence-closure-manifest/v1": ["verify_evidence_closure_context"],
}
ALLOWED_ENTRY_SCHEMA_IDS = (
    "budget-summary/v1",
    "canonical-document/v1",
    "change-set-patch/v1",
    "change-set/v1",
    "completion-proposal/v1",
    "debug-redaction-plan/v1",
    "effect-ledger-snapshot/v1",
    "effective-execution-policy/v1",
    "error-response/v1",
    "execution-profile/v1",
    "execution-state-manifest/v1",
    "gate-decision/v1",
    "gate-input-snapshot/v1",
    "local-tool-receipt/v1",
    "observation-manifest/v1",
    "observation/v1",
    "pre-gate-evidence-closure-manifest/v1",
    "pre-gate-root-set/v1",
    "pre-verifier-observation-manifest/v1",
    "publication-content-manifest/v1",
    "quality-policy-plan/v1",
    "r0-read-payload/v1",
    "r0-read-result/v1",
    "requirement-ledger/v1",
    "source-content/v1",
    "source-selection-policy/v1",
    "source-tree-manifest/v1",
    "stable-error/v1",
    "task-charter/v1",
    "task-report/v1",
    "task-request/v1",
    "terminalization-fact/v1",
    "terminalization-input-snapshot/v1",
    "verification-attestation-manifest/v1",
    "verification-attestation/v1",
    "verifier-input-manifest/v1",
    "verifier-work-report/v1",
    "waiver-event/v1",
)
FORBIDDEN_ENTRY_SCHEMA_IDS = {
    "evidence-closure-manifest/v1",
    "server-debug-assembly/v1",
    "server-debug-snapshot/v1",
    "task-result-core/v1",
    "task-result-transport-ack/v1",
    "task-result-transport-envelope/v1",
    "task-result/v1",
    "worker-debug-fragment/v1",
}


def entry_key(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item["content_schema_id"],
        item["artifact_id"],
        item["sha256"],
    )


class TaskEvidenceFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)
        pre_gate_family = json.loads(PRE_GATE_FAMILY_PATH.read_text(encoding="utf-8"))
        cls.pre_gate_schema = next(
            item
            for item in pre_gate_family["schemas"]
            if item["$id"] == "pre-gate-evidence-closure-manifest/v1"
        )
        cls.pre_gate_fixtures = {
            item["fixture_id"]: item for item in pre_gate_family["fixtures"]
        }

    @staticmethod
    def _content_identity(item: dict[str, object]) -> tuple[object, ...]:
        return tuple(
            item[key]
            for key in (
                "content_schema_id",
                "sha256",
                "size_bytes",
                "media_type",
                "encoding",
            )
        )

    @classmethod
    def _has_artifact_conflict(cls, entries: list[dict[str, object]]) -> bool:
        by_artifact: dict[str, set[tuple[object, ...]]] = {}
        for item in entries:
            by_artifact.setdefault(item["artifact_id"], set()).add(
                cls._content_identity(item)
            )
        return any(len(identities) != 1 for identities in by_artifact.values())

    @classmethod
    def _has_content_alias(cls, entries: list[dict[str, object]]) -> bool:
        by_content: dict[tuple[object, ...], set[str]] = {}
        for item in entries:
            by_content.setdefault(cls._content_identity(item), set()).add(
                item["artifact_id"]
            )
        return any(len(artifact_ids) != 1 for artifact_ids in by_content.values())

    @staticmethod
    def _ref_matches_document(
        ref: dict[str, object], schema_id: str, document: dict[str, object]
    ) -> bool:
        payload = canonical_bytes(document)
        return (
            valid_content_ref(ref, {schema_id})
            and ref["sha256"] == hashlib.sha256(payload).hexdigest()
            and ref["size_bytes"] == len(payload)
            and ref["media_type"] == "application/json"
            and ref["encoding"] == "utf-8"
        )

    @staticmethod
    def _expected_entries(
        document: dict[str, object], pre_gate_manifest: dict[str, object]
    ) -> list[dict[str, object]]:
        candidates = [
            *pre_gate_manifest["entries"],
            document["pre_gate_evidence_closure_ref"],
            document["input_snapshot_ref"],
            document["gate_decision_ref"],
        ]
        unique = {canonical_bytes(item): item for item in candidates}
        return sorted(unique.values(), key=entry_key)

    def _base_manifest_invariants(
        self,
        document: dict[str, object],
        pre_gate_manifest: dict[str, object],
    ) -> bool:
        entries = document.get("entries", [])
        pre_gate_entries = pre_gate_manifest.get("entries", [])
        return (
            sealed(document, self.schemas["evidence-closure-manifest/v1"])
            and sealed(pre_gate_manifest, self.pre_gate_schema)
            and document["task_id"] == pre_gate_manifest["task_id"]
            and self._ref_matches_document(
                document["pre_gate_evidence_closure_ref"],
                "pre-gate-evidence-closure-manifest/v1",
                pre_gate_manifest,
            )
            and valid_content_ref(
                document["input_snapshot_ref"],
                {"gate-input-snapshot/v1", "terminalization-input-snapshot/v1"},
            )
            and valid_content_ref(
                document["gate_decision_ref"], {"gate-decision/v1"}
            )
            and all(valid_content_ref(item) for item in entries)
            and all(valid_content_ref(item) for item in pre_gate_entries)
            and pre_gate_manifest["entry_count"] == len(pre_gate_entries)
            and ordered_unique(pre_gate_entries, entry_key)
            and pre_gate_manifest["pre_gate_closure_digest"]
            == digest(pre_gate_entries)
            and document["entry_count"] == len(entries)
            and ordered_unique(entries, entry_key)
            and document["evidence_closure_digest"] == digest(entries)
        )

    def valid_manifest(
        self,
        document: dict[str, object],
        pre_gate_manifest: dict[str, object],
    ) -> bool:
        entries = document.get("entries", [])
        return (
            self._base_manifest_invariants(document, pre_gate_manifest)
            and all(
                item["content_schema_id"] in ALLOWED_ENTRY_SCHEMA_IDS
                for item in entries
            )
            and not self._has_artifact_conflict(entries)
            and not self._has_content_alias(entries)
            and entries == self._expected_entries(document, pre_gate_manifest)
        )

    @staticmethod
    def _reseal(document: dict[str, object]) -> dict[str, object]:
        result = deepcopy(document)
        result["entry_count"] = len(result["entries"])
        result["evidence_closure_digest"] = digest(result["entries"])
        result.pop("manifest_digest", None)
        result["manifest_digest"] = hashlib.sha256(
            b"pullwise:evidence-closure-manifest:v1\0" + canonical_bytes(result)
        ).hexdigest()
        return result

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-evidence", SCHEMAS, HELPERS)

    def test_entries_have_the_exact_native_content_ref_allowlist(self) -> None:
        entries = self.schemas["evidence-closure-manifest/v1"]["properties"][
            "entries"
        ]
        self.assertEqual(
            {
                "$ref": "content-ref/v1",
                "x-pullwise-content-schema-ids": list(ALLOWED_ENTRY_SCHEMA_IDS),
            },
            entries["items"],
        )
        self.assertEqual(
            tuple(sorted(ALLOWED_ENTRY_SCHEMA_IDS)), ALLOWED_ENTRY_SCHEMA_IDS
        )
        self.assertEqual(38, len(ALLOWED_ENTRY_SCHEMA_IDS))
        self.assertFalse(
            FORBIDDEN_ENTRY_SCHEMA_IDS.intersection(ALLOWED_ENTRY_SCHEMA_IDS)
        )

    def test_final_cardinality_is_pre_gate_cardinality_plus_three(self) -> None:
        final = self.schemas["evidence-closure-manifest/v1"]["properties"]
        pre_gate = self.pre_gate_schema["properties"]
        self.assertEqual(
            pre_gate["entries"]["minItems"] + 3,
            final["entries"]["minItems"],
        )
        self.assertEqual(
            pre_gate["entries"]["maxItems"] + 3,
            final["entries"]["maxItems"],
        )
        self.assertEqual(
            final["entries"]["minItems"], final["entry_count"]["minimum"]
        )
        self.assertEqual(
            final["entries"]["maxItems"], final["entry_count"]["maximum"]
        )

    def test_final_closure_has_exact_typed_gate_edges(self) -> None:
        props = self.schemas["evidence-closure-manifest/v1"]["properties"]
        self.assertEqual(
            "pre-gate-evidence-closure-manifest/v1",
            props["pre_gate_evidence_closure_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            ["gate-input-snapshot/v1", "terminalization-input-snapshot/v1"],
            props["input_snapshot_ref"]["x-pullwise-content-schema-ids"],
        )
        self.assertEqual(
            "gate-decision/v1",
            props["gate_decision_ref"]["x-pullwise-content-schema-id"],
        )

    def test_nonnegative_fixtures_execute_against_their_pre_gate_context(self) -> None:
        pre_gate_golden = self.pre_gate_fixtures[
            "pre_gate_golden_evidence_closure"
        ]["document"]
        pre_gate_retry = self.pre_gate_fixtures[
            "pre_gate_idempotency_evidence_closure"
        ]["document"]
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        golden = fixtures["task_evidence_golden_manifest"]["document"]
        retry = fixtures["task_evidence_idempotency_manifest"]["document"]

        self.assertTrue(self.valid_manifest(golden, pre_gate_golden))
        self.assertTrue(self.valid_manifest(retry, pre_gate_retry))
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
        self.assert_fixture_matrix(
            {
                "evidence-closure-manifest/v1": lambda document: (
                    self.valid_manifest(document, pre_gate_golden)
                ),
            }
        )

    def test_terminalization_snapshot_is_the_other_exact_input_branch(self) -> None:
        pre_gate = self.pre_gate_fixtures[
            "pre_gate_golden_evidence_closure"
        ]["document"]
        golden = next(
            item["document"]
            for item in self.family["fixtures"]
            if item["fixture_id"] == "task_evidence_golden_manifest"
        )
        terminal = deepcopy(golden)
        old_ref = terminal["input_snapshot_ref"]
        new_ref = {
            **old_ref,
            "content_schema_id": "terminalization-input-snapshot/v1",
        }
        terminal["input_snapshot_ref"] = new_ref
        terminal["entries"] = [
            new_ref if item == old_ref else item for item in terminal["entries"]
        ]
        terminal["entries"].sort(key=entry_key)
        self.assertTrue(self.valid_manifest(self._reseal(terminal), pre_gate))

    def test_negative_entries_are_sealed_single_addition_faults(self) -> None:
        pre_gate = self.pre_gate_fixtures[
            "pre_gate_golden_evidence_closure"
        ]["document"]
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        golden = fixtures["task_evidence_golden_manifest"]["document"]
        golden_keys = {canonical_bytes(item) for item in golden["entries"]}

        for fixture_id in (
            "task_evidence_negative_artifact_alias",
            "task_evidence_negative_artifact_digest_conflict",
            "task_evidence_negative_result_back_edge",
        ):
            with self.subTest(fixture_id=fixture_id):
                fixture = fixtures[fixture_id]
                document = fixture["document"]
                self.assertEqual(
                    "CONTRACT_DOCUMENT_INVALID", fixture["expected_code"]
                )
                self.assertTrue(self._base_manifest_invariants(document, pre_gate))
                extras = [
                    item
                    for item in document["entries"]
                    if canonical_bytes(item) not in golden_keys
                ]
                self.assertEqual(1, len(extras))
                repaired = deepcopy(document)
                repaired["entries"].remove(extras[0])
                self.assertEqual(golden, self._reseal(repaired))
                self.assertFalse(self.valid_manifest(document, pre_gate))

        alias = fixtures["task_evidence_negative_artifact_alias"]["document"]
        self.assertTrue(self._has_content_alias(alias["entries"]))
        self.assertFalse(self._has_artifact_conflict(alias["entries"]))
        self.assertTrue(
            all(
                item["content_schema_id"] in ALLOWED_ENTRY_SCHEMA_IDS
                for item in alias["entries"]
            )
        )

        conflict = fixtures[
            "task_evidence_negative_artifact_digest_conflict"
        ]["document"]
        self.assertTrue(self._has_artifact_conflict(conflict["entries"]))
        self.assertFalse(self._has_content_alias(conflict["entries"]))
        self.assertTrue(
            all(
                item["content_schema_id"] in ALLOWED_ENTRY_SCHEMA_IDS
                for item in conflict["entries"]
            )
        )

        back_edge = fixtures["task_evidence_negative_result_back_edge"]["document"]
        self.assertFalse(self._has_artifact_conflict(back_edge["entries"]))
        self.assertFalse(self._has_content_alias(back_edge["entries"]))
        self.assertEqual(
            {"task-result/v1"},
            {
                item["content_schema_id"]
                for item in back_edge["entries"]
                if item["content_schema_id"] not in ALLOWED_ENTRY_SCHEMA_IDS
            },
        )


if __name__ == "__main__":
    unittest.main()
