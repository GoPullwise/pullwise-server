"""Python facade document-rule handlers for control and tool documents."""

from __future__ import annotations


PYTHON_CONTROL = r'''
def _utf8_fields(value: dict[str, object]) -> None:
    limits = {
        "objective": 16384,
        "statement": 16384,
        "rationale": 16384,
        "objective_restated": 16384,
        "reason": 16384,
    }
    for field, limit in limits.items():
        if field in value:
            _require(len(value[field].encode("utf-8")) <= limit, "UTF8_BYTE_LIMIT_INVALID", f"$.{field}")


def _rule_task_request(value: dict[str, object]) -> None:
    _utf8_fields(value)
    for field in ("acceptance_criteria", "constraints"):
        items = value[field]
        _require(_ordered_unique(items, lambda item: item["source_id"]), "TASK_REQUEST_SOURCE_ID_ORDER_INVALID", f"$.{field}")
    _require(_sorted_unique(value["requested_capabilities"]), "TASK_REQUEST_CAPABILITY_ORDER_INVALID")
    _require(_sorted_unique(value["delivery"]["required_outputs"]), "TASK_REQUEST_DELIVERY_ORDER_INVALID")


def _rule_effective_policy(value: dict[str, object]) -> None:
    granted = value["granted_capabilities"]
    denied = value["denied_capabilities"]
    denied_ids = [item["id"] for item in denied]
    _require(_sorted_unique(granted), "POLICY_CAPABILITY_ORDER_INVALID")
    _require(_ordered_unique(denied, lambda item: item["id"]), "POLICY_CAPABILITY_ORDER_INVALID")
    _require(not set(granted).intersection(denied_ids), "POLICY_CAPABILITY_OVERLAP")
    _require(_sorted_unique(value["allowed_read_roots"]), "POLICY_ROOT_ORDER_INVALID")
    _require(_sorted_unique(value["allowed_write_roots"]), "POLICY_ROOT_ORDER_INVALID")
    origins = value["agent_tool_network"]["origins"]
    _require(_sorted_unique(origins), "POLICY_ORIGIN_ORDER_INVALID")
    _require(value["capability_risk_ceiling"] in {"R0", "R1"}, "POLICY_RISK_CEILING_INVALID")
    _require(value["source_write_mode"] == "read_only", "POLICY_SOURCE_WRITE_INVALID")
    _require(value["agent_tool_network"] == {"mode": "deny", "origins": []}, "POLICY_NETWORK_INVALID")
    _require(value["dependency_install"] == "deny", "POLICY_DEPENDENCY_INSTALL_INVALID")
    _require(value["interaction_mode"] == "unavailable", "POLICY_INTERACTION_INVALID")
    _require(value["authorized_waiver_issuers"] == [], "POLICY_WAIVER_ISSUER_INVALID")
    budgets = value["budgets"]
    _require(value["terminalization_reserve_ms"] <= budgets["wall_ms"], "POLICY_RESERVE_INVALID")
    _require(value["max_agent_sessions_total"] <= budgets["agent_sessions"], "POLICY_SESSION_CEILING_INVALID")
    _require(value["max_attempts"] <= budgets["attempts"], "POLICY_ATTEMPT_CEILING_INVALID")


_REQUIREMENT_KIND_RANK = {
    "user_objective": 0,
    "user_acceptance": 1,
    "user_constraint": 2,
    "delivery": 3,
    "policy": 4,
    "interaction": 5,
    "derived": 5,
}


def _requirement_ingest_key(value: dict[str, object]) -> tuple[object, ...]:
    rank = _REQUIREMENT_KIND_RANK[value["source_kind"]]
    return (
        rank,
        value["ledger_version"] if rank >= 5 else 0,
        value["source_id"],
        value["requirement_id"],
    )


def _rule_requirement_entry(value: dict[str, object]) -> None:
    _utf8_fields(value)
    expected = "req_" + value["source_kind"] + "_"
    _require(value["requirement_id"].startswith(expected), "REQUIREMENT_ID_KIND_INVALID")
    _require(_sorted_unique(value["parent_requirement_ids"]), "REQUIREMENT_PARENT_ORDER_INVALID")
    _require(_sorted_unique(value["supersedes"]), "REQUIREMENT_SUPERSEDES_ORDER_INVALID")
    if value["source_kind"] == "derived" and value["mandatory"]:
        _require(bool(value["rationale"]), "DERIVED_REQUIREMENT_RATIONALE_REQUIRED")


def _requirement_graph_acyclic(entries: list[dict[str, object]]) -> None:
    graph = {item["requirement_id"]: item["parent_requirement_ids"] for item in entries}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(requirement_id: str) -> None:
        if requirement_id in visiting:
            _fail("REQUIREMENT_CYCLE_INVALID")
        if requirement_id in visited:
            return
        visiting.add(requirement_id)
        for parent in graph[requirement_id]:
            if parent in graph:
                visit(parent)
        visiting.remove(requirement_id)
        visited.add(requirement_id)

    for requirement_id in graph:
        visit(requirement_id)


def _rule_requirement_ledger(value: dict[str, object]) -> None:
    entries = value["entries"]
    for item in entries:
        _rule_requirement_entry(item)
        _require(item["ledger_version"] <= value["ledger_version"], "REQUIREMENT_LEDGER_VERSION_INVALID")
    _require(entries == sorted(entries, key=_requirement_ingest_key), "REQUIREMENT_INGEST_ORDER_INVALID")
    _requirement_graph_acyclic(entries)
    superseded = {identity for item in entries for identity in item["supersedes"]}
    expected = sorted(
        item["requirement_id"]
        for item in entries
        if item["requirement_id"] not in superseded
    )
    _require(value["active_requirement_ids"] == expected, "REQUIREMENT_ACTIVE_SET_INVALID")


def _rule_task_charter(value: dict[str, object]) -> None:
    _utf8_fields(value)
    for field in (
        "scope_in", "scope_out", "assumptions", "requirement_ids",
        "unresolved_questions",
    ):
        _require(_sorted_unique(value[field]), "CHARTER_SET_ORDER_INVALID", f"$.{field}")
    _require(_sorted_unique(value["delivery_plan"]["required_outputs"]), "CHARTER_DELIVERY_ORDER_INVALID")
    _require(
        (value["charter_version"] == 1 and value["previous_charter_ref"] is None)
        or (value["charter_version"] > 1 and value["previous_charter_ref"] is not None),
        "CHARTER_PREDECESSOR_INVALID",
    )


def _rule_waiver(value: dict[str, object]) -> None:
    _utf8_fields(value)
    _require(value["issued_at"] < value["expires_at"], "WAIVER_TIME_RANGE_INVALID", code="WAIVER_INVALID")


def _transport_binding_complete(value: dict[str, object]) -> bool:
    fields = ("outer_job_id", "run_id", "lease_id", "transport_epoch")
    values = [value[key] for key in fields]
    return all(item is None for item in values) or all(item is not None for item in values)


def _rule_attempt_record(value: dict[str, object]) -> None:
    binding = value["transport_binding"]
    _require(_transport_binding_complete(binding), "ATTEMPT_TRANSPORT_BINDING_INVALID")
    terminal = value["state"] in {"SUCCEEDED", "SUSPENDED", "FAILED", "CANCELLED", "FENCED"}
    _require(
        (value["ended_at"] is not None and value["termination_reason"] is not None) == terminal,
        "ATTEMPT_STATE_NULLABILITY_INVALID",
    )
    if value["state"] == "FENCED":
        _require(value["termination_reason"] == "OWNERSHIP_LOST", "ATTEMPT_FENCE_REASON_INVALID")


def _rule_task_owner(value: dict[str, object]) -> None:
    terminal = value["state"] in {"CLOSED", "FENCED"}
    _require(
        (value["ended_at"] is not None and value["termination_reason"] is not None) == terminal,
        "OWNER_STATE_NULLABILITY_INVALID",
    )
    if value["state"] == "FENCED":
        _require(value["termination_reason"] == "OWNERSHIP_LOST", "OWNER_FENCE_REASON_INVALID")


def _rule_task_record(value: dict[str, object]) -> None:
    values = [value[key] for key in ("outer_job_id", "run_id", "lease_id", "transport_epoch")]
    _require(
        all(item is None for item in values) or all(item is not None for item in values),
        "TASK_RECORD_TRANSPORT_BINDING_INVALID",
    )
    terminal = value["lifecycle"] == "TERMINAL"
    terminal_fields = ("terminal_kind", "result_ref", "result_digest", "outcome", "terminal_at")
    _require(
        all(value[key] is not None for key in terminal_fields) == terminal,
        "TASK_RECORD_TERMINAL_RESULT_INVALID",
    )
    _require((value["charter_ref"] is None) == (value["charter_version"] == 0), "TASK_RECORD_CHARTER_HEAD_INVALID")
    checkpoint_null = value["current_checkpoint_generation"] is None
    _require(checkpoint_null == (value["current_checkpoint_hash"] is None), "TASK_RECORD_CHECKPOINT_HEAD_INVALID")


def _rule_tool_catalog(value: dict[str, object]) -> None:
    tools = value["tools"]
    _require(_ordered_unique(tools, lambda item: item["tool_key"]), "TOOL_CATALOG_ORDER_INVALID")
    for item in tools:
        _require(item["risk"] == "R0", "TOOL_CATALOG_RISK_INVALID")
        _require(not any(item[key] for key in ("uses_command", "uses_network", "uses_secret", "requests_approval")), "TOOL_CATALOG_EFFECT_INVALID")
        _require(item["request_schema_id"] == "agent-tool-request/v1", "TOOL_CATALOG_REQUEST_SCHEMA_INVALID")


def _rule_agent_tool_request(value: dict[str, object]) -> None:
    _require(set(value["tool_input"]) == {"relative_path"}, "TOOL_INPUT_SHAPE_INVALID")


def _rule_tool_invocation(value: dict[str, object]) -> None:
    _rule_agent_tool_request({
        "tool_input": value["tool_input"],
    })


def _rule_tool_intent(value: dict[str, object]) -> None:
    _require(value["state"] == "INTENT", "TOOL_INTENT_STATE_INVALID")
    _require(set(value["tool_input"]) == {"relative_path"}, "TOOL_INPUT_SHAPE_INVALID")


def _rule_tool_capability(value: dict[str, object]) -> None:
    _require(value["capability_kind"] == "opaque_dispatch", "TOOL_CAPABILITY_KIND_INVALID")
    _require(value["max_uses"] == 1, "TOOL_CAPABILITY_USE_LIMIT_INVALID")


def _rule_local_receipt(value: dict[str, object]) -> None:
    _require(value["receipt_kind"] == "local_tool", "TOOL_RECEIPT_KIND_INVALID")
    _require(value["status"] == "succeeded", "TOOL_RECEIPT_STATUS_INVALID")
    started, completed = _timestamp_millis(value["started_at"]), _timestamp_millis(value["completed_at"])
    _require(
        started is not None and completed is not None and completed >= started
        and value["elapsed_ms"] == completed - started,
        "TOOL_RECEIPT_TIMING_INVALID",
    )


def _normalized_relative_path(value: str) -> bool:
    return (
        value != ""
        and "\\" not in value
        and not value.startswith("/")
        and all(item not in {"", ".", ".."} for item in value.split("/"))
    )


def _rule_r0_payload(value: dict[str, object]) -> None:
    _require(_normalized_relative_path(value["relative_path"]), "R0_PATH_INVALID")


def _rule_r0_result(value: dict[str, object]) -> None:
    _require(
        value["source_state_before_id"] == value["source_state_after_id"],
        "R0_SOURCE_STATE_CHANGED",
        code="SOURCE_STATE_CHANGED",
    )


def _rule_source_state(value: dict[str, object]) -> None:
    identity = {
        "task_id": value["task_id"],
        "attempt_id": value["attempt_id"],
        "native_epoch": value["native_epoch"],
        "repository_root_id": value["repository_root_id"],
        "entry_count": value["entry_count"],
        "manifest_sha256": value["manifest_sha256"],
    }
    _require(value["source_state_id"] == canonical_document_sha256(identity), "SOURCE_STATE_ID_INVALID")
'''


__all__ = ["PYTHON_CONTROL"]
