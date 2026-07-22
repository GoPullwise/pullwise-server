from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    sealed,
    timestamp_millis,
    valid_actor,
    valid_availability,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-observation.json"
SCHEMAS = ("observation/v1",)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class TaskObservationFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_observation(self, document: dict[str, object]) -> bool:
        started = timestamp_millis(document["started_at"])
        completed = timestamp_millis(document["completed_at"])
        duration = document["duration_ms"]
        status = document["status"]
        execution = document["execution_state_id"]
        result = document["result_ref"]
        if status == "policy_denied":
            timing = all(
                document[key] is None
                for key in ("started_at", "completed_at", "duration_ms", "exit_code")
            )
            status_matrix = (
                timing
                and result.get("availability") == "available"
                and result["ref"]["content_schema_id"] == "error-response/v1"
                and document["source_state_before_id"]
                == document["source_state_after_id"]
                and execution is None
            )
        else:
            timing = (
                started is not None
                and completed is not None
                and completed >= started
                and duration == completed - started
            )
            status_matrix = timing and (
                status != "succeeded" or result.get("availability") == "available"
            )
        return (
            sealed(document, self.schemas["observation/v1"])
            and valid_actor(document["actor"])
            and status_matrix
            and (execution is None or HEX64.fullmatch(execution) is not None)
            and valid_availability(document["stdout_ref"], {"source-content/v1"})
            and valid_availability(document["stderr_ref"], {"source-content/v1"})
            and valid_availability(
                result, {"error-response/v1", "r0-read-result/v1"}
            )
            and valid_availability(
                document["redaction_report_ref"], {"source-content/v1"}
            )
            and document["partial_side_effect"] is False
            and document["observation_seq"] >= 1
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

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix({"observation/v1": self.valid_observation})

    def test_current_domain_reviewer_actor_has_no_legacy_alias(self) -> None:
        actor = {
            "schema_id": "actor/v1",
            "kind": "domain_reviewer",
            "id": "reviewer_11111111111111111111111111111111",
            "session_id": "sess_11111111111111111111111111111111",
        }
        self.assertTrue(valid_actor(actor))
        actor["kind"] = "legacy_domain_reviewer"
        self.assertFalse(valid_actor(actor))

    def test_adversarial_fixtures_cover_full_execution_matrix(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertIs(
            True,
            fixtures["task_observation_negative_partial_side_effect"]["document"][
                "partial_side_effect"
            ],
        )
        invalid = fixtures["task_observation_negative_invalid_instant"]["document"]
        self.assertIsNone(timestamp_millis(invalid["completed_at"]))
        denied = fixtures["task_observation_negative_status_timing_matrix"]["document"]
        self.assertEqual("policy_denied", denied["status"])
        self.assertIsNotNone(denied["started_at"])


if __name__ == "__main__":
    unittest.main()
