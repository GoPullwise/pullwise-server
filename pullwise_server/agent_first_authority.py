"""Direct current-only Server authority facade; no legacy route or dispatch path."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
from typing import Callable, Mapping

from . import db
from ._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    ContractValidationError,
    canonical_document_bytes,
    canonical_document_sha256,
    canonical_validated_bytes,
    fixture,
    package_tuple,
    schema,
    schema_ids,
    seal_document,
    tool_catalog,
    verify_bundle,
    verify_document_digest,
)
from .agent_first_authority_store import (
    AgentFirstAuthorityStore,
    AuthorityStoreError,
    FaultInjector,
)
from .agent_first_claim_authority import ClaimAuthorityStore
from .agent_first_transport_receipts import TransportReceiptStore


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STORE_ERROR_MAP = {
    "AGENT_GRANT_INVALID": "AGENT_GRANT_INVALID",
    "AUTHORITY_FENCED": "AUTHORITY_FENCED",
    "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
    "TASK_ALREADY_EXISTS": "IDEMPOTENCY_CONFLICT",
    "TASK_NOT_CLAIMABLE": "TASK_NOT_CLAIMABLE",
    "TRANSPORT_RECEIPT_ALREADY_BOUND": "TRANSPORT_RECEIPT_ALREADY_BOUND",
    "TRANSPORT_RECEIPT_BINDING_CONFLICT": "TRANSPORT_RECEIPT_BINDING_CONFLICT",
    "TRANSPORT_RECEIPT_CONFLICT": "TRANSPORT_RECEIPT_BINDING_CONFLICT",
    "WORKER_NOT_REGISTERED": "AUTHORITY_INPUT_UNTRUSTED",
    "WORKER_PACKAGE_MISMATCH": "CURRENT_PACKAGE_PIN_MISMATCH",
    "WORKER_REGISTRATION_INVALID": "AGENT_GRANT_INVALID",
    "TASK_PACKAGE_MISMATCH": "CURRENT_PACKAGE_PIN_MISMATCH",
}


def _now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _error_policies() -> dict[str, tuple[bool, str]]:
    document = fixture("error_golden_current_registry")["document"]
    registry = verify_document_digest("stable-error-registry/v1", document)
    return {
        entry["code"]: (entry["retryable"], entry["retry_scope"])
        for entry in registry["entries"]
    }


class AuthorityError(RuntimeError):
    """Typed stable service failure with package-canonical ErrorResponse bytes."""

    def __init__(self, code: str):
        policies = _error_policies()
        stable_code = code if code in policies else "AUTHORITY_INPUT_UNTRUSTED"
        retryable, retry_scope = policies[stable_code]
        request_seed = f"pullwise:authority-error:v1\0{stable_code}".encode("ascii")
        request_id = f"req_{hashlib.sha256(request_seed).hexdigest()[:32]}"
        error = seal_document(
            "stable-error/v1",
            {
                "schema_id": "stable-error/v1",
                "code": stable_code,
                "message": stable_code,
                "retryable": retryable,
                "retry_scope": retry_scope,
                "request_id": request_id,
                "details": {"stable_reason": stable_code.lower()},
            },
        )
        response = {"schema_id": "error-response/v1", "error": error}
        self.code = stable_code
        self.response_bytes = canonical_validated_bytes("error-response/v1", response)
        self.canonical_bytes = self.response_bytes
        super().__init__(stable_code)


class AgentFirstAuthority:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection] = db.connect,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        verify_bundle()
        self._store = AgentFirstAuthorityStore(connect_factory, fault_injector)
        self._claims = ClaimAuthorityStore(connect_factory, fault_injector)
        self._receipts = TransportReceiptStore(connect_factory, fault_injector)

    @staticmethod
    def _raise(code: str) -> None:
        raise AuthorityError(code)

    @classmethod
    def _package(cls, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or "package" not in request:
            cls._raise("CURRENT_PACKAGE_PIN_MISSING")
        presented = request["package"]
        current = package_tuple()
        if presented != current:
            cls._raise("CURRENT_PACKAGE_PIN_MISMATCH")
        return current

    @classmethod
    def _validate(cls, schema_id: str, request: object) -> dict[str, object]:
        cls._package(request)
        try:
            return json.loads(canonical_validated_bytes(schema_id, request))
        except (ContractValidationError, UnicodeError, ValueError, TypeError):
            cls._raise("CONTRACT_DOCUMENT_INVALID")
        raise AssertionError("unreachable")

    @classmethod
    def _verify_digest(
        cls, schema_id: str, request: object, *, require_package: bool = True
    ) -> dict[str, object]:
        if require_package:
            cls._package(request)
        try:
            return verify_document_digest(schema_id, request)
        except (ContractValidationError, UnicodeError, ValueError, TypeError):
            cls._raise("CONTRACT_DOCUMENT_INVALID")
        raise AssertionError("unreachable")

    @classmethod
    def _store_call(cls, callback: Callable[[], bytes]) -> bytes:
        try:
            return callback()
        except AuthorityStoreError as error:
            cls._raise(_STORE_ERROR_MAP.get(error.code, "AUTHORITY_INPUT_UNTRUSTED"))
        raise AssertionError("unreachable")

    def register_worker(self, request: dict[str, object]) -> bytes:
        document = self._verify_digest("agent-worker-register/v1", request)
        try:
            for schema_id in document["supported_schema_ids"]:
                schema(schema_id)
        except (KeyError, TypeError):
            self._raise("CONTRACT_DOCUMENT_INVALID")
        response = seal_document(
            "agent-worker-register-response/v1",
            {
                "schema_id": "agent-worker-register-response/v1",
                "package": package_tuple(),
                "worker_id": document["worker_id"],
                "accepted_schema_ids": document["supported_schema_ids"],
                "registered_at": _now(),
            },
        )
        request_bytes = canonical_validated_bytes("agent-worker-register/v1", document)
        values = {
            "registration_id": f"registration_{secrets.token_hex(16)}",
            "worker_id": document["worker_id"],
            "package_tuple": PACKAGE_TUPLE,
            "supported_schema_ids": canonical_document_bytes(
                document["supported_schema_ids"]
            ),
            "tool_catalog_digest": document["tool_catalog_digest"],
            "request_digest": canonical_document_sha256(document),
            "request_bytes": request_bytes,
            "response_bytes": canonical_validated_bytes(
                "agent-worker-register-response/v1", response
            ),
        }
        return self._store_call(lambda: self._store.register_worker(values))

    def accept_current_task(self, request: dict[str, object]) -> bytes:
        document = self._validate("agent-task-request/v1", request)
        policy = self._verify_digest(
            "agent-task-policy/v1", document["policy"], require_package=False
        )
        response = seal_document(
            "agent-task-accept-response/v1",
            {
                "schema_id": "agent-task-accept-response/v1",
                "package": package_tuple(),
                "task_id": document["task_id"],
                "task_version": 1,
                "deletion_version": 0,
                "lifecycle": "QUEUED",
                "desired_state": "RUN",
                "accepted_at": _now(),
            },
        )
        values = {
            "task_id": document["task_id"],
            "task_type": document["task_type"],
            "package_tuple": PACKAGE_TUPLE,
            "policy_digest": policy["policy_digest"],
            "policy_bytes": canonical_validated_bytes("agent-task-policy/v1", policy),
            "idempotency_key": document["idempotency_key"],
            "request_digest": canonical_document_sha256(document),
            "request_bytes": canonical_validated_bytes("agent-task-request/v1", document),
            "owner_id": f"owner_{secrets.token_hex(16)}",
            "response_bytes": canonical_validated_bytes(
                "agent-task-accept-response/v1", response
            ),
        }
        return self._store_call(lambda: self._store.accept_task(values))

    def claim_and_issue_current_grant(self, request: dict[str, object]) -> bytes:
        document = self._validate("agent-task-claim-request/v1", request)
        request_digest = canonical_document_sha256(document)
        values = {
            **document,
            "package_tuple": PACKAGE_TUPLE,
            "request_digest": request_digest,
            "required_schema_ids": tuple(schema_ids()),
            "expected_tool_catalog_digest": tool_catalog()["catalog_digest"],
        }

        def build(head: sqlite3.Row) -> Mapping[str, object]:
            policy = json.loads(self._store._blob(head["policy_bytes"]))
            policy_fields = (
                "capability_ids",
                "tool_keys",
                "elapsed_limit_ms",
                "tool_call_limit",
            )
            if any(document[field] != policy[field] for field in policy_fields):
                raise AuthorityStoreError("AGENT_GRANT_INVALID")
            task_version = head["task_version"] + 1
            attempt_id = f"attempt_{secrets.token_hex(16)}"
            session_id = f"sess_{secrets.token_hex(16)}"
            grant_id = f"grant_{secrets.token_hex(16)}"
            common = {
                "package": package_tuple(),
                "task_id": document["task_id"],
                "attempt_id": attempt_id,
                "session_id": session_id,
                "owner_id": head["owner_id"],
                "lease_id": document["lease_id"],
                "task_version": task_version,
                "deletion_version": head["deletion_version"],
                "owner_epoch": head["owner_epoch"] + 1,
                "native_epoch": head["native_epoch"] + 1,
                "transport_epoch": document["transport_epoch"],
            }
            grant = seal_document(
                "agent-worker-grant/v1",
                {
                    "schema_id": "agent-worker-grant/v1",
                    **common,
                    "grant_id": grant_id,
                    "policy_digest": head["policy_digest"],
                    **{field: policy[field] for field in policy_fields},
                },
            )
            claim = seal_document(
                "agent-task-claim/v1",
                {
                    "schema_id": "agent-task-claim/v1",
                    **common,
                    "claim_id": f"claim_{secrets.token_hex(16)}",
                    "grant": grant,
                },
            )
            envelope = seal_document(
                "server-authority-envelope/v1",
                {
                    "schema_id": "server-authority-envelope/v1",
                    **common,
                    "lifecycle": "ACTIVE",
                    "desired_state": "RUN",
                    "grant": grant,
                },
            )
            return {
                **common,
                "previous_task_version": head["task_version"],
                "claim_id": claim["claim_id"],
                "claim_digest": claim["claim_digest"],
                "claim_bytes": canonical_validated_bytes("agent-task-claim/v1", claim),
                "grant_id": grant_id,
                "grant_digest": grant["grant_digest"],
                "grant_bytes": canonical_validated_bytes("agent-worker-grant/v1", grant),
                "authority_digest": envelope["authority_digest"],
                "authority_bytes": canonical_validated_bytes(
                    "server-authority-envelope/v1", envelope
                ),
                "response_bytes": canonical_validated_bytes(
                    "server-authority-envelope/v1", envelope
                ),
            }

        return self._store_call(lambda: self._claims.claim_task(values, build))

    def store_transport_receipt(self, receipt: dict[str, object]) -> bytes:
        if isinstance(receipt, dict) and receipt.get("receipt_kind") != "server_transport":
            self._raise("TRANSPORT_RECEIPT_TYPE_INVALID")
        document = self._verify_digest("server-transport-receipt/v1", receipt)
        receipt_bytes = canonical_validated_bytes("server-transport-receipt/v1", document)
        values = {
            **document,
            "package_tuple": PACKAGE_TUPLE,
            "receipt_bytes": receipt_bytes,
            "response_bytes": receipt_bytes,
        }
        return self._store_call(lambda: self._receipts.store_receipt(values))

    def bind_transport_receipt(
        self, receipt_digest: str, transport_envelope_digest: str
    ) -> bytes:
        if not isinstance(transport_envelope_digest, str) or not _HEX_DIGEST.fullmatch(
            transport_envelope_digest
        ):
            self._raise("TRANSPORT_ENVELOPE_DIGEST_INVALID")
        if not isinstance(receipt_digest, str) or not _HEX_DIGEST.fullmatch(receipt_digest):
            self._raise("AUTHORITY_INPUT_UNTRUSTED")

        def build(receipt_id: str) -> bytes:
            response = seal_document(
                "server-transport-receipt-binding-response/v1",
                {
                    "schema_id": "server-transport-receipt-binding-response/v1",
                    "receipt_id": receipt_id,
                    "receipt_digest": receipt_digest,
                    "transport_envelope_digest": transport_envelope_digest,
                    "state": "bound",
                    "bound_at": _now(),
                },
            )
            return canonical_validated_bytes(
                "server-transport-receipt-binding-response/v1", response
            )

        return self._store_call(
            lambda: self._receipts.bind_receipt(
                receipt_digest, transport_envelope_digest, build
            )
        )

    def abandon_current_claim(self, request: dict[str, object]) -> bytes:
        document = self._validate("agent-claim-abandon-request/v1", request)
        task_version = document["expected_task_version"] + 1
        response = seal_document(
            "agent-claim-abandon-response/v1",
            {
                "schema_id": "agent-claim-abandon-response/v1",
                "package": package_tuple(),
                **{
                    key: document[key]
                    for key in (
                        "task_id",
                        "attempt_id",
                        "session_id",
                        "owner_id",
                        "grant_id",
                        "lease_id",
                        "deletion_version",
                        "owner_epoch",
                        "native_epoch",
                        "transport_epoch",
                    )
                },
                "task_version": task_version,
                "state": "FENCED",
                "abandoned_at": _now(),
            },
        )
        response_bytes = canonical_validated_bytes(
            "agent-claim-abandon-response/v1", response
        )
        values = {
            **document,
            "package_tuple": PACKAGE_TUPLE,
            "task_version": task_version,
            "request_digest": canonical_document_sha256(document),
            "abandonment_id": f"abandonment_{secrets.token_hex(16)}",
            "abandonment_digest": response["response_digest"],
            "abandonment_bytes": response_bytes,
            "response_bytes": response_bytes,
        }
        return self._store_call(lambda: self._claims.abandon_claim(values))


__all__ = ["AgentFirstAuthority", "AuthorityError"]
