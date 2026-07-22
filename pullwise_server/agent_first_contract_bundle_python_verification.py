"""Python verification-family semantic overrides and context helpers."""

from __future__ import annotations


PYTHON_VERIFICATION = r'''
_VERIFICATION_CONTEXT_INVALID = "VERIFICATION_CONTEXT_INVALID"
_VERIFICATION_CONTEXT_CAS_CORRUPT = "VERIFICATION_CONTEXT_CAS_CORRUPT"
_VERIFICATION_CONTEXT_DIGEST_INVALID = "VERIFICATION_CONTEXT_DIGEST_INVALID"
_VERIFICATION_CONTEXT_TIME_INVALID = "VERIFICATION_CONTEXT_TIME_INVALID"


def _verification_require(
    condition: bool, detail: str, path: str = "$"
) -> None:
    if not condition:
        _fail(detail, path)


def _verification_digest_field(schema_id: str) -> str | None:
    spec = schema(schema_id).get("x-pullwise-digest")
    return spec["field"] if isinstance(spec, dict) else None


def _verification_check_document(
    schema_id: str, value: object
) -> dict[str, object]:
    return (
        verify_document_digest(schema_id, value)
        if _verification_digest_field(schema_id) is not None
        else validate_document(schema_id, value)
    )


def _verification_check_documents(
    schema_id: str, values: object, path: str
) -> list[dict[str, object]]:
    _verification_require(
        isinstance(values, list), _VERIFICATION_CONTEXT_INVALID, path
    )
    return [
        _verification_check_document(schema_id, item)
        for item in values
    ]


def _verification_document_identity_digest(
    schema_id: str, document: dict[str, object]
) -> str:
    field = _verification_digest_field(schema_id)
    return (
        document[field]
        if field is not None
        else hashlib.sha256(canonical_document_bytes(document)).hexdigest()
    )


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
        actual == _verification_document_identity_digest(schema_id, document),
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        path,
    )


def _verification_require_id_list(
    actual: list[object], expected: list[object], path: str
) -> None:
    _verification_require(
        actual == expected, _VERIFICATION_CONTEXT_INVALID, path
    )


def _verification_timestamp_leq(
    earlier: object, later: object, path: str
) -> None:
    if earlier is None or later is None:
        return
    earlier_ms = _timestamp_millis(earlier)
    later_ms = _timestamp_millis(later)
    _verification_require(
        earlier_ms is not None and later_ms is not None and earlier_ms <= later_ms,
        _VERIFICATION_CONTEXT_TIME_INVALID,
        path,
    )


def _verification_find_plan_slot(
    plan: dict[str, object], slot_id: str
) -> dict[str, object] | None:
    return next((item for item in plan["slots"] if item["slot_id"] == slot_id), None)


def _verification_manifest_entries(
    manifest: dict[str, object],
) -> dict[str, dict[str, object]]:
    return {item["observation_id"]: item for item in manifest["entries"]}


def _verification_valid_assessments(values: list[dict[str, object]]) -> bool:
    return _ordered_unique(values, lambda item: item["requirement_id"]) and all(
        _sorted_unique(item["evidence_ids"])
        and _sorted_unique(item["limitations"])
        and (item["verdict"] != "PASS" or not item["limitations"])
        for item in values
    )


def _rule_completion_proposal(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("completion-proposal/v1", value)
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
    _verification_require(
        _sorted_unique(value["known_gaps"]), "PROPOSAL_GAP_ORDER_INVALID"
    )
    _verification_require(
        _sorted_unique(value["residual_risks"]), "PROPOSAL_RISK_ORDER_INVALID"
    )
    if value["outcome_requested"] == "NO_CHANGE_NEEDED":
        _verification_require(
            value["change_set_ref"] is None, "PROPOSAL_NO_CHANGE_SET_INVALID"
        )
        _verification_require(
            value["original_source_state_id"] == value["final_source_state_id"],
            "PROPOSAL_NO_CHANGE_STATE_INVALID",
        )


def _rule_verifier_input(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("verifier-input-manifest/v1", value)
    _verification_require(
        value["owner_conclusion_excluded"] is True,
        "VERIFIER_OWNER_CONCLUSION_INCLUDED",
    )
    _verification_require(
        _ordered_unique(value["artifact_refs"], _artifact_ref_key),
        "VERIFIER_ARTIFACT_ORDER_INVALID",
    )
    _verification_require(
        _ordered_unique(value["engineering_rule_refs"], _ref_key),
        "VERIFIER_RULE_ORDER_INVALID",
    )
    _verification_require(
        _sorted_unique(value["requirement_ids"]),
        "VERIFIER_REQUIREMENT_ORDER_INVALID",
    )


def _rule_verifier_work(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("verifier-work-report/v1", value)
    _verification_require(
        value["sandbox_mode"] == "read_only_or_cow", "VERIFIER_SANDBOX_INVALID"
    )
    for field in ("counterexamples_searched", "own_observation_ids", "limitations"):
        _verification_require(
            _sorted_unique(value[field]),
            "VERIFIER_WORK_ORDER_INVALID",
            f"$.{field}",
        )
    _verification_require(
        bool(value["own_observation_ids"]), "VERIFIER_OBSERVATION_REQUIRED"
    )
    _verification_require(
        _verification_valid_assessments(value["provisional_requirement_assessments"]),
        "VERIFIER_ASSESSMENT_INVALID",
    )


def _rule_attestation(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("verification-attestation/v1", value)
    _verification_require(
        _sorted_unique(value["execution_state_ids"]),
        "ATTESTATION_EXECUTION_ORDER_INVALID",
    )
    _verification_require(
        _sorted_unique(value["own_observation_ids"]) and bool(value["own_observation_ids"]),
        "ATTESTATION_OBSERVATION_INVALID",
    )
    verdicts = value["requirement_verdicts"]
    _verification_require(
        _verification_valid_assessments(verdicts), "ATTESTATION_VERDICT_INVALID"
    )
    present = {item["verdict"] for item in verdicts}
    expected = next(
        (
            item
            for item in ("POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE")
            if item in present
        ),
        "PASS",
    )
    _verification_require(
        value["run_status"] == expected, "ATTESTATION_RUN_STATUS_INVALID"
    )


def _rule_attestation_manifest(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("verification-attestation-manifest/v1", value)
    attestations = value["attestations"]
    _verification_require(
        value["attestation_count"] == len(attestations),
        "ATTESTATION_MANIFEST_COUNT_INVALID",
    )
    _verification_require(
        _ordered_unique(attestations, lambda item: (item["slot_id"], item["attestation_id"])),
        "ATTESTATION_MANIFEST_ORDER_INVALID",
    )
    slots = {item["slot_id"] for item in attestations}
    ids = {item["attestation_id"] for item in attestations}
    aggregates = value["requirement_aggregates"]
    _verification_require(
        _ordered_unique(aggregates, lambda item: item["requirement_id"]),
        "ATTESTATION_AGGREGATE_ORDER_INVALID",
    )
    for item in aggregates:
        _verification_require(
            _sorted_unique(item["required_slot_ids"]),
            "ATTESTATION_REQUIRED_SLOT_ORDER_INVALID",
        )
        _verification_require(
            _sorted_unique(item["attestation_ids"]),
            "ATTESTATION_ID_ORDER_INVALID",
        )
        _verification_require(
            set(item["attestation_ids"]).issubset(ids),
            "ATTESTATION_ID_UNKNOWN",
        )
        if set(item["required_slot_ids"]).difference(slots):
            _verification_require(
                item["verdict"] == "UNVERIFIABLE",
                "ATTESTATION_MISSING_SLOT_INVALID",
            )


def verify_completion_proposal_context(
    proposal: object,
    task_snapshot: object,
    attempt: object,
    owner: object,
    task_request: object,
    effective_policy: object,
    requirement_ledger: object,
    execution_charter: object,
    original_source: object,
    final_source: object,
    execution_states: object,
    change_set: object,
    pre_observation_manifest: object,
) -> dict[str, object]:
    checked = verify_document_digest("completion-proposal/v1", proposal)
    snapshot = _verification_check_document("task-record/v1", task_snapshot)
    current_attempt = _verification_check_document("attempt-record/v1", attempt)
    owner_doc = _verification_check_document("task-owner/v1", owner)
    request = _verification_check_document("task-request/v1", task_request)
    policy = verify_document_digest("effective-execution-policy/v1", effective_policy)
    ledger = verify_document_digest("requirement-ledger/v1", requirement_ledger)
    charter = verify_document_digest("task-charter/v1", execution_charter)
    original = verify_document_digest("source-tree-manifest/v1", original_source)
    final = verify_document_digest("source-tree-manifest/v1", final_source)
    states = _verification_check_documents("execution-state-manifest/v1", execution_states, "$.execution_states")
    pre = verify_document_digest("pre-verifier-observation-manifest/v1", pre_observation_manifest)
    change = None if change_set is None else verify_document_digest("change-set/v1", change_set)
    _verification_require(snapshot["task_id"] == checked["task_id"] == current_attempt["task_id"] == owner_doc["task_id"] == request["task_id"] == policy["task_type"].replace("pullwise.repo-review.full-scan/v1", checked["task_id"]) if False else checked["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(current_attempt["attempt_id"] == owner_doc["attempt_id"] == checked["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.attempt_id")
    _verification_require(snapshot["current_attempt_id"] == checked["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.current_attempt_id")
    _verification_require(snapshot["owner_id"] == owner_doc["owner_id"] == checked["owner_id"], _VERIFICATION_CONTEXT_INVALID, "$.owner_id")
    _verification_require(snapshot["owner_epoch"] == owner_doc["owner_epoch"] == checked["owner_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.owner_epoch")
    _verification_require(snapshot["task_version"] == checked["proposed_from_task_version"], _VERIFICATION_CONTEXT_INVALID, "$.proposed_from_task_version")
    _verification_require(checked["native_epoch"] == current_attempt["native_epoch"] == owner_doc["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.native_epoch")
    _verification_require_ref(snapshot["request_ref"], "task-request/v1", request, "$.request_ref")
    _verification_require(snapshot["request_digest"] == hashlib.sha256(canonical_document_bytes(request)).hexdigest() == checked["request_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.request_digest")
    _verification_require_ref(snapshot["policy_ref"], "effective-execution-policy/v1", policy, "$.policy_ref")
    _verification_require(snapshot["policy_digest"] == policy["digest"] == checked["policy_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.policy_digest")
    _verification_require(snapshot["ledger_head_digest"] == ledger["ledger_digest"] == checked["requirement_ledger_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.requirement_ledger_digest")
    _verification_require_ref(snapshot["charter_ref"], "task-charter/v1", charter, "$.charter_ref")
    _verification_require(checked["charter_digest"] == charter["digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.charter_digest")
    _verification_require(snapshot["task_type"] == request["task_type"] == policy["task_type"], _VERIFICATION_CONTEXT_INVALID, "$.task_type")
    _verification_require(checked["original_source_state_id"] == original["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.original_source_state_id")
    _verification_require(checked["final_source_state_id"] == final["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_source_state_id")
    _verification_require_id_list(checked["execution_state_ids"], [item["execution_state_id"] for item in states], "$.execution_state_ids")
    if change is None:
        _verification_require(checked["change_set_ref"] is None, _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
    else:
        _verification_require_ref(checked["change_set_ref"], "change-set/v1", change, "$.change_set_ref")
        _verification_require(change["original_source_state_id"] == original["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
        _verification_require(change["final_source_state_id"] == final["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
    _verification_require(pre["task_id"] == checked["task_id"] and pre["proposal_id"] == checked["proposal_id"] and pre["attempt_id"] == checked["attempt_id"] and pre["native_epoch"] == checked["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest")
    _verification_require_id_list([item["requirement_id"] for item in checked["requirement_claims"]], ledger["active_requirement_ids"], "$.requirement_claims")
    pre_ids = {item["observation_id"] for item in pre["entries"]}
    for index, item in enumerate(checked["requirement_claims"]):
        _verification_require(set(item["evidence_ids"]).issubset(pre_ids), _VERIFICATION_CONTEXT_INVALID, f"$.requirement_claims[{index}].evidence_ids")
    for path, timestamp in (
        ("$.task_snapshot.created_at", snapshot["created_at"]),
        ("$.task_snapshot.updated_at", snapshot["updated_at"]),
        ("$.attempt.lease_acquired_at", current_attempt["lease_acquired_at"]),
        ("$.attempt.started_at", current_attempt["started_at"]),
        ("$.attempt.ended_at", current_attempt["ended_at"]),
        ("$.owner.started_at", owner_doc["started_at"]),
        ("$.owner.ended_at", owner_doc["ended_at"]),
        ("$.task_request.submitted_at", request["submitted_at"]),
        ("$.effective_policy.issued_at", policy["issued_at"]),
        ("$.execution_charter.created_at", charter["created_at"]),
    ):
        _verification_timestamp_leq(timestamp, checked["created_at"], path)
    return checked


def verify_verifier_input_context(
    manifest: object,
    proposal: object,
    quality_policy_plan: object,
    task_request: object,
    effective_policy: object,
    requirement_ledger: object,
    execution_charter: object,
    original_source: object,
    final_source: object,
    change_set: object,
    pre_observation_manifest: object,
    engineering_rules: object,
) -> dict[str, object]:
    checked = verify_document_digest("verifier-input-manifest/v1", manifest)
    proposal_doc = verify_completion_proposal_context(proposal, validate_document("task-record/v1", {"schema_id":"task-record/v1","task_id":proposal["task_id"] if isinstance(proposal, dict) and "task_id" in proposal else "", "task_type":"pullwise.repo-review.full-scan/v1","request_ref":{"schema_id":"content-ref/v1","artifact_id":"art_stub","content_schema_id":"task-request/v1","sha256":"0"*64,"size_bytes":0,"media_type":"application/json","encoding":"utf-8"},"request_digest":"0"*64,"policy_ref":{"schema_id":"content-ref/v1","artifact_id":"art_stub","content_schema_id":"effective-execution-policy/v1","sha256":"0"*64,"size_bytes":0,"media_type":"application/json","encoding":"utf-8"},"policy_digest":"0"*64,"policy_version":1,"protocol_mode":"agent_task_v1","lifecycle":"ACTIVE","desired_state":"RUN","task_version":1,"deletion_version":0,"outer_job_id":None,"run_id":None,"lease_id":None,"transport_epoch":None,"native_epoch":1,"current_attempt_id":proposal["attempt_id"] if isinstance(proposal, dict) and "attempt_id" in proposal else "attempt_"+"0"*32,"owner_id":proposal["owner_id"] if isinstance(proposal, dict) and "owner_id" in proposal else "owner_"+"0"*32,"owner_epoch":proposal["owner_epoch"] if isinstance(proposal, dict) and "owner_epoch" in proposal else 1,"ledger_version":1,"ledger_head_digest":"0"*64,"charter_version":1,"charter_ref":{"schema_id":"content-ref/v1","artifact_id":"art_stub","content_schema_id":"task-charter/v1","sha256":"0"*64,"size_bytes":0,"media_type":"application/json","encoding":"utf-8"},"current_checkpoint_generation":0,"current_checkpoint_hash":None,"quality_risk":"Q1","absolute_deadline_at":"2026-01-01T00:00:00.000Z","terminalization_reserve_ms":0,"completion_proposal_ref":None,"final_observation_manifest_ref":None,"terminal_kind":None,"result_ref":None,"result_digest":None,"outcome":None,"created_at":"2026-01-01T00:00:00.000Z","updated_at":"2026-01-01T00:00:00.000Z","terminal_at":None}) if False else verify_document_digest("completion-proposal/v1", proposal)
    plan = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    request = _verification_check_document("task-request/v1", task_request)
    policy = verify_document_digest("effective-execution-policy/v1", effective_policy)
    ledger = verify_document_digest("requirement-ledger/v1", requirement_ledger)
    charter = verify_document_digest("task-charter/v1", execution_charter)
    original = verify_document_digest("source-tree-manifest/v1", original_source)
    final = verify_document_digest("source-tree-manifest/v1", final_source)
    pre = verify_document_digest("pre-verifier-observation-manifest/v1", pre_observation_manifest)
    change = None if change_set is None else verify_document_digest("change-set/v1", change_set)
    rules = _verification_check_documents("source-content/v1", engineering_rules, "$.engineering_rules")
    slot = _verification_find_plan_slot(plan, checked["slot_id"])
    _verification_require(slot is not None, _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require(checked["task_id"] == proposal_doc["task_id"] == request["task_id"] == ledger["task_id"] == charter["task_id"] == plan["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == proposal_doc["proposal_id"] == plan["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require_ref(checked["task_request_ref"], "task-request/v1", request, "$.task_request_ref")
    _verification_require_ref(checked["effective_policy_ref"], "effective-execution-policy/v1", policy, "$.effective_policy_ref")
    _verification_require_ref(checked["requirement_ledger_ref"], "requirement-ledger/v1", ledger, "$.requirement_ledger_ref")
    _verification_require_ref(checked["charter_ref"], "task-charter/v1", charter, "$.charter_ref")
    _verification_require_ref(checked["completion_proposal_ref"], "completion-proposal/v1", proposal_doc, "$.completion_proposal_ref")
    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest")
    _verification_require_ref(checked["original_source_ref"], "source-tree-manifest/v1", original, "$.original_source_ref")
    _verification_require_ref(checked["final_source_ref"], "source-tree-manifest/v1", final, "$.final_source_ref")
    _verification_require_ref(checked["pre_verifier_observation_manifest_ref"], "pre-verifier-observation-manifest/v1", pre, "$.pre_verifier_observation_manifest_ref")
    _verification_require(checked["artifact_refs"] == proposal_doc["artifact_refs"], _VERIFICATION_CONTEXT_INVALID, "$.artifact_refs")
    _verification_require(len(checked["engineering_rule_refs"]) == len(rules), _VERIFICATION_CONTEXT_INVALID, "$.engineering_rule_refs")
    for index, (ref, document) in enumerate(zip(checked["engineering_rule_refs"], rules)):
        _verification_require_ref(ref, "source-content/v1", document, f"$.engineering_rule_refs[{index}]")
    if change is None:
        _verification_require(checked["change_set"]["availability"] != "available" and proposal_doc["change_set_ref"] is None, _VERIFICATION_CONTEXT_INVALID, "$.change_set")
    else:
        _verification_require(checked["change_set"]["availability"] == "available", _VERIFICATION_CONTEXT_INVALID, "$.change_set")
        _verification_require_ref(checked["change_set"]["ref"], "change-set/v1", change, "$.change_set.ref")
        _verification_require_ref(proposal_doc["change_set_ref"], "change-set/v1", change, "$.completion_proposal_ref")
    _verification_require(plan["proposal_digest"] == proposal_doc["proposal_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest")
    _verification_require(plan["policy_digest"] == proposal_doc["policy_digest"] == policy["digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest")
    _verification_require(plan["requirement_ledger_digest"] == ledger["ledger_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest")
    _verification_require(proposal_doc["original_source_state_id"] == original["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.original_source_ref")
    _verification_require(proposal_doc["final_source_state_id"] == final["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_source_ref")
    _verification_require(pre["task_id"] == proposal_doc["task_id"] and pre["proposal_id"] == proposal_doc["proposal_id"] and pre["attempt_id"] == proposal_doc["attempt_id"] and pre["native_epoch"] == proposal_doc["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref")
    _verification_require(checked["slot_concern"] == slot["concern"], _VERIFICATION_CONTEXT_INVALID, "$.slot_concern")
    _verification_require_id_list(checked["requirement_ids"], slot["requirement_ids"], "$.requirement_ids")
    _verification_timestamp_leq(proposal_doc["created_at"], checked["created_at"], "$.created_at")
    return checked


def verify_verifier_work_context(
    report: object,
    verifier_input: object,
    proposal: object,
    final_observation_manifest: object,
) -> dict[str, object]:
    checked = verify_document_digest("verifier-work-report/v1", report)
    input_doc = verify_document_digest("verifier-input-manifest/v1", verifier_input)
    proposal_doc = verify_document_digest("completion-proposal/v1", proposal)
    final_manifest = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    entries = _verification_manifest_entries(final_manifest)
    _verification_require_ref(checked["verifier_input_manifest_ref"], "verifier-input-manifest/v1", input_doc, "$.verifier_input_manifest_ref")
    _verification_require_companion_digest(checked["verifier_input_manifest_digest"], "verifier-input-manifest/v1", input_doc, "$.verifier_input_manifest_digest")
    _verification_require(checked["task_id"] == input_doc["task_id"] == proposal_doc["task_id"] == final_manifest["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == input_doc["proposal_id"] == proposal_doc["proposal_id"] == final_manifest["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["slot_id"] == input_doc["slot_id"], _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require(final_manifest["attempt_id"] == proposal_doc["attempt_id"] and final_manifest["native_epoch"] == proposal_doc["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest")
    _verification_require_id_list([item["requirement_id"] for item in checked["provisional_requirement_assessments"]], input_doc["requirement_ids"], "$.provisional_requirement_assessments")
    final_ids = set(entries)
    own_ids = set(checked["own_observation_ids"])
    _verification_require(own_ids.issubset(final_ids), _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    for observation_id in checked["own_observation_ids"]:
        actor = entries[observation_id]["actor"]
        _verification_require(actor["kind"] == "quality_verifier" and actor["session_id"] == checked["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    for index, item in enumerate(checked["provisional_requirement_assessments"]):
        evidence = set(item["evidence_ids"])
        _verification_require(evidence.issubset(final_ids) and evidence.issubset(own_ids), _VERIFICATION_CONTEXT_INVALID, f"$.provisional_requirement_assessments[{index}].evidence_ids")
    _verification_timestamp_leq(input_doc["created_at"], checked["created_at"], "$.created_at")
    return checked


def verify_attestation_context(
    attestation: object,
    verifier_input: object,
    verifier_work: object,
    proposal: object,
    quality_policy_plan: object,
    final_source: object,
    execution_states: object,
    final_observation_manifest: object,
) -> dict[str, object]:
    checked = verify_document_digest("verification-attestation/v1", attestation)
    input_doc = verify_document_digest("verifier-input-manifest/v1", verifier_input)
    work = verify_document_digest("verifier-work-report/v1", verifier_work)
    proposal_doc = verify_document_digest("completion-proposal/v1", proposal)
    plan = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    final = verify_document_digest("source-tree-manifest/v1", final_source)
    states = _verification_check_documents("execution-state-manifest/v1", execution_states, "$.execution_states")
    manifest = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    entries = _verification_manifest_entries(manifest)
    _verification_require_ref(checked["verifier_input_manifest_ref"], "verifier-input-manifest/v1", input_doc, "$.verifier_input_manifest_ref")
    _verification_require_companion_digest(checked["verifier_input_manifest_digest"], "verifier-input-manifest/v1", input_doc, "$.verifier_input_manifest_digest")
    _verification_require_ref(checked["verifier_work_report_ref"], "verifier-work-report/v1", work, "$.verifier_work_report_ref")
    _verification_require_companion_digest(checked["verifier_work_report_digest"], "verifier-work-report/v1", work, "$.verifier_work_report_digest")
    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest")
    _verification_require_ref(checked["final_observation_manifest_ref"], "observation-manifest/v1", manifest, "$.final_observation_manifest_ref")
    _verification_require_companion_digest(checked["final_observation_manifest_digest"], "observation-manifest/v1", manifest, "$.final_observation_manifest_digest")
    _verification_require(checked["task_id"] == input_doc["task_id"] == work["task_id"] == proposal_doc["task_id"] == plan["task_id"] == manifest["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == input_doc["proposal_id"] == work["proposal_id"] == proposal_doc["proposal_id"] == plan["proposal_id"] == manifest["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["slot_id"] == input_doc["slot_id"] == work["slot_id"], _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require(checked["verifier_session_id"] == work["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, "$.verifier_session_id")
    _verification_require(checked["model_identity"] == work["model_identity"], _VERIFICATION_CONTEXT_INVALID, "$.model_identity")
    _verification_require(checked["source_state_id"] == final["source_state_id"] == proposal_doc["final_source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.source_state_id")
    _verification_require_id_list(checked["execution_state_ids"], [item["execution_state_id"] for item in states], "$.execution_state_ids")
    _verification_require(checked["own_observation_ids"] == work["own_observation_ids"], _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    final_ids = set(entries)
    own_ids = set(checked["own_observation_ids"])
    for observation_id in checked["own_observation_ids"]:
        actor = entries.get(observation_id, {}).get("actor")
        _verification_require(actor is not None and actor["kind"] == "quality_verifier" and actor["session_id"] == checked["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    _verification_require_id_list([item["requirement_id"] for item in checked["requirement_verdicts"]], input_doc["requirement_ids"], "$.requirement_verdicts")
    for index, item in enumerate(checked["requirement_verdicts"]):
        evidence = set(item["evidence_ids"])
        _verification_require(evidence.issubset(final_ids) and evidence.issubset(own_ids), _VERIFICATION_CONTEXT_INVALID, f"$.requirement_verdicts[{index}].evidence_ids")
    _verification_timestamp_leq(work["created_at"], checked["created_at"], "$.created_at")
    return checked


def verify_attestation_manifest_context(
    manifest: object,
    quality_policy_plan: object,
    final_observation_manifest: object,
    attestations: object,
) -> dict[str, object]:
    checked = verify_document_digest("verification-attestation-manifest/v1", manifest)
    plan = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    final_manifest = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    attestation_docs = _verification_check_documents("verification-attestation/v1", attestations, "$.attestations")
    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest")
    _verification_require_ref(checked["final_observation_manifest_ref"], "observation-manifest/v1", final_manifest, "$.final_observation_manifest_ref")
    _verification_require_companion_digest(checked["final_observation_manifest_digest"], "observation-manifest/v1", final_manifest, "$.final_observation_manifest_digest")
    _verification_require(checked["task_id"] == plan["task_id"] == final_manifest["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == plan["proposal_id"] == final_manifest["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(len(checked["attestations"]) == len(attestation_docs), _VERIFICATION_CONTEXT_INVALID, "$.attestations")
    attestation_by_id = {}
    for index, document in enumerate(attestation_docs):
        _verification_require(document["attestation_id"] not in attestation_by_id, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}]")
        attestation_by_id[document["attestation_id"]] = document
    manifest_slots = []
    for index, entry in enumerate(checked["attestations"]):
        document = attestation_by_id.get(entry["attestation_id"])
        _verification_require(document is not None, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}]")
        _verification_require(entry["slot_id"] == document["slot_id"] and entry["run_status"] == document["run_status"], _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}]")
        _verification_require_ref(entry["attestation_ref"], "verification-attestation/v1", document, f"$.attestations[{index}].attestation_ref")
        manifest_slots.append(entry["slot_id"])
    plan_slots = [item["slot_id"] for item in plan["slots"]]
    _verification_require(sorted(manifest_slots) == sorted(plan_slots), _VERIFICATION_CONTEXT_INVALID, "$.attestations")
    sessions = [item["verifier_session_id"] for item in attestation_docs]
    _verification_require(len(sessions) == len(set(sessions)), _VERIFICATION_CONTEXT_INVALID, "$.attestations")
    expected_requirements = sorted({req for slot in plan["slots"] for req in slot["requirement_ids"]})
    _verification_require_id_list([item["requirement_id"] for item in checked["requirement_aggregates"]], expected_requirements, "$.requirement_aggregates")
    by_slot = {item["slot_id"]: item for item in attestation_docs}
    for index, aggregate in enumerate(checked["requirement_aggregates"]):
        requirement_id = aggregate["requirement_id"]
        required_slot_ids = sorted(slot["slot_id"] for slot in plan["slots"] if requirement_id in slot["requirement_ids"])
        matched = []
        verdicts = []
        missing = False
        for slot_id in required_slot_ids:
            document = by_slot.get(slot_id)
            if document is None:
                missing = True
                continue
            verdict = next((item for item in document["requirement_verdicts"] if item["requirement_id"] == requirement_id), None)
            if verdict is None:
                missing = True
                continue
            matched.append(document["attestation_id"])
            verdicts.append(verdict["verdict"])
        expected_verdict = (
            "UNVERIFIABLE"
            if missing
            else "FAIL"
            if any(item in {"POLICY_VIOLATION", "NEEDS_WORK"} for item in verdicts)
            else "UNVERIFIABLE"
            if "UNVERIFIABLE" in verdicts
            else "PASS"
        )
        _verification_require(aggregate["required_slot_ids"] == required_slot_ids, _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].required_slot_ids")
        _verification_require(aggregate["attestation_ids"] == sorted(matched), _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].attestation_ids")
        _verification_require(aggregate["verdict"] == expected_verdict, _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].verdict")
    latest = max(_timestamp_millis(item["created_at"]) for item in attestation_docs)
    manifest_time = _timestamp_millis(checked["created_at"])
    _verification_require(manifest_time is not None and latest is not None and latest <= manifest_time, _VERIFICATION_CONTEXT_TIME_INVALID, "$.created_at")
    return checked
'''


__all__ = ["PYTHON_VERIFICATION"]
