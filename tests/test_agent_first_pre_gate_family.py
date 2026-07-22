from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import load_family
from tests.agent_first_task_evidence_support import (
    canonical_bytes,
    sealed,
    valid_availability,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/pre-gate.json"
)


class AgentFirstPreGateFamilyTest(unittest.TestCase):
    ROOT_FIELDS = {
        "request",
        "policy",
        "charter",
        "ledger",
        "waiver_events",
        "proposal",
        "original_source",
        "final_source",
        "execution_states",
        "change_set",
        "pre_observation_manifest",
        "final_observation_manifest",
        "verifier_inputs",
        "verifier_work",
        "attestations",
        "artifacts",
        "report",
        "effect_ledger",
        "budget_summary",
        "termination_facts",
        "publication_content_manifest",
        "debug_redaction_plan",
    }
    ROOT_TARGETS = {
        "request": {"task-request/v1"},
        "policy": {"effective-execution-policy/v1"},
        "charter": {"task-charter/v1"},
        "ledger": {"requirement-ledger/v1"},
        "waiver_events": {"waiver-event/v1"},
        "proposal": {"completion-proposal/v1"},
        "original_source": {"source-tree-manifest/v1"},
        "final_source": {"source-tree-manifest/v1"},
        "execution_states": {"execution-state-manifest/v1"},
        "change_set": {"change-set/v1"},
        "pre_observation_manifest": {
            "pre-verifier-observation-manifest/v1"
        },
        "final_observation_manifest": {"observation-manifest/v1"},
        "verifier_inputs": {"verifier-input-manifest/v1"},
        "verifier_work": {"verifier-work-report/v1"},
        "attestations": {"verification-attestation-manifest/v1"},
        "artifacts": {
            "change-set-patch/v1",
            "change-set/v1",
            "r0-read-result/v1",
            "source-content/v1",
            "task-report/v1",
        },
        "report": {"task-report/v1"},
        "effect_ledger": {"effect-ledger-snapshot/v1"},
        "budget_summary": {"budget-summary/v1"},
        "termination_facts": {"terminalization-fact/v1"},
        "publication_content_manifest": {"publication-content-manifest/v1"},
        "debug_redaction_plan": {"debug-redaction-plan/v1"},
    }
    ARRAY_ROOTS = {
        "waiver_events",
        "execution_states",
        "verifier_inputs",
        "verifier_work",
        "artifacts",
        "termination_facts",
    }
    SUCCESS_OUTCOMES = {
        "COMPLETED",
        "COMPLETED_WITH_WAIVERS",
        "NO_CHANGE_NEEDED",
    }
    FORBIDDEN_CLOSURE_TARGETS = {
        "evidence-closure-manifest/v1",
        "error-response/v1",
        "gate-decision/v1",
        "gate-input-snapshot/v1",
        "server-debug-assembly/v1",
        "server-debug-snapshot/v1",
        "task-result-core/v1",
        "task-result/v1",
        "terminalization-input-snapshot/v1",
        "worker-debug-fragment/v1",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.family = load_family(FAMILY_PATH, "pre-gate", {}, set())
        cls.schemas = {
            schema["$id"]: schema for schema in cls.family["schemas"]
        }

    def test_family_is_available_through_the_public_source_api(self) -> None:
        self.assertEqual("pre-gate", self.family["family_id"])
        self.assertEqual(
            [
                "pre-gate-evidence-closure-manifest/v1",
                "pre-gate-root-set/v1",
            ],
            [schema["$id"] for schema in self.family["schemas"]],
        )

    def test_root_keys_and_reference_targets_are_fixed_and_finite(self) -> None:
        root = self.schemas["pre-gate-root-set/v1"]
        metadata = {
            "schema_id",
            "task_id",
            "outcome_candidate",
            "root_set_digest",
        }
        self.assertEqual(metadata | self.ROOT_FIELDS, set(root["required"]))
        self.assertEqual(set(root["required"]), set(root["properties"]))

        typed_nodes = [
            root["properties"][field].get(
                "items", root["properties"][field]
            )
            for field in self.ROOT_FIELDS
        ]
        closure = self.schemas["pre-gate-evidence-closure-manifest/v1"]
        typed_nodes.append(closure["properties"]["entries"]["items"])
        for node in typed_nodes:
            annotations = [
                value
                for key, value in node.items()
                if key.startswith("x-pullwise-")
            ]
            self.assertEqual(1, len(annotations), node)
            targets = (
                annotations[0]
                if isinstance(annotations[0], list)
                else [annotations[0]]
            )
            self.assertTrue(targets, node)
            self.assertEqual(sorted(set(targets)), targets, node)
        closure_targets = closure["properties"]["entries"]["items"][
            "x-pullwise-content-schema-ids"
        ]
        self.assertFalse(
            self.FORBIDDEN_CLOSURE_TARGETS.intersection(closure_targets)
        )

    @staticmethod
    def _availability_branch(document: dict[str, object], field: str) -> str:
        return document[field]["availability"]

    def _valid_root(
        self,
        document: dict[str, object],
        *,
        enforce_outcome_availability: bool = True,
    ) -> bool:
        schema = self.schemas["pre-gate-root-set/v1"]
        if set(document) != set(schema["required"]) or not sealed(
            document, schema
        ):
            return False
        for field, targets in self.ROOT_TARGETS.items():
            values = document[field] if field in self.ARRAY_ROOTS else [document[field]]
            if not all(valid_availability(value, targets) for value in values):
                return False
        if not enforce_outcome_availability:
            return True
        always_available = (
            "request",
            "policy",
            "ledger",
            "effect_ledger",
            "budget_summary",
            "publication_content_manifest",
            "debug_redaction_plan",
        )
        if any(
            self._availability_branch(document, field) != "available"
            for field in always_available
        ):
            return False
        outcome = document["outcome_candidate"]
        if outcome in self.SUCCESS_OUTCOMES:
            required = (
                "charter",
                "proposal",
                "original_source",
                "final_source",
                "pre_observation_manifest",
                "final_observation_manifest",
                "attestations",
                "report",
            )
            return all(
                self._availability_branch(document, field) == "available"
                for field in required
            ) and all(
                document[field]
                and all(item["availability"] == "available" for item in document[field])
                for field in ("verifier_inputs", "verifier_work")
            )
        if outcome == "PARTIAL":
            required = (
                "proposal",
                "original_source",
                "final_source",
                "final_observation_manifest",
                "report",
            )
            return all(
                self._availability_branch(document, field) == "available"
                for field in required
            )
        unavailable_only = (
            "proposal",
            "attestations",
            "report",
        )
        honest_source = (
            "original_source",
            "final_source",
            "final_observation_manifest",
        )
        return bool(document["termination_facts"]) and all(
            item["availability"] == "available"
            for item in document["termination_facts"]
        ) and all(
            self._availability_branch(document, field)
            in {"not_applicable", "unavailable"}
            for field in unavailable_only
        ) and all(
            self._availability_branch(document, field)
            in {"available", "unavailable"}
            for field in honest_source
        )

    def _valid_closure(
        self,
        document: dict[str, object],
        *,
        enforce_direction: bool = True,
    ) -> bool:
        schema = self.schemas["pre-gate-evidence-closure-manifest/v1"]
        entries = document.get("entries", [])
        if set(document) != set(schema["required"]) or not sealed(
            document, schema
        ):
            return False
        keys = [
            (item["content_schema_id"], item["artifact_id"], item["sha256"])
            for item in entries
        ]
        root_ref = document["pre_gate_root_set_ref"]
        return (
            all(valid_content_ref(item) for item in entries)
            and valid_content_ref(root_ref, {"pre-gate-root-set/v1"})
            and document["entry_count"] == len(entries)
            and keys == sorted(set(keys))
            and root_ref in entries
            and document["pre_gate_closure_digest"]
            == hashlib.sha256(canonical_bytes(entries)).hexdigest()
            and (
                not enforce_direction
                or not self.FORBIDDEN_CLOSURE_TARGETS.intersection(
                    item["content_schema_id"] for item in entries
                )
            )
        )

    def test_fixtures_are_sealed_meaningful_and_byte_exact_on_retry(self) -> None:
        fixtures = {
            item["fixture_id"]: item for item in self.family["fixtures"]
        }
        self.assertEqual(sorted(fixtures), list(fixtures))
        pairs = (
            (
                "pre_gate_golden_evidence_closure",
                "pre_gate_idempotency_evidence_closure",
                self._valid_closure,
            ),
            (
                "pre_gate_golden_root_set",
                "pre_gate_idempotency_root_set",
                self._valid_root,
            ),
        )
        for golden_id, retry_id, validator in pairs:
            golden = fixtures[golden_id]["document"]
            retry = fixtures[retry_id]["document"]
            self.assertTrue(validator(golden), golden_id)
            self.assertNotEqual("0" * 64, golden[next(
                key for key in golden if key.endswith("_digest")
            )])
            self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))

        reverse = fixtures["pre_gate_negative_evidence_reverse_edge"]
        policy = fixtures["pre_gate_negative_required_policy_unavailable"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", reverse["expected_code"])
        self.assertTrue(
            self._valid_closure(reverse["document"], enforce_direction=False)
        )
        self.assertFalse(self._valid_closure(reverse["document"]))
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", policy["expected_code"])
        self.assertTrue(
            self._valid_root(
                policy["document"], enforce_outcome_availability=False
            )
        )
        self.assertFalse(self._valid_root(policy["document"]))

    def _seal_root(self, document: dict[str, object]) -> dict[str, object]:
        sealed_document = copy.deepcopy(document)
        sealed_document.pop("root_set_digest", None)
        domain = b"pullwise:pre-gate-root-set:v1\0"
        sealed_document["root_set_digest"] = hashlib.sha256(
            domain + canonical_bytes(sealed_document)
        ).hexdigest()
        return sealed_document

    def test_terminal_outcomes_keep_honest_availability(self) -> None:
        fixtures = {
            item["fixture_id"]: item for item in self.family["fixtures"]
        }
        golden = fixtures["pre_gate_golden_terminal_root_set"]["document"]
        self.assertEqual("FAILED", golden["outcome_candidate"])
        self.assertTrue(self._valid_root(golden))

        for outcome in ("BLOCKED", "CANCELLED"):
            candidate = copy.deepcopy(golden)
            candidate["outcome_candidate"] = outcome
            self.assertTrue(self._valid_root(self._seal_root(candidate)), outcome)

        false_proposal = copy.deepcopy(golden)
        false_proposal["proposal"] = copy.deepcopy(
            fixtures["pre_gate_golden_root_set"]["document"]["proposal"]
        )
        self.assertFalse(self._valid_root(self._seal_root(false_proposal)))

        dishonest_source = copy.deepcopy(golden)
        dishonest_source["final_source"] = {
            "availability": "not_applicable",
            "reason_code": "SOURCE_STATE_UNAVAILABLE",
        }
        self.assertFalse(self._valid_root(self._seal_root(dishonest_source)))


if __name__ == "__main__":
    unittest.main()
