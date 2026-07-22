"""Python verification-family common utilities and document rules."""

from __future__ import annotations


PYTHON_VERIFICATION_RULES = r'''
_VERIFICATION_CONTEXT_INVALID = "VERIFICATION_CONTEXT_INVALID"
_VERIFICATION_CONTEXT_CAS_CORRUPT = "VERIFICATION_CONTEXT_CAS_CORRUPT"
_VERIFICATION_CONTEXT_DIGEST_INVALID = "VERIFICATION_CONTEXT_DIGEST_INVALID"
_VERIFICATION_CONTEXT_TIME_INVALID = "VERIFICATION_CONTEXT_TIME_INVALID"


def _verification_require(
    condition: bool,
    detail: str,
    path: str = "$",
) -> None:
    if not condition:
        _fail(detail, path)


def _verification_verify_embedded_digest(
    schema_id: str,
    value: dict[str, object],
) -> None:
    spec = schema(schema_id).get("x-pullwise-digest")
    _verification_require(
        isinstance(spec, dict),
        "CONTRACT_DIGEST_UNDECLARED",
        "$",
    )
    field = spec["field"]
    domain = spec["domain"]
    unsigned = {key: item for key, item in value.items() if key != field}
    raw = domain.encode("utf-8") + b"\0" + canonical_document_bytes(unsigned)
    _verification_require(
        value[field] == hashlib.sha256(raw).hexdigest(),
        "CONTRACT_DIGEST_MISMATCH",
        f"$.{field}",
    )


def _verification_digest_field(schema_id: str) -> str | None:
    spec = schema(schema_id).get("x-pullwise-digest")
    return spec["field"] if isinstance(spec, dict) else None


def _verification_check_document(
    schema_id: str,
    value: object,
) -> dict[str, object]:
    return (
        verify_document_digest(schema_id, value)
        if _verification_digest_field(schema_id) is not None
        else validate_document(schema_id, value)
    )


def _verification_check_documents(
    schema_id: str,
    values: object,
    path: str,
) -> list[dict[str, object]]:
    _verification_require(isinstance(values, list), _VERIFICATION_CONTEXT_INVALID, path)
    return [_verification_check_document(schema_id, item) for item in values]


def _verification_request_digest(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_document_bytes(document)).hexdigest()


def _verification_companion_digest(
    schema_id: str,
    document: dict[str, object],
) -> str:
    field = _verification_digest_field(schema_id)
    _verification_require(field is not None, _VERIFICATION_CONTEXT_DIGEST_INVALID, "$")
    return document[field]


def _verification_require_ref(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
    path: str,
) -> None:
    _verification_require(
        _seo_ref_matches_document(ref, schema_id, document),
        _VERIFICATION_CONTEXT_CAS_CORRUPT,
        path,
    )


def _verification_require_companion_digest(
    actual: object,
    schema_id: str,
    document: dict[str, object],
    path: str,
) -> None:
    _verification_require(
        actual == _verification_companion_digest(schema_id, document),
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        path,
    )


def _verification_require_time_order(
    values: list[object],
    path: str,
) -> None:
    epochs = [_timestamp_millis(item) for item in values if item is not None]
    _verification_require(
        len(epochs) == len([item for item in values if item is not None]),
        _VERIFICATION_CONTEXT_TIME_INVALID,
        path,
    )
    for index in range(1, len(epochs)):
        _verification_require(
            epochs[index - 1] <= epochs[index],
            _VERIFICATION_CONTEXT_TIME_INVALID,
            path,
        )


def _verification_change_binding(
    availability: object,
    change_set: dict[str, object] | None,
    path: str,
) -> dict[str, object]:
    checked = validate_document("availability-ref/v1", availability)
    if change_set is None:
        _verification_require(
            checked["availability"] == "not_applicable",
            _VERIFICATION_CONTEXT_INVALID,
            path,
        )
        return checked
    _verification_require(
        checked["availability"] == "available",
        _VERIFICATION_CONTEXT_INVALID,
        path,
    )
    _verification_require_ref(
        checked["ref"],
        "change-set/v1",
        change_set,
        f"{path}.ref",
    )
    return checked


def _verification_find_plan_slot(
    plan: dict[str, object],
    slot_id: str,
) -> dict[str, object] | None:
    return next((item for item in plan["slots"] if item["slot_id"] == slot_id), None)


def _verification_manifest_entries(
    manifest: dict[str, object],
) -> dict[str, dict[str, object]]:
    return {item["observation_id"]: item for item in manifest["entries"]}


def _verification_requirement_ids(
    values: list[dict[str, object]],
) -> list[str]:
    return [item["requirement_id"] for item in values]


def _verification_valid_assessments(
    values: list[dict[str, object]],
) -> bool:
    return _ordered_unique(values, lambda item: item["requirement_id"]) and all(
        _sorted_unique(item["evidence_ids"])
        and _sorted_unique(item["limitations"])
        and (item["verdict"] != "PASS" or not item["limitations"])
        for item in values
    )


def _verification_run_status(
    values: list[dict[str, object]],
) -> str:
    verdicts = {item["verdict"] for item in values}
    for verdict in ("POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE"):
        if verdict in verdicts:
            return verdict
    return "PASS"


def _verification_aggregate_result(
    requirement_id: str,
    required_slot_ids: list[str],
    attestation_by_slot: dict[str, dict[str, object]],
) -> tuple[list[str], str]:
    attestation_ids: list[str] = []
    verdicts: list[str] = []
    missing_slot = False
    for slot_id in required_slot_ids:
        document = attestation_by_slot.get(slot_id)
        if document is None:
            missing_slot = True
            continue
        attestation_ids.append(document["attestation_id"])
        verdict = next(
            (
                item["verdict"]
                for item in document["requirement_verdicts"]
                if item["requirement_id"] == requirement_id
            ),
            None,
        )
        _verification_require(
            verdict is not None,
            _VERIFICATION_CONTEXT_INVALID,
            "$.requirement_aggregates",
        )
        verdicts.append(verdict)
    if any(item in {"POLICY_VIOLATION", "NEEDS_WORK"} for item in verdicts):
        return attestation_ids, "FAIL"
    if missing_slot or "UNVERIFIABLE" in verdicts:
        return attestation_ids, "UNVERIFIABLE"
    return attestation_ids, "PASS"


def _rule_completion_proposal(value: dict[str, object]) -> None:
    _verification_verify_embedded_digest("completion-proposal/v1", value)
    _verification_require(
        _sorted_unique(value["execution_state_ids"]),
        "PROPOSAL_EXECUTION_STATE_ORDER_INVALID",
    )
    _verification_require(
        _ordered_unique(value["artifact_refs"], _artifact_ref_key),
        "PROPOSAL_ARTIFACT_ORDER_INVALID",
    )
    _verification_require(
        _ordered_unique(value["requirement_claims"], lambda item: item["requirement_id"]),
        "PROPOSAL_CLAIM_ORDER_INVALID",
    )
    for item in value["requirement_claims"]:
        _verification_require(
            _sorted_unique(item["evidence_ids"]),
            "PROPOSAL_EVIDENCE_ORDER_INVALID",
        )
    _verification_require(_sorted_unique(value["known_gaps"]), "PROPOSAL_GAP_ORDER_INVALID")
    _verification_require(
        _sorted_unique(value["residual_risks"]),
        "PROPOSAL_RISK_ORDER_INVALID",
    )
    if value["outcome_requested"] == "NO_CHANGE_NEEDED":
        _verification_require(value["change_set_ref"] is None, "PROPOSAL_NO_CHANGE_SET_INVALID")
        _verification_require(
            value["original_source_state_id"] == value["final_source_state_id"],
            "PROPOSAL_NO_CHANGE_STATE_INVALID",
        )


def _rule_verifier_input(value: dict[str, object]) -> None:
    _verification_verify_embedded_digest("verifier-input-manifest/v1", value)
    _verification_require(value["owner_conclusion_excluded"] is True, "VERIFIER_OWNER_CONCLUSION_INCLUDED")
    _verification_require(
        _ordered_unique(value["artifact_refs"], _artifact_ref_key),
        "VERIFIER_ARTIFACT_ORDER_INVALID",
    )
    _verification_require(
        _ordered_unique(value["engineering_rule_refs"], _ref_key),
        "VERIFIER_RULE_ORDER_INVALID",
    )
    _verification_require(_sorted_unique(value["requirement_ids"]), "VERIFIER_REQUIREMENT_ORDER_INVALID")


def _rule_verifier_work(value: dict[str, object]) -> None:
    _verification_verify_embedded_digest("verifier-work-report/v1", value)
    _verification_require(value["sandbox_mode"] == "read_only_or_cow", "VERIFIER_SANDBOX_INVALID")
    for field in ("counterexamples_searched", "own_observation_ids", "limitations"):
        _verification_require(_sorted_unique(value[field]), "VERIFIER_WORK_ORDER_INVALID", f"$.{field}")
    _verification_require(bool(value["own_observation_ids"]), "VERIFIER_OBSERVATION_REQUIRED")
    _verification_require(
        _verification_valid_assessments(value["provisional_requirement_assessments"]),
        "VERIFIER_ASSESSMENT_INVALID",
    )


def _rule_attestation(value: dict[str, object]) -> None:
    _verification_verify_embedded_digest("verification-attestation/v1", value)
    _verification_require(
        _sorted_unique(value["execution_state_ids"]),
        "ATTESTATION_EXECUTION_ORDER_INVALID",
    )
    _verification_require(
        _sorted_unique(value["own_observation_ids"]) and bool(value["own_observation_ids"]),
        "ATTESTATION_OBSERVATION_INVALID",
    )
    _verification_require(
        _verification_valid_assessments(value["requirement_verdicts"]),
        "ATTESTATION_VERDICT_INVALID",
    )
    _verification_require(
        value["run_status"] == _verification_run_status(value["requirement_verdicts"]),
        "ATTESTATION_RUN_STATUS_INVALID",
    )


def _rule_attestation_manifest(value: dict[str, object]) -> None:
    _verification_verify_embedded_digest("verification-attestation-manifest/v1", value)
    _verification_require(
        value["attestation_count"] == len(value["attestations"]),
        "ATTESTATION_MANIFEST_COUNT_INVALID",
    )
    _verification_require(
        _ordered_unique(
            value["attestations"],
            lambda item: (item["slot_id"], item["attestation_id"]),
        ),
        "ATTESTATION_MANIFEST_ORDER_INVALID",
    )
    slot_ids = {item["slot_id"] for item in value["attestations"]}
    attestation_ids = {item["attestation_id"] for item in value["attestations"]}
    _verification_require(
        _ordered_unique(value["requirement_aggregates"], lambda item: item["requirement_id"]),
        "ATTESTATION_AGGREGATE_ORDER_INVALID",
    )
    for item in value["requirement_aggregates"]:
        _verification_require(
            _sorted_unique(item["required_slot_ids"]),
            "ATTESTATION_REQUIRED_SLOT_ORDER_INVALID",
        )
        _verification_require(
            _sorted_unique(item["attestation_ids"]),
            "ATTESTATION_ID_ORDER_INVALID",
        )
        _verification_require(
            set(item["attestation_ids"]).issubset(attestation_ids),
            "ATTESTATION_ID_UNKNOWN",
        )
        if set(item["required_slot_ids"]).difference(slot_ids):
            _verification_require(
                item["verdict"] == "UNVERIFIABLE",
                "ATTESTATION_MISSING_SLOT_INVALID",
            )
'''


__all__ = ["PYTHON_VERIFICATION_RULES"]
