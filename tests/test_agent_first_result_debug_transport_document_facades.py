from __future__ import annotations

import unittest

from tests.agent_first_result_debug_transport_facade_support import (
    ResultDebugTransportFacadeHarness,
)


SCHEMA_IDS = (
    "actor/v1",
    "availability-reason-registry/v1",
    "task-result/v1",
    "task-result-core/v1",
    "worker-debug-file-manifest/v1",
    "worker-debug-redaction-report/v1",
    "worker-debug-fragment-descriptor/v1",
    "worker-debug-fragment/v1",
    "task-result-transport-ack/v1",
    "task-result-transport-envelope/v1",
)

HELPER_ALIASES = {
    "verify_task_result_context": "verifyTaskResultContext",
    "verify_task_result_core": "verifyTaskResultCore",
    "verify_task_result_transport_ack": "verifyTaskResultTransportAck",
    "verify_task_result_transport_envelope": "verifyTaskResultTransportEnvelope",
    "verify_worker_debug_descriptor_content": "verifyWorkerDebugDescriptorContent",
    "verify_worker_debug_fragment_content": "verifyWorkerDebugFragmentContent",
    "derive_task_result_core": "deriveTaskResultCore",
}


class AgentFirstResultDebugTransportDocumentFacadesTest(
    ResultDebugTransportFacadeHarness, unittest.TestCase
):
    def test_source_fixtures_and_constructed_availability_branches_have_expected_parity(self) -> None:
        fixture_cases = self.fixture_cases(SCHEMA_IDS)
        fixture_results = self.assert_document_parity([case for _, case in fixture_cases])

        for (fixture, (_, document)), result in zip(fixture_cases, fixture_results):
            with self.subTest(fixture_id=fixture["fixture_id"]):
                self.assertEqual(
                    fixture["expected_code"],
                    None if result["ok"] else result["code"],
                )
                if fixture["expected_code"] is None:
                    self.assertEqual(document, result["value"])

        availability_cases = [
            (
                "availability-ref/v1",
                {
                    "availability": "available",
                    "ref": self.fixture_document("task_result_golden_completed")[
                        "diagnostics"
                    ]["worker_debug_fragment"]["ref"],
                },
            ),
            (
                "availability-ref/v1",
                {
                    "availability": "not_applicable",
                    "reason_code": "CAPABILITY_NOT_IMPLEMENTED",
                },
            ),
        ]
        self.assertEqual(
            [{"ok": True, "value": case[1]} for case in availability_cases],
            self.assert_document_parity(availability_cases),
        )

    def test_idempotency_and_all_x_digest_documents_fail_closed(self) -> None:
        seen: dict[str, bytes] = {}
        for fixture, (_, document) in self.fixture_cases(SCHEMA_IDS):
            schema_id = fixture["schema_id"]
            if fixture["fixture_class"] == "golden":
                seen[schema_id] = self.python.canonical_document_bytes(document)
            if fixture["fixture_class"] == "idempotency":
                self.assertEqual(
                    seen[schema_id],
                    self.python.canonical_document_bytes(document),
                    fixture["fixture_id"],
                )

        cases = []
        for fixture_id, schema_id in (
            (
                "task_result_golden_availability_reason_registry",
                "availability-reason-registry/v1",
            ),
            (
                "worker_debug_content_golden_file_manifest",
                "worker-debug-file-manifest/v1",
            ),
            (
                "worker_debug_content_golden_redaction_report",
                "worker-debug-redaction-report/v1",
            ),
            ("task_result_transport_ack_golden_bound", "task-result-transport-ack/v1"),
        ):
            document = self.fixture_document(fixture_id)
            field = self.schema(schema_id)["x-pullwise-digest"]["field"]
            document[field] = ("0" if document[field][0] != "0" else "1") + document[field][1:]
            cases.append((schema_id, document))

        self.assertEqual(
            [
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_DIGEST_MISMATCH",
                    "path": "$.registry_digest",
                },
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_DIGEST_MISMATCH",
                    "path": "$.manifest_digest",
                },
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_DIGEST_MISMATCH",
                    "path": "$.report_digest",
                },
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_DIGEST_MISMATCH",
                    "path": "$.ack_digest",
                },
            ],
            self.assert_document_parity(cases),
        )

    def test_declared_rules_reject_resealed_semantic_drift(self) -> None:
        result = self.fixture_document("task_result_golden_completed")
        result["provenance"]["attempt_ids"] = ["attempt_" + "2" * 32, "attempt_" + "1" * 32]
        core = self.fixture_document("task_result_core_golden_completed")
        core["diagnostics"] = {"worker_debug_fragment": {"availability": "not_applicable", "reason_code": "CAPABILITY_NOT_IMPLEMENTED"}}
        file_manifest = self.fixture_document("worker_debug_content_golden_file_manifest")
        file_manifest["entry_count"] = 2
        file_manifest = self.reseal("worker-debug-file-manifest/v1", file_manifest)
        redaction = self.fixture_document("worker_debug_content_golden_redaction_report")
        redaction["archive_rescan_detection_count"] = 1
        redaction = self.reseal("worker-debug-redaction-report/v1", redaction)
        descriptor = self.fixture_document("worker_debug_transport_descriptor_golden_uploaded")
        descriptor["server_fragment_ref"] = None
        fragment = self.fixture_document("worker_debug_transport_fragment_golden_terminal")
        fragment["last_server_acked_event_seq"] = 11
        ack = self.fixture_document("task_result_transport_ack_golden_bound")
        ack["receipt_binding_state"] = "not_applicable"
        ack["receipt_digest"] = None
        ack = self.reseal("task-result-transport-ack/v1", ack)
        envelope = self.fixture_document("task_result_transport_crash_uploaded_replay")
        envelope["task_result_digest"] = "0" * 64
        cases = [
            ("task-result/v1", result),
            ("task-result-core/v1", core),
            ("worker-debug-file-manifest/v1", file_manifest),
            ("worker-debug-redaction-report/v1", redaction),
            ("worker-debug-fragment-descriptor/v1", descriptor),
            ("worker-debug-fragment/v1", fragment),
            ("task-result-transport-ack/v1", ack),
            ("task-result-transport-envelope/v1", envelope),
        ]

        self.assertEqual(
            [
                ("TASK_RESULT_ATTEMPT_ORDER_INVALID", "$"),
                ("TASK_RESULT_CORE_DEBUG_FIELD_INVALID", "$"),
                ("DEBUG_FILE_MANIFEST_COUNT_INVALID", "$"),
                ("DEBUG_REDACTION_RESCAN_FAILED", "$"),
                ("DEBUG_DESCRIPTOR_BINDING_INVALID", "$"),
                ("DEBUG_EVENT_SEQUENCE_INVALID", "$"),
                ("TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$"),
                ("TRANSPORT_ENVELOPE_DIGEST_INVALID", "$"),
            ],
            [
                (item["detail"], item["path"])
                for item in self.assert_document_parity(cases)
            ],
        )

    def test_result_debug_transport_runtime_exports_are_present_in_python_and_node(self) -> None:
        self.assertEqual(
            {
                name: {"present": True, "exported": True}
                for name in HELPER_ALIASES
            },
            self.python_exports(list(HELPER_ALIASES)),
        )
        self.assertEqual(
            {
                name: {"snake": True, "camel": True, "same": True}
                for name in HELPER_ALIASES
            },
            self.node_exports(HELPER_ALIASES),
        )


if __name__ == "__main__":
    unittest.main()
