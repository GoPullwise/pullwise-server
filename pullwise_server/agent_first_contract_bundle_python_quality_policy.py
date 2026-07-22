"""Generated Python QualityPolicyPlan rules and contextual verification."""

from __future__ import annotations


PYTHON_QUALITY_POLICY = r'''
_QUALITY_POLICY_INPUT_FIELDS = (
    "proposal_digest",
    "policy_digest",
    "task_type",
    "requirement_ledger_digest",
    "change_set_classification_digest",
    "capability_usage_digest",
)

_QUALITY_POLICY_SLOT_TABLE = {
    "Q1": (
        ("slot_11111111111111111111111111111111", "contract_and_data"),
    ),
    "Q2": (
        ("slot_11111111111111111111111111111111", "contract_and_data"),
        (
            "slot_22222222222222222222222222222222",
            "security_and_concurrency",
        ),
    ),
    "Q3": (),
}

_QUALITY_RISK_RANK = {"Q0": 0, "Q1": 1, "Q2": 2, "Q3": 3}


def _rule_quality_policy_plan(value: dict[str, object]) -> None:
    input_projection = {
        field: value[field] for field in _QUALITY_POLICY_INPUT_FIELDS
    }
    _require(
        value["input_digest"] == canonical_document_sha256(input_projection),
        "QUALITY_POLICY_INPUT_DIGEST_INVALID",
        "$.input_digest",
    )
    expected_slots = _QUALITY_POLICY_SLOT_TABLE.get(value["quality_risk"])
    _require(
        expected_slots is not None
        and tuple(
            (item["slot_id"], item["concern"]) for item in value["slots"]
        )
        == expected_slots,
        "QUALITY_POLICY_SLOT_TABLE_INVALID",
        "$.slots",
    )
    _require(
        value["self_attestation_allowed"] is False,
        "QUALITY_POLICY_SELF_ATTESTATION_INVALID",
        "$.self_attestation_allowed",
    )
    for index, slot in enumerate(value["slots"]):
        requirement_ids = slot["requirement_ids"]
        _require(
            bool(requirement_ids) and _sorted_unique(requirement_ids),
            "QUALITY_POLICY_REQUIREMENT_ORDER_INVALID",
            f"$.slots[{index}].requirement_ids",
        )
    unsigned = {
        key: item for key, item in value.items() if key != "plan_digest"
    }
    digest_input = (
        b"pullwise:quality-policy-plan:v1\0"
        + canonical_document_bytes(unsigned)
    )
    _require(
        value["plan_digest"] == hashlib.sha256(digest_input).hexdigest(),
        "CONTRACT_DIGEST_MISMATCH",
        "$.plan_digest",
    )


def _quality_context_object(value: object, path: str) -> dict[str, object]:
    _require(isinstance(value, dict), "QUALITY_POLICY_CONTEXT_INVALID", path)
    return value


def _quality_context_field(
    value: dict[str, object], field: str, path: str
) -> object:
    _require(field in value, "QUALITY_POLICY_CONTEXT_INVALID", f"{path}.{field}")
    return value[field]


def verify_quality_policy_plan_context(
    plan: object,
    proposal: object,
    policy: object,
    task_request: object,
    requirement_ledger: object,
    change_set: object,
) -> dict[str, object]:
    checked = verify_document_digest("quality-policy-plan/v1", plan)
    proposal_value = _quality_context_object(proposal, "$.proposal")
    policy_value = _quality_context_object(policy, "$.policy")
    request_value = _quality_context_object(task_request, "$.task_request")
    ledger_value = _quality_context_object(
        requirement_ledger, "$.requirement_ledger"
    )
    change_value = _quality_context_object(change_set, "$.change_set")

    bindings = (
        ("task_id", proposal_value, "task_id", "$.proposal"),
        ("proposal_id", proposal_value, "proposal_id", "$.proposal"),
        (
            "proposal_digest",
            proposal_value,
            "proposal_digest",
            "$.proposal",
        ),
        ("policy_digest", proposal_value, "policy_digest", "$.proposal"),
        (
            "requirement_ledger_digest",
            proposal_value,
            "requirement_ledger_digest",
            "$.proposal",
        ),
        ("policy_digest", policy_value, "digest", "$.policy"),
        ("task_type", policy_value, "task_type", "$.policy"),
        ("task_id", request_value, "task_id", "$.task_request"),
        ("task_type", request_value, "task_type", "$.task_request"),
        ("task_id", ledger_value, "task_id", "$.requirement_ledger"),
        (
            "requirement_ledger_digest",
            ledger_value,
            "ledger_digest",
            "$.requirement_ledger",
        ),
        (
            "change_set_classification_digest",
            change_value,
            "change_set_classification_digest",
            "$.change_set",
        ),
        (
            "capability_usage_digest",
            change_value,
            "capability_usage_digest",
            "$.change_set",
        ),
    )
    for plan_field, context, context_field, path in bindings:
        _require(
            checked[plan_field]
            == _quality_context_field(context, context_field, path),
            "QUALITY_POLICY_CONTEXT_BINDING_INVALID",
            f"{path}.{context_field}",
        )

    floor = _quality_context_field(
        policy_value, "quality_risk_floor", "$.policy"
    )
    _require(
        isinstance(floor, str)
        and floor in _QUALITY_RISK_RANK
        and _QUALITY_RISK_RANK[checked["quality_risk"]]
        >= _QUALITY_RISK_RANK[floor],
        "QUALITY_POLICY_RISK_FLOOR_INVALID",
        "$.policy.quality_risk_floor",
    )

    active = _quality_context_field(
        ledger_value, "active_requirement_ids", "$.requirement_ledger"
    )
    entries = _quality_context_field(
        ledger_value, "entries", "$.requirement_ledger"
    )
    _require(
        isinstance(active, list)
        and bool(active)
        and all(isinstance(item, str) for item in active)
        and _sorted_unique(active),
        "QUALITY_POLICY_ACTIVE_REQUIREMENTS_INVALID",
        "$.requirement_ledger.active_requirement_ids",
    )
    _require(
        isinstance(entries, list),
        "QUALITY_POLICY_LEDGER_ENTRIES_INVALID",
        "$.requirement_ledger.entries",
    )
    entries_by_id: dict[str, bool] = {}
    for index, entry in enumerate(entries):
        path = f"$.requirement_ledger.entries[{index}]"
        _require(
            isinstance(entry, dict)
            and isinstance(entry.get("requirement_id"), str)
            and isinstance(entry.get("mandatory"), bool),
            "QUALITY_POLICY_LEDGER_ENTRIES_INVALID",
            path,
        )
        requirement_id = entry["requirement_id"]
        _require(
            requirement_id not in entries_by_id,
            "QUALITY_POLICY_LEDGER_ENTRIES_INVALID",
            f"{path}.requirement_id",
        )
        entries_by_id[requirement_id] = entry["mandatory"]
    active_set = set(active)
    _require(
        active_set.issubset(entries_by_id),
        "QUALITY_POLICY_ACTIVE_REQUIREMENTS_INVALID",
        "$.requirement_ledger.active_requirement_ids",
    )
    mandatory_active = {
        requirement_id
        for requirement_id in active
        if entries_by_id[requirement_id]
    }
    for index, slot in enumerate(checked["slots"]):
        covered = set(slot["requirement_ids"])
        _require(
            covered.issubset(active_set),
            "QUALITY_POLICY_SLOT_REQUIREMENT_INACTIVE",
            f"$.slots[{index}].requirement_ids",
        )
        _require(
            mandatory_active.issubset(covered),
            "QUALITY_POLICY_MANDATORY_COVERAGE_INVALID",
            f"$.slots[{index}].requirement_ids",
        )
    return checked
'''


__all__ = ["PYTHON_QUALITY_POLICY"]
