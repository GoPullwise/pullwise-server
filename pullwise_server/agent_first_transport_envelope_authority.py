"""Package-canonical preparation for atomic terminal envelope publication."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from typing import Mapping

from ._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    ContractValidationError,
    canonical_validated_bytes,
    derive_task_result_core,
    package_tuple,
    seal_document,
    verify_document_digest,
    verify_task_result_core,
    verify_task_result_transport_envelope,
)
from .agent_first_authority_store import AuthorityStoreError, FaultInjector
from .agent_first_transport_envelopes import TransportEnvelopeStore


def _now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class TransportEnvelopeAuthority:
    def __init__(
        self,
        connect_factory,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._store = TransportEnvelopeStore(connect_factory, fault_injector)

    def commit(self, envelope: dict[str, object]) -> bytes:
        document, task_result, core = self._prepare_documents(envelope)
        descriptor = document["worker_debug_descriptor"]
        receipt_ref = self._receipt_ref(document["transport_receipt"])
        fence = document["full_fence"]
        authority = document["authority"]
        envelope_bytes = canonical_validated_bytes(
            "task-result-transport-envelope/v1", document
        )
        task_result_bytes = canonical_validated_bytes("task-result/v1", task_result)
        core_bytes = canonical_validated_bytes("task-result-core/v1", core)
        task_result_digest = hashlib.sha256(task_result_bytes).hexdigest()
        if document["task_result_digest"] != task_result_digest:
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
        values = {
            "transport_envelope_digest": hashlib.sha256(envelope_bytes).hexdigest(),
            "task_result_digest": task_result_digest,
            "task_result_core_digest": hashlib.sha256(core_bytes).hexdigest(),
            "result_id": task_result["result_id"],
            "task_id": fence["task_id"],
            "outcome": task_result["outcome"],
            "published_from_version": task_result["published_from_version"],
            "terminal_task_version": task_result["terminal_task_version"],
            "diagnostics_state": self._diagnostics_state(task_result, descriptor),
            "receipt_ref_sha256": None if receipt_ref is None else receipt_ref["sha256"],
            "receipt_ref_size_bytes": (
                None if receipt_ref is None else receipt_ref["size_bytes"]
            ),
            "attempt_id": fence["attempt_id"],
            "session_id": fence["session_id"],
            "owner_id": fence["owner_id"],
            "grant_id": authority["grant"]["grant_id"],
            "lease_id": fence["lease_id"],
            "deletion_version": fence["deletion_version"],
            "owner_epoch": fence["owner_epoch"],
            "native_epoch": fence["native_epoch"],
            "transport_epoch": fence["transport_epoch"],
            "grant_digest": authority["grant"]["grant_digest"],
            "authority_digest": authority["authority_digest"],
            "package_tuple": PACKAGE_TUPLE,
            "task_result_bytes": task_result_bytes,
            "task_result_core_bytes": core_bytes,
            "worker_debug_descriptor_bytes": self._descriptor_bytes(descriptor),
            "envelope_bytes": envelope_bytes,
        }

        def build(
            current: sqlite3.Row,
            receipt_row: sqlite3.Row | None,
        ) -> Mapping[str, object]:
            return self._build_responses(
                document,
                core,
                descriptor,
                current,
                receipt_row,
                values,
            )

        return self._store.commit(values, build)

    @staticmethod
    def _prepare_documents(
        envelope: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        try:
            document = json.loads(
                canonical_validated_bytes(
                    "task-result-transport-envelope/v1", envelope
                )
            )
            task_result = document["task_result"]
            core = derive_task_result_core(task_result)
            verify_task_result_core(task_result, core)
            return document, task_result, core
        except (ContractValidationError, KeyError, TypeError, UnicodeError, ValueError):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID") from None

    @staticmethod
    def _receipt_ref(transport_receipt: object) -> dict[str, object] | None:
        if not isinstance(transport_receipt, dict):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
        if transport_receipt.get("availability") == "available":
            reference = transport_receipt.get("ref")
            if not isinstance(reference, dict):
                raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
            return reference
        return None

    @staticmethod
    def _diagnostics_state(
        task_result: Mapping[str, object],
        descriptor: object,
    ) -> object:
        if isinstance(descriptor, dict):
            return descriptor["state"]
        diagnostics = task_result["diagnostics"]
        if not isinstance(diagnostics, dict):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
        worker_debug = diagnostics["worker_debug_fragment"]
        if not isinstance(worker_debug, dict):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
        return worker_debug["availability"]

    @staticmethod
    def _descriptor_bytes(descriptor: object) -> bytes | None:
        if descriptor is None:
            return None
        return canonical_validated_bytes(
            "worker-debug-fragment-descriptor/v1", descriptor
        )

    def _build_responses(
        self,
        document: dict[str, object],
        core: dict[str, object],
        descriptor: object,
        current: sqlite3.Row,
        receipt_row: sqlite3.Row | None,
        values: Mapping[str, object],
    ) -> Mapping[str, object]:
        try:
            stored_authority = verify_document_digest(
                "server-authority-envelope/v1",
                json.loads(self._store._blob(current["authority_bytes"])),
            )
            stored_grant = verify_document_digest(
                "agent-worker-grant/v1",
                json.loads(self._store._blob(current["grant_bytes"])),
            )
            if document["authority"] != stored_authority:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            if stored_authority["grant"] != stored_grant:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            receipt = self._stored_receipt(receipt_row)
            verified = verify_task_result_transport_envelope(
                document,
                core,
                transport_receipt=receipt,
                worker_debug_descriptor=descriptor,
            )
            if (
                verified["document"] != document
                or verified["canonical_bytes"] != values["envelope_bytes"]
                or verified["transport_envelope_digest"]
                != values["transport_envelope_digest"]
            ):
                raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
            return self._response_bytes(document, receipt, values)
        except AuthorityStoreError:
            raise
        except (ContractValidationError, KeyError, TypeError, UnicodeError, ValueError):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID") from None

    def _stored_receipt(self, row: sqlite3.Row | None) -> dict[str, object] | None:
        if row is None:
            return None
        receipt = verify_document_digest(
            "server-transport-receipt/v1",
            json.loads(self._store._blob(row["receipt_bytes"])),
        )
        if receipt["receipt_digest"] != row["receipt_digest"]:
            raise AuthorityStoreError("AUTHORITY_STORAGE_CORRUPT")
        return receipt

    @staticmethod
    def _response_bytes(
        document: Mapping[str, object],
        receipt: dict[str, object] | None,
        values: Mapping[str, object],
    ) -> Mapping[str, object]:
        task_result = document["task_result"]
        if not isinstance(task_result, dict):
            raise AuthorityStoreError("TERMINAL_ENVELOPE_INVALID")
        accepted_at = _now()
        ack = seal_document(
            "task-result-transport-ack/v1",
            {
                "schema_id": "task-result-transport-ack/v1",
                "package": package_tuple(),
                "result_id": task_result["result_id"],
                "task_id": task_result["task_id"],
                "outcome": task_result["outcome"],
                "published_from_version": task_result["published_from_version"],
                "terminal_task_version": task_result["terminal_task_version"],
                "transport_envelope_digest": values["transport_envelope_digest"],
                "receipt_binding_state": (
                    "bound" if receipt is not None else "not_applicable"
                ),
                "receipt_digest": (
                    None if receipt is None else receipt["receipt_digest"]
                ),
                "accepted_at": accepted_at,
            },
        )
        binding = None
        if receipt is not None:
            binding = seal_document(
                "server-transport-receipt-binding-response/v1",
                {
                    "schema_id": "server-transport-receipt-binding-response/v1",
                    "receipt_id": receipt["receipt_id"],
                    "receipt_digest": receipt["receipt_digest"],
                    "transport_envelope_digest": values["transport_envelope_digest"],
                    "state": "bound",
                    "bound_at": accepted_at,
                },
            )
        return {
            "response_bytes": canonical_validated_bytes(
                "task-result-transport-ack/v1", ack
            ),
            "binding_response_bytes": (
                None
                if binding is None
                else canonical_validated_bytes(
                    "server-transport-receipt-binding-response/v1", binding
                )
            ),
        }


__all__ = ["TransportEnvelopeAuthority"]
