from __future__ import annotations

from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"


class AgentFirstCheckpointContractTest(unittest.TestCase):
    def test_current_bundle_publishes_closed_dual_checkpoint_shapes(self) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        families = {
            family["family_id"]: family for family in bundle.document["families"]
        }
        self.assertIn("task-checkpoint-state", families)
        self.assertIn("task-checkpoint-manifest", families)
        schemas = {
            schema["$id"]: schema
            for family_id in ("task-checkpoint-state", "task-checkpoint-manifest")
            for schema in families[family_id]["schemas"]
        }
        self.assertEqual(
            {
                "machine-checkpoint/v1",
                "semantic-checkpoint/v1",
                "committed-checkpoint-manifest/v1",
            },
            set(schemas),
        )
        expected_fields = {
            "machine-checkpoint/v1": {
                "schema_id",
                "package",
                "task_id",
                "generation",
                "task_version",
                "attempt_id",
                "native_epoch",
                "owner_id",
                "owner_epoch",
                "session_id",
                "transport_binding",
                "runtime_thread_id",
                "workspace_state_ref",
                "execution_state_ref",
                "in_flight_tool_invocation_ids",
                "budget_watermark",
                "effect_watermark",
                "observation_watermark",
                "event_seq",
                "created_at",
                "machine_checkpoint_digest",
            },
            "semantic-checkpoint/v1": {
                "schema_id",
                "package",
                "task_id",
                "generation",
                "task_version",
                "owner_id",
                "owner_epoch",
                "task_request_ref",
                "charter_ref",
                "requirement_ledger_ref",
                "owner_summary",
                "pending_interaction_ids",
                "proposal_round",
                "evidence_refs",
                "created_at",
                "semantic_checkpoint_digest",
            },
            "committed-checkpoint-manifest/v1": {
                "schema_id",
                "package",
                "task_id",
                "generation",
                "previous_generation",
                "previous_manifest_hash",
                "committed_from_task_version",
                "committed_task_version",
                "native_epoch",
                "attempt_id",
                "owner_epoch",
                "machine_state_ref",
                "semantic_state_ref",
                "budget_watermark",
                "effect_watermark",
                "observation_watermark",
                "event_seq",
                "created_at",
                "manifest_hash",
            },
        }
        for schema_id, fields in expected_fields.items():
            with self.subTest(schema_id=schema_id):
                schema = schemas[schema_id]
                self.assertEqual(fields, set(schema["required"]))
                self.assertEqual(fields, set(schema["properties"]))
                self.assertFalse(schema["additionalProperties"])

        machine = schemas["machine-checkpoint/v1"]
        self.assertEqual(
            "source-tree-manifest/v1",
            machine["properties"]["workspace_state_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "execution-state-manifest/v1",
            machine["properties"]["execution_state_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        manifest = schemas["committed-checkpoint-manifest/v1"]
        self.assertEqual(
            "machine-checkpoint/v1",
            manifest["properties"]["machine_state_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "semantic-checkpoint/v1",
            manifest["properties"]["semantic_state_ref"][
                "x-pullwise-content-schema-id"
            ],
        )


if __name__ == "__main__":
    unittest.main()
