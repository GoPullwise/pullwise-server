"""Python facade contextual helpers for task-control authority semantics."""

from __future__ import annotations


PYTHON_TASK_CONTROL_HELPERS = r'''
def validate_task_request_acceptance(
    request: object,
    expected_task_id: str | None = None,
    expected_task_type: str | None = None,
) -> dict[str, object]:
    checked = validate_document("task-request/v1", request)
    _require(
        checked["submitted_by"]["kind"] in {"user_control", "server_control"},
        "TASK_REQUEST_SUBMITTER_INVALID",
    )
    if expected_task_id is not None:
        _require(
            checked["task_id"] == expected_task_id,
            "TASK_REQUEST_TASK_ID_INVALID",
            "$.task_id",
        )
    if expected_task_type is not None:
        _require(
            checked["task_type"] == expected_task_type,
            "TASK_REQUEST_TASK_TYPE_INVALID",
            "$.task_type",
        )
    interaction = checked["interaction_policy"]
    if interaction["mode"] == "unavailable":
        _require(
            interaction["input_deadline_ms"] == 0
            and interaction["approval_deadline_ms"] == 0,
            "TASK_REQUEST_INTERACTION_DEADLINE_INVALID",
            "$.interaction_policy",
        )
    return checked


def validate_effective_policy_derivation(
    request: object, policy: object
) -> dict[str, object]:
    accepted = validate_task_request_acceptance(request)
    effective = verify_document_digest(
        "effective-execution-policy/v1", policy
    )
    _require(
        effective["issuer"]["kind"] == "server_control",
        "POLICY_ISSUER_INVALID",
        "$.issuer",
        code="POLICY_INVARIANT_BROKEN",
    )
    _require(
        effective["task_type"] == accepted["task_type"],
        "POLICY_TASK_TYPE_DERIVATION_INVALID",
        "$.task_type",
    )
    _require(
        effective["issued_at"] >= accepted["submitted_at"],
        "POLICY_ISSUED_BEFORE_REQUEST",
        "$.issued_at",
    )
    requested = set(accepted["requested_capabilities"])
    granted = set(effective["granted_capabilities"])
    denied = {item["id"] for item in effective["denied_capabilities"]}
    _require(
        granted.issubset(requested),
        "POLICY_UNREQUESTED_CAPABILITY_GRANTED",
        "$.granted_capabilities",
        code="POLICY_INVARIANT_BROKEN",
    )
    _require(
        requested == granted.union(requested.intersection(denied)),
        "POLICY_REQUESTED_CAPABILITY_UNDECIDED",
        code="POLICY_INVARIANT_BROKEN",
    )
    for field, ceiling in accepted["requested_budgets"].items():
        if ceiling != 0:
            _require(
                effective["budgets"][field] <= ceiling,
                "POLICY_REQUEST_BUDGET_EXCEEDED",
                f"$.budgets.{field}",
                code="POLICY_INVARIANT_BROKEN",
            )
    _require(
        effective["interaction_mode"]
        == accepted["interaction_policy"]["mode"],
        "POLICY_INTERACTION_DERIVATION_INVALID",
        "$.interaction_mode",
        code="POLICY_INVARIANT_BROKEN",
    )
    return effective


def validate_requirement_entry_ingest(
    entry: object, previous_ledger: object | None = None
) -> dict[str, object]:
    checked = validate_document("requirement-entry/v1", entry)
    if previous_ledger is None:
        _require(
            checked["ledger_version"] == 1,
            "REQUIREMENT_INGEST_VERSION_INVALID",
            "$.ledger_version",
        )
        return checked
    previous = verify_document_digest(
        "requirement-ledger/v1", previous_ledger
    )
    _require(
        checked["ledger_version"] == previous["ledger_version"] + 1,
        "REQUIREMENT_INGEST_VERSION_INVALID",
        "$.ledger_version",
    )
    by_id = {item["requirement_id"]: item for item in previous["entries"]}
    _require(
        checked["requirement_id"] not in by_id,
        "REQUIREMENT_ID_COLLISION",
        "$.requirement_id",
        code="REQUIREMENT_ID_COLLISION",
    )
    _require(
        set(checked["parent_requirement_ids"]).issubset(by_id),
        "REQUIREMENT_PARENT_UNKNOWN",
        "$.parent_requirement_ids",
    )
    active = set(previous["active_requirement_ids"])
    for requirement_id in checked["supersedes"]:
        target = by_id.get(requirement_id)
        _require(
            requirement_id in active
            and target is not None
            and target["source_kind"] == "derived",
            "REQUIREMENT_SUPERSEDES_INVALID",
            "$.supersedes",
        )
    return checked


def validate_requirement_ledger_transition(
    previous_ledger: object, candidate_ledger: object
) -> dict[str, object]:
    previous = verify_document_digest(
        "requirement-ledger/v1", previous_ledger
    )
    candidate = verify_document_digest(
        "requirement-ledger/v1", candidate_ledger
    )
    _require(
        candidate["task_id"] == previous["task_id"],
        "REQUIREMENT_LEDGER_TASK_INVALID",
        "$.task_id",
    )
    _require(
        candidate["ledger_version"] == previous["ledger_version"] + 1,
        "REQUIREMENT_LEDGER_SUCCESSOR_INVALID",
        "$.ledger_version",
    )
    prefix_length = len(previous["entries"])
    _require(
        candidate["entries"][:prefix_length] == previous["entries"],
        "REQUIREMENT_LEDGER_HISTORY_MUTATED",
        "$.entries",
    )
    appended = candidate["entries"][prefix_length:]
    _require(bool(appended), "REQUIREMENT_LEDGER_APPEND_REQUIRED", "$.entries")
    _require(
        all(
            item["ledger_version"] == candidate["ledger_version"]
            for item in appended
        ),
        "REQUIREMENT_INGEST_VERSION_INVALID",
        "$.entries",
    )
    previous_ids = {item["requirement_id"] for item in previous["entries"]}
    appended_ids = [item["requirement_id"] for item in appended]
    _require(
        not previous_ids.intersection(appended_ids)
        and len(appended_ids) == len(set(appended_ids)),
        "REQUIREMENT_ID_COLLISION",
        "$.entries",
        code="REQUIREMENT_ID_COLLISION",
    )
    return candidate


def validate_task_charter_transition(
    previous_charter: object | None,
    candidate_charter: object,
    requirement_ledger: object | None = None,
) -> dict[str, object]:
    candidate = verify_document_digest("task-charter/v1", candidate_charter)
    if previous_charter is None:
        _require(
            candidate["charter_version"] == 1
            and candidate["previous_charter_ref"] is None,
            "CHARTER_PREDECESSOR_INVALID",
            "$.previous_charter_ref",
        )
    else:
        previous = verify_document_digest("task-charter/v1", previous_charter)
        predecessor = candidate["previous_charter_ref"]
        previous_bytes = canonical_document_bytes(previous)
        _require(
            candidate["task_id"] == previous["task_id"],
            "CHARTER_TASK_INVALID",
            "$.task_id",
        )
        _require(
            candidate["charter_version"] == previous["charter_version"] + 1,
            "CHARTER_VERSION_INVALID",
            "$.charter_version",
        )
        _require(
            predecessor is not None
            and predecessor["sha256"]
            == hashlib.sha256(previous_bytes).hexdigest()
            and predecessor["size_bytes"] == len(previous_bytes),
            "CHARTER_PREDECESSOR_INVALID",
            "$.previous_charter_ref",
        )
        _require(
            candidate["created_at"] >= previous["created_at"],
            "CHARTER_TIME_ORDER_INVALID",
            "$.created_at",
        )
    if requirement_ledger is not None:
        ledger = verify_document_digest(
            "requirement-ledger/v1", requirement_ledger
        )
        _require(
            ledger["task_id"] == candidate["task_id"],
            "CHARTER_LEDGER_TASK_INVALID",
            "$.task_id",
        )
        _require(
            set(candidate["requirement_ids"]).issubset(
                ledger["active_requirement_ids"]
            ),
            "CHARTER_REQUIREMENT_INACTIVE",
            "$.requirement_ids",
        )
    return candidate


def verify_waiver_event_authority(
    waiver: object, effective_policy: object, now: str
) -> dict[str, object]:
    event = validate_document("waiver-event/v1", waiver)
    policy = verify_document_digest(
        "effective-execution-policy/v1", effective_policy
    )
    now_millis = _timestamp_millis(now)
    issued = _timestamp_millis(event["issued_at"])
    expires = _timestamp_millis(event["expires_at"])
    _require(
        now_millis is not None
        and issued is not None
        and expires is not None
        and issued <= now_millis < expires,
        "WAIVER_TIME_INVALID",
        code="WAIVER_INVALID",
    )
    _require(
        event["policy_version"] == policy["policy_version"],
        "WAIVER_POLICY_VERSION_INVALID",
        code="WAIVER_INVALID",
    )
    _require(
        event["issuer"] in policy["authorized_waiver_issuers"],
        "WAIVER_ISSUER_NOT_AUTHORIZED",
        code="WAIVER_INVALID",
    )
    _fail(
        "WAIVER_SIGNATURE_AUTHORITY_UNAVAILABLE",
        code="WAIVER_INVALID",
    )


_TASK_CONTROL_ATTEMPT_TRANSITIONS = {
    "CREATED": {"LEASED", "FENCED"},
    "LEASED": {"PREPARING", "FAILED", "CANCELLED", "FENCED"},
    "PREPARING": {"RUNNING", "FAILED", "CANCELLED", "FENCED"},
    "RUNNING": {
        "VERIFYING", "SUSPENDING", "FAILED", "CANCELLED", "FENCED"
    },
    "VERIFYING": {
        "RUNNING", "SUSPENDING", "PUBLISHING", "FAILED", "CANCELLED",
        "FENCED",
    },
    "SUSPENDING": {"SUSPENDED"},
    "PUBLISHING": {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "FENCED"},
    "SUCCEEDED": set(),
    "SUSPENDED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "FENCED": set(),
}


def validate_attempt_transition(
    previous_attempt: object, candidate_attempt: object
) -> dict[str, object]:
    previous = validate_document("attempt-record/v1", previous_attempt)
    candidate = validate_document("attempt-record/v1", candidate_attempt)
    immutable = (
        "attempt_id", "task_id", "native_epoch", "transport_binding",
        "predecessor_checkpoint_generation", "owner_session_id",
    )
    _require(
        all(candidate[field] == previous[field] for field in immutable),
        "ATTEMPT_IDENTITY_MUTATED",
        code="AUTHORITY_FENCED",
    )
    _require(
        candidate["state_version"] == previous["state_version"] + 1,
        "ATTEMPT_STATE_VERSION_INVALID",
        "$.state_version",
        code="STATE_TRANSITION_INVALID",
    )
    _require(
        candidate["state"] in _TASK_CONTROL_ATTEMPT_TRANSITIONS[previous["state"]],
        "ATTEMPT_TRANSITION_INVALID",
        "$.state",
        code="STATE_TRANSITION_INVALID",
    )
    return candidate


_TASK_CONTROL_OWNER_TRANSITIONS = {
    "STARTING": {"ACTIVE", "FENCED"},
    "ACTIVE": {"CLOSED", "FENCED"},
    "CLOSED": set(),
    "FENCED": set(),
}


def validate_task_owner_transition(
    previous_owner: object, candidate_owner: object
) -> dict[str, object]:
    previous = validate_document("task-owner/v1", previous_owner)
    candidate = validate_document("task-owner/v1", candidate_owner)
    immutable = (
        "task_id", "owner_id", "owner_epoch", "session_id", "attempt_id",
        "native_epoch", "started_at",
    )
    _require(
        all(candidate[field] == previous[field] for field in immutable),
        "OWNER_IDENTITY_MUTATED",
        code="AUTHORITY_FENCED",
    )
    _require(
        candidate["state_version"] == previous["state_version"] + 1,
        "OWNER_STATE_VERSION_INVALID",
        "$.state_version",
        code="STATE_TRANSITION_INVALID",
    )
    _require(
        candidate["state"] in _TASK_CONTROL_OWNER_TRANSITIONS[previous["state"]],
        "OWNER_TRANSITION_INVALID",
        "$.state",
        code="STATE_TRANSITION_INVALID",
    )
    return candidate


_TASK_CONTROL_RECORD_TRANSITIONS = {
    "QUEUED": {"ACTIVE", "FINALIZING"},
    "ACTIVE": {"ACTIVE", "WAITING_INPUT", "WAITING_APPROVAL", "FINALIZING"},
    "WAITING_INPUT": {"QUEUED", "FINALIZING"},
    "WAITING_APPROVAL": {"QUEUED", "FINALIZING"},
    "FINALIZING": {"ACTIVE", "QUEUED", "FINALIZING", "TERMINAL"},
    "TERMINAL": set(),
}


def validate_task_record_transition(
    previous_record: object, candidate_record: object
) -> dict[str, object]:
    previous = validate_document("task-record/v1", previous_record)
    candidate = validate_document("task-record/v1", candidate_record)
    _require(
        candidate["task_version"] == previous["task_version"] + 1,
        "TASK_VERSION_STALE",
        "$.task_version",
        code="TASK_VERSION_STALE",
    )
    _require(
        candidate["lifecycle"]
        in _TASK_CONTROL_RECORD_TRANSITIONS[previous["lifecycle"]],
        "TASK_RECORD_TRANSITION_INVALID",
        "$.lifecycle",
        code="STATE_TRANSITION_INVALID",
    )
    immutable = (
        "task_id", "task_type", "request_ref", "request_digest",
        "policy_ref", "policy_digest", "policy_version", "protocol_mode",
        "owner_id", "absolute_deadline_at", "terminalization_reserve_ms",
        "created_at",
    )
    _require(
        all(candidate[field] == previous[field] for field in immutable),
        "TASK_RECORD_IDENTITY_MUTATED",
        code="AUTHORITY_FENCED",
    )
    _require(
        not (
            previous["desired_state"] == "CANCEL"
            and candidate["desired_state"] != "CANCEL"
        ),
        "TASK_RECORD_CANCEL_ROLLBACK_INVALID",
        code="STATE_TRANSITION_INVALID",
    )
    _require(
        candidate["deletion_version"] >= previous["deletion_version"],
        "TASK_RECORD_DELETION_VERSION_INVALID",
        code="STATE_TRANSITION_INVALID",
    )
    _require(
        candidate["native_epoch"] >= previous["native_epoch"],
        "TASK_RECORD_NATIVE_EPOCH_INVALID",
        code="NATIVE_EPOCH_STALE",
    )
    _require(
        candidate["owner_epoch"] >= previous["owner_epoch"],
        "TASK_RECORD_OWNER_EPOCH_INVALID",
        code="OWNER_EPOCH_STALE",
    )
    _require(
        candidate["updated_at"] >= previous["updated_at"],
        "TASK_RECORD_TIME_ORDER_INVALID",
        "$.updated_at",
    )
    return candidate


def validate_claim_write_set(
    previous_record: object,
    claimed_record: object,
    attempt_record: object,
    task_owner: object,
) -> dict[str, object]:
    previous = validate_document("task-record/v1", previous_record)
    claimed = validate_task_record_transition(previous, claimed_record)
    attempt = validate_document("attempt-record/v1", attempt_record)
    owner = validate_document("task-owner/v1", task_owner)
    _require(
        previous["lifecycle"] == "QUEUED"
        and claimed["lifecycle"] == "ACTIVE"
        and claimed["desired_state"] == "RUN",
        "CLAIM_TASK_STATE_INVALID",
        code="TASK_NOT_CLAIMABLE",
    )
    _require(
        claimed["native_epoch"] == previous["native_epoch"] + 1
        == attempt["native_epoch"]
        == owner["native_epoch"],
        "CLAIM_NATIVE_EPOCH_INVALID",
        code="NATIVE_EPOCH_STALE",
    )
    _require(
        claimed["owner_epoch"] == previous["owner_epoch"] + 1
        == owner["owner_epoch"],
        "CLAIM_OWNER_EPOCH_INVALID",
        code="OWNER_EPOCH_STALE",
    )
    exact = (
        claimed["task_id"] == attempt["task_id"] == owner["task_id"]
        and claimed["current_attempt_id"]
        == attempt["attempt_id"]
        == owner["attempt_id"]
        and claimed["owner_id"] == owner["owner_id"]
        and attempt["owner_session_id"] == owner["session_id"]
        and attempt["state"] == "LEASED"
        and owner["state"] == "STARTING"
    )
    _require(exact, "CLAIM_WRITE_SET_INVALID", code="AUTHORITY_FENCED")
    binding = attempt["transport_binding"]
    _require(
        binding["protocol_mode"] == claimed["protocol_mode"]
        and all(
            binding[field] == claimed[field]
            for field in ("outer_job_id", "run_id", "lease_id", "transport_epoch")
        ),
        "CLAIM_TRANSPORT_BINDING_INVALID",
        code="AUTHORITY_FENCED",
    )
    return claimed


def _task_control_normalized_instant(value: str) -> str:
    if "." not in value:
        return value
    prefix, fraction = value[:-1].split(".", 1)
    fraction = fraction.rstrip("0")
    return prefix + (("." + fraction) if fraction else "") + "Z"


def validate_task_result_publication(
    previous_record: object,
    terminal_record: object,
    task_result: object,
) -> dict[str, object]:
    previous = validate_document("task-record/v1", previous_record)
    terminal = validate_task_record_transition(previous, terminal_record)
    result = validate_document("task-result/v1", task_result)
    result_bytes = canonical_document_bytes(result)
    result_digest = hashlib.sha256(result_bytes).hexdigest()
    _require(
        previous["lifecycle"] == "FINALIZING"
        and terminal["lifecycle"] == "TERMINAL",
        "TASK_RESULT_PUBLICATION_STATE_INVALID",
        code="STATE_TRANSITION_INVALID",
    )
    _require(
        result["published_from_version"] == previous["task_version"]
        and result["terminal_task_version"] == terminal["task_version"],
        "TASK_RESULT_PUBLICATION_VERSION_INVALID",
        code="TASK_VERSION_STALE",
    )
    _require(
        result["task_id"] == terminal["task_id"]
        and result["task_type"] == terminal["task_type"]
        and result["outcome"] == terminal["outcome"],
        "TASK_RESULT_PUBLICATION_IDENTITY_INVALID",
    )
    _require(
        terminal["terminal_kind"] == "task_result"
        and terminal["result_digest"] == result_digest
        and terminal["result_ref"]["sha256"] == result_digest
        and terminal["result_ref"]["size_bytes"] == len(result_bytes),
        "TASK_RESULT_PUBLICATION_REF_INVALID",
    )
    _require(
        _task_control_normalized_instant(terminal["terminal_at"])
        == _task_control_normalized_instant(result["terminal_at"]),
        "TASK_RESULT_PUBLICATION_TIME_INVALID",
        "$.terminal_at",
    )
    _require(
        result["request_ref"] == terminal["request_ref"]
        and result["policy_ref"] == terminal["policy_ref"],
        "TASK_RESULT_PUBLICATION_CONTROL_REF_INVALID",
    )
    attempt = result["attempt_identity"]
    owner = result["owner_identity"]
    if attempt["kind"] == "started":
        _require(
            attempt["attempt_id"] == terminal["current_attempt_id"]
            and attempt["native_epoch"] == terminal["native_epoch"]
            and owner["owner_id"] == terminal["owner_id"]
            and owner["owner_epoch"] == terminal["owner_epoch"],
            "TASK_RESULT_PUBLICATION_AUTHORITY_INVALID",
            code="AUTHORITY_FENCED",
        )
    return terminal
'''


__all__ = ["PYTHON_TASK_CONTROL_HELPERS"]
