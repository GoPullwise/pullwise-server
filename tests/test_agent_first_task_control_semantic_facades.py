from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"


class AgentFirstTaskControlSemanticFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(FAMILIES.glob("*.json"))
        ]
        canonical = json.dumps(
            {"families": families},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        python_wrapper = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_task_control_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def test_task_request_sets_are_enforced_in_python_and_node(self) -> None:
        request = deepcopy(
            self.python.fixture("task_control_golden_task_request")["document"]
        )
        request["requested_capabilities"] = ["source.write", "source.read"]
        python_result = self.python_validation("task-request/v1", request)
        node_result = self.node_validation("task-request/v1", request)
        expected = {
            "ok": False,
            "code": "CONTRACT_DOCUMENT_INVALID",
            "detail": "TASK_REQUEST_CAPABILITY_ORDER_INVALID",
            "path": "$",
        }
        self.assertEqual(expected, python_result)
        self.assertEqual(expected, node_result)

    def test_all_owned_source_fixtures_are_executable(self) -> None:
        fixture_ids = (
            "task_control_golden_effective_policy",
            "task_control_golden_task_request",
            "requirements_golden_charter",
            "requirements_golden_ledger",
            "requirements_negative_charter_predecessor",
            "requirements_negative_derived_cycle",
            "requirements_negative_derived_mandatory_without_rationale",
            "task_control_golden_attempt_record",
            "task_control_golden_task_owner",
            "task_control_golden_task_record",
        )
        for fixture_id in fixture_ids:
            with self.subTest(fixture_id=fixture_id):
                item = self.python.fixture(fixture_id)
                python_result = self.python_validation(item["schema_id"], item["document"])
                node_result = self.node_validation(item["schema_id"], item["document"])
                self.assertEqual(python_result, node_result)
                if item["fixture_class"] == "golden":
                    self.assertTrue(python_result["ok"])
                else:
                    self.assertEqual(item["expected_code"], python_result["code"])

    def test_task_request_source_ids_and_utf8_bytes_are_enforced(self) -> None:
        request = deepcopy(self.python.fixture("task_control_golden_task_request")["document"])
        duplicate = deepcopy(request)
        duplicate["constraints"][0]["source_id"] = duplicate["acceptance_criteria"][0]["source_id"]
        oversized = deepcopy(request)
        oversized["acceptance_criteria"][0]["statement"] = "é" * 8193
        cases = (
            (duplicate, "TASK_REQUEST_SOURCE_ID_INVALID", "$"),
            (oversized, "UTF8_BYTE_LIMIT_INVALID", "$.acceptance_criteria[0].statement"),
        )
        for document, detail, path in cases:
            with self.subTest(detail=detail):
                expected = {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": detail, "path": path}
                self.assertEqual(expected, self.python_validation("task-request/v1", document))
                self.assertEqual(expected, self.node_validation("task-request/v1", document))

    def test_effective_policy_derivation_and_waiver_authority_have_parity(self) -> None:
        request = self.python.fixture("task_control_golden_task_request")["document"]
        policy = self.python.fixture("task_control_golden_effective_policy")["document"]
        python_ok = self.python_call("validate_effective_policy_derivation", [request, policy])
        node_ok = self.node_call("validateEffectivePolicyDerivation", [request, policy])
        self.assertEqual(python_ok, node_ok)
        self.assertTrue(python_ok["ok"])

        bounded = deepcopy(request)
        bounded["requested_budgets"]["wall_ms"] = policy["budgets"]["wall_ms"] - 1
        python_error = self.python_call("validate_effective_policy_derivation", [bounded, policy])
        node_error = self.node_call("validateEffectivePolicyDerivation", [bounded, policy])
        self.assertEqual(python_error, node_error)
        self.assertEqual("POLICY_INVARIANT_BROKEN", python_error["code"])
        self.assertEqual("POLICY_REQUEST_BUDGET_EXCEEDED", python_error["detail"])

        waiver = self.python.fixture("requirements_negative_waiver_empty_issuer_profile")["document"]
        args = [waiver, policy, "2026-07-22T00:30:00.000Z"]
        python_waiver = self.python_call("verify_waiver_event_authority", args)
        node_waiver = self.node_call("verifyWaiverEventAuthority", args)
        self.assertEqual(python_waiver, node_waiver)
        self.assertEqual("WAIVER_INVALID", python_waiver["code"])
        self.assertEqual("WAIVER_ISSUER_NOT_AUTHORIZED", python_waiver["detail"])

    def test_requirement_ledger_and_charter_transitions_have_parity(self) -> None:
        ledger = self.python.fixture("requirements_golden_ledger")["document"]
        charter = self.python.fixture("requirements_golden_charter")["document"]
        initial_args = [None, charter, ledger]
        self.assertEqual(
            self.python_call("validate_task_charter_transition", initial_args),
            self.node_call("validateTaskCharterTransition", initial_args),
        )

        entry = deepcopy(self.python.fixture(
            "requirements_negative_derived_mandatory_without_rationale"
        )["document"])
        entry["rationale"] = "Required to preserve the accepted objective."
        ingest_args = [entry, ledger]
        python_ingest = self.python_call("validate_requirement_entry_ingest", ingest_args)
        node_ingest = self.node_call("validateRequirementEntryIngest", ingest_args)
        self.assertEqual(python_ingest, node_ingest)
        self.assertTrue(python_ingest["ok"])

        candidate_unsigned = deepcopy(ledger)
        candidate_unsigned.pop("ledger_digest")
        candidate_unsigned["ledger_version"] = 2
        candidate_unsigned["entries"].append(entry)
        candidate_unsigned["active_requirement_ids"] = sorted(
            candidate_unsigned["active_requirement_ids"] + [entry["requirement_id"]]
        )
        candidate = self.python.seal_document("requirement-ledger/v1", candidate_unsigned)
        ledger_args = [ledger, candidate]
        python_ledger = self.python_call("validate_requirement_ledger_transition", ledger_args)
        node_ledger = self.node_call("validateRequirementLedgerTransition", ledger_args)
        self.assertEqual(python_ledger, node_ledger)
        self.assertTrue(python_ledger["ok"])

        mutated_unsigned = deepcopy(candidate)
        mutated_unsigned.pop("ledger_digest")
        mutated_unsigned["entries"][0]["statement"] = "Mutated history."
        mutated = self.python.seal_document("requirement-ledger/v1", mutated_unsigned)
        bad_args = [ledger, mutated]
        python_bad = self.python_call("validate_requirement_ledger_transition", bad_args)
        node_bad = self.node_call("validateRequirementLedgerTransition", bad_args)
        self.assertEqual(python_bad, node_bad)
        self.assertEqual("REQUIREMENT_LEDGER_HISTORY_MUTATED", python_bad["detail"])

        previous_bytes = self.python.canonical_document_bytes(charter)
        charter_unsigned = deepcopy(charter)
        charter_unsigned.pop("digest")
        charter_unsigned["charter_version"] = 2
        charter_unsigned["previous_charter_ref"] = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_00000000000000000000000000000099",
            "content_schema_id": "task-charter/v1",
            "sha256": self.python.canonical_document_sha256(charter),
            "size_bytes": len(previous_bytes),
            "media_type": "application/json",
            "encoding": "utf-8",
        }
        charter_unsigned["created_at"] = "2026-07-22T00:01:00.000Z"
        charter_v2 = self.python.seal_document("task-charter/v1", charter_unsigned)
        charter_args = [charter, charter_v2, candidate]
        python_charter = self.python_call("validate_task_charter_transition", charter_args)
        node_charter = self.node_call("validateTaskCharterTransition", charter_args)
        self.assertEqual(python_charter, node_charter)
        self.assertTrue(python_charter["ok"])

    def test_claim_attempt_owner_and_task_record_transitions_have_parity(self) -> None:
        queued = self.python.fixture("task_control_golden_task_record")["document"]
        attempt = self.python.fixture("task_control_golden_attempt_record")["document"]
        owner = self.python.fixture("task_control_golden_task_owner")["document"]
        claimed = deepcopy(queued)
        claimed.update(
            lifecycle="ACTIVE",
            task_version=2,
            native_epoch=1,
            current_attempt_id=attempt["attempt_id"],
            owner_epoch=1,
            updated_at="2026-07-22T00:00:01.000Z",
        )
        transition_args = [queued, claimed]
        self.assertEqual(
            self.python_call("validate_task_record_transition", transition_args),
            self.node_call("validateTaskRecordTransition", transition_args),
        )
        claim_args = [queued, claimed, attempt, owner]
        python_claim = self.python_call("validate_claim_write_set", claim_args)
        node_claim = self.node_call("validateClaimWriteSet", claim_args)
        self.assertEqual(python_claim, node_claim)
        self.assertTrue(python_claim["ok"])

        preparing = deepcopy(attempt)
        preparing.update(state="PREPARING", state_version=2)
        attempt_args = [attempt, preparing]
        self.assertEqual(
            self.python_call("validate_attempt_transition", attempt_args),
            self.node_call("validateAttemptTransition", attempt_args),
        )
        active_owner = deepcopy(owner)
        active_owner.update(state="ACTIVE", state_version=2)
        owner_args = [owner, active_owner]
        self.assertEqual(
            self.python_call("validate_task_owner_transition", owner_args),
            self.node_call("validateTaskOwnerTransition", owner_args),
        )

        version_jump = deepcopy(claimed)
        version_jump["task_version"] = 3
        stale_args = [queued, version_jump]
        python_stale = self.python_call("validate_task_record_transition", stale_args)
        node_stale = self.node_call("validateTaskRecordTransition", stale_args)
        self.assertEqual(python_stale, node_stale)
        self.assertEqual("TASK_VERSION_STALE", python_stale["code"])

        orphan_owner = deepcopy(owner)
        orphan_owner["attempt_id"] = "attempt_ffffffffffffffffffffffffffffffff"
        orphan_args = [queued, claimed, attempt, orphan_owner]
        python_orphan = self.python_call("validate_claim_write_set", orphan_args)
        node_orphan = self.node_call("validateClaimWriteSet", orphan_args)
        self.assertEqual(python_orphan, node_orphan)
        self.assertEqual("AUTHORITY_FENCED", python_orphan["code"])

        preterminal_result = deepcopy(claimed)
        preterminal_result["result_ref"] = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_00000000000000000000000000000098",
            "content_schema_id": "task-result/v1",
            "sha256": "9" * 64,
            "size_bytes": 1,
            "media_type": "application/json",
            "encoding": "utf-8",
        }
        python_preterminal = self.python_validation("task-record/v1", preterminal_result)
        node_preterminal = self.node_validation("task-record/v1", preterminal_result)
        self.assertEqual(python_preterminal, node_preterminal)
        self.assertFalse(python_preterminal["ok"])
        self.assertEqual("TASK_RECORD_TERMINAL_RESULT_INVALID", python_preterminal["detail"])

    def test_task_result_publication_binds_only_the_terminal_successor(self) -> None:
        queued = self.python.fixture("task_control_golden_task_record")["document"]
        attempt = self.python.fixture("task_control_golden_attempt_record")["document"]
        claimed = deepcopy(queued)
        claimed.update(
            lifecycle="ACTIVE", task_version=2, native_epoch=1,
            current_attempt_id=attempt["attempt_id"], owner_epoch=1,
            updated_at="2026-07-22T00:00:01.000Z",
        )
        finalizing = deepcopy(claimed)
        finalizing.update(
            lifecycle="FINALIZING", task_version=3,
            updated_at="2026-07-22T00:00:02.000Z",
        )
        result = {
            "schema_id": "task-result/v1",
            "task_id": finalizing["task_id"],
            "task_type": finalizing["task_type"],
            "outcome": "COMPLETED",
            "published_from_version": 3,
            "terminal_task_version": 4,
            "request_ref": finalizing["request_ref"],
            "policy_ref": finalizing["policy_ref"],
            "attempt_identity": {
                "kind": "started", "attempt_id": attempt["attempt_id"],
                "native_epoch": 1,
            },
            "owner_identity": {
                "kind": "started", "owner_id": finalizing["owner_id"],
                "owner_epoch": 1,
            },
            "terminal_at": "2026-07-22T00:01:00Z",
        }
        result_bytes = self.python.canonical_document_bytes(result)
        result_digest = self.python.canonical_document_sha256(result)
        terminal = deepcopy(finalizing)
        terminal.update(
            lifecycle="TERMINAL", task_version=4,
            terminal_kind="task_result", result_digest=result_digest,
            result_ref={
                "schema_id": "content-ref/v1",
                "artifact_id": "art_00000000000000000000000000000097",
                "content_schema_id": "task-result/v1",
                "sha256": result_digest, "size_bytes": len(result_bytes),
                "media_type": "application/json", "encoding": "utf-8",
            },
            outcome="COMPLETED", updated_at="2026-07-22T00:01:00.000Z",
            terminal_at="2026-07-22T00:01:00.000Z",
        )
        args = [finalizing, terminal, result]
        python_ok = self.python_call("validate_task_result_publication", args)
        node_ok = self.node_call("validateTaskResultPublication", args)
        self.assertEqual(python_ok, node_ok)
        self.assertTrue(python_ok["ok"])

        wrong_result = deepcopy(result)
        wrong_result["task_id"] = "task_ffffffffffffffffffffffffffffffff"
        bad_args = [finalizing, terminal, wrong_result]
        python_bad = self.python_call("validate_task_result_publication", bad_args)
        node_bad = self.node_call("validateTaskResultPublication", bad_args)
        self.assertEqual(python_bad, node_bad)
        self.assertEqual("TASK_RESULT_PUBLICATION_IDENTITY_INVALID", python_bad["detail"])

    def python_validation(
        self, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        try:
            value = self.python.validate_document(schema_id, document)
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        return {"ok": True, "value": value}

    def python_call(self, name: str, arguments: list[object]) -> dict[str, object]:
        try:
            value = getattr(self.python, name)(*arguments)
        except self.python.ContractValidationError as error:
            return {"ok": False, "code": error.code, "detail": error.detail, "path": error.path}
        return {"ok": True, "value": value}

    def node_validation(
        self, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="task-control-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const document = {json.dumps(document, separators=(',', ':'))};",
                        "let result;",
                        "try {",
                        "  result = {ok: true, value: facade.validateDocument(",
                        f"    {json.dumps(schema_id)}, document",
                        "  )};",
                        "} catch (error) {",
                        "  result = {ok: false, code: error.code,",
                        "    detail: error.detail, path: error.path};",
                        "}",
                        "process.stdout.write(JSON.stringify(result));",
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

    def node_call(self, name: str, arguments: list[object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="task-control-helper-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text("\n".join((
                f"import * as facade from {json.dumps(facade_path.as_uri())};",
                f"const args = {json.dumps(arguments, separators=(',', ':'))};",
                "let result;",
                "try {",
                f"  result = {{ok: true, value: await facade[{json.dumps(name)}](...args)}};",
                "} catch (error) {",
                "  result = {ok: false, code: error.code, detail: error.detail, path: error.path};",
                "}",
                "process.stdout.write(JSON.stringify(result));",
            )), encoding="utf-8")
            completed = subprocess.run(["node", str(runner_path)], check=True, capture_output=True, encoding="utf-8")
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
