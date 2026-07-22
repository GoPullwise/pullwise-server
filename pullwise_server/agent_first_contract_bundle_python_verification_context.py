"""Python verification-family direct-document contextual helpers."""

from __future__ import annotations


PYTHON_VERIFICATION_CONTEXT = r'''
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
    snapshot = validate_document("task-record/v1", task_snapshot)
    attempt_value = validate_document("attempt-record/v1", attempt)
    owner_value = validate_document("task-owner/v1", owner)
    request_value = validate_document("task-request/v1", task_request)
    policy_value = verify_document_digest("effective-execution-policy/v1", effective_policy)
    ledger_value = verify_document_digest("requirement-ledger/v1", requirement_ledger)
    charter_value = verify_document_digest("task-charter/v1", execution_charter)
    original_value = verify_document_digest("source-tree-manifest/v1", original_source)
    final_value = verify_document_digest("source-tree-manifest/v1", final_source)
    pre_value = verify_document_digest("pre-verifier-observation-manifest/v1", pre_observation_manifest)
    state_values = _verification_check_documents("execution-state-manifest/v1", execution_states, "$.execution_state_ids")
    change_value = None if change_set is None else verify_document_digest("change-set/v1", change_set)

    _verification_require(checked["task_id"] == snapshot["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == attempt_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == owner_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == request_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == ledger_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == charter_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["attempt_id"] == snapshot["current_attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.attempt_id")
    _verification_require(checked["attempt_id"] == attempt_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.attempt_id")
    _verification_require(checked["attempt_id"] == owner_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.attempt_id")
    _verification_require(checked["native_epoch"] == snapshot["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.native_epoch")
    _verification_require(checked["native_epoch"] == attempt_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.native_epoch")
    _verification_require(checked["native_epoch"] == owner_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.native_epoch")
    _verification_require(checked["owner_id"] == snapshot["owner_id"], _VERIFICATION_CONTEXT_INVALID, "$.owner_id")
    _verification_require(checked["owner_id"] == owner_value["owner_id"], _VERIFICATION_CONTEXT_INVALID, "$.owner_id")
    _verification_require(checked["owner_epoch"] == snapshot["owner_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.owner_epoch")
    _verification_require(checked["owner_epoch"] == owner_value["owner_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.owner_epoch")
    _verification_require(snapshot["task_type"] == request_value["task_type"], _VERIFICATION_CONTEXT_INVALID, "$.task_type")
    _verification_require(snapshot["task_type"] == policy_value["task_type"], _VERIFICATION_CONTEXT_INVALID, "$.task_type")
    _verification_require(snapshot["task_version"] == checked["proposed_from_task_version"], _VERIFICATION_CONTEXT_INVALID, "$.proposed_from_task_version")
    _verification_require(owner_value["session_id"] == attempt_value["owner_session_id"], _VERIFICATION_CONTEXT_INVALID, "$.session_id")
    _verification_require(pre_value["task_id"] == checked["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest.task_id")
    _verification_require(pre_value["proposal_id"] == checked["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest.proposal_id")
    _verification_require(pre_value["attempt_id"] == checked["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest.attempt_id")
    _verification_require(pre_value["native_epoch"] == checked["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest.native_epoch")
    _verification_require(snapshot["charter_ref"] is not None, _VERIFICATION_CONTEXT_INVALID, "$.charter_ref")
    _verification_require_ref(snapshot["request_ref"], "task-request/v1", request_value, "$.request_ref")
    _verification_require_ref(snapshot["policy_ref"], "effective-execution-policy/v1", policy_value, "$.policy_ref")
    _verification_require_ref(snapshot["charter_ref"], "task-charter/v1", charter_value, "$.charter_ref")
    if snapshot["completion_proposal_ref"] is not None:
        _verification_require_ref(snapshot["completion_proposal_ref"], "completion-proposal/v1", checked, "$.completion_proposal_ref")
    _verification_require(
        checked["request_digest"] == snapshot["request_digest"] == _verification_request_digest(request_value),
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        "$.request_digest",
    )
    _verification_require(
        checked["policy_digest"] == snapshot["policy_digest"] == policy_value["digest"],
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        "$.policy_digest",
    )
    _verification_require(
        checked["requirement_ledger_digest"] == snapshot["ledger_head_digest"] == ledger_value["ledger_digest"],
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        "$.requirement_ledger_digest",
    )
    _verification_require(
        checked["charter_digest"] == _verification_companion_digest("task-charter/v1", charter_value),
        _VERIFICATION_CONTEXT_DIGEST_INVALID,
        "$.charter_digest",
    )
    _verification_require(checked["original_source_state_id"] == original_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.original_source_state_id")
    _verification_require(checked["final_source_state_id"] == final_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_source_state_id")
    _verification_require(
        checked["execution_state_ids"] == [item["execution_state_id"] for item in state_values],
        _VERIFICATION_CONTEXT_INVALID,
        "$.execution_state_ids",
    )
    for index, state_value in enumerate(state_values):
        _verification_require(
            state_value["source_state_id"] == final_value["source_state_id"],
            _VERIFICATION_CONTEXT_INVALID,
            f"$.execution_state_ids[{index}]",
        )
    if change_value is None:
        _verification_require(checked["change_set_ref"] is None, _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
    else:
        _verification_require_ref(checked["change_set_ref"], "change-set/v1", change_value, "$.change_set_ref")
        _verification_require(change_value["original_source_state_id"] == original_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
        _verification_require(change_value["final_source_state_id"] == final_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set_ref")
    _verification_require(
        _verification_requirement_ids(checked["requirement_claims"]) == ledger_value["active_requirement_ids"],
        _VERIFICATION_CONTEXT_INVALID,
        "$.requirement_claims",
    )
    known_observations = set(_verification_manifest_entries(pre_value))
    for index, item in enumerate(checked["requirement_claims"]):
        _verification_require(
            set(item["evidence_ids"]).issubset(known_observations),
            _VERIFICATION_CONTEXT_INVALID,
            f"$.requirement_claims[{index}].evidence_ids",
        )
    _verification_require_time_order(
        [
            request_value["submitted_at"],
            policy_value["issued_at"],
            charter_value["created_at"],
            snapshot["created_at"],
            snapshot["updated_at"],
            attempt_value["lease_acquired_at"],
            attempt_value["started_at"],
            attempt_value["ended_at"],
            owner_value["started_at"],
            owner_value["ended_at"],
            checked["created_at"],
        ],
        "$.created_at",
    )
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
    proposal_value = verify_document_digest("completion-proposal/v1", proposal)
    plan_value = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    request_value = validate_document("task-request/v1", task_request)
    policy_value = verify_document_digest("effective-execution-policy/v1", effective_policy)
    ledger_value = verify_document_digest("requirement-ledger/v1", requirement_ledger)
    charter_value = verify_document_digest("task-charter/v1", execution_charter)
    original_value = verify_document_digest("source-tree-manifest/v1", original_source)
    final_value = verify_document_digest("source-tree-manifest/v1", final_source)
    pre_value = verify_document_digest("pre-verifier-observation-manifest/v1", pre_observation_manifest)
    rule_values = _verification_check_documents("source-content/v1", engineering_rules, "$.engineering_rule_refs")
    change_value = None if change_set is None else verify_document_digest("change-set/v1", change_set)
    slot = _verification_find_plan_slot(plan_value, checked["slot_id"])

    _verification_require(slot is not None, _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require(checked["task_id"] == proposal_value["task_id"] == plan_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == request_value["task_id"] == ledger_value["task_id"] == charter_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == proposal_value["proposal_id"] == plan_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["artifact_refs"] == proposal_value["artifact_refs"], _VERIFICATION_CONTEXT_INVALID, "$.artifact_refs")
    _verification_require_ref(checked["task_request_ref"], "task-request/v1", request_value, "$.task_request_ref")
    _verification_require_ref(checked["effective_policy_ref"], "effective-execution-policy/v1", policy_value, "$.effective_policy_ref")
    _verification_require_ref(checked["requirement_ledger_ref"], "requirement-ledger/v1", ledger_value, "$.requirement_ledger_ref")
    _verification_require_ref(checked["charter_ref"], "task-charter/v1", charter_value, "$.charter_ref")
    _verification_require_ref(checked["completion_proposal_ref"], "completion-proposal/v1", proposal_value, "$.completion_proposal_ref")
    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_ref")
    _verification_require_ref(checked["original_source_ref"], "source-tree-manifest/v1", original_value, "$.original_source_ref")
    _verification_require_ref(checked["final_source_ref"], "source-tree-manifest/v1", final_value, "$.final_source_ref")
    _verification_require_ref(checked["pre_verifier_observation_manifest_ref"], "pre-verifier-observation-manifest/v1", pre_value, "$.pre_verifier_observation_manifest_ref")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_digest")
    _verification_require(plan_value["proposal_digest"] == proposal_value["proposal_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.proposal_digest")
    _verification_require(plan_value["policy_digest"] == proposal_value["policy_digest"] == policy_value["digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.policy_digest")
    _verification_require(plan_value["requirement_ledger_digest"] == ledger_value["ledger_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.requirement_ledger_digest")
    _verification_require(checked["slot_concern"] == slot["concern"], _VERIFICATION_CONTEXT_INVALID, "$.slot_concern")
    _verification_require(checked["requirement_ids"] == slot["requirement_ids"], _VERIFICATION_CONTEXT_INVALID, "$.requirement_ids")
    _verification_require(proposal_value["request_digest"] == _verification_request_digest(request_value), _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.request_digest")
    _verification_require(request_value["task_type"] == policy_value["task_type"], _VERIFICATION_CONTEXT_INVALID, "$.task_type")
    _verification_require(proposal_value["charter_digest"] == _verification_companion_digest("task-charter/v1", charter_value), _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.charter_digest")
    _verification_require(proposal_value["original_source_state_id"] == original_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.original_source_ref")
    _verification_require(proposal_value["final_source_state_id"] == final_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_source_ref")
    _verification_require(pre_value["task_id"] == proposal_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref.task_id")
    _verification_require(pre_value["proposal_id"] == proposal_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref.proposal_id")
    _verification_require(pre_value["attempt_id"] == proposal_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref.attempt_id")
    _verification_require(pre_value["native_epoch"] == proposal_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref.native_epoch")
    _verification_change_binding(checked["change_set"], change_value, "$.change_set")
    if change_value is None:
        _verification_require(proposal_value["change_set_ref"] is None, _VERIFICATION_CONTEXT_INVALID, "$.change_set")
    else:
        _verification_require_ref(proposal_value["change_set_ref"], "change-set/v1", change_value, "$.completion_proposal_ref.change_set_ref")
        _verification_require(change_value["original_source_state_id"] == original_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set.original_source_state_id")
        _verification_require(change_value["final_source_state_id"] == final_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.change_set.final_source_state_id")
    _verification_require(len(rule_values) == len(checked["engineering_rule_refs"]), _VERIFICATION_CONTEXT_INVALID, "$.engineering_rule_refs")
    for index, (ref, document) in enumerate(zip(checked["engineering_rule_refs"], rule_values)):
        _verification_require_ref(ref, "source-content/v1", document, f"$.engineering_rule_refs[{index}]")
    _verification_require_time_order([proposal_value["created_at"], checked["created_at"]], "$.created_at")
    return checked


def verify_verifier_work_context(
    report: object,
    verifier_input: object,
    proposal: object,
    final_observation_manifest: object,
) -> dict[str, object]:
    checked = verify_document_digest("verifier-work-report/v1", report)
    input_value = verify_document_digest("verifier-input-manifest/v1", verifier_input)
    proposal_value = verify_document_digest("completion-proposal/v1", proposal)
    final_value = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    entries = _verification_manifest_entries(final_value)

    _verification_require_ref(checked["verifier_input_manifest_ref"], "verifier-input-manifest/v1", input_value, "$.verifier_input_manifest_ref")
    _verification_require_companion_digest(checked["verifier_input_manifest_digest"], "verifier-input-manifest/v1", input_value, "$.verifier_input_manifest_digest")
    _verification_require(checked["task_id"] == input_value["task_id"] == proposal_value["task_id"] == final_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == input_value["proposal_id"] == proposal_value["proposal_id"] == final_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["slot_id"] == input_value["slot_id"], _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require_ref(input_value["completion_proposal_ref"], "completion-proposal/v1", proposal_value, "$.completion_proposal_ref")
    _verification_require(final_value["attempt_id"] == proposal_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.attempt_id")
    _verification_require(final_value["native_epoch"] == proposal_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.native_epoch")
    _verification_require(
        _verification_requirement_ids(checked["provisional_requirement_assessments"]) == input_value["requirement_ids"],
        _VERIFICATION_CONTEXT_INVALID,
        "$.provisional_requirement_assessments",
    )
    own_ids = set(checked["own_observation_ids"])
    final_ids = set(entries)
    _verification_require(own_ids.issubset(final_ids), _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    for index, observation_id in enumerate(checked["own_observation_ids"]):
        actor = entries[observation_id]["actor"]
        _verification_require(actor["kind"] == "quality_verifier", _VERIFICATION_CONTEXT_INVALID, f"$.own_observation_ids[{index}]")
        _verification_require(actor["session_id"] == checked["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, f"$.own_observation_ids[{index}]")
    for index, item in enumerate(checked["provisional_requirement_assessments"]):
        evidence_ids = set(item["evidence_ids"])
        _verification_require(evidence_ids.issubset(own_ids), _VERIFICATION_CONTEXT_INVALID, f"$.provisional_requirement_assessments[{index}].evidence_ids")
        _verification_require(evidence_ids.issubset(final_ids), _VERIFICATION_CONTEXT_INVALID, f"$.provisional_requirement_assessments[{index}].evidence_ids")
    _verification_require_time_order([proposal_value["created_at"], input_value["created_at"], checked["created_at"]], "$.created_at")
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
    input_value = verify_document_digest("verifier-input-manifest/v1", verifier_input)
    work_value = verify_document_digest("verifier-work-report/v1", verifier_work)
    proposal_value = verify_document_digest("completion-proposal/v1", proposal)
    plan_value = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    final_source_value = verify_document_digest("source-tree-manifest/v1", final_source)
    state_values = _verification_check_documents("execution-state-manifest/v1", execution_states, "$.execution_state_ids")
    final_manifest_value = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    entries = _verification_manifest_entries(final_manifest_value)
    slot = _verification_find_plan_slot(plan_value, checked["slot_id"])

    _verification_require(slot is not None, _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require_ref(checked["verifier_input_manifest_ref"], "verifier-input-manifest/v1", input_value, "$.verifier_input_manifest_ref")
    _verification_require_ref(checked["verifier_work_report_ref"], "verifier-work-report/v1", work_value, "$.verifier_work_report_ref")
    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_ref")
    _verification_require_ref(checked["final_observation_manifest_ref"], "observation-manifest/v1", final_manifest_value, "$.final_observation_manifest_ref")
    _verification_require_companion_digest(checked["verifier_input_manifest_digest"], "verifier-input-manifest/v1", input_value, "$.verifier_input_manifest_digest")
    _verification_require_companion_digest(checked["verifier_work_report_digest"], "verifier-work-report/v1", work_value, "$.verifier_work_report_digest")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_digest")
    _verification_require_companion_digest(checked["final_observation_manifest_digest"], "observation-manifest/v1", final_manifest_value, "$.final_observation_manifest_digest")
    _verification_require(checked["task_id"] == input_value["task_id"] == work_value["task_id"] == proposal_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["task_id"] == plan_value["task_id"] == final_manifest_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == input_value["proposal_id"] == work_value["proposal_id"] == proposal_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["proposal_id"] == plan_value["proposal_id"] == final_manifest_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["slot_id"] == input_value["slot_id"] == work_value["slot_id"], _VERIFICATION_CONTEXT_INVALID, "$.slot_id")
    _verification_require(checked["verifier_session_id"] == work_value["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, "$.verifier_session_id")
    _verification_require(checked["model_identity"] == work_value["model_identity"], _VERIFICATION_CONTEXT_INVALID, "$.model_identity")
    _verification_require(final_manifest_value["attempt_id"] == proposal_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.attempt_id")
    _verification_require(final_manifest_value["native_epoch"] == proposal_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.native_epoch")
    _verification_require(checked["source_state_id"] == proposal_value["final_source_state_id"] == final_source_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, "$.source_state_id")
    _verification_require_ref(input_value["quality_policy_plan_ref"], "quality-policy-plan/v1", plan_value, "$.completion_proposal_ref.quality_policy_plan_ref")
    _verification_require_companion_digest(input_value["quality_policy_plan_digest"], "quality-policy-plan/v1", plan_value, "$.completion_proposal_ref.quality_policy_plan_digest")
    _verification_require(plan_value["proposal_digest"] == proposal_value["proposal_digest"], _VERIFICATION_CONTEXT_DIGEST_INVALID, "$.proposal_digest")
    _verification_require(final_manifest_value["attempt_id"] == proposal_value["attempt_id"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.attempt_id")
    _verification_require(final_manifest_value["native_epoch"] == proposal_value["native_epoch"], _VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest.native_epoch")
    _verification_require(
        checked["execution_state_ids"] == [item["execution_state_id"] for item in state_values],
        _VERIFICATION_CONTEXT_INVALID,
        "$.execution_state_ids",
    )
    for index, state_value in enumerate(state_values):
        _verification_require(state_value["source_state_id"] == final_source_value["source_state_id"], _VERIFICATION_CONTEXT_INVALID, f"$.execution_state_ids[{index}]")
    _verification_require(checked["own_observation_ids"] == work_value["own_observation_ids"], _VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids")
    _verification_require(
        _verification_requirement_ids(checked["requirement_verdicts"]) == slot["requirement_ids"],
        _VERIFICATION_CONTEXT_INVALID,
        "$.requirement_verdicts",
    )
    _verification_require(
        _verification_requirement_ids(checked["requirement_verdicts"]) == input_value["requirement_ids"],
        _VERIFICATION_CONTEXT_INVALID,
        "$.requirement_verdicts",
    )
    own_ids = set(checked["own_observation_ids"])
    final_ids = set(entries)
    for index, observation_id in enumerate(checked["own_observation_ids"]):
        actor = entries.get(observation_id, {}).get("actor")
        _verification_require(actor is not None, _VERIFICATION_CONTEXT_INVALID, f"$.own_observation_ids[{index}]")
        _verification_require(actor["kind"] == "quality_verifier", _VERIFICATION_CONTEXT_INVALID, f"$.own_observation_ids[{index}]")
        _verification_require(actor["session_id"] == checked["verifier_session_id"], _VERIFICATION_CONTEXT_INVALID, f"$.own_observation_ids[{index}]")
    for index, entry in enumerate(final_manifest_value["entries"]):
        actor = entry["actor"]
        _verification_require(
            actor["session_id"] != checked["verifier_session_id"] or actor["kind"] == "quality_verifier",
            _VERIFICATION_CONTEXT_INVALID,
            f"$.final_observation_manifest.entries[{index}].actor",
        )
    for index, item in enumerate(checked["requirement_verdicts"]):
        evidence_ids = set(item["evidence_ids"])
        _verification_require(evidence_ids.issubset(own_ids), _VERIFICATION_CONTEXT_INVALID, f"$.requirement_verdicts[{index}].evidence_ids")
        _verification_require(evidence_ids.issubset(final_ids), _VERIFICATION_CONTEXT_INVALID, f"$.requirement_verdicts[{index}].evidence_ids")
    _verification_require_time_order([proposal_value["created_at"], input_value["created_at"], work_value["created_at"], checked["created_at"]], "$.created_at")
    return checked


def verify_attestation_manifest_context(
    manifest: object,
    quality_policy_plan: object,
    final_observation_manifest: object,
    attestations: object,
) -> dict[str, object]:
    checked = verify_document_digest("verification-attestation-manifest/v1", manifest)
    plan_value = verify_document_digest("quality-policy-plan/v1", quality_policy_plan)
    final_value = verify_document_digest("observation-manifest/v1", final_observation_manifest)
    attestation_values = _verification_check_documents("verification-attestation/v1", attestations, "$.attestations")

    _verification_require_ref(checked["quality_policy_plan_ref"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_ref")
    _verification_require_ref(checked["final_observation_manifest_ref"], "observation-manifest/v1", final_value, "$.final_observation_manifest_ref")
    _verification_require_companion_digest(checked["quality_policy_plan_digest"], "quality-policy-plan/v1", plan_value, "$.quality_policy_plan_digest")
    _verification_require_companion_digest(checked["final_observation_manifest_digest"], "observation-manifest/v1", final_value, "$.final_observation_manifest_digest")
    _verification_require(checked["task_id"] == plan_value["task_id"] == final_value["task_id"], _VERIFICATION_CONTEXT_INVALID, "$.task_id")
    _verification_require(checked["proposal_id"] == plan_value["proposal_id"] == final_value["proposal_id"], _VERIFICATION_CONTEXT_INVALID, "$.proposal_id")
    _verification_require(checked["attestation_count"] == len(attestation_values), _VERIFICATION_CONTEXT_INVALID, "$.attestation_count")

    attestation_by_id: dict[str, dict[str, object]] = {}
    attestation_by_slot: dict[str, dict[str, object]] = {}
    plan_slots = {item["slot_id"]: item for item in plan_value["slots"]}
    for index, document in enumerate(attestation_values):
        _verification_require(document["task_id"] == checked["task_id"], _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].task_id")
        _verification_require(document["proposal_id"] == checked["proposal_id"], _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].proposal_id")
        _verification_require_ref(document["quality_policy_plan_ref"], "quality-policy-plan/v1", plan_value, f"$.attestations[{index}].quality_policy_plan_ref")
        _verification_require_ref(document["final_observation_manifest_ref"], "observation-manifest/v1", final_value, f"$.attestations[{index}].final_observation_manifest_ref")
        _verification_require_companion_digest(document["quality_policy_plan_digest"], "quality-policy-plan/v1", plan_value, f"$.attestations[{index}].quality_policy_plan_digest")
        _verification_require_companion_digest(document["final_observation_manifest_digest"], "observation-manifest/v1", final_value, f"$.attestations[{index}].final_observation_manifest_digest")
        _verification_require(document["attestation_id"] not in attestation_by_id, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].attestation_id")
        _verification_require(document["slot_id"] in plan_slots, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].slot_id")
        _verification_require(document["slot_id"] not in attestation_by_slot, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].slot_id")
        _verification_require(
            _verification_requirement_ids(document["requirement_verdicts"]) == plan_slots[document["slot_id"]]["requirement_ids"],
            _VERIFICATION_CONTEXT_INVALID,
            f"$.attestations[{index}].requirement_verdicts",
        )
        attestation_by_id[document["attestation_id"]] = document
        attestation_by_slot[document["slot_id"]] = document

    sessions = [item["verifier_session_id"] for item in attestation_values]
    _verification_require(len(sessions) == len(set(sessions)), _VERIFICATION_CONTEXT_INVALID, "$.attestations")
    seen_manifest_slots: set[str] = set()
    for index, item in enumerate(checked["attestations"]):
        document = attestation_by_id.get(item["attestation_id"])
        _verification_require(document is not None, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].attestation_id")
        _verification_require(item["slot_id"] == document["slot_id"], _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].slot_id")
        _verification_require(item["slot_id"] in plan_slots, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].slot_id")
        _verification_require(item["slot_id"] not in seen_manifest_slots, _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].slot_id")
        _verification_require(item["run_status"] == document["run_status"], _VERIFICATION_CONTEXT_INVALID, f"$.attestations[{index}].run_status")
        _verification_require_ref(item["attestation_ref"], "verification-attestation/v1", document, f"$.attestations[{index}].attestation_ref")
        seen_manifest_slots.add(item["slot_id"])

    expected_requirement_ids = sorted(
        {
            requirement_id
            for slot in plan_value["slots"]
            for requirement_id in slot["requirement_ids"]
        }
    )
    _verification_require(
        [item["requirement_id"] for item in checked["requirement_aggregates"]] == expected_requirement_ids,
        _VERIFICATION_CONTEXT_INVALID,
        "$.requirement_aggregates",
    )
    for index, aggregate in enumerate(checked["requirement_aggregates"]):
        required_slot_ids = sorted(
            slot["slot_id"]
            for slot in plan_value["slots"]
            if aggregate["requirement_id"] in slot["requirement_ids"]
        )
        expected_attestation_ids, expected_verdict = _verification_aggregate_result(
            aggregate["requirement_id"],
            required_slot_ids,
            attestation_by_slot,
        )
        _verification_require(aggregate["required_slot_ids"] == required_slot_ids, _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].required_slot_ids")
        _verification_require(aggregate["attestation_ids"] == expected_attestation_ids, _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].attestation_ids")
        _verification_require(aggregate["verdict"] == expected_verdict, _VERIFICATION_CONTEXT_INVALID, f"$.requirement_aggregates[{index}].verdict")

    _verification_require_time_order([*(item["created_at"] for item in attestation_values), checked["created_at"]], "$.created_at")
    return checked
'''


__all__ = ["PYTHON_VERIFICATION_CONTEXT"]
