"""Canonical acceptance and atomic runtime-bootstrap document construction."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import secrets
from typing import Mapping

from ._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    ContractValidationError,
    canonical_document_sha256,
    canonical_validated_bytes,
    package_tuple,
    seal_document,
    tool_catalog,
    validate_claim_write_set,
    verify_document_digest,
)


PROTOCOL_MODE = "agent_task_v1"


class RuntimeBootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def effective_policy_grant_fields(
    policy: Mapping[str, object],
) -> dict[str, object]:
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
        not any(tool["capability_id"] == item for tool in catalog_tools)
        for item in capability_ids
    ):
        raise ValueError("granted capability has no catalog tool")
    budgets = policy["budgets"]
    elapsed_limit_ms = budgets["wall_ms"]
    tool_call_limit = budgets["tool_calls"]
    for value in (elapsed_limit_ms, tool_call_limit):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 1
        ):
            raise ValueError("effective policy has invalid representable budgets")
    return {
        "capability_ids": capability_ids,
        "tool_keys": tool_keys,
        "elapsed_limit_ms": elapsed_limit_ms,
        "tool_call_limit": tool_call_limit,
    }


def effective_policy_deadline_fields(
    policy: Mapping[str, object], accepted_at: str
) -> dict[str, object]:
    wall_ms = policy["budgets"]["wall_ms"]
    reserve_ms = policy["terminalization_reserve_ms"]
    for value, minimum in ((wall_ms, 1), (reserve_ms, 0)):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or value > 9007199254740991
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


def build_acceptance_values(
    accept_request: dict[str, object], *, accepted_at: str, owner_id: str
) -> dict[str, object]:
    task_request = accept_request["task_request"]
    policy = accept_request["effective_policy"]
    ledger = accept_request["requirement_ledger"]
    effective_policy_grant_fields(policy)
    deadline = effective_policy_deadline_fields(policy, accepted_at)
    response = seal_document(
        "agent-task-accept-response/v1",
        {
            "schema_id": "agent-task-accept-response/v1",
            "package": package_tuple(),
            "task_id": task_request["task_id"],
            "task_version": 1,
            "deletion_version": 0,
            "lifecycle": "QUEUED",
            "desired_state": "RUN",
            "accepted_at": accepted_at,
        },
    )
    return {
        "task_id": task_request["task_id"],
        "task_type": task_request["task_type"],
        "package_tuple": PACKAGE_TUPLE,
        "policy_digest": policy["digest"],
        "policy_bytes": canonical_validated_bytes(
            "effective-execution-policy/v1", policy
        ),
        "idempotency_key": accept_request["idempotency_key"],
        "request_digest": canonical_document_sha256(task_request),
        "event_request_digest": accept_request["accept_request_digest"],
        "request_bytes": canonical_validated_bytes("task-request/v1", task_request),
        "accept_request_digest": accept_request["accept_request_digest"],
        "accept_request_bytes": canonical_validated_bytes(
            "agent-task-accept-request/v1", accept_request
        ),
        "requirement_ledger_digest": ledger["ledger_digest"],
        "requirement_ledger_version": ledger["ledger_version"],
        "requirement_ledger_bytes": canonical_validated_bytes(
            "requirement-ledger/v1", ledger
        ),
        "outer_job_id": accept_request["outer_job_id"],
        "run_id": accept_request["run_id"],
        "owner_id": owner_id,
        "accepted_at": accepted_at,
        **deadline,
        "accept_response_digest": response["response_digest"],
        "response_bytes": canonical_validated_bytes(
            "agent-task-accept-response/v1", response
        ),
    }


def _stored_document(
    head: Mapping[str, object], field: str, schema_id: str
) -> tuple[dict[str, object], bytes]:
    raw = head[field]
    if not isinstance(raw, bytes):
        raise RuntimeBootstrapError("AUTHORITY_STORAGE_CORRUPT")
    try:
        document = verify_document_digest(schema_id, json.loads(raw))
        if canonical_validated_bytes(schema_id, document) != raw:
            raise ValueError
        return document, raw
    except (ContractValidationError, TypeError, UnicodeError, ValueError):
        raise RuntimeBootstrapError("AUTHORITY_STORAGE_CORRUPT") from None


def _content_ref(
    schema_id: str, document: dict[str, object]
) -> tuple[dict[str, object], bytes]:
    raw = canonical_validated_bytes(schema_id, document)
    sha256 = hashlib.sha256(raw).hexdigest()
    artifact_seed = hashlib.sha256(
        f"{schema_id}\0{sha256}".encode("utf-8")
    ).hexdigest()
    return (
        {
            "schema_id": "content-ref/v1",
            "artifact_id": f"art_{artifact_seed[:32]}",
            "content_schema_id": schema_id,
            "sha256": sha256,
            "size_bytes": len(raw),
            "media_type": "application/json",
            "encoding": "utf-8",
        },
        raw,
    )


def build_runtime_bootstrap(
    head: Mapping[str, object], claim_request: dict[str, object], *, claimed_at: str
) -> dict[str, object]:
    accept_request, accept_bytes = _stored_document(
        head, "accept_request_bytes", "agent-task-accept-request/v1"
    )
    accept_response, response_bytes = _stored_document(
        head, "accept_response_bytes", "agent-task-accept-response/v1"
    )
    task_request = accept_request["task_request"]
    policy = accept_request["effective_policy"]
    ledger = accept_request["requirement_ledger"]
    request_ref, request_bytes = _content_ref("task-request/v1", task_request)
    policy_ref, policy_bytes = _content_ref(
        "effective-execution-policy/v1", policy
    )
    ledger_bytes = canonical_validated_bytes("requirement-ledger/v1", ledger)
    stored_exact = (
        accept_request["accept_request_digest"] == head["accept_request_digest"]
        and accept_request["outer_job_id"] == head["outer_job_id"]
        and accept_request["run_id"] == head["run_id"]
        and ledger["ledger_digest"] == head["requirement_ledger_digest"]
        and ledger["ledger_version"] == head["requirement_ledger_version"]
        and request_bytes == head["request_bytes"]
        and policy_bytes == head["policy_bytes"]
        and ledger_bytes == head["requirement_ledger_bytes"]
        and response_bytes == head["accept_response_bytes"]
        and accept_response["response_digest"] == head["accept_response_digest"]
    )
    if not stored_exact:
        raise RuntimeBootstrapError("AUTHORITY_STORAGE_CORRUPT")
    policy_fields = effective_policy_grant_fields(policy)
    expected_deadline = effective_policy_deadline_fields(policy, head["accepted_at"])
    if any(head[field] != value for field, value in expected_deadline.items()):
        raise RuntimeBootstrapError("AUTHORITY_STORAGE_CORRUPT")
    if any(claim_request[field] != value for field, value in policy_fields.items()):
        raise RuntimeBootstrapError("AGENT_GRANT_INVALID")

    task_version = head["task_version"] + 1
    attempt_id = f"attempt_{secrets.token_hex(16)}"
    session_id = f"sess_{secrets.token_hex(16)}"
    grant_id = f"grant_{secrets.token_hex(16)}"
    owner_id = head["owner_id"]
    owner_epoch = head["owner_epoch"] + 1
    native_epoch = head["native_epoch"] + 1
    transport_epoch = claim_request["transport_epoch"]
    package = package_tuple()
    common = {
        "package": package,
        "task_id": claim_request["task_id"],
        "attempt_id": attempt_id,
        "session_id": session_id,
        "owner_id": owner_id,
        "lease_id": claim_request["lease_id"],
        "task_version": task_version,
        "deletion_version": head["deletion_version"],
        "owner_epoch": owner_epoch,
        "native_epoch": native_epoch,
        "transport_epoch": transport_epoch,
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
    authority = seal_document(
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
    base_task = {
        "schema_id": "task-record/v1",
        "task_id": task_request["task_id"],
        "task_type": task_request["task_type"],
        "request_ref": request_ref,
        "request_digest": request_ref["sha256"],
        "policy_ref": policy_ref,
        "policy_digest": policy["digest"],
        "policy_version": policy["policy_version"],
        "protocol_mode": PROTOCOL_MODE,
        "desired_state": "RUN",
        "deletion_version": head["deletion_version"],
        "owner_id": owner_id,
        "ledger_version": ledger["ledger_version"],
        "ledger_head_digest": ledger["ledger_digest"],
        "charter_version": 0,
        "charter_ref": None,
        "current_checkpoint_generation": 0,
        "current_checkpoint_hash": None,
        "quality_risk": policy["quality_risk_floor"],
        **deadline_wire,
        "completion_proposal_ref": None,
        "final_observation_manifest_ref": None,
        "terminal_kind": None,
        "result_ref": None,
        "result_digest": None,
        "outcome": None,
        "created_at": accept_response["accepted_at"],
        "terminal_at": None,
    }
    previous_task = {
        **base_task,
        "lifecycle": "QUEUED",
        "task_version": accept_response["task_version"],
        "outer_job_id": None,
        "run_id": None,
        "lease_id": None,
        "transport_epoch": None,
        "native_epoch": 0,
        "current_attempt_id": None,
        "owner_epoch": 0,
        "updated_at": accept_response["accepted_at"],
    }
    task_record = {
        **base_task,
        "lifecycle": "ACTIVE",
        "task_version": task_version,
        "outer_job_id": accept_request["outer_job_id"],
        "run_id": accept_request["run_id"],
        "lease_id": claim_request["lease_id"],
        "transport_epoch": transport_epoch,
        "native_epoch": native_epoch,
        "current_attempt_id": attempt_id,
        "owner_epoch": owner_epoch,
        "updated_at": claimed_at,
    }
    binding = {
        "outer_job_id": accept_request["outer_job_id"],
        "run_id": accept_request["run_id"],
        "lease_id": claim_request["lease_id"],
        "transport_epoch": transport_epoch,
    }
    attempt = {
        "schema_id": "attempt-record/v1",
        "attempt_id": attempt_id,
        "task_id": task_request["task_id"],
        "native_epoch": native_epoch,
        "transport_binding": {**binding, "protocol_mode": PROTOCOL_MODE},
        "state": "LEASED",
        "state_version": 1,
        "predecessor_checkpoint_generation": None,
        "owner_session_id": session_id,
        "lease_acquired_at": claimed_at,
        "started_at": None,
        "ended_at": None,
        "termination_reason": None,
        "budget_reservation_id": f"reserve_{secrets.token_hex(16)}",
    }
    owner = {
        "schema_id": "task-owner/v1",
        "task_id": task_request["task_id"],
        "owner_id": owner_id,
        "owner_epoch": owner_epoch,
        "session_id": session_id,
        "attempt_id": attempt_id,
        "native_epoch": native_epoch,
        "state": "STARTING",
        "state_version": 1,
        "started_at": claimed_at,
        "ended_at": None,
        "termination_reason": None,
    }
    validate_claim_write_set(previous_task, task_record, attempt, owner)
    bootstrap = seal_document(
        "agent-task-runtime-bootstrap/v1",
        {
            "schema_id": "agent-task-runtime-bootstrap/v1",
            "package": package,
            "accept_request": accept_request,
            "accept_response": accept_response,
            "authority": authority,
            "transport_binding": binding,
            "construction_roots": {
                "task_record": task_record,
                "attempt": attempt,
                "owner": owner,
            },
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
        "authority_digest": authority["authority_digest"],
        "authority_bytes": canonical_validated_bytes(
            "server-authority-envelope/v1", authority
        ),
        "bootstrap_digest": bootstrap["bootstrap_digest"],
        "bootstrap_bytes": canonical_validated_bytes(
            "agent-task-runtime-bootstrap/v1", bootstrap
        ),
        "task_record_bytes": canonical_validated_bytes("task-record/v1", task_record),
        "attempt_record_bytes": canonical_validated_bytes("attempt-record/v1", attempt),
        "owner_record_bytes": canonical_validated_bytes("task-owner/v1", owner),
        "response_bytes": canonical_validated_bytes(
            "agent-task-runtime-bootstrap/v1", bootstrap
        ),
        "accept_request_bytes": accept_bytes,
    }


__all__ = [
    "RuntimeBootstrapError",
    "build_acceptance_values",
    "build_runtime_bootstrap",
    "effective_policy_deadline_fields",
    "effective_policy_grant_fields",
]
