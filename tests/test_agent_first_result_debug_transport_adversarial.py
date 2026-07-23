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
        for index, (schema_id, document, expected) in enumerate(cases):
            with self.subTest(
                index=index, schema_id=schema_id, expected=expected, path=expected[2]
            ):
                self.facade.python._validate_node(
                    self.facade.schema(schema_id), deepcopy(document), "$"
                )
        python = self.facade.python_document_results(
            [(schema_id, document) for schema_id, document, _ in cases]
        )
        node = self.facade.node_document_results(
            [(schema_id, document) for schema_id, document, _ in cases]
        )
        for runtime, results in (("python", python), ("node", node)):
            for index, (case, item) in enumerate(zip(cases, results)):
                schema_id, _, expected = case
                actual = ("OK", None, None) if item["ok"] else (
                    item["code"], item["detail"], item["path"]
                )
                with self.subTest(
                    runtime=runtime, index=index, schema_id=schema_id,
                    expected=expected, path=expected[2],
                ):
                    self.assertEqual(expected, actual)

    def assert_helper_batch(
        self,
        operations: list[dict[str, object]],
        schema_cases: list[tuple[str, dict[str, object]]],
        expected: list[tuple[str, str | None, str | None]],
    ) -> None:
        for index, (schema_id, document) in enumerate(schema_cases):
            wanted = expected[min(index, len(expected) - 1)]
            with self.subTest(
                index=index, schema_id=schema_id, expected=wanted, path=wanted[2]
            ):
                self.facade.python._validate_node(
                    self.facade.schema(schema_id), deepcopy(document), "$"
                )
        python = self.facade.python_helper_results(operations)
        node = self.facade.node_helper_results(operations)
        for runtime, results in (("python", python), ("node", node)):
            for index, (operation, wanted, item) in enumerate(
                zip(operations, expected, results)
            ):
                actual = ("OK", None, None) if item["ok"] else (
                    item["code"], item["detail"], item["path"]
                )
                with self.subTest(
                    runtime=runtime, index=index, operation=operation["python"],
                    expected=wanted, path=wanted[2],
                ):
                    self.assertEqual(wanted, actual)

    def task_result_branch(self, outcome: str, not_started: bool = False) -> dict[str, object]:
        """Schema-valid outcome branches with two canonically sorted requirements."""
        result = self.document("task_result_golden_completed")
        first = deepcopy(result["requirement_results"][0])
        second = deepcopy(first)
        second["requirement_id"] = second["requirement_id"][:-1] + "2"
        result["requirement_results"] = [first, second]
        reqs = [first["requirement_id"], second["requirement_id"]]
        ref = deepcopy(result["evidence_closure_ref"])
        ref["artifact_id"] = ref["artifact_id"][:-1] + "f"
        artifact_ref = {
            "schema_id": "artifact-content-ref/v1",
            "artifact_kind": "task_report",
            "ref": self.facade.content_ref(
                "art_00000000000000000000000000000020", "task-report/v1", {"x": 1}
            ),
        }
        detail: dict[str, object]
        if outcome == "COMPLETED":
            detail = {"kind": "completed", "delivered_scope": [{"statement": "Delivered.", "requirement_ids": reqs, "artifact_refs": [artifact_ref]}]}
            reason = "SUCCESS"
        elif outcome == "NO_CHANGE_NEEDED":
            detail, reason = {"kind": "no_change_needed", "satisfaction_observation_ids": ["obs_00000000000000000000000000000001"]}, "ALREADY_SATISFIED"
        elif outcome == "COMPLETED_WITH_WAIVERS":
            detail, reason = {"kind": "completed_with_waivers", "waiver_ids": ["waiver_00000000000000000000000000000001"], "original_verdicts": [{"requirement_id": reqs[0], "verdict": "FAIL", "waiver_id": "waiver_00000000000000000000000000000001"}]}, "AUTHORIZED_WAIVER"
        elif outcome == "PARTIAL":
            detail, reason = {"kind": "partial", "delivered_scope": [{"statement": "Delivered.", "requirement_ids": reqs, "artifact_refs": [artifact_ref]}], "gaps": [{"requirement_id": reqs[0], "verdict": "FAIL", "reason_code": "GAP"}], "residual_risks": [{"risk_id": "risk_00000000000000000000000000000001", "statement": "Risk.", "evidence_ids": ["evidence_a"]}]}, "SAFE_PARTIAL_DELIVERY"
        elif outcome == "BLOCKED":
            detail, reason = {"kind": "blocked", "blockers": [{"code": "WAITING", "requirement_ids": reqs, "unblock_condition": "Approve."}]}, "INPUT_REQUIRED"
        elif outcome in {"CANCELLED", "CANCELLED_WITH_EFFECTS"}:
            detail, reason = {"kind": outcome.lower(), "request_id": "cancel_00000000000000000000000000000001", "linearized_at": result["terminal_at"], "requested_by": {"schema_id": "actor/v1", "kind": "user_control", "id": "user", "session_id": None}}, "USER_CANCELLED"
        elif outcome == "TERMINATED_WITH_UNKNOWN_EFFECTS":
            detail, reason = {"kind": "terminated_with_unknown_effects"}, "DEADLINE_REACHED"
        else:
            detail, reason = {"kind": "failed", "failures": [{"code": "FAILED", "evidence_refs": [ref]}]}, "RUNTIME_FAILURE"
        result["outcome"], result["reason_code"], result["outcome_details"] = outcome, reason, detail
        if outcome in {"PARTIAL", "CANCELLED_WITH_EFFECTS"}:
            result["effects"]["committed"] = 1
        elif outcome == "TERMINATED_WITH_UNKNOWN_EFFECTS":
            result["effects"]["unknown"] = 1
        if outcome == "NO_CHANGE_NEEDED":
            result["change_set_ref"] = None
            result["final_source_state"] = deepcopy(result["original_source_state"])
        if outcome in {
            "BLOCKED", "FAILED", "CANCELLED", "CANCELLED_WITH_EFFECTS",
            "TERMINATED_WITH_UNKNOWN_EFFECTS",
        }:
            result["change_set_ref"] = None
            for field in ("completion_proposal", "attestations", "report"):
                result[field] = {"availability": "not_applicable", "reason_code": "CAPABILITY_NOT_IMPLEMENTED"}
        if not_started:
            result["attempt_identity"] = {
                "kind": "not_started", "attempt_id": None, "native_epoch": 0,
                "reason_code": "ATTEMPT_NOT_STARTED",
            }
            result["owner_identity"] = {
                "kind": "not_started", "owner_id": result["owner_identity"]["owner_id"],
                "owner_epoch": 0, "reason_code": "OWNER_NOT_STARTED",
            }
            result["charter"] = {
                "availability": "not_applicable", "reason_code": "CHARTER_NOT_CREATED"
            }
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
        if schema_id == "availability-reason-registry/v1":
            branches = self.facade.schema("availability-ref/v1")["oneOf"]
        else:
            branches = self.facade.schema("task-result/v1")["oneOf"]
        reasons: set[str] = set()
        for branch in branches:
            if schema_id == "availability-reason-registry/v1":
                properties = branch.get("properties", {})
                if "reason_code" not in properties:
                    continue
                rule = properties["reason_code"]
            else:
                branch_id = branch["$ref"]
                rule = self.facade.schema(branch_id)["properties"]["reason_code"]
            values = [rule["const"]] if "const" in rule else rule["enum"]
            reasons.update(values)
        return sorted(reasons)

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

    def test_all_nine_task_result_outcomes_match_across_runtimes(self) -> None:
        outcomes = (
            "COMPLETED", "NO_CHANGE_NEEDED", "COMPLETED_WITH_WAIVERS",
            "PARTIAL", "BLOCKED", "FAILED", "CANCELLED",
            "CANCELLED_WITH_EFFECTS", "TERMINATED_WITH_UNKNOWN_EFFECTS",
        )
        results = [self.task_result_branch(outcome) for outcome in outcomes]
        cases = [("task-result/v1", result) for result in results]
        expected = [{"ok": True, "value": result} for result in results]
        operations = [
            {"python": "verify_task_result_context", "node": "verifyTaskResultContext",
             "args": [result]}
            for result in results
        ]

        self.assertEqual(expected, self.facade.python_document_results(cases))
        self.assertEqual(expected, self.facade.node_document_results(cases))
        self.assertEqual(expected, self.facade.python_helper_results(operations))
        self.assertEqual(expected, self.facade.node_helper_results(operations))

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
        invalid_manifest["entries"][0]["media_type"] = "application/x-ndjson"
        invalid_manifest = self.facade.reseal(
            "worker-debug-file-manifest/v1", invalid_manifest
        )
        self.assert_schema_only("worker-debug-file-manifest/v1", invalid_manifest)

        self.assertEqual(
            self.facade.python_document_results(
                [("worker-debug-file-manifest/v1", invalid_manifest)]
            ),
            self.facade.node_document_results(
                [("worker-debug-file-manifest/v1", invalid_manifest)]
            ),
        )

    def test_batched_resealed_document_transport_reds(self) -> None:
        documents = self.uploaded_documents()
        availability = self.document("task_result_golden_availability_reason_registry")
        availability["reasons"] = list(reversed(availability["reasons"]))
        outcome = self.document("task_result_golden_outcome_reason_registry")
        outcome["reasons"] = list(reversed(outcome["reasons"]))
        fence = deepcopy(documents["task_result_transport_envelope"])
        fence["full_fence"]["task_version"] += 1
        version = deepcopy(documents["task_result_transport_envelope"])
        version["authority"]["task_version"] += 1
        version["full_fence"]["task_version"] += 1
        manifest = self.document("worker_debug_content_golden_file_manifest")
        manifest["entries"][0]["media_type"] = "application/x-ndjson"
        self.assert_document_batch([
            ("availability-reason-registry/v1", self.facade.reseal("availability-reason-registry/v1", availability), ("CONTRACT_DOCUMENT_INVALID", "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID", "$")),
            ("task-result-outcome-reason-registry/v1", self.facade.reseal("task-result-outcome-reason-registry/v1", outcome), ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_OUTCOME_REASON_REGISTRY_BIJECTION_INVALID", "$")),
            ("task-result-transport-envelope/v1", fence, ("CONTRACT_DOCUMENT_INVALID", "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence.task_version")),
            ("task-result-transport-envelope/v1", version, ("CONTRACT_DOCUMENT_INVALID", "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version")),
            ("worker-debug-file-manifest/v1", self.facade.reseal("worker-debug-file-manifest/v1", manifest), ("CONTRACT_DOCUMENT_INVALID", "DEBUG_FILE_MEDIA_TYPE_INVALID", "$.entries[0].media_type")),
        ])

    def test_batched_helper_transport_reds_and_controls(self) -> None:
        documents = self.uploaded_documents()
        fragment = documents["worker_debug_fragment"]
        core = documents["task_result_core"]
        manifest = documents["worker_debug_file_manifest"]
        report = documents["worker_debug_redaction_report"]
        not_started_result = self.task_result_branch("FAILED", not_started=True)
        terminal_not_started = self.facade.derive_core_expected(not_started_result)
        terminal_not_started_ref = self.facade.content_ref(
            "art_99999999999999999999999999999992",
            "task-result-core/v1",
            terminal_not_started,
        )
        not_started_fragment = deepcopy(fragment)
        not_started_fragment["task_result_core"] = {
            "availability": "available", "ref": terminal_not_started_ref
        }
        not_started_fragment["fragment_id"] = self.facade.python._result_fragment_identity(
            not_started_fragment, manifest["manifest_digest"]
        )
        native = deepcopy(fragment)
        native["native_attempt_id"] = native["native_attempt_id"][:-1] + "2"
        native["fragment_id"] = self.facade.python._result_fragment_identity(native, manifest["manifest_digest"])
        late = deepcopy(fragment)
        late["captured_at"] = "2026-07-22T00:01:00.000000001Z"
        descriptor = deepcopy(documents["worker_debug_descriptor"])
        descriptor["snapshot_seq"] += 1
        bad_ref = deepcopy(documents["worker_debug_descriptor"])
        bad_ref["server_receipt_ref"]["sha256"] = "f" * 64
        early_receipt = deepcopy(documents["transport_receipt"])
        early_receipt["accepted_at"] = "2026-07-21T23:59:59Z"
        early_receipt = self.facade.reseal("server-transport-receipt/v1", early_receipt)
        early_descriptor = deepcopy(documents["worker_debug_descriptor"])
        early_descriptor["server_receipt_ref"] = self.receipt_ref(early_receipt)
        ops = [
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [fragment, core, manifest, report]},
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [not_started_fragment, terminal_not_started, manifest, report]},
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [native, core, manifest, report]},
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [late, core, manifest, report]},
            {"python": "verify_worker_debug_descriptor_content", "node": "verifyWorkerDebugDescriptorContent", "args": [descriptor, fragment], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_worker_debug_descriptor_content", "node": "verifyWorkerDebugDescriptorContent", "args": [bad_ref, fragment], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_worker_debug_descriptor_content", "node": "verifyWorkerDebugDescriptorContent", "args": [early_descriptor, fragment], "kwargs": {"transport_receipt": early_receipt}},
        ]
        self.assert_helper_batch(ops, [("task-result-core/v1", terminal_not_started), ("worker-debug-fragment/v1", not_started_fragment), ("worker-debug-fragment/v1", native), ("worker-debug-fragment/v1", late), ("worker-debug-fragment-descriptor/v1", descriptor), ("worker-debug-fragment-descriptor/v1", bad_ref), ("worker-debug-fragment-descriptor/v1", early_descriptor), ("server-transport-receipt/v1", early_receipt)], [
            ("OK", None, None),
            ("CONTRACT_DOCUMENT_INVALID", "DEBUG_TERMINAL_CORE_INVALID", "$.task_result_core"),
            ("CONTRACT_DOCUMENT_INVALID", "DEBUG_TERMINAL_CORE_INVALID", "$.native_attempt_id"),
            ("CONTRACT_DOCUMENT_INVALID", "DEBUG_TERMINAL_CORE_INVALID", "$.captured_at"),
            ("CONTRACT_DOCUMENT_INVALID", "DEBUG_DESCRIPTOR_BINDING_INVALID", "$.snapshot_seq"),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref"),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref"),
        ])

    def test_schema_valid_outcome_details_order_and_text_reds(self) -> None:
        completed = self.task_result_branch("COMPLETED")
        no_change = self.task_result_branch("NO_CHANGE_NEEDED")
        partial = self.task_result_branch("PARTIAL")
        waived = self.task_result_branch("COMPLETED_WITH_WAIVERS")
        blocked = self.task_result_branch("BLOCKED")
        failed = self.task_result_branch("FAILED")
        req_a, req_b = [item["requirement_id"] for item in completed["requirement_results"]]
        artifact = deepcopy(completed["outcome_details"]["delivered_scope"][0]["artifact_refs"][0])
        artifact_next = deepcopy(artifact)
        artifact_next["ref"]["artifact_id"] = "art_00000000000000000000000000000021"
        artifact_next["ref"]["sha256"] = "3" * 64
        artifact_duplicate = deepcopy(artifact)
        artifact_duplicate["ref"]["sha256"] = "4" * 64
        cases: list[tuple[str, dict[str, object], tuple[str, str | None, str | None]]] = [
            ("task-result/v1", document, ("OK", None, None))
            for document in (completed, no_change, partial, waived, blocked, failed)
        ]
        def add(document: dict[str, object], mutate, path: str) -> None:
            value = deepcopy(document)
            mutate(value)
            cases.append(("task-result/v1", value, ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", path)))
        def add_text(document: dict[str, object], mutate, path: str) -> None:
            value = deepcopy(document)
            mutate(value)
            cases.append(("task-result/v1", value, ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_OUTCOME_TEXT_INVALID", path)))
        add(completed, lambda d: d["outcome_details"]["delivered_scope"].append({"statement": "A.", "requirement_ids": [req_a], "artifact_refs": []}), "$.outcome_details.delivered_scope")
        add(completed, lambda d: d["outcome_details"]["delivered_scope"][0].__setitem__("requirement_ids", [req_b, req_a]), "$.outcome_details.delivered_scope[0].requirement_ids")
        add(completed, lambda d: d["outcome_details"]["delivered_scope"][0].__setitem__("artifact_refs", [artifact_next, artifact]), "$.outcome_details.delivered_scope[0].artifact_refs")
        add(completed, lambda d: d["outcome_details"]["delivered_scope"][0].__setitem__("artifact_refs", [artifact, artifact_duplicate]), "$.outcome_details.delivered_scope[0].artifact_refs")
        add(no_change, lambda d: d["outcome_details"].__setitem__("satisfaction_observation_ids", ["obs_00000000000000000000000000000002", "obs_00000000000000000000000000000001"]), "$.outcome_details.satisfaction_observation_ids")
        add(waived, lambda d: d["outcome_details"].__setitem__("waiver_ids", ["waiver_00000000000000000000000000000002", "waiver_00000000000000000000000000000001"]), "$.outcome_details.waiver_ids")
        original = deepcopy(waived["outcome_details"]["original_verdicts"][0])
        add(waived, lambda d: d["outcome_details"].__setitem__("original_verdicts", [{**original, "requirement_id": req_b}, original]), "$.outcome_details.original_verdicts")
        add(waived, lambda d: d["outcome_details"].__setitem__("original_verdicts", [original, {**original, "verdict": "UNVERIFIABLE"}]), "$.outcome_details.original_verdicts")
        gap = deepcopy(partial["outcome_details"]["gaps"][0])
        add(partial, lambda d: d["outcome_details"].__setitem__("gaps", [{**gap, "requirement_id": req_b}, gap]), "$.outcome_details.gaps")
        add(partial, lambda d: d["outcome_details"].__setitem__("gaps", [gap, {**gap, "verdict": "UNVERIFIABLE"}]), "$.outcome_details.gaps")
        risk = deepcopy(partial["outcome_details"]["residual_risks"][0])
        add(partial, lambda d: d["outcome_details"].__setitem__("residual_risks", [{**risk, "risk_id": "risk_00000000000000000000000000000002"}, risk]), "$.outcome_details.residual_risks")
        add(partial, lambda d: d["outcome_details"].__setitem__("residual_risks", [risk, {**risk, "statement": "Other risk."}]), "$.outcome_details.residual_risks")
        add(partial, lambda d: d["outcome_details"]["residual_risks"][0].__setitem__("evidence_ids", ["b", "a"]), "$.outcome_details.residual_risks[0].evidence_ids")
        blocker = deepcopy(blocked["outcome_details"]["blockers"][0])
        add(blocked, lambda d: d["outcome_details"]["blockers"][0].__setitem__("requirement_ids", [req_b, req_a]), "$.outcome_details.blockers[0].requirement_ids")
        add(blocked, lambda d: d["outcome_details"].__setitem__("blockers", [{**blocker, "code": "ZZZ"}, blocker]), "$.outcome_details.blockers")
        failure = deepcopy(failed["outcome_details"]["failures"][0])
        add(failed, lambda d: d["outcome_details"]["failures"][0].__setitem__("evidence_refs", [failure["evidence_refs"][0], {**failure["evidence_refs"][0], "size_bytes": 1}]), "$.outcome_details.failures[0].evidence_refs")
        lower_ref = deepcopy(failure["evidence_refs"][0])
        lower_ref["artifact_id"] = lower_ref["artifact_id"][:-1] + "e"
        add(failed, lambda d: d["outcome_details"]["failures"][0].__setitem__("evidence_refs", [failure["evidence_refs"][0], lower_ref]), "$.outcome_details.failures[0].evidence_refs")
        add(failed, lambda d: d["outcome_details"].__setitem__("failures", [{**failure, "code": "ZZZ"}, failure]), "$.outcome_details.failures")
        overlimit = chr(0x754C) * 1366
        add_text(completed, lambda d: d["outcome_details"]["delivered_scope"][0].__setitem__("statement", overlimit), "$.outcome_details.delivered_scope[0].statement")
        add_text(partial, lambda d: d["outcome_details"]["residual_risks"][0].__setitem__("statement", overlimit), "$.outcome_details.residual_risks[0].statement")
        add_text(blocked, lambda d: d["outcome_details"]["blockers"][0].__setitem__("unblock_condition", overlimit), "$.outcome_details.blockers[0].unblock_condition")
        self.assert_document_batch(cases)

    def test_local_uploaded_ack_receipt_matrix_and_time_reds(self) -> None:
        documents = self.uploaded_documents()
        local_descriptor = deepcopy(documents["worker_debug_descriptor"])
        local_descriptor.update({"state": "local_only", "transport_kind": "none", "server_fragment_ref": None, "server_receipt_ref": None, "reason_code": "DEBUG_UPLOAD_FAILED"})
        local_result = deepcopy(documents["task_result"])
        local_result["diagnostics"]["worker_debug_fragment"] = {"availability": "available", "ref": self.descriptor_ref(local_descriptor)}
        local_envelope = deepcopy(documents["task_result_transport_envelope"])
        local_envelope["task_result"] = local_result
        local_envelope["task_result_digest"] = hashlib.sha256(canonical_bytes(local_result)).hexdigest()
        local_envelope["worker_debug_descriptor"] = local_descriptor
        local_envelope["transport_receipt"] = {"availability": "not_applicable", "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE"}
        local_ack = deepcopy(documents["task_result_transport_ack"])
        local_ack["receipt_binding_state"], local_ack["receipt_digest"] = "not_applicable", None
        local_ack["transport_envelope_digest"] = hashlib.sha256(canonical_bytes(local_envelope)).hexdigest()
        local_ack = self.facade.reseal("task-result-transport-ack/v1", local_ack)
        wrong_ack = deepcopy(documents["task_result_transport_ack"])
        wrong_ack["receipt_digest"] = "0" * 64
        wrong_ack = self.facade.reseal("task-result-transport-ack/v1", wrong_ack)
        after_local = deepcopy(local_ack)
        after_local["accepted_at"] = "2026-07-22T00:00:59Z"
        after_local = self.facade.reseal("task-result-transport-ack/v1", after_local)
        receipt_time_ack = deepcopy(documents["task_result_transport_ack"])
        receipt_time_ack["accepted_at"] = "2026-07-22T00:01:04Z"
        receipt_time_ack = self.facade.reseal("task-result-transport-ack/v1", receipt_time_ack)
        ops = [
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [documents["task_result_transport_envelope"], documents["task_result_core"]], "kwargs": {"worker_debug_descriptor": documents["worker_debug_descriptor"], "transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [local_envelope, self.facade.derive_core_expected(local_result)], "kwargs": {"worker_debug_descriptor": local_descriptor}},
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [documents["task_result_transport_envelope"], documents["task_result_core"]], "kwargs": {"worker_debug_descriptor": documents["worker_debug_descriptor"]}},
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [local_envelope, self.facade.derive_core_expected(local_result)], "kwargs": {"worker_debug_descriptor": local_descriptor, "transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [documents["task_result_transport_ack"], documents["task_result_transport_envelope"]], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [local_ack, local_envelope]},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [documents["task_result_transport_ack"], documents["task_result_transport_envelope"]]},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [local_ack, local_envelope], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [wrong_ack, documents["task_result_transport_envelope"]], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [after_local, local_envelope]},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [receipt_time_ack, documents["task_result_transport_envelope"]], "kwargs": {"transport_receipt": documents["transport_receipt"]}},
        ]
        self.assert_helper_batch(ops, [("worker-debug-fragment-descriptor/v1", local_descriptor), ("task-result/v1", local_result), ("task-result-transport-envelope/v1", local_envelope), ("task-result-transport-ack/v1", local_ack), ("task-result-transport-ack/v1", wrong_ack), ("task-result-transport-ack/v1", after_local), ("task-result-transport-ack/v1", receipt_time_ack)], [
            ("OK", None, None),
            ("OK", None, None),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt"),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt"),
            ("OK", None, None),
            ("OK", None, None),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt"),
            ("TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt"),
            ("CONTRACT_DOCUMENT_INVALID", "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state"),
            ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_TIME_ORDER_INVALID", "$.accepted_at"),
            ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_TIME_ORDER_INVALID", "$.accepted_at"),
        ])


if __name__ == "__main__":
    unittest.main()
