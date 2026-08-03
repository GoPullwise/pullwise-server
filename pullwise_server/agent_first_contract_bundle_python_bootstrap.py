"""Python facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


PYTHON_BOOTSTRAP = r'''
def _verify_bootstrap_digest(
    schema_id: str, value: object
) -> dict[str, object]:
    validated = validate_document(schema_id, value)
    _verify_embedded_digest(schema_id, validated)
    return validated


def _rule_agent_task_accept_request(value: dict[str, object]) -> None:
    request = value["task_request"]
    validate_effective_policy_derivation(request, value["effective_policy"])
    ledger = _verify_bootstrap_digest(
        "requirement-ledger/v1", value["requirement_ledger"]
    )
    _require(
        ledger["task_id"] == request["task_id"],
        "ACCEPT_REQUEST_TASK_BINDING_MISMATCH",
        "$.requirement_ledger.task_id",
    )


def _rule_agent_task_runtime_bootstrap(value: dict[str, object]) -> None:
    accept_request = _verify_bootstrap_digest(
        "agent-task-accept-request/v1", value["accept_request"]
    )
    accept_response = _verify_bootstrap_digest(
        "agent-task-accept-response/v1", value["accept_response"]
    )
    authority = _verify_bootstrap_digest(
        "server-authority-envelope/v1", value["authority"]
    )
    roots = value["construction_roots"]
    task = roots["task_record"]
    attempt = roots["attempt"]
    owner = roots["owner"]
    packages = (
        value["package"],
        accept_request["package"],
        accept_response["package"],
        authority["package"],
        authority["grant"]["package"],
    )
    _require(
        all(_json_equal(packages[0], item) for item in packages[1:]),
        "BOOTSTRAP_PACKAGE_MISMATCH",
        "$.package",
    )
    same_generation = (
        authority["owner_epoch"] == task["owner_epoch"] == owner["owner_epoch"]
        and authority["native_epoch"]
        == task["native_epoch"]
        == attempt["native_epoch"]
        == owner["native_epoch"]
    )
    _require(
        same_generation,
        "BOOTSTRAP_GENERATION_MISMATCH",
        "$.construction_roots",
    )
    request = accept_request["task_request"]
    policy = accept_request["effective_policy"]
    ledger = accept_request["requirement_ledger"]
    _require(
        request["task_id"]
        == ledger["task_id"]
        == accept_response["task_id"]
        == authority["task_id"]
        == task["task_id"]
        == attempt["task_id"]
        == owner["task_id"]
        and request["task_type"] == task["task_type"],
        "BOOTSTRAP_TASK_BINDING_MISMATCH",
        "$.construction_roots",
    )
    _require(
        task["task_version"]
        == authority["task_version"]
        == accept_response["task_version"] + 1
        and task["deletion_version"]
        == authority["deletion_version"]
        == accept_response["deletion_version"],
        "BOOTSTRAP_TASK_VERSION_MISMATCH",
        "$.construction_roots.task_record.task_version",
    )
    binding = value["transport_binding"]
    attempt_binding = attempt["transport_binding"]
    transport_fields = (
        "outer_job_id",
        "run_id",
        "lease_id",
        "transport_attempt_id",
        "transport_epoch",
    )
    transport_matches = (
        accept_request["outer_job_id"] == binding["outer_job_id"]
        and accept_request["run_id"] == binding["run_id"]
        and all(task[field] == binding[field] for field in transport_fields)
        and all(
            attempt_binding[field] == binding[field]
            for field in transport_fields
        )
        and attempt_binding["protocol_mode"] == task["protocol_mode"]
        and authority["lease_id"] == binding["lease_id"]
        and authority["transport_epoch"] == binding["transport_epoch"]
    )
    _require(
        transport_matches,
        "BOOTSTRAP_TRANSPORT_BINDING_MISMATCH",
        "$.transport_binding",
    )
    authority_matches = (
        task["current_attempt_id"]
        == authority["attempt_id"]
        == attempt["attempt_id"]
        == owner["attempt_id"]
        and task["owner_id"] == authority["owner_id"] == owner["owner_id"]
        and authority["session_id"]
        == attempt["owner_session_id"]
        == owner["session_id"]
        and task["lifecycle"] == authority["lifecycle"] == "ACTIVE"
        and task["desired_state"] == authority["desired_state"] == "RUN"
        and attempt["state"] == "LEASED"
        and owner["state"] == "STARTING"
    )
    _require(
        authority_matches,
        "BOOTSTRAP_AUTHORITY_BINDING_MISMATCH",
        "$.construction_roots",
    )
    request_bytes = canonical_document_bytes(request)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    _require(
        task["request_ref"]["sha256"]
        == task["request_digest"]
        == request_sha256
        and task["request_ref"]["size_bytes"] == len(request_bytes),
        "BOOTSTRAP_REQUEST_REF_MISMATCH",
        "$.construction_roots.task_record.request_ref",
    )
    policy_bytes = canonical_document_bytes(policy)
    _require(
        task["policy_ref"]["sha256"]
        == hashlib.sha256(policy_bytes).hexdigest()
        and task["policy_ref"]["size_bytes"] == len(policy_bytes)
        and task["policy_digest"] == policy["digest"]
        and task["policy_version"] == policy["policy_version"],
        "BOOTSTRAP_POLICY_REF_MISMATCH",
        "$.construction_roots.task_record.policy_ref",
    )
    _require(
        task["ledger_version"] == ledger["ledger_version"]
        and task["ledger_head_digest"] == ledger["ledger_digest"],
        "BOOTSTRAP_LEDGER_BINDING_MISMATCH",
        "$.construction_roots.task_record.ledger_head_digest",
    )
    _require(
        task["absolute_deadline_at"] == authority["absolute_deadline_at"]
        and task["terminalization_reserve_ms"]
        == authority["terminalization_reserve_ms"]
        == policy["terminalization_reserve_ms"],
        "BOOTSTRAP_DEADLINE_BINDING_MISMATCH",
        "$.construction_roots.task_record.absolute_deadline_at",
    )
    accepted_millis = _timestamp_millis(accept_response["accepted_at"])
    deadline_millis = _timestamp_millis(task["absolute_deadline_at"])
    _require(
        accepted_millis is not None
        and deadline_millis
        == accepted_millis + policy["budgets"]["wall_ms"],
        "BOOTSTRAP_DEADLINE_DERIVATION_MISMATCH",
        "$.construction_roots.task_record.absolute_deadline_at",
    )
    grant = authority["grant"]
    _require(
        set(grant["capability_ids"]).issubset(policy["granted_capabilities"])
        and grant["elapsed_limit_ms"] <= policy["budgets"]["wall_ms"]
        and grant["tool_call_limit"] <= policy["budgets"]["tool_calls"],
        "BOOTSTRAP_GRANT_POLICY_MISMATCH",
        "$.authority.grant",
    )
    _require(
        accept_response["accepted_at"] == task["created_at"]
        and task["current_checkpoint_generation"] == 0
        and task["current_checkpoint_hash"] is None
        and attempt["predecessor_checkpoint_generation"] is None,
        "BOOTSTRAP_CONSTRUCTION_ROOT_INVALID",
        "$.construction_roots",
    )
'''


__all__ = ["PYTHON_BOOTSTRAP"]
