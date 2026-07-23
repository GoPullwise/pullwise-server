"""Current-only Server authority facade; dispatch execution is Worker-owned."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
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
    validate_effective_policy_derivation,
    validate_task_request_acceptance,
    verify_bundle,
    verify_document_digest,
)
from .agent_first_authority_store import (
    AgentFirstAuthorityStore,
    AuthorityStoreError,
    FaultInjector,
)
from .agent_first_claim_authority import ClaimAuthorityStore
from .agent_first_transport_envelope_authority import TransportEnvelopeAuthority
from .agent_first_transport_receipts import TransportReceiptStore


_STORE_ERROR_MAP = {
    "AGENT_GRANT_INVALID": "AGENT_GRANT_INVALID",
    "AUTHORITY_FENCED": "AUTHORITY_FENCED",
    "AUTHORITY_STORAGE_CORRUPT": "AUTHORITY_RELOAD_REQUIRED",
    "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
    "TASK_ALREADY_EXISTS": "IDEMPOTENCY_CONFLICT",
    "TASK_NOT_CLAIMABLE": "TASK_NOT_CLAIMABLE",
    "TERMINAL_ENVELOPE_INVALID": "CONTRACT_DOCUMENT_INVALID",
    "TERMINAL_OUTCOME_INVALID": "CONTRACT_DOCUMENT_INVALID",
    "TERMINAL_RESULT_CONFLICT": "IDEMPOTENCY_CONFLICT",
    "AUTHORITY_MISMATCH": "AUTHORITY_INPUT_UNTRUSTED",
    "TRANSPORT_ENVELOPE_DIGEST_INVALID": "TRANSPORT_ENVELOPE_DIGEST_INVALID",
    "TRANSPORT_RECEIPT_ALREADY_BOUND": "TRANSPORT_RECEIPT_ALREADY_BOUND",
    "TRANSPORT_RECEIPT_BINDING_CONFLICT": "TRANSPORT_RECEIPT_BINDING_CONFLICT",
    "TRANSPORT_RECEIPT_CONFLICT": "TRANSPORT_RECEIPT_BINDING_CONFLICT",
    "TRANSPORT_RECEIPT_NOT_FOUND": "TRANSPORT_RECEIPT_BINDING_CONFLICT",
    "TRANSPORT_RECEIPT_TYPE_INVALID": "TRANSPORT_RECEIPT_TYPE_INVALID",
    "WORKER_NOT_REGISTERED": "AUTHORITY_INPUT_UNTRUSTED",
    "WORKER_PACKAGE_MISMATCH": "CURRENT_PACKAGE_PIN_MISMATCH",
    "WORKER_REGISTRATION_INVALID": "AGENT_GRANT_INVALID",
    "TASK_PACKAGE_MISMATCH": "CURRENT_PACKAGE_PIN_MISMATCH",
}


ACCEPTANCE_OPERATION_ENVELOPE_KEYS = (
    "package",
    "idempotency_key",
    "task_request",
    "effective_policy",
)


def _effective_policy_grant_fields(policy: Mapping[str, object]) -> dict[str, object]:
    capability_ids = list(policy["granted_capabilities"])
    catalog_tools = tool_catalog()["tools"]
    tool_keys = sorted(
        tool["tool_key"]
        for tool in catalog_tools
        if tool["capability_id"] in capability_ids
    )
    if not capability_ids or not tool_keys:
        raise ValueError("effective policy has no representable grants")
    if any(
        not any(
            tool["capability_id"] == capability_id for tool in catalog_tools
        )
        for capability_id in capability_ids
    ):
        raise ValueError("granted capability has no catalog tool")
    budgets = policy["budgets"]
    elapsed_limit_ms = budgets["wall_ms"]
    tool_call_limit = budgets["tool_calls"]
    if (
        not isinstance(elapsed_limit_ms, (int, float))
        or isinstance(elapsed_limit_ms, bool)
        or not math.isfinite(elapsed_limit_ms)
        or not elapsed_limit_ms >= 1
        or not isinstance(tool_call_limit, (int, float))
        or isinstance(tool_call_limit, bool)
        or not math.isfinite(tool_call_limit)
        or not tool_call_limit >= 1
    ):
        raise ValueError("effective policy has invalid representable budgets")
    return {
        "capability_ids": capability_ids,
        "tool_keys": tool_keys,
        "elapsed_limit_ms": elapsed_limit_ms,
        "tool_call_limit": tool_call_limit,
    }


def _effective_policy_deadline_fields(
    policy: Mapping[str, object], accepted_at: str
) -> dict[str, object]:
    budgets = policy["budgets"]
    wall_ms = budgets["wall_ms"]
    reserve_ms = policy["terminalization_reserve_ms"]
    if (
        not isinstance(wall_ms, int)
        or isinstance(wall_ms, bool)
        or wall_ms < 1
        or wall_ms > 9007199254740991
        or not isinstance(reserve_ms, int)
        or isinstance(reserve_ms, bool)
        or reserve_ms < 0
        or reserve_ms > 9007199254740991
    ):
        raise ValueError("effective policy has invalid deadline fields")
    try:
        accepted = dt.datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
        if accepted.tzinfo != dt.timezone.utc:
            raise ValueError
        deadline = accepted + dt.timedelta(milliseconds=wall_ms)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("effective policy deadline is not representable") from None
    return {
        "absolute_deadline_at": deadline.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "terminalization_reserve_ms": reserve_ms,
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
        self._transport_envelopes = TransportEnvelopeAuthority(
            connect_factory,
            fault_injector,
        )

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
        self._package(request)
        try:
            if (
                set(request) != set(ACCEPTANCE_OPERATION_ENVELOPE_KEYS)
                or not isinstance(request["idempotency_key"], str)
                or not 1 <= len(request["idempotency_key"]) <= 160
            ):
                self._raise("CONTRACT_DOCUMENT_INVALID")
            document = validate_task_request_acceptance(request["task_request"])
            policy = validate_effective_policy_derivation(
                document,
                request["effective_policy"],
            )
            _effective_policy_grant_fields(policy)
            accepted_at = _now()
            deadline_fields = _effective_policy_deadline_fields(policy, accepted_at)
            request_bytes = canonical_validated_bytes("task-request/v1", document)
            policy_bytes = canonical_validated_bytes(
                "effective-execution-policy/v1", policy
            )
            event_request_digest = canonical_document_sha256(
                {
                    "package": request["package"],
                    "idempotency_key": request["idempotency_key"],
                    "task_request": document,
                    "effective_policy": policy,
                }
            )
        except (ContractValidationError, UnicodeError, ValueError, TypeError, KeyError):
            self._raise("CONTRACT_DOCUMENT_INVALID")
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
                "accepted_at": accepted_at,
            },
        )
        values = {
            "task_id": document["task_id"],
            "task_type": document["task_type"],
            "package_tuple": PACKAGE_TUPLE,
            "policy_digest": policy["digest"],
            "policy_bytes": policy_bytes,
            "idempotency_key": request["idempotency_key"],
            "request_digest": canonical_document_sha256(document),
            "event_request_digest": event_request_digest,
            "request_bytes": request_bytes,
            "owner_id": f"owner_{secrets.token_hex(16)}",
            "accepted_at": accepted_at,
            **deadline_fields,
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
            try:
                policy = verify_document_digest(
                    "effective-execution-policy/v1",
                    json.loads(self._store._blob(head["policy_bytes"])),
                )
                if policy["digest"] != head["policy_digest"]:
                    raise ValueError("stored policy digest mismatch")
                policy_fields = _effective_policy_grant_fields(policy)
                expected_deadline = _effective_policy_deadline_fields(
                    policy, head["accepted_at"]
                )
                if any(
                    head[field] != value
                    for field, value in expected_deadline.items()
                ):
                    raise ValueError("stored deadline wire mismatch")
            except (ContractValidationError, UnicodeError, ValueError, TypeError, KeyError):
                raise AuthorityStoreError("AUTHORITY_STORAGE_CORRUPT") from None
            if any(document[field] != value for field, value in policy_fields.items()):
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
            deadline_wire = {
                "absolute_deadline_at": head["absolute_deadline_at"],
                "terminalization_reserve_ms": head["terminalization_reserve_ms"],
            }
            grant = seal_document(
                "agent-worker-grant/v1",
                {
                    "schema_id": "agent-worker-grant/v1",
                    **common,
                    "grant_id": grant_id,
                    "policy_digest": head["policy_digest"],
                    **deadline_wire,
                    **policy_fields,
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
                    **deadline_wire,
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
            "receipt_bytes_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "receipt_size_bytes": len(receipt_bytes),
            "receipt_bytes": receipt_bytes,
            "response_bytes": receipt_bytes,
        }
        return self._store_call(lambda: self._receipts.store_receipt(values))

    def commit_current_transport_envelope(
        self,
        envelope: dict[str, object],
    ) -> bytes:
        self._package(envelope)
        return self._store_call(
            lambda: self._transport_envelopes.commit(envelope)
        )

    def abandon_current_claim(self, request: dict[str, object]) -> bytes:
        document = self._validate("agent-claim-abandon-request/v1", request)
        values = {
            **document,
            "package_tuple": PACKAGE_TUPLE,
            "request_digest": canonical_document_sha256(document),
        }

        def build(head: sqlite3.Row, stored: sqlite3.Row) -> Mapping[str, object]:
            grant = self._verify_digest(
                "agent-worker-grant/v1",
                json.loads(self._store._blob(stored["grant_bytes"])),
            )
            task_version = head["task_version"] + 1
            abandonment_id = f"abandonment_{secrets.token_hex(16)}"
            abandoned_at = _now()
            superseded_authority_digest = head["current_authority_digest"]
            response = seal_document("agent-claim-abandon-response/v1", {
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
                "previous_task_version": head["task_version"],
                "task_version": task_version,
                "state": "FENCED",
                "grant": grant,
                "superseded_authority_digest": superseded_authority_digest,
                "reason": document["reason"],
                "abandoned_at": abandoned_at,
            })
            response_bytes = canonical_validated_bytes(
                "agent-claim-abandon-response/v1", response
            )
            abandonment = seal_document("transport-abandonment-record/v1", {
                "schema_id": "transport-abandonment-record/v1",
                "package": package_tuple(),
                "abandonment_id": abandonment_id,
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
                "previous_task_version": head["task_version"],
                "abandoned_task_version": task_version,
                "grant_digest": stored["grant_digest"],
                "superseded_authority_digest": superseded_authority_digest,
                "reason": document["reason"],
                "abandoned_at": abandoned_at,
            })
            abandonment_bytes = canonical_validated_bytes(
                "transport-abandonment-record/v1", abandonment
            )
            return {
                "task_version": task_version,
                "abandonment_id": abandonment_id,
                "abandonment_digest": abandonment["abandonment_digest"],
                "abandonment_bytes": abandonment_bytes,
                "grant_digest": stored["grant_digest"],
                "superseded_authority_digest": superseded_authority_digest,
                "successor_authority_digest": response["response_digest"],
                "response_bytes": response_bytes,
            }

        return self._store_call(lambda: self._claims.abandon_claim(values, build))

__all__ = ["AgentFirstAuthority", "AuthorityError"]
