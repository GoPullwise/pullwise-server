from __future__ import annotations

from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import load_family


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


if __name__ == "__main__":
    unittest.main()
