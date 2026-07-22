from __future__ import annotations

from copy import deepcopy

from tests.agent_first_task_evidence_support import sealed, timestamp_millis
from tests.agent_first_tool_evidence_support import ToolEvidenceCase, seal


class AgentFirstToolEvidenceJournalTest(ToolEvidenceCase):
    def validate_invocation_binding(
        self,
        request: dict[str, object],
        invocation: dict[str, object],
        catalog: dict[str, object],
    ) -> None:
        if set(request) != set(
            self.schemas["agent-tool-request/v1"]["required"]
        ):
            raise ValueError("AUTHORITY_INPUT_UNTRUSTED")
        tool = next(
            (
                item
                for item in catalog["tools"]
                if item["tool_key"] == request["tool_key"]
            ),
            None,
        )
        if tool is None or any(
            invocation[key] != request[key]
            for key in ("idempotency_key", "tool_key", "tool_input")
        ):
            raise ValueError("TOOL_INVOCATION_BINDING_INVALID")
        if tool["request_schema_id"] != request["schema_id"]:
            raise ValueError("TOOL_INVOCATION_BINDING_INVALID")
        if not sealed(invocation, self.schemas["tool-invocation/v1"]):
            raise ValueError("TOOL_INVOCATION_BINDING_INVALID")

    def validate_journal_begin(
        self,
        invocation: dict[str, object],
        intent: dict[str, object],
        capability: dict[str, object],
    ) -> None:
        if not (
            sealed(intent, self.schemas["tool-dispatch-intent/v1"])
            and sealed(
                capability, self.schemas["tool-dispatch-capability/v1"]
            )
        ):
            raise ValueError("TOOL_JOURNAL_BEGIN_INVALID")
        exact = (
            "package",
            "authority_digest",
            "grant_digest",
            "invocation_digest",
            "task_id",
            "idempotency_key",
            "tool_key",
            "tool_input",
        )
        if any(intent[key] != invocation[key] for key in exact):
            raise ValueError("TOOL_INTENT_BINDING_INVALID")
        if (
            intent["state"] != "INTENT"
            or capability["package"] != intent["package"]
            or capability["intent_digest"] != intent["intent_digest"]
            or capability["capability_digest"] != intent["capability_digest"]
            or capability["issued_at"] != intent["created_at"]
            or capability["max_uses"] != 1
        ):
            raise ValueError("TOOL_CAPABILITY_BINDING_INVALID")

    @staticmethod
    def validate_capability_consumption(
        intent: dict[str, object],
        capability: dict[str, object],
        consumed_capability_digests: list[str],
    ) -> None:
        if consumed_capability_digests != sorted(
            set(consumed_capability_digests)
        ):
            raise ValueError("TOOL_CAPABILITY_CONSUMPTION_INVALID")
        if capability["capability_digest"] != intent["capability_digest"]:
            raise ValueError("TOOL_CAPABILITY_BINDING_INVALID")
        if capability["capability_digest"] in consumed_capability_digests:
            raise ValueError("CAPABILITY_ALREADY_CONSUMED")

    def validate_journal_settlement(
        self,
        invocation: dict[str, object],
        intent: dict[str, object],
        receipt: dict[str, object],
        payload: dict[str, object],
        result: dict[str, object],
        source_before: dict[str, object],
        source_after: dict[str, object],
    ) -> None:
        documents = (
            ("local-tool-receipt/v1", receipt),
            ("r0-read-payload/v1", payload),
            ("r0-read-result/v1", result),
            ("source-state/v1", source_before),
            ("source-state/v1", source_after),
        )
        if any(
            not sealed(item, self.schemas[schema])
            for schema, item in documents
        ):
            raise ValueError("TOOL_SETTLEMENT_DOCUMENT_INVALID")
        if receipt["receipt_kind"] != "local_tool":
            raise ValueError("LOCAL_RECEIPT_TYPE_INVALID")
        if receipt["status"] != "succeeded":
            raise ValueError("LOCAL_RECEIPT_STATUS_INVALID")
        started = timestamp_millis(receipt["started_at"])
        completed = timestamp_millis(receipt["completed_at"])
        if (
            started is None
            or completed is None
            or completed < started
            or receipt["elapsed_ms"] != completed - started
        ):
            raise ValueError("LOCAL_RECEIPT_TIMING_INVALID")
        invocation_digest = invocation["invocation_digest"]
        if (
            receipt["tool_key"] != invocation["tool_key"]
            or receipt["invocation_digest"] != invocation_digest
            or payload["invocation_digest"] != invocation_digest
            or result["invocation_digest"] != invocation_digest
            or payload["relative_path"] != invocation["tool_input"]["relative_path"]
            or result["local_receipt_digest"] != receipt["receipt_digest"]
            or receipt["payload_ref"] != result["payload_ref"]
        ):
            raise ValueError("TOOL_SETTLEMENT_BINDING_INVALID")
        self.assert_content_ref(
            receipt["payload_ref"], "r0-read-payload/v1", payload
        )
        self.assert_content_ref(
            payload["content_ref"], "source-content/v1", self.source_content()
        )
        source_identity = ("task_id", "attempt_id", "native_epoch")
        if any(
            source_before[key] != invocation[key]
            or source_after[key] != invocation[key]
            for key in source_identity
        ):
            raise ValueError("SOURCE_STATE_BINDING_INVALID")
        if (
            source_before["source_state_id"] != source_after["source_state_id"]
            or result["source_state_before_id"]
            != source_before["source_state_id"]
            or result["source_state_after_id"] != source_after["source_state_id"]
        ):
            raise ValueError("SOURCE_STATE_CHANGED")
        if intent["invocation_digest"] != invocation_digest:
            raise ValueError("TOOL_SETTLEMENT_BINDING_INVALID")

    def test_invocation_begin_crash_and_one_shot_context(self) -> None:
        request = self.request()
        invocation = self.fixture("tool_golden_invocation")
        catalog = self.fixture("tool_golden_current_catalog")
        intent = self.fixture("tool_crash_after_intent")
        capability = self.fixture("tool_golden_dispatch_capability")

        self.validate_invocation_binding(request, invocation, catalog)
        self.validate_journal_begin(invocation, intent, capability)
        self.validate_capability_consumption(intent, capability, [])
        self.assertEqual(
            "INVOCATION_PENDING",
            self.fixtures["tool_crash_after_intent"]["expected_code"],
        )
        with self.assertRaisesRegex(ValueError, "CAPABILITY_ALREADY_CONSUMED"):
            self.validate_capability_consumption(
                intent, capability, [capability["capability_digest"]]
            )

        forged = self.fixture("tool_negative_agent_selected_authority")
        with self.assertRaisesRegex(ValueError, "AUTHORITY_INPUT_UNTRUSTED"):
            self.validate_invocation_binding(forged, invocation, catalog)
        mismatched = deepcopy(intent)
        mismatched["idempotency_key"] = "invoke:other"
        mismatched = seal(
            self.schemas["tool-dispatch-intent/v1"], mismatched
        )
        with self.assertRaisesRegex(ValueError, "TOOL_INTENT_BINDING_INVALID"):
            self.validate_journal_begin(invocation, mismatched, capability)

    def test_settlement_binds_receipt_payload_source_and_result(self) -> None:
        invocation = self.fixture("tool_golden_invocation")
        intent = self.fixture("tool_crash_after_intent")
        receipt = self.fixture("tool_golden_local_receipt")
        payload = self.fixture("tool_golden_r0_payload")
        result = self.fixture("tool_golden_r0_result")
        source = self.source_state()
        self.validate_journal_settlement(
            invocation, intent, receipt, payload, result, source, source
        )

        transport = deepcopy(receipt)
        transport["receipt_kind"] = "server_transport"
        transport = seal(self.schemas["local-tool-receipt/v1"], transport)
        with self.assertRaisesRegex(ValueError, "LOCAL_RECEIPT_TYPE_INVALID"):
            self.validate_journal_settlement(
                invocation, intent, transport, payload, result, source, source
            )

        wrong_time = deepcopy(receipt)
        wrong_time["elapsed_ms"] = 4
        wrong_time = seal(self.schemas["local-tool-receipt/v1"], wrong_time)
        with self.assertRaisesRegex(ValueError, "LOCAL_RECEIPT_TIMING_INVALID"):
            self.validate_journal_settlement(
                invocation, intent, wrong_time, payload, result, source, source
            )

        changed = deepcopy(source)
        changed["manifest_sha256"] = "9" * 64
        changed = seal(self.schemas["source-state/v1"], changed)
        changed_result = deepcopy(result)
        changed_result["source_state_after_id"] = changed["source_state_id"]
        changed_result = seal(
            self.schemas["r0-read-result/v1"], changed_result
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_STATE_CHANGED"):
            self.validate_journal_settlement(
                invocation,
                intent,
                receipt,
                payload,
                changed_result,
                source,
                changed,
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
