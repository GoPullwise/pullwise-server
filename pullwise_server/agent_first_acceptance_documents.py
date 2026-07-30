"""Canonical Task acceptance and policy projections."""

from __future__ import annotations

import datetime as dt
import math
from typing import Mapping

from ._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    canonical_document_sha256,
    canonical_validated_bytes,
    package_tuple,
    seal_document,
    tool_catalog,
)


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


__all__ = [
    "build_acceptance_values",
    "effective_policy_deadline_fields",
    "effective_policy_grant_fields",
]
