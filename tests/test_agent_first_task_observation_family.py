from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    ordered_unique,
    sealed,
    timestamp_millis,
    valid_actor,
    valid_availability,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-observation.json"
SCHEMAS = (
    "observation-manifest/v1",
    "observation/v1",
    "pre-verifier-observation-manifest/v1",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class TaskObservationFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_observation(self, document: dict[str, object]) -> bool:
        schema = self.schemas["observation/v1"]
        started = timestamp_millis(document["started_at"])
        completed = timestamp_millis(document["completed_at"])
        timing = (
            started is None
            and completed is None
            and document["duration_ms"] is None
            or started is not None
            and completed is not None
            and completed >= started
            and document["duration_ms"] == completed - started
        )
        execution = document["execution_state_id"]
        return (
            sealed(document, schema)
            and valid_actor(document["actor"])
            and timing
            and (execution is None or HEX64.fullmatch(execution) is not None)
            and valid_availability(document["stdout_ref"], {"source-content/v1"})
            and valid_availability(document["stderr_ref"], {"source-content/v1"})
            and valid_availability(
                document["result_ref"], {"error-response/v1", "r0-read-result/v1"}
            )
            and valid_availability(document["redaction_report_ref"], {"source-content/v1"})
            and document["partial_side_effect"] is False
            and document["observation_seq"] >= 1
        )

    def valid_manifest(self, document: dict[str, object]) -> bool:
        schema = self.schemas[document["schema_id"]]
        entries = document["entries"]
        if not sealed(document, schema) or document["entry_count"] != len(entries):
            return False
        if not ordered_unique(
            entries, lambda item: (item["observation_seq"], item["observation_id"])
        ):
            return False
        for entry in entries:
            execution = entry["execution_state_id"]
            if not (
                valid_content_ref(entry["observation_ref"], {"observation/v1"})
                and valid_actor(entry["actor"])
                and (execution is None or HEX64.fullmatch(execution) is not None)
            ):
                return False
        if document["schema_id"] == "pre-verifier-observation-manifest/v1":
            return all(
                item["actor"]["kind"] in {"task_owner", "legacy_domain_reviewer"}
                for item in entries
            )
        return valid_content_ref(
            document["pre_verifier_observation_manifest_ref"],
            {"pre-verifier-observation-manifest/v1"},
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-observation", SCHEMAS)

    def test_all_cross_schema_edges_are_typed(self) -> None:
        observation = self.schemas["observation/v1"]["properties"]
        self.assertEqual("actor/v1", observation["actor"]["$ref"])
        self.assertEqual(
            ["error-response/v1", "r0-read-result/v1"],
            observation["result_ref"]["x-pullwise-availability-content-schema-ids"],
        )
        for key in ("stdout_ref", "stderr_ref", "redaction_report_ref"):
            self.assertEqual("availability-ref/v1", observation[key]["$ref"])
            self.assertEqual(
                "source-content/v1",
                observation[key]["x-pullwise-availability-content-schema-id"],
            )
        final = self.schemas["observation-manifest/v1"]["properties"]
        self.assertEqual(
            "pre-verifier-observation-manifest/v1",
            final["pre_verifier_observation_manifest_ref"][
                "x-pullwise-content-schema-id"
            ],
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {
                "observation-manifest/v1": self.valid_manifest,
                "observation/v1": self.valid_observation,
                "pre-verifier-observation-manifest/v1": self.valid_manifest,
            }
        )

    def test_adversarial_fixtures_cover_side_effect_and_manifest_order(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertIs(
            True,
            fixtures["task_observation_negative_partial_side_effect"]["document"][
                "partial_side_effect"
            ],
        )
        entries = fixtures["task_observation_negative_final_manifest_order"][
            "document"
        ]["entries"]
        self.assertGreater(entries[0]["observation_seq"], entries[1]["observation_seq"])


if __name__ == "__main__":
    unittest.main()
