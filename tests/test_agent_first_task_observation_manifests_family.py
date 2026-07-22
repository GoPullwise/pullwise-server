from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    ordered_unique,
    sealed,
    valid_actor,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT
    / "contracts/agent-first/current/source/families/task-observation-manifests.json"
)
SCHEMAS = (
    "observation-manifest/v1",
    "pre-verifier-observation-manifest/v1",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HELPERS = {
    "observation-manifest/v1": ["verify_observation_manifest_extension"],
    "pre-verifier-observation-manifest/v1": [
        "verify_observation_manifest_extension"
    ],
}


class TaskObservationManifestsFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_manifest(self, document: dict[str, object]) -> bool:
        entries = document["entries"]
        if not (
            sealed(document, self.schemas[document["schema_id"]])
            and document["entry_count"] == len(entries)
            and ordered_unique(
                entries,
                lambda item: (item["observation_seq"], item["observation_id"]),
            )
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
                item["actor"]["kind"]
                in {"task_owner", "legacy_domain_reviewer"}
                for item in entries
            )
        return valid_content_ref(
            document["pre_verifier_observation_manifest_ref"],
            {"pre-verifier-observation-manifest/v1"},
        ) and any(item["actor"]["kind"] == "quality_verifier" for item in entries)

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-observation-manifests", SCHEMAS, HELPERS)

    def test_manifest_entries_and_predecessor_are_typed(self) -> None:
        for schema_id in SCHEMAS:
            entry = self.schemas[schema_id]["properties"]["entries"]["items"]
            self.assertEqual("actor/v1", entry["properties"]["actor"]["$ref"])
            self.assertEqual(
                "observation/v1",
                entry["properties"]["observation_ref"][
                    "x-pullwise-content-schema-id"
                ],
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
                "pre-verifier-observation-manifest/v1": self.valid_manifest,
            }
        )

    def test_context_helper_must_enforce_exact_extension_and_identity(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        final = fixtures["task_observation_golden_final_manifest"]["document"]
        pre = fixtures["task_observation_golden_pre_manifest"]["document"]
        pre_ids = {item["observation_id"] for item in pre["entries"]}
        final_ids = {item["observation_id"] for item in final["entries"]}
        self.assertTrue(pre_ids < final_ids)
        self.assertEqual(
            (pre["task_id"], pre["attempt_id"], pre["native_epoch"]),
            (final["task_id"], final["attempt_id"], final["native_epoch"]),
        )
        entries = fixtures["task_observation_negative_final_manifest_order"][
            "document"
        ]["entries"]
        self.assertGreater(entries[0]["observation_seq"], entries[1]["observation_seq"])


if __name__ == "__main__":
    unittest.main()
