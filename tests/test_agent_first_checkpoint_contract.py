from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle
from tests.agent_first_checkpoint_support import (
    content_ref,
    golden_checkpoint_set,
    reseal_adversarial,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"


class AgentFirstCheckpointContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = build_bundle(SOURCE_ROOT)
        cls.contract = types.ModuleType("_agent_first_checkpoint_source_contract")
        exec(cls.bundle.python_wrapper, cls.contract.__dict__)

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

    def test_checkpoint_schemas_declare_closed_semantics(self) -> None:
        expected = {
            "machine-checkpoint/v1": {
                "document_rules": ["machine_checkpoint"],
                "contextual_helpers": [],
            },
            "semantic-checkpoint/v1": {
                "document_rules": ["semantic_checkpoint"],
                "contextual_helpers": [],
            },
            "committed-checkpoint-manifest/v1": {
                "document_rules": ["committed_checkpoint_manifest"],
                "contextual_helpers": ["verify_committed_checkpoint_context"],
            },
        }
        for schema_id, semantics in expected.items():
            with self.subTest(schema_id=schema_id):
                self.assertEqual(semantics, self.contract.schema(schema_id)[
                    "x-pullwise-semantics"
                ])

    def assert_contract_error(
        self,
        operation: object,
        detail: str,
        path: str,
    ) -> None:
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            operation()
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
        self.assertEqual(detail, raised.exception.detail)
        self.assertEqual(path, raised.exception.path)

    def test_manifest_rejects_non_contiguous_generation_and_task_version(self) -> None:
        baseline = golden_checkpoint_set(self.contract)["manifest"]
        predecessor = deepcopy(baseline)
        predecessor["previous_generation"] = 1
        predecessor["previous_manifest_hash"] = "f" * 64
        predecessor = reseal_adversarial(
            self.contract, "committed-checkpoint-manifest/v1", predecessor
        )
        self.assert_contract_error(
            lambda: self.contract.verify_document_digest(
                "committed-checkpoint-manifest/v1", predecessor
            ),
            "CHECKPOINT_PREDECESSOR_INVALID",
            "$.previous_generation",
        )

        version = deepcopy(baseline)
        version["committed_task_version"] += 1
        version = reseal_adversarial(
            self.contract, "committed-checkpoint-manifest/v1", version
        )
        self.assert_contract_error(
            lambda: self.contract.verify_document_digest(
                "committed-checkpoint-manifest/v1", version
            ),
            "CHECKPOINT_TASK_VERSION_INVALID",
            "$.committed_task_version",
        )

    def test_semantic_checkpoint_rejects_ambiguous_owner_summary(self) -> None:
        baseline = golden_checkpoint_set(self.contract)["semantic"]
        requirement_ids = baseline["owner_summary"]["next_requirement_ids"]
        cases = []

        unordered = deepcopy(baseline)
        unordered["owner_summary"]["next_requirement_ids"] = list(
            reversed(requirement_ids)
        )
        cases.append((
            "unordered",
            unordered,
            "SEMANTIC_CHECKPOINT_SUMMARY_ORDER_INVALID",
            "$.owner_summary.next_requirement_ids",
        ))

        overlap = deepcopy(baseline)
        overlap["owner_summary"]["completed_requirement_ids"] = [requirement_ids[0]]
        cases.append((
            "overlap",
            overlap,
            "SEMANTIC_CHECKPOINT_REQUIREMENT_OVERLAP",
            "$.owner_summary",
        ))

        pending = deepcopy(baseline)
        pending["pending_interaction_ids"] = [
            "interaction_11111111111111111111111111111111"
        ]
        cases.append((
            "pending",
            pending,
            "SEMANTIC_CHECKPOINT_INTERACTION_MISMATCH",
            "$.pending_interaction_ids",
        ))

        for name, document, detail, path in cases:
            with self.subTest(name=name):
                adversarial = reseal_adversarial(
                    self.contract, "semantic-checkpoint/v1", document
                )
                self.assert_contract_error(
                    lambda adversarial=adversarial: (
                        self.contract.verify_document_digest(
                            "semantic-checkpoint/v1", adversarial
                        )
                    ),
                    detail,
                    path,
                )

    def test_committed_checkpoint_context_binds_both_states_and_chain(self) -> None:
        first = golden_checkpoint_set(self.contract)
        self.assertEqual(
            first["manifest"],
            self.contract.verify_committed_checkpoint_context(
                first["manifest"], first["machine"], first["semantic"]
            ),
        )
        second = golden_checkpoint_set(
            self.contract, generation=2, previous=first["manifest"]
        )
        self.assertEqual(
            second["manifest"],
            self.contract.verify_committed_checkpoint_context(
                second["manifest"],
                second["machine"],
                second["semantic"],
                first["manifest"],
            ),
        )

        drifted_ref = deepcopy(first["manifest"])
        drifted_ref["machine_state_ref"]["sha256"] = "f" * 64
        drifted_ref = reseal_adversarial(
            self.contract, "committed-checkpoint-manifest/v1", drifted_ref
        )
        self.assert_contract_error(
            lambda: self.contract.verify_committed_checkpoint_context(
                drifted_ref, first["machine"], first["semantic"]
            ),
            "CHECKPOINT_STATE_REF_MISMATCH",
            "$.machine_state_ref",
        )

        drifted_chain = deepcopy(second["manifest"])
        drifted_chain["previous_manifest_hash"] = "f" * 64
        drifted_chain = reseal_adversarial(
            self.contract, "committed-checkpoint-manifest/v1", drifted_chain
        )
        self.assert_contract_error(
            lambda: self.contract.verify_committed_checkpoint_context(
                drifted_chain,
                second["machine"],
                second["semantic"],
                first["manifest"],
            ),
            "CHECKPOINT_CHAIN_MISMATCH",
            "$.previous_manifest_hash",
        )

    def test_source_golden_checkpoint_fixture_is_contextually_closed(self) -> None:
        machine = self.contract.fixture("checkpoint_state_golden_machine")[
            "document"
        ]
        semantic = self.contract.fixture("checkpoint_state_golden_semantic")[
            "document"
        ]
        manifest = self.contract.fixture(
            "checkpoint_manifest_golden_genesis_commit"
        )["document"]
        self.assertEqual(
            manifest,
            self.contract.verify_committed_checkpoint_context(
                manifest, machine, semantic
            ),
        )

    def test_python_and_node_checkpoint_helpers_have_exact_parity(self) -> None:
        first = golden_checkpoint_set(self.contract)
        invalid = deepcopy(first["manifest"])
        invalid["semantic_state_ref"]["size_bytes"] += 1
        invalid = reseal_adversarial(
            self.contract, "committed-checkpoint-manifest/v1", invalid
        )
        operations = [
            [first["manifest"], first["machine"], first["semantic"], None],
            [invalid, first["machine"], first["semantic"], None],
        ]
        python = []
        for args in operations:
            try:
                value = self.contract.verify_committed_checkpoint_context(*args)
            except self.contract.ContractValidationError as error:
                python.append({
                    "ok": False,
                    "code": error.code,
                    "detail": error.detail,
                    "path": error.path,
                })
            else:
                python.append({"ok": True, "value": value})

        with tempfile.TemporaryDirectory(prefix="checkpoint-parity-") as scratch:
            root = Path(scratch)
            facade = root / "facade.mjs"
            runner = root / "runner.mjs"
            facade.write_bytes(self.bundle.npm_wrapper)
            runner.write_text(
                "\n".join((
                    f"import * as facade from {json.dumps(facade.as_uri())};",
                    f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                    "const out = [];",
                    "for (const args of operations) {",
                    "  try {",
                    "    const value = await facade.verifyCommittedCheckpointContext(...args);",
                    "    out.push({ok: true, value});",
                    "  } catch (error) {",
                    "    out.push({ok: false, code: error.code, detail: error.detail, path: error.path});",
                    "  }",
                    "}",
                    "process.stdout.write(JSON.stringify(out));",
                )),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner)],
                check=True,
                capture_output=True,
                encoding="utf-8",
                timeout=120,
            )
        self.assertEqual(python, json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
