from __future__ import annotations

from copy import deepcopy
import hashlib
import unittest

from tests.agent_first_result_debug_transport_facade_support import canonical_bytes
from tests.test_agent_first_result_debug_transport_helper_red import (
    AgentFirstResultDebugTransportHelperRedTest,
)


class AgentFirstResultDebugTransportAdversarialTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        AgentFirstResultDebugTransportHelperRedTest.setUpClass()
        cls.facade = AgentFirstResultDebugTransportHelperRedTest(
            "test_positive_helper_bindings_have_exact_parity"
        )

    def document(self, fixture_id: str) -> dict[str, object]:
        return self.facade.fixture_document(fixture_id)

    def uploaded_documents(self) -> dict[str, dict[str, object]]:
        return self.facade.build_uploaded_documents()

    def assert_schema_valid(self, schema_id: str, document: dict[str, object]) -> None:
        self.assertEqual(
            {"ok": True, "value": document},
            self.facade.validate_case(schema_id, document),
        )

    def assert_helper_parity(self, operations: list[dict[str, object]]) -> None:
        python = self.facade.python_helper_results(operations)
        node = self.facade.node_helper_results(operations)
        self.assertEqual(python, node)

    def assert_schema_only(self, schema_id: str, document: dict[str, object]) -> None:
        """Prove a RED reaches semantic validation rather than JSON-schema rejection."""
        self.facade.python._validate_node(self.facade.schema(schema_id), document, "$")

    def assert_document_batch(
        self,
        cases: list[tuple[str, dict[str, object], tuple[str, str | None, str | None]]],
    ) -> None:
        for schema_id, document, _ in cases:
            self.assert_schema_only(schema_id, document)
        python = self.facade.python_document_results(
            [(schema_id, document) for schema_id, document, _ in cases]
        )
        node = self.facade.node_document_results(
            [(schema_id, document) for schema_id, document, _ in cases]
        )
        self.assertEqual(python, node)
        self.assertEqual(
            [expected for _, _, expected in cases],
            [("OK", None, None) if item["ok"] else (item["code"], item["detail"], item["path"]) for item in python],
        )

    def assert_helper_batch(
        self,
        operations: list[dict[str, object]],
        schema_cases: list[tuple[str, dict[str, object]]],
        expected: list[tuple[str, str | None, str | None]],
    ) -> None:
        for schema_id, document in schema_cases:
            self.assert_schema_only(schema_id, document)
        python = self.facade.python_helper_results(operations)
        node = self.facade.node_helper_results(operations)
        self.assertEqual(python, node)
        self.assertEqual(
            expected,
            [("OK", None, None) if item["ok"] else (item["code"], item["detail"], item["path"]) for item in python],
        )

    def task_result_branch(self, outcome: str) -> dict[str, object]:
        """Schema-valid outcome branches with two canonically sorted requirements."""
        result = self.document("task_result_golden_completed")
        first = result["requirement_results"][0]
        second = deepcopy(first)
        second["requirement_id"] = second["requirement_id"][:-1] + "2"
        result["requirement_results"] = [first, second]
        reqs = [first["requirement_id"], second["requirement_id"]]
        ref = deepcopy(result["evidence_closure_ref"])
        ref["artifact_id"] = ref["artifact_id"][:-1] + "f"
        detail: dict[str, object]
        if outcome == "COMPLETED":
            detail = {"kind": "completed", "delivered_scope": [{"statement": "Delivered.", "requirement_ids": reqs, "artifact_refs": []}]}
            reason = "SUCCESS"
        elif outcome == "NO_CHANGE_NEEDED":
            detail, reason = {"kind": "no_change_needed", "satisfaction_observation_ids": ["obs_00000000000000000000000000000001"]}, "ALREADY_SATISFIED"
        elif outcome == "COMPLETED_WITH_WAIVERS":
            detail, reason = {"kind": "completed_with_waivers", "waiver_ids": ["waiver_00000000000000000000000000000001"], "original_verdicts": [{"requirement_id": reqs[0], "verdict": "FAIL", "waiver_id": "waiver_00000000000000000000000000000001"}]}, "AUTHORIZED_WAIVER"
        elif outcome == "PARTIAL":
            detail, reason = {"kind": "partial", "delivered_scope": [{"statement": "Delivered.", "requirement_ids": reqs, "artifact_refs": []}], "gaps": [{"requirement_id": reqs[0], "verdict": "FAIL", "reason_code": "GAP"}], "residual_risks": [{"risk_id": "risk_00000000000000000000000000000001", "statement": "Risk.", "evidence_ids": ["a"]}]}, "SAFE_PARTIAL_DELIVERY"
        elif outcome == "BLOCKED":
            detail, reason = {"kind": "blocked", "blockers": [{"code": "WAITING", "requirement_ids": reqs, "unblock_condition": "Approve."}]}, "INPUT_REQUIRED"
        else:
            detail, reason = {"kind": "failed", "failures": [{"code": "FAILED", "evidence_refs": [ref]}]}, "RUNTIME_FAILURE"
        result["outcome"], result["reason_code"], result["outcome_details"] = outcome, reason, detail
        if outcome == "NO_CHANGE_NEEDED":
            result["change_set_ref"] = None
        if outcome in {"BLOCKED", "FAILED"}:
            result["change_set_ref"] = None
            for field in ("completion_proposal", "attestations", "report"):
                result[field] = {"availability": "unavailable", "reason_code": "CAPABILITY_NOT_IMPLEMENTED"}
        return result

    def receipt_ref(self, receipt: dict[str, object]) -> dict[str, object]:
        return self.facade.content_ref(
            "art_99999999999999999999999999999993",
            "server-transport-receipt/v1",
            receipt,
        )

    def descriptor_ref(self, descriptor: dict[str, object]) -> dict[str, object]:
        return self.facade.content_ref(
            "art_99999999999999999999999999999994",
            "worker-debug-fragment-descriptor/v1",
            descriptor,
        )

    def registry_order(self, schema_id: str) -> list[str]:
        schema = self.facade.schema(schema_id)
        if schema_id == "availability-reason-registry/v1":
            unavailable = schema["oneOf"][1]["properties"]["reason_code"]["enum"]
            not_applicable = schema["oneOf"][2]["properties"]["reason_code"]["enum"]
            self.assertEqual(unavailable, not_applicable)
            return unavailable
        reasons: list[str] = []
        seen: set[str] = set()
        for branch in self.facade.schema("task-result/v1")["oneOf"]:
            branch_id = branch["$ref"]
            rule = self.facade.schema(branch_id)["properties"]["reason_code"]
            values = [rule["const"]] if "const" in rule else rule["enum"]
            for value in values:
                if value not in seen:
                    seen.add(value)
                    reasons.append(value)
        return reasons

    def test_reason_registries_match_exact_schema_derived_bijections(self) -> None:
        availability = self.document("task_result_golden_availability_reason_registry")
        outcome = self.document("task_result_golden_outcome_reason_registry")

        self.assertEqual(
            self.registry_order("availability-reason-registry/v1"),
            availability["reasons"],
        )
        self.assertEqual(
            self.registry_order("task-result-outcome-reason-registry/v1"),
            outcome["reasons"],
        )

    def test_control_transport_branches_are_structurally_valid(self) -> None:
        documents = self.uploaded_documents()
        self.assert_schema_valid("task-result/v1", documents["task_result"])
        self.assert_schema_valid(
            "worker-debug-fragment-descriptor/v1", documents["worker_debug_descriptor"]
        )
        self.assert_schema_valid(
            "task-result-transport-envelope/v1",
            documents["task_result_transport_envelope"],
        )
        self.assert_schema_valid(
            "task-result-transport-ack/v1", documents["task_result_transport_ack"]
        )

        local_only_descriptor = deepcopy(documents["worker_debug_descriptor"])
        local_only_descriptor["state"] = "local_only"
        local_only_descriptor["transport_kind"] = "none"
        local_only_descriptor["server_fragment_ref"] = None
        local_only_descriptor["server_receipt_ref"] = None
        local_only_descriptor["reason_code"] = "DEBUG_UPLOAD_FAILED"
        self.assert_schema_valid(
            "worker-debug-fragment-descriptor/v1", local_only_descriptor
        )

        local_only_task_result = deepcopy(documents["task_result"])
        local_only_task_result["diagnostics"]["worker_debug_fragment"] = {
            "availability": "available",
            "ref": self.descriptor_ref(local_only_descriptor),
        }
        self.assert_schema_valid("task-result/v1", local_only_task_result)

        local_only_envelope = deepcopy(documents["task_result_transport_envelope"])
        local_only_envelope["task_result"] = local_only_task_result
        local_only_envelope["task_result_digest"] = hashlib.sha256(
            canonical_bytes(local_only_task_result)
        ).hexdigest()
        local_only_envelope["worker_debug_descriptor"] = local_only_descriptor
        local_only_envelope["transport_receipt"] = {
            "availability": "not_applicable",
            "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE",
        }
        self.assert_schema_valid("task-result-transport-envelope/v1", local_only_envelope)

        unavailable_task_result = deepcopy(documents["task_result"])
        unavailable_task_result["diagnostics"]["worker_debug_fragment"] = {
            "availability": "unavailable",
            "reason_code": "DEBUG_UNAVAILABLE",
        }
        self.assert_schema_valid("task-result/v1", unavailable_task_result)

        unavailable_envelope = deepcopy(documents["task_result_transport_envelope"])
        unavailable_envelope["task_result"] = unavailable_task_result
        unavailable_envelope["task_result_digest"] = hashlib.sha256(
            canonical_bytes(unavailable_task_result)
        ).hexdigest()
        unavailable_envelope["worker_debug_descriptor"] = None
        unavailable_envelope["transport_receipt"] = {
            "availability": "not_applicable",
            "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE",
        }
        self.assert_schema_valid("task-result-transport-envelope/v1", unavailable_envelope)

    def test_terminal_fragment_native_attempt_context_matches_across_runtimes(self) -> None:
        documents = self.uploaded_documents()
        invalid_fragment = deepcopy(documents["worker_debug_fragment"])
        invalid_fragment["native_attempt_id"] = (
            "attempt_00000000000000000000000000000002"
        )
        self.assert_schema_valid("worker-debug-fragment/v1", invalid_fragment)

        self.assert_helper_parity(
            [
                {
                    "python": "verify_worker_debug_fragment_content",
                    "node": "verifyWorkerDebugFragmentContent",
                    "args": [
                        invalid_fragment,
                        documents["task_result_core"],
                        documents["worker_debug_file_manifest"],
                        documents["worker_debug_redaction_report"],
                    ],
                }
            ]
        )

    def test_terminal_fragment_time_window_matches_across_runtimes(self) -> None:
        documents = self.uploaded_documents()
        invalid_fragment = deepcopy(documents["worker_debug_fragment"])
        invalid_fragment["captured_at"] = "2026-07-22T00:01:00.000000001Z"
        self.assert_schema_valid("worker-debug-fragment/v1", invalid_fragment)

        self.assert_helper_parity(
            [
                {
                    "python": "verify_worker_debug_fragment_content",
                    "node": "verifyWorkerDebugFragmentContent",
                    "args": [
                        invalid_fragment,
                        documents["task_result_core"],
                        documents["worker_debug_file_manifest"],
                        documents["worker_debug_redaction_report"],
                    ],
                }
            ]
        )

    def test_descriptor_snapshot_binding_matches_across_runtimes(self) -> None:
        documents = self.uploaded_documents()
        invalid_descriptor = deepcopy(documents["worker_debug_descriptor"])
        invalid_descriptor["snapshot_seq"] += 1
        self.assert_schema_valid(
            "worker-debug-fragment-descriptor/v1", invalid_descriptor
        )

        self.assert_helper_parity(
            [
                {
                    "python": "verify_worker_debug_descriptor_content",
                    "node": "verifyWorkerDebugDescriptorContent",
                    "args": [invalid_descriptor, documents["worker_debug_fragment"]],
                    "kwargs": {"transport_receipt": documents["transport_receipt"]},
                }
            ]
        )

    def test_descriptor_receipt_time_binding_matches_across_runtimes(self) -> None:
        documents = self.uploaded_documents()
        invalid_receipt = deepcopy(documents["transport_receipt"])
        invalid_receipt["accepted_at"] = "2026-07-21T23:59:59Z"
        invalid_receipt = self.facade.reseal("server-transport-receipt/v1", invalid_receipt)
        invalid_descriptor = deepcopy(documents["worker_debug_descriptor"])
        invalid_descriptor["server_receipt_ref"] = self.receipt_ref(invalid_receipt)
        self.assert_schema_valid("server-transport-receipt/v1", invalid_receipt)
        self.assert_schema_valid(
            "worker-debug-fragment-descriptor/v1", invalid_descriptor
        )

        self.assert_helper_parity(
            [
                {
                    "python": "verify_worker_debug_descriptor_content",
                    "node": "verifyWorkerDebugDescriptorContent",
                    "args": [invalid_descriptor, documents["worker_debug_fragment"]],
                    "kwargs": {"transport_receipt": invalid_receipt},
                }
            ]
        )

    def test_ack_time_bindings_match_across_runtimes(self) -> None:
        documents = self.uploaded_documents()
        invalid_ack = deepcopy(documents["task_result_transport_ack"])
        invalid_ack["accepted_at"] = "2026-07-22T00:00:59Z"
        invalid_ack = self.facade.reseal("task-result-transport-ack/v1", invalid_ack)
        self.assert_schema_valid("task-result-transport-ack/v1", invalid_ack)

        self.assert_helper_parity(
            [
                {
                    "python": "verify_task_result_transport_ack",
                    "node": "verifyTaskResultTransportAck",
                    "args": [invalid_ack, documents["task_result_transport_envelope"]],
                    "kwargs": {"transport_receipt": documents["transport_receipt"]},
                }
            ]
        )

    def test_file_manifest_media_type_paths_match_across_runtimes(self) -> None:
        invalid_manifest = self.document("worker_debug_content_golden_file_manifest")
        invalid_manifest["entries"][0]["path"] = "worker.log.jsonl"
        invalid_manifest = self.facade.reseal(
            "worker-debug-file-manifest/v1", invalid_manifest
        )
        self.assert_schema_valid("worker-debug-file-manifest/v1", invalid_manifest)

        self.assertEqual(
            self.facade.python_document_results(
                [("worker-debug-file-manifest/v1", invalid_manifest)]
            ),
            self.facade.node_document_results(
                [("worker-debug-file-manifest/v1", invalid_manifest)]
            ),
        )


if __name__ == "__main__":
    unittest.main()
