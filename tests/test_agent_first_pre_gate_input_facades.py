from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"
FAMILY_IDS = (
    "core",
    "task-result-identities",
    "task-result-reasons",
    "gate-preparation",
    "pre-gate",
    "gate-input",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def reseal(
    document: dict[str, object], field: str, domain: str
) -> dict[str, object]:
    result = deepcopy(document)
    result.pop(field, None)
    result[field] = hashlib.sha256(
        domain.encode("utf-8") + b"\0" + canonical_bytes(result)
    ).hexdigest()
    return result


def content_ref(
    document: dict[str, object], artifact_seed: str
) -> dict[str, object]:
    raw = canonical_bytes(document)
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": f"art_{artifact_seed * 32}",
        "content_schema_id": document["schema_id"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }


class AgentFirstPreGateInputFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = [
            json.loads(
                (FAMILY_ROOT / f"{family_id}.json").read_text(encoding="utf-8")
            )
            for family_id in FAMILY_IDS
        ]
        reasons = next(
            family
            for family in families
            if family["family_id"] == "task-result-reasons"
        )
        availability = next(
            schema
            for schema in reasons["schemas"]
            if schema["$id"] == "availability-ref/v1"
        )
        # The small facade evaluator applies oneOf and the containing object.
        # These two outer keywords duplicate the already closed branches.
        availability.pop("type")
        availability.pop("additionalProperties")

        error_family = json.loads(
            (FAMILY_ROOT / "receipt-error.json").read_text(encoding="utf-8")
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        families.append(
            {
                "family_id": "receipt-error",
                "schemas": [],
                "fixtures": [error_fixture],
            }
        )
        canonical = canonical_bytes(
            {"root_manifest": {"schema_registry": []}, "families": families}
        )
        python_wrapper = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        cls.python = types.ModuleType("_pre_gate_input_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        cls.families = {family["family_id"]: family for family in families}
        cls.fixtures = {
            item["fixture_id"]: item
            for family_id in ("pre-gate", "gate-input", "gate-preparation")
            for item in cls.families[family_id]["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    @staticmethod
    def available_refs(root_set: dict[str, object]) -> list[dict[str, object]]:
        refs = []
        for field, value in root_set.items():
            if field in {
                "schema_id",
                "task_id",
                "outcome_candidate",
                "root_set_digest",
            }:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict) and item.get("availability") == "available":
                    refs.append(deepcopy(item["ref"]))
        return refs

    @staticmethod
    def ref_key(value: dict[str, object]) -> tuple[object, ...]:
        return (
            value["content_schema_id"],
            value["artifact_id"],
            value["sha256"],
        )

    def make_manifest(
        self,
        root_set: dict[str, object],
        *,
        extra_refs: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        root_ref = content_ref(root_set, "a")
        entries = self.available_refs(root_set) + [root_ref] + (extra_refs or [])
        by_key = {self.ref_key(item): item for item in entries}
        entries = [by_key[key] for key in sorted(by_key)]
        unsigned = {
            "schema_id": "pre-gate-evidence-closure-manifest/v1",
            "task_id": root_set["task_id"],
            "pre_gate_root_set_ref": root_ref,
            "entries": entries,
            "entry_count": len(entries),
            "pre_gate_closure_digest": hashlib.sha256(
                canonical_bytes(entries)
            ).hexdigest(),
        }
        return reseal(
            unsigned,
            "manifest_digest",
            "pullwise:pre-gate-evidence-closure-manifest:v1",
        )

    def make_success_context(self) -> list[object]:
        root_set = self.document("pre_gate_golden_root_set")
        snapshot = self.document("gate_input_golden_success_snapshot")
        quality_ref = deepcopy(snapshot["quality_policy_plan_ref"])
        quality_ref["artifact_id"] = "art_deadbeefdeadbeefdeadbeefdeadbeef"
        manifest = self.make_manifest(root_set, extra_refs=[quality_ref])
        mapping = {
            "request_ref": "request",
            "policy_ref": "policy",
            "requirement_ledger_ref": "ledger",
            "completion_proposal_ref": "proposal",
            "original_source_ref": "original_source",
            "final_source_ref": "final_source",
            "pre_observation_manifest_ref": "pre_observation_manifest",
            "final_observation_manifest_ref": "final_observation_manifest",
            "verification_attestation_manifest_ref": "attestations",
            "effect_ledger_ref": "effect_ledger",
            "budget_summary_ref": "budget_summary",
            "publication_content_manifest_ref": "publication_content_manifest",
            "debug_redaction_plan_ref": "debug_redaction_plan",
        }
        for target, source in mapping.items():
            snapshot[target] = deepcopy(root_set[source]["ref"])
        snapshot["execution_state_refs"] = [
            deepcopy(item["ref"]) for item in root_set["execution_states"]
        ]
        snapshot["change_set"] = deepcopy(root_set["change_set"])
        snapshot["requested_outcome"] = root_set["outcome_candidate"]
        snapshot["pre_gate_root_set_ref"] = content_ref(root_set, "a")
        snapshot["pre_gate_evidence_closure_ref"] = content_ref(manifest, "b")
        snapshot["pre_gate_closure_digest"] = manifest[
            "pre_gate_closure_digest"
        ]
        snapshot["quality_policy_plan_ref"] = deepcopy(quality_ref)
        snapshot = reseal(
            snapshot, "input_digest", "pullwise:gate-input-snapshot:v1"
        )
        return [snapshot, root_set, manifest]

    def make_terminal_context(self) -> list[object]:
        root_set = self.document("pre_gate_golden_terminal_root_set")
        snapshot = self.document("gate_input_golden_terminalization_snapshot")
        fact = self.document("gate_preparation_golden_terminalization_fact")
        fact_ref = content_ref(fact, "c")
        root_set["termination_facts"] = [
            {"availability": "available", "ref": fact_ref}
        ]
        root_set = reseal(
            root_set, "root_set_digest", "pullwise:pre-gate-root-set:v1"
        )
        manifest = self.make_manifest(root_set)
        mapping = {
            "request_ref": "request",
            "policy_ref": "policy",
            "requirement_ledger_ref": "ledger",
            "original_source": "original_source",
            "final_source": "final_source",
            "final_observation_manifest": "final_observation_manifest",
            "effect_ledger_ref": "effect_ledger",
            "budget_summary_ref": "budget_summary",
            "publication_content_manifest_ref": "publication_content_manifest",
            "debug_redaction_plan_ref": "debug_redaction_plan",
        }
        for target, source in mapping.items():
            value = root_set[source]
            snapshot[target] = deepcopy(
                value["ref"] if target.endswith("_ref") else value
            )
        snapshot["terminalization_fact_refs"] = [fact_ref]
        snapshot["pre_gate_root_set_ref"] = content_ref(root_set, "a")
        snapshot["pre_gate_evidence_closure_ref"] = content_ref(manifest, "b")
        snapshot["pre_gate_closure_digest"] = manifest[
            "pre_gate_closure_digest"
        ]
        snapshot = reseal(
            snapshot,
            "input_digest",
            "pullwise:terminalization-input-snapshot:v1",
        )
        return [snapshot, root_set, manifest, [fact]]

    def test_source_fixture_matrix_executes_with_python_node_parity(self) -> None:
        fixtures = [
            item
            for family_id in ("pre-gate", "gate-input")
            for item in self.families[family_id]["fixtures"]
        ]
        operations = [
            {
                "kind": "document",
                "schema_id": item["schema_id"],
                "args": [item["document"]],
            }
            for item in fixtures
        ]
        results = self.assert_parity(operations)
        for fixture, result in zip(fixtures, results):
            with self.subTest(fixture_id=fixture["fixture_id"]):
                self.assertEqual(
                    fixture["expected_code"], None if result["ok"] else result["code"]
                )

    def test_document_rules_reject_digest_order_and_binding_drift(self) -> None:
        closure = self.document("pre_gate_golden_evidence_closure")
        reversed_closure = deepcopy(closure)
        reversed_closure["entries"].reverse()
        reversed_closure = reseal(
            reversed_closure,
            "manifest_digest",
            "pullwise:pre-gate-evidence-closure-manifest:v1",
        )
        wrong_closure_digest = deepcopy(closure)
        wrong_closure_digest["pre_gate_closure_digest"] = "0" * 64
        wrong_closure_digest = reseal(
            wrong_closure_digest,
            "manifest_digest",
            "pullwise:pre-gate-evidence-closure-manifest:v1",
        )
        success = self.document("gate_input_golden_success_snapshot")
        wrong_deadline = deepcopy(success)
        wrong_deadline["trusted_wall_time_at"] = "2026-01-01T00:09:01.000Z"
        wrong_deadline = reseal(
            wrong_deadline, "input_digest", "pullwise:gate-input-snapshot:v1"
        )
        terminal = self.document("gate_input_golden_terminalization_snapshot")
        wrong_attempt = deepcopy(terminal)
        wrong_attempt["attempt_id"] = None
        wrong_attempt = reseal(
            wrong_attempt,
            "input_digest",
            "pullwise:terminalization-input-snapshot:v1",
        )
        cases = [
            (
                "pre-gate-evidence-closure-manifest/v1",
                reversed_closure,
                "PRE_GATE_CLOSURE_ENTRY_ORDER_INVALID",
                "$.entries",
            ),
            (
                "pre-gate-evidence-closure-manifest/v1",
                wrong_closure_digest,
                "PRE_GATE_CLOSURE_DIGEST_INVALID",
                "$.pre_gate_closure_digest",
            ),
            (
                "gate-input-snapshot/v1",
                wrong_deadline,
                "GATE_INPUT_DEADLINE_INVALID",
                "$.trusted_wall_time_at",
            ),
            (
                "terminalization-input-snapshot/v1",
                wrong_attempt,
                "TERMINALIZATION_ATTEMPT_BINDING_INVALID",
                "$.attempt_id",
            ),
        ]
        results = self.assert_parity(
            [
                {"kind": "document", "schema_id": schema_id, "args": [document]}
                for schema_id, document, _, _ in cases
            ]
        )
        for result, (_, _, detail, path) in zip(results, cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": path,
                },
                result,
            )

    def test_direct_document_context_helpers_bind_every_projection(self) -> None:
        success = self.make_success_context()
        terminal = self.make_terminal_context()
        root = success[1]
        valid = [
            {
                "kind": "root_context",
                "args": [root, root["task_id"], root["outcome_candidate"]],
            },
            {"kind": "closure_context", "args": [success[2], success[1]]},
            {"kind": "success_context", "args": success},
            {"kind": "terminal_context", "args": terminal},
        ]
        invalid = []

        stale_root = deepcopy(success)
        stale_root[0]["request_ref"]["sha256"] = "0" * 64
        stale_root[0] = reseal(
            stale_root[0], "input_digest", "pullwise:gate-input-snapshot:v1"
        )
        invalid.append(
            (
                {"kind": "success_context", "args": stale_root},
                "GATE_INPUT_STALE",
                "$.request_ref",
            )
        )

        stale_closure = deepcopy(success)
        stale_closure[0]["pre_gate_closure_digest"] = "0" * 64
        stale_closure[0] = reseal(
            stale_closure[0], "input_digest", "pullwise:gate-input-snapshot:v1"
        )
        invalid.append(
            (
                {"kind": "success_context", "args": stale_closure},
                "EVIDENCE_CLOSURE_INVALID",
                "$.pre_gate_closure_digest",
            )
        )

        missing_root = deepcopy(success)
        direct_ref = missing_root[1]["request"]["ref"]
        missing_root[2]["entries"].remove(direct_ref)
        missing_root[2]["entry_count"] -= 1
        missing_root[2]["pre_gate_closure_digest"] = hashlib.sha256(
            canonical_bytes(missing_root[2]["entries"])
        ).hexdigest()
        missing_root[2] = reseal(
            missing_root[2],
            "manifest_digest",
            "pullwise:pre-gate-evidence-closure-manifest:v1",
        )
        invalid.append(
            (
                {
                    "kind": "closure_context",
                    "args": [missing_root[2], missing_root[1]],
                },
                "EVIDENCE_CLOSURE_INVALID",
                "$.entries",
            )
        )

        bad_fact = deepcopy(terminal)
        bad_fact[3][0]["observed_task_version"] = terminal[0]["task_version"]
        bad_fact[3][0]["idempotency_key"] = (
            f"terminalize:budget_exhausted:{terminal[0]['task_version']}"
        )
        bad_fact[3][0] = reseal(
            bad_fact[3][0],
            "fact_digest",
            "pullwise:terminalization-fact:v1",
        )
        invalid.append(
            (
                {"kind": "terminal_context", "args": bad_fact},
                "GATE_INPUT_STALE",
                "$.terminalization_fact_refs[0]",
            )
        )

        results = self.assert_parity(valid + [item[0] for item in invalid])
        self.assertTrue(all(result["ok"] for result in results[: len(valid)]))
        for result, (_, detail, path) in zip(results[len(valid) :], invalid):
            self.assertEqual(detail, result["code"])
            self.assertEqual(detail, result["detail"])
            self.assertEqual(path, result["path"])

    def test_helper_exports_and_python_signatures_are_stable(self) -> None:
        signatures = {
            "verify_pre_gate_root_set_context": [
                "root_set",
                "task_id",
                "outcome_candidate",
            ],
            "verify_pre_gate_evidence_closure_context": [
                "manifest",
                "root_set",
            ],
            "verify_gate_input_snapshot_context": [
                "snapshot",
                "root_set",
                "pre_gate_manifest",
            ],
            "verify_terminalization_input_snapshot_context": [
                "snapshot",
                "root_set",
                "pre_gate_manifest",
                "terminalization_facts",
            ],
        }
        for name, parameters in signatures.items():
            with self.subTest(name=name):
                self.assertIn(name, self.python.__all__)
                helper = getattr(self.python, name)
                self.assertEqual(parameters, list(inspect.signature(helper).parameters))
                self.assertIn("direct", inspect.getdoc(helper).lower())

    def python_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        names = {
            "root_context": "verify_pre_gate_root_set_context",
            "closure_context": "verify_pre_gate_evidence_closure_context",
            "success_context": "verify_gate_input_snapshot_context",
            "terminal_context": "verify_terminalization_input_snapshot_context",
        }
        results = []
        for operation in operations:
            try:
                if operation["kind"] == "document":
                    value = self.python.verify_document_digest(
                        operation["schema_id"], *operation["args"]
                    )
                else:
                    value = getattr(self.python, names[operation["kind"]])(
                        *operation["args"]
                    )
            except self.python.ContractValidationError as error:
                results.append(
                    {
                        "ok": False,
                        "code": error.code,
                        "detail": error.detail,
                        "path": error.path,
                    }
                )
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="pre-gate-input-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const helpers = {",
                        "  root_context: facade.verifyPreGateRootSetContext,",
                        "  closure_context: facade.verifyPreGateEvidenceClosureContext,",
                        "  success_context: facade.verifyGateInputSnapshotContext,",
                        "  terminal_context: facade.verifyTerminalizationInputSnapshotContext,",
                        "};",
                        "if (facade.verify_pre_gate_root_set_context !== helpers.root_context ||",
                        "    facade.verify_pre_gate_evidence_closure_context !== helpers.closure_context ||",
                        "    facade.verify_gate_input_snapshot_context !== helpers.success_context ||",
                        "    facade.verify_terminalization_input_snapshot_context !== helpers.terminal_context) {",
                        "  throw new Error('helper alias mismatch');",
                        "}",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = operation.kind === 'document'",
                        "      ? await facade.verifyDocumentDigest(operation.schema_id, ...operation.args)",
                        "      : await helpers[operation.kind](...operation.args);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) {",
                        "    results.push({ok: false, code: error.code, detail: error.detail, path: error.path});",
                        "  }",
                        "}",
                        "process.stdout.write(JSON.stringify(results));",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        return json.loads(completed.stdout)

    def assert_parity(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python = self.python_results(operations)
        node = self.node_results(operations)
        self.assertEqual(python, node)
        return python


if __name__ == "__main__":
    unittest.main()
