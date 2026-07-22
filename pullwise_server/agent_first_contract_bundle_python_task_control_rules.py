"""Python facade rules for task-control and requirement documents."""

from __future__ import annotations


PYTHON_TASK_CONTROL_RULES = r'''
_TASK_CONTROL_REQUIREMENT_KIND_RANK = {
    "user_objective": 0,
    "user_acceptance": 1,
    "user_constraint": 2,
    "delivery": 3,
    "policy": 4,
    "interaction": 5,
    "derived": 5,
}


def _task_control_utf8_walk(
    rule: dict[str, object], value: object, path: str
) -> None:
    if "$ref" in rule:
        _task_control_utf8_walk(schema(rule["$ref"]), value, path)
        return
    if "oneOf" in rule:
        for option in rule["oneOf"]:
            try:
                _validate_node(option, value, path)
            except ContractValidationError:
                continue
            _task_control_utf8_walk(option, value, path)
            break
    if isinstance(value, str):
        _require(
            unicodedata.normalize("NFC", value) == value,
            "UTF8_NFC_INVALID",
            path,
        )
        limit = rule.get("maxLength")
        if isinstance(limit, int):
            _require(
                len(value.encode("utf-8")) <= limit,
                "UTF8_BYTE_LIMIT_INVALID",
                path,
            )
    elif isinstance(value, dict):
        properties = rule.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _task_control_utf8_walk(
                    properties[key], item, f"{path}.{key}"
                )
    elif isinstance(value, list) and "items" in rule:
        for index, item in enumerate(value):
            _task_control_utf8_walk(
                rule["items"], item, f"{path}[{index}]"
            )


def _task_control_rule_utf8(value: dict[str, object]) -> None:
    _task_control_utf8_walk(schema(value["schema_id"]), value, "$")


def _task_control_rule_request_acceptance_sources(
    value: dict[str, object]
) -> None:
    items = value["acceptance_criteria"] + value["constraints"]
    source_ids = [item["source_id"] for item in items]
    _require(
        len(source_ids) == len(set(source_ids)),
        "TASK_REQUEST_SOURCE_ID_INVALID",
    )


def _task_control_rule_request_sets(value: dict[str, object]) -> None:
    _require(
        _sorted_unique(value["requested_capabilities"]),
        "TASK_REQUEST_CAPABILITY_ORDER_INVALID",
    )
    _require(
        _sorted_unique(value["delivery"]["required_outputs"]),
        "TASK_REQUEST_DELIVERY_ORDER_INVALID",
    )


def _task_control_rule_policy_capabilities(value: dict[str, object]) -> None:
    granted = value["granted_capabilities"]
    denied_ids = [item["id"] for item in value["denied_capabilities"]]
    _require(
        _sorted_unique(granted) and _sorted_unique(denied_ids),
        "POLICY_CAPABILITY_ORDER_INVALID",
    )
    _require(
        not set(granted).intersection(denied_ids),
        "POLICY_CAPABILITY_OVERLAP",
    )


def _task_control_rule_policy_roots(value: dict[str, object]) -> None:
    for field in ("allowed_read_roots", "allowed_write_roots"):
        _require(
            _sorted_unique(value[field]),
            "POLICY_ROOT_ORDER_INVALID",
        )
    _require(
        _sorted_unique(value["agent_tool_network"]["origins"]),
        "POLICY_ORIGIN_ORDER_INVALID",
    )


def _task_control_rule_policy_mvp(value: dict[str, object]) -> None:
    _require(
        value["capability_risk_ceiling"] in {"R0", "R1"},
        "POLICY_RISK_CEILING_INVALID",
    )
    _require(
        value["quality_risk_floor"] == "Q1",
        "POLICY_QUALITY_RISK_FLOOR_INVALID",
    )
    _require(
        value["source_write_mode"] == "read_only",
        "POLICY_SOURCE_WRITE_INVALID",
    )
    _require(
        value["agent_tool_network"] == {"mode": "deny", "origins": []},
        "POLICY_NETWORK_INVALID",
    )
    _require(
        value["dependency_install"] == "deny",
        "POLICY_DEPENDENCY_INSTALL_INVALID",
    )
    _require(
        value["interaction_mode"] == "unavailable",
        "POLICY_INTERACTION_INVALID",
    )
    _require(
        value["authorized_waiver_issuers"] == [],
        "POLICY_WAIVER_ISSUER_INVALID",
    )


def _task_control_rule_policy_budgets(value: dict[str, object]) -> None:
    budgets = value["budgets"]
    _require(
        value["terminalization_reserve_ms"] <= budgets["wall_ms"],
        "POLICY_RESERVE_INVALID",
    )
    _require(
        value["max_agent_sessions_total"] <= budgets["agent_sessions"],
        "POLICY_SESSION_CEILING_INVALID",
    )
    _require(
        value["max_attempts"] <= budgets["attempts"],
        "POLICY_ATTEMPT_CEILING_INVALID",
    )
    _require(
        value["max_agents"] <= value["max_agent_sessions_total"],
        "POLICY_AGENT_CEILING_INVALID",
    )


def _task_control_embedded_digest(
    value: dict[str, object], field: str, domain: str
) -> None:
    unsigned = {key: item for key, item in value.items() if key != field}
    expected = hashlib.sha256(
        domain.encode("utf-8")
        + b"\0"
        + canonical_document_bytes(unsigned)
    ).hexdigest()
    _require(value[field] == expected, "CONTRACT_DIGEST_MISMATCH", f"$.{field}")


def _task_control_rule_policy_digest(value: dict[str, object]) -> None:
    _task_control_embedded_digest(
        value, "digest", "pullwise:effective-execution-policy/v1"
    )


def _task_control_rule_requirement_shape(value: dict[str, object]) -> None:
    if value["source_kind"] == "derived" and value["mandatory"]:
        _require(
            bool(value["rationale"]),
            "DERIVED_REQUIREMENT_RATIONALE_REQUIRED",
            "$.rationale",
        )


def _task_control_rule_requirement_id(value: dict[str, object]) -> None:
    _require(
        value["requirement_id"].startswith(
            "req_" + value["source_kind"] + "_"
        ),
        "REQUIREMENT_ID_KIND_INVALID",
        "$.requirement_id",
    )


def _task_control_rule_requirement_links(value: dict[str, object]) -> None:
    for field in ("parent_requirement_ids", "supersedes"):
        _require(
            _sorted_unique(value[field]),
            "REQUIREMENT_LINK_ORDER_INVALID",
            f"$.{field}",
        )
    _require(
        value["requirement_id"] not in value["parent_requirement_ids"],
        "REQUIREMENT_SELF_LINK_INVALID",
        "$.parent_requirement_ids",
    )
    _require(
        value["requirement_id"] not in value["supersedes"],
        "REQUIREMENT_SELF_LINK_INVALID",
        "$.supersedes",
    )


def _task_control_requirement_key(
    value: dict[str, object]
) -> tuple[object, ...]:
    rank = _TASK_CONTROL_REQUIREMENT_KIND_RANK[value["source_kind"]]
    return (
        rank,
        value["ledger_version"] if rank >= 5 else 0,
        value["source_id"],
        value["requirement_id"],
    )


def _task_control_requirement_graph(
    entries: list[dict[str, object]]
) -> None:
    by_id = {item["requirement_id"]: item for item in entries}
    _require(
        len(by_id) == len(entries),
        "REQUIREMENT_ID_COLLISION",
        code="REQUIREMENT_ID_COLLISION",
    )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(requirement_id: str) -> None:
        if requirement_id in visiting:
            _fail("REQUIREMENT_CYCLE_INVALID")
        if requirement_id in visited:
            return
        visiting.add(requirement_id)
        for parent in by_id[requirement_id]["parent_requirement_ids"]:
            _require(parent in by_id, "REQUIREMENT_PARENT_UNKNOWN")
            visit(parent)
        visiting.remove(requirement_id)
        visited.add(requirement_id)

    for requirement_id in by_id:
        visit(requirement_id)
    for item in entries:
        for superseded in item["supersedes"]:
            target = by_id.get(superseded)
            _require(
                target is not None and target["source_kind"] == "derived",
                "REQUIREMENT_SUPERSEDES_INVALID",
            )


def _task_control_rule_ledger_entries(value: dict[str, object]) -> None:
    entries = value["entries"]
    for item in entries:
        validate_document("requirement-entry/v1", item)
        _require(
            item["ledger_version"] <= value["ledger_version"],
            "REQUIREMENT_LEDGER_VERSION_INVALID",
        )
    _require(
        entries == sorted(entries, key=_task_control_requirement_key),
        "REQUIREMENT_INGEST_ORDER_INVALID",
    )
    if value["ledger_version"] == 1:
        _require(
            all(item["ledger_version"] == 1 for item in entries),
            "REQUIREMENT_LEDGER_VERSION_INVALID",
        )
    _task_control_requirement_graph(entries)


def _task_control_rule_ledger_digest(value: dict[str, object]) -> None:
    _task_control_embedded_digest(
        value, "ledger_digest", "pullwise:requirement-ledger:v1"
    )


def _task_control_rule_ledger_active(value: dict[str, object]) -> None:
    superseded = {
        requirement_id
        for item in value["entries"]
        for requirement_id in item["supersedes"]
    }
    expected = sorted(
        item["requirement_id"]
        for item in value["entries"]
        if item["requirement_id"] not in superseded
    )
    _require(
        value["active_requirement_ids"] == expected,
        "REQUIREMENT_ACTIVE_SET_INVALID",
        "$.active_requirement_ids",
    )


def _task_control_rule_charter_digest(value: dict[str, object]) -> None:
    _task_control_embedded_digest(
        value, "digest", "pullwise:task-charter:v1"
    )


def _task_control_rule_charter_sets(value: dict[str, object]) -> None:
    for field in ("scope_in", "scope_out", "requirement_ids"):
        _require(
            _sorted_unique(value[field]),
            "CHARTER_SET_ORDER_INVALID",
            f"$.{field}",
        )
    _require(
        _sorted_unique(value["delivery_plan"]["required_outputs"]),
        "CHARTER_DELIVERY_ORDER_INVALID",
        "$.delivery_plan.required_outputs",
    )
    predecessor = value["previous_charter_ref"]
    _require(
        (value["charter_version"] == 1 and predecessor is None)
        or (value["charter_version"] > 1 and predecessor is not None),
        "CHARTER_PREDECESSOR_INVALID",
        "$.previous_charter_ref",
    )


def _task_control_rule_waiver_time(value: dict[str, object]) -> None:
    _require(
        value["issued_at"] < value["expires_at"],
        "WAIVER_TIME_RANGE_INVALID",
        code="WAIVER_INVALID",
    )


_TASK_CONTROL_ATTEMPT_TERMINAL = {
    "SUCCEEDED", "SUSPENDED", "FAILED", "CANCELLED", "FENCED"
}


def _task_control_rule_attempt_nullability(value: dict[str, object]) -> None:
    terminal = value["state"] in _TASK_CONTROL_ATTEMPT_TERMINAL
    _require(
        (value["ended_at"] is not None)
        == (value["termination_reason"] is not None)
        == terminal,
        "ATTEMPT_STATE_NULLABILITY_INVALID",
    )


def _task_control_rule_attempt_transport(value: dict[str, object]) -> None:
    binding = value["transport_binding"]
    fields = ("outer_job_id", "run_id", "lease_id", "transport_epoch")
    present = [binding[field] is not None for field in fields]
    _require(
        all(present) or not any(present),
        "ATTEMPT_TRANSPORT_BINDING_INVALID",
        "$.transport_binding",
    )


def _task_control_rule_fenced_reason(value: dict[str, object]) -> None:
    if value["state"] == "FENCED":
        _require(
            value["termination_reason"] == "OWNERSHIP_LOST",
            "FENCED_REASON_INVALID",
            "$.termination_reason",
        )


def _task_control_rule_owner_nullability(value: dict[str, object]) -> None:
    terminal = value["state"] in {"CLOSED", "FENCED"}
    _require(
        (value["ended_at"] is not None)
        == (value["termination_reason"] is not None)
        == terminal,
        "OWNER_STATE_NULLABILITY_INVALID",
    )


def _task_control_rule_record_heads(value: dict[str, object]) -> None:
    _require(
        (value["charter_version"] == 0) == (value["charter_ref"] is None),
        "TASK_RECORD_CHARTER_HEAD_INVALID",
        "$.charter_ref",
    )
    _require(
        (value["current_checkpoint_generation"] == 0)
        == (value["current_checkpoint_hash"] is None),
        "TASK_RECORD_CHECKPOINT_HEAD_INVALID",
        "$.current_checkpoint_hash",
    )


def _task_control_rule_record_transport(value: dict[str, object]) -> None:
    fields = ("outer_job_id", "run_id", "lease_id", "transport_epoch")
    present = [value[field] is not None for field in fields]
    _require(
        all(present) or not any(present),
        "TASK_RECORD_TRANSPORT_BINDING_INVALID",
    )


def _task_control_rule_record_terminal(value: dict[str, object]) -> None:
    terminal = value["lifecycle"] == "TERMINAL"
    fields = (
        "terminal_kind", "result_ref", "result_digest", "outcome",
        "terminal_at",
    )
    _require(
        all(value[field] is not None for field in fields) == terminal,
        "TASK_RECORD_TERMINAL_RESULT_INVALID",
    )
    if not terminal:
        _require(
            all(value[field] is None for field in fields),
            "TASK_RECORD_TERMINAL_RESULT_INVALID",
        )
'''


__all__ = ["PYTHON_TASK_CONTROL_RULES"]
