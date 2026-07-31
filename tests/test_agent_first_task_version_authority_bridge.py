from __future__ import annotations

from copy import deepcopy
import unittest

from tests import test_agent_first_result_debug_transport_adversarial as adversarial


class AgentFirstTaskVersionAuthorityBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adversarial.AgentFirstResultDebugTransportAdversarialTest.setUpClass()
        cls.builder = adversarial.AgentFirstResultDebugTransportAdversarialTest(
            "test_all_nine_task_result_outcomes_match_across_runtimes"
        )

    def reference(
        self,
        suffix: str,
        schema_id: str,
        document: dict[str, object],
    ) -> dict[str, object]:
        return self.builder.facade.content_ref(
            "art_" + suffix * 32,
            schema_id,
            document,
        )

    def bridge_documents(self) -> dict[str, dict[str, object]]:
        envelope = self.builder.document(
            "task_result_transport_crash_uploaded_replay"
        )
        authority = envelope["authority"]
        package = envelope["package"]
        fence = envelope["full_fence"]
        base = self.builder.document("task_control_golden_task_record")
        base.update(
            {
                "task_id": authority["task_id"],
                "task_version": authority["task_version"],
                "deletion_version": authority["deletion_version"],
                "lifecycle": "ACTIVE",
                "desired_state": "RUN",
                "outer_job_id": "job-1",
                "run_id": "run-1",
                "lease_id": authority["lease_id"],
                "transport_epoch": authority["transport_epoch"],
                "native_epoch": authority["native_epoch"],
                "current_attempt_id": authority["attempt_id"],
                "owner_id": authority["owner_id"],
                "owner_epoch": authority["owner_epoch"],
                "policy_digest": authority["grant"]["policy_digest"],
                "updated_at": "2026-07-22T00:00:30.000Z",
                "terminal_kind": None,
                "result_ref": None,
                "result_digest": None,
                "outcome": None,
                "terminal_at": None,
            }
        )
        finalizing = deepcopy(base)
        finalizing.update(
            {
                "task_version": base["task_version"] + 1,
                "lifecycle": "FINALIZING",
                "updated_at": "2026-07-22T00:00:40.000Z",
            }
        )
        result = self.builder.task_result_branch("BLOCKED")
        result.update(
            {
                "task_id": base["task_id"],
                "task_type": base["task_type"],
                "published_from_version": finalizing["task_version"],
                "terminal_task_version": finalizing["task_version"] + 1,
                "attempt_identity": {
                    "kind": "started",
                    "attempt_id": base["current_attempt_id"],
                    "native_epoch": base["native_epoch"],
                },
                "owner_identity": {
                    "kind": "started",
                    "owner_id": base["owner_id"],
                    "owner_epoch": base["owner_epoch"],
                },
                "request_ref": deepcopy(base["request_ref"]),
                "policy_ref": deepcopy(base["policy_ref"]),
                "terminal_at": "2026-07-22T00:00:50.000Z",
            }
        )
        result_ref = self.reference("d", "task-result/v1", result)
        terminal = deepcopy(finalizing)
        terminal.update(
            {
                "task_version": result["terminal_task_version"],
                "lifecycle": "TERMINAL",
                "terminal_kind": "task_result",
                "result_ref": deepcopy(result_ref),
                "result_digest": result_ref["sha256"],
                "outcome": result["outcome"],
                "updated_at": result["terminal_at"],
                "terminal_at": result["terminal_at"],
            }
        )
        snapshot = self.builder.document(
            "gate_input_golden_terminalization_snapshot"
        )
        snapshot.update(
            {
                "task_id": base["task_id"],
                "attempt_id": base["current_attempt_id"],
                "native_epoch": base["native_epoch"],
                "owner_id": base["owner_id"],
                "owner_epoch": base["owner_epoch"],
                "task_version": finalizing["task_version"],
                "deletion_version": base["deletion_version"],
                "lifecycle": "FINALIZING",
                "desired_state": "RUN",
                "lease_id": base["lease_id"],
                "absolute_deadline_at": authority["absolute_deadline_at"],
                "terminal_budget_reserved_ms": authority[
                    "terminalization_reserve_ms"
                ],
            }
        )
        snapshot = self.builder.facade.reseal(
            "terminalization-input-snapshot/v1",
            snapshot,
        )

        def event(
            suffix: str,
            kind: str,
            previous: dict[str, object],
            current: dict[str, object],
            input_schema_id: str,
            input_document: dict[str, object],
        ) -> dict[str, object]:
            return self.builder.facade.reseal(
                "task-control-event/v1",
                {
                    "schema_id": "task-control-event/v1",
                    "package": deepcopy(package),
                    "event_id": "event_" + suffix * 32,
                    "event_kind": kind,
                    "idempotency_key": f"{kind}:{suffix}",
                    "authority_digest": authority["authority_digest"],
                    "grant_digest": authority["grant"]["grant_digest"],
                    "full_fence": deepcopy(fence),
                    "task_id": base["task_id"],
                    "previous_task_version": previous["task_version"],
                    "task_version": current["task_version"],
                    "input_ref": self.reference(
                        suffix, input_schema_id, input_document
                    ),
                    "previous_task_record_ref": self.reference(
                        suffix, "task-record/v1", previous
                    ),
                    "task_record_ref": self.reference(
                        chr(ord(suffix) + 1), "task-record/v1", current
                    ),
                    "occurred_at": current["updated_at"],
                },
            )

        requested = event(
            "1",
            "terminalization_requested",
            base,
            finalizing,
            "terminalization-input-snapshot/v1",
            snapshot,
        )
        published = event(
            "3",
            "task_result_published",
            finalizing,
            terminal,
            "task-result/v1",
            result,
        )
        proof = self.builder.facade.reseal(
            "task-version-authority-proof/v1",
            {
                "schema_id": "task-version-authority-proof/v1",
                "package": deepcopy(package),
                "task_id": base["task_id"],
                "authority_digest": authority["authority_digest"],
                "grant_digest": authority["grant"]["grant_digest"],
                "full_fence": deepcopy(fence),
                "base_task_record_ref": self.reference(
                    "0", "task-record/v1", base
                ),
                "version_chain": [
                    {
                        "transition_kind": "terminalization_requested",
                        "previous_task_version": base["task_version"],
                        "task_version": finalizing["task_version"],
                        "transition_ref": self.reference(
                            "5", "task-control-event/v1", requested
                        ),
                        "task_record_ref": self.reference(
                            "2", "task-record/v1", finalizing
                        ),
                    },
                    {
                        "transition_kind": "task_result_published",
                        "previous_task_version": finalizing["task_version"],
                        "task_version": terminal["task_version"],
                        "transition_ref": self.reference(
                            "6", "task-control-event/v1", published
                        ),
                        "task_record_ref": self.reference(
                            "4", "task-record/v1", terminal
                        ),
                    },
                ],
                "published_from_version": result["published_from_version"],
                "terminal_task_version": result["terminal_task_version"],
                "task_result_ref": deepcopy(result_ref),
            },
        )
        return {
            "authority": authority,
            "base": base,
            "finalizing": finalizing,
            "result": result,
            "snapshot": snapshot,
            "terminal": terminal,
            "requested": requested,
            "published": published,
            "proof": proof,
        }

    def test_versioned_bridge_closes_control_events_and_transport_authority(
        self,
    ) -> None:
        for schema_id in (
            "task-control-event/v1",
            "task-version-authority-proof/v1",
        ):
            self.assertIn(schema_id, self.builder.facade.schemas)
        for helper in (
            "verify_task_control_event_context",
            "verify_task_version_authority_proof",
        ):
            self.assertTrue(hasattr(self.builder.facade.python, helper), helper)
        documents = self.bridge_documents()
        schema_cases = [
            ("task-control-event/v1", documents["requested"]),
            ("task-control-event/v1", documents["published"]),
            ("task-version-authority-proof/v1", documents["proof"]),
        ]
        expected_documents = [
            {"ok": True, "value": document} for _, document in schema_cases
        ]
        self.assertEqual(
            expected_documents,
            self.builder.facade.python_document_results(schema_cases),
        )
        self.assertEqual(
            expected_documents,
            self.builder.facade.node_document_results(schema_cases),
        )
        operations = [
            {
                "python": "verify_task_control_event_context",
                "node": "verifyTaskControlEventContext",
                "args": [
                    documents["requested"],
                    documents["authority"],
                    documents["base"],
                    documents["finalizing"],
                    documents["snapshot"],
                ],
            },
            {
                "python": "verify_task_control_event_context",
                "node": "verifyTaskControlEventContext",
                "args": [
                    documents["published"],
                    documents["authority"],
                    documents["finalizing"],
                    documents["terminal"],
                    documents["result"],
                ],
            },
            {
                "python": "verify_task_version_authority_proof",
                "node": "verifyTaskVersionAuthorityProof",
                "args": [
                    documents["proof"],
                    documents["authority"],
                    documents["result"],
                ],
            },
        ]
        expected_helpers = [
            {"ok": True, "value": documents["requested"]},
            {"ok": True, "value": documents["published"]},
            {"ok": True, "value": documents["proof"]},
        ]
        self.assertEqual(
            expected_helpers,
            self.builder.facade.python_helper_results(operations),
        )
        self.assertEqual(
            expected_helpers,
            self.builder.facade.node_helper_results(operations),
        )
        fenced = deepcopy(documents["proof"])
        fenced["full_fence"]["task_version"] += 1
        fenced = self.builder.facade.reseal(
            "task-version-authority-proof/v1", fenced
        )
        fence_operation = {
            "python": "verify_task_version_authority_proof",
            "node": "verifyTaskVersionAuthorityProof",
            "args": [fenced, documents["authority"], documents["result"]],
        }
        expected_fence = [
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "TASK_VERSION_AUTHORITY_FENCE_INVALID",
                "path": "$.full_fence.task_version",
            }
        ]
        self.assertEqual(
            expected_fence,
            self.builder.facade.python_helper_results([fence_operation]),
        )
        self.assertEqual(
            expected_fence,
            self.builder.facade.node_helper_results([fence_operation]),
        )

    def test_transport_uses_local_version_proof_not_server_base_version(
        self,
    ) -> None:
        documents = self.bridge_documents()
        result = documents["result"]
        core = self.builder.facade.derive_core_expected(result)
        core_ref = self.reference("7", "task-result-core/v1", core)
        envelope = {
            "schema_id": "task-result-transport-envelope/v1",
            "package": deepcopy(documents["authority"]["package"]),
            "authority": deepcopy(documents["authority"]),
            "full_fence": deepcopy(documents["authority"]),
            "task_result": deepcopy(result),
            "task_result_digest": documents["proof"]["task_result_ref"]["sha256"],
            "task_result_core_ref": core_ref,
            "task_result_core_digest": core_ref["sha256"],
            "task_version_authority": deepcopy(documents["proof"]),
            "worker_debug_descriptor": None,
            "transport_receipt": {
                "availability": "not_applicable",
                "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE",
            },
        }
        envelope["full_fence"] = deepcopy(documents["proof"]["full_fence"])
        self.assertNotEqual(
            envelope["authority"]["task_version"],
            result["published_from_version"],
        )
        expected_document = {"ok": True, "value": envelope}
        self.assertEqual(
            [expected_document],
            self.builder.facade.python_document_results(
                [("task-result-transport-envelope/v1", envelope)]
            ),
        )
        self.assertEqual(
            [expected_document],
            self.builder.facade.node_document_results(
                [("task-result-transport-envelope/v1", envelope)]
            ),
        )
        operation = {
            "python": "verify_task_result_transport_envelope",
            "node": "verifyTaskResultTransportEnvelope",
            "args": [envelope, core],
        }
        python = self.builder.facade.python_helper_results([operation])
        node = self.builder.facade.node_helper_results([operation])
        self.assertEqual(python, node)
        self.assertTrue(python[0]["ok"], python)
        self.assertEqual(envelope, python[0]["value"]["document"])


if __name__ == "__main__":
    unittest.main()
