from __future__ import annotations

import hashlib
import json

from pullwise_server._generated_agent_task_contract import (
    canonical_validated_bytes,
    derive_task_result_core,
    package_tuple,
    seal_document,
)


class TransportEnvelopeHarness:
    def content_ref(
        self,
        *,
        artifact_id: str,
        content_schema_id: str,
        content: bytes,
    ) -> dict[str, object]:
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": artifact_id,
            "content_schema_id": content_schema_id,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    def transport_envelope(
        self,
        authority: dict[str, object],
        *,
        diagnostics_state: str,
        outcome: str = "COMPLETED",
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        fragment_ref = self.content_ref(
            artifact_id="art_99999999999999999999999999999991",
            content_schema_id="worker-debug-fragment/v1",
            content=b'{"debug":"fragment"}',
        )
        receipt, receipt_ref = self._transport_receipt(
            authority,
            diagnostics_state,
            fragment_ref,
        )
        descriptor = self._debug_descriptor(
            diagnostics_state,
            fragment_ref,
            receipt_ref,
        )
        task_result = self.task_result(authority, outcome=outcome)
        task_result["diagnostics"] = {
            "worker_debug_fragment": self._debug_availability(
                diagnostics_state,
                descriptor,
            )
        }
        task_result_bytes = canonical_validated_bytes("task-result/v1", task_result)
        core = derive_task_result_core(task_result)
        core_bytes = canonical_validated_bytes("task-result-core/v1", core)
        full_fence = self._full_fence(authority)
        document = {
            "schema_id": "task-result-transport-envelope/v1",
            "package": package_tuple(),
            "authority": authority,
            "full_fence": full_fence,
            "task_result": task_result,
            "task_result_digest": hashlib.sha256(task_result_bytes).hexdigest(),
            "task_result_core_ref": self.content_ref(
                artifact_id="art_99999999999999999999999999999992",
                content_schema_id="task-result-core/v1",
                content=core_bytes,
            ),
            "task_result_core_digest": hashlib.sha256(core_bytes).hexdigest(),
            "transport_receipt": (
                {"availability": "available", "ref": receipt_ref}
                if receipt_ref is not None
                else {
                    "availability": "not_applicable",
                    "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE",
                }
            ),
            "worker_debug_descriptor": descriptor,
        }
        canonical = canonical_validated_bytes(
            "task-result-transport-envelope/v1", document
        )
        return json.loads(canonical), receipt

    def _transport_receipt(
        self,
        authority: dict[str, object],
        diagnostics_state: str,
        fragment_ref: dict[str, object],
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        if diagnostics_state != "uploaded":
            return None, None
        receipt = self.receipt(authority, content_ref=fragment_ref)
        receipt_bytes = canonical_validated_bytes(
            "server-transport-receipt/v1", receipt
        )
        reference = self.content_ref(
            artifact_id="art_99999999999999999999999999999993",
            content_schema_id="server-transport-receipt/v1",
            content=receipt_bytes,
        )
        return receipt, reference

    def _debug_descriptor(
        self,
        state: str,
        fragment_ref: dict[str, object],
        receipt_ref: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if state not in ("uploaded", "local_only"):
            return None
        return {
            "schema_id": "worker-debug-fragment-descriptor/v1",
            "state": state,
            "fragment_ref": fragment_ref,
            "server_fragment_ref": fragment_ref if state == "uploaded" else None,
            "server_receipt_ref": receipt_ref,
            "transport_kind": "server_transport" if state == "uploaded" else "none",
        }

    def _debug_availability(
        self,
        state: str,
        descriptor: dict[str, object] | None,
    ) -> dict[str, object]:
        if descriptor is None:
            return {
                "availability": state,
                "reason_code": (
                    "DEBUG_UNAVAILABLE"
                    if state == "unavailable"
                    else "CAPABILITY_NOT_IMPLEMENTED"
                ),
            }
        descriptor_bytes = canonical_validated_bytes(
            "worker-debug-fragment-descriptor/v1", descriptor
        )
        return {
            "availability": "available",
            "ref": self.content_ref(
                artifact_id="art_99999999999999999999999999999994",
                content_schema_id="worker-debug-fragment-descriptor/v1",
                content=descriptor_bytes,
            ),
        }

    @staticmethod
    def _full_fence(authority: dict[str, object]) -> dict[str, object]:
        fields = (
            "task_id",
            "attempt_id",
            "session_id",
            "owner_id",
            "lease_id",
            "task_version",
            "deletion_version",
            "owner_epoch",
            "native_epoch",
            "transport_epoch",
        )
        return seal_document(
            "task-fence/v1",
            {
                "schema_id": "task-fence/v1",
                **{field: authority[field] for field in fields},
            },
        )


__all__ = ["TransportEnvelopeHarness"]
