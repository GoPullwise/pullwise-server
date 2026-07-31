"""Python facade helpers for immutable Server/local Task version authority."""

from __future__ import annotations


PYTHON_TASK_VERSION_AUTHORITY = r'''
_TASK_VERSION_FENCE_FIELDS = (
    "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
    "task_version", "deletion_version", "owner_epoch", "native_epoch",
    "transport_epoch",
)
_TASK_VERSION_RECORD_AUTHORITY_FIELDS = (
    "task_id", "deletion_version", "owner_id", "owner_epoch", "native_epoch",
    "lease_id", "transport_epoch",
)


def _rule_task_control_event(value: dict[str, object]) -> None:
    _require(
        value["task_version"] == value["previous_task_version"] + 1,
        "TASK_CONTROL_EVENT_VERSION_INVALID",
        "$.task_version",
        code="TASK_VERSION_STALE",
    )


def _rule_task_version_authority_proof(value: dict[str, object]) -> None:
    chain = value["version_chain"]
    previous_version = chain[0]["previous_task_version"]
    phase = "checkpoint"
    for index, link in enumerate(chain):
        path = f"$.version_chain[{index}]"
        _require(
            link["previous_task_version"] == previous_version
            and link["task_version"] == previous_version + 1,
            "TASK_VERSION_AUTHORITY_CHAIN_INVALID",
            path + ".task_version",
            code="TASK_VERSION_STALE",
        )
        kind = link["transition_kind"]
        allowed = (
            kind == "checkpoint" and phase == "checkpoint"
            or kind == "terminalization_requested" and phase == "checkpoint"
            or kind == "task_result_published"
            and phase == "terminalization_requested"
            and index == len(chain) - 1
        )
        _require(
            allowed,
            "TASK_VERSION_AUTHORITY_PUBLICATION_ORDER_INVALID",
            path + ".transition_kind",
            code="STATE_TRANSITION_INVALID",
        )
        if kind != "checkpoint":
            phase = kind
        previous_version = link["task_version"]
    last = chain[-1]
    _require(
        phase == "task_result_published"
        and value["published_from_version"] == last["previous_task_version"]
        and value["terminal_task_version"] == last["task_version"],
        "TASK_VERSION_AUTHORITY_TERMINAL_BINDING_INVALID",
        "$.terminal_task_version",
    )


def _task_version_ref_matches(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> bool:
    raw = canonical_document_bytes(document)
    return bool(
        ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def _task_version_authority_binding(
    document: dict[str, object],
    authority_value: object,
) -> dict[str, object]:
    authority = verify_document_digest(
        "server-authority-envelope/v1", authority_value
    )
    fence = document["full_fence"]
    _require(
        all(fence[field] == authority[field] for field in _TASK_VERSION_FENCE_FIELDS),
        "TASK_VERSION_AUTHORITY_FENCE_INVALID",
        next(
            (
                f"$.full_fence.{field}"
                for field in _TASK_VERSION_FENCE_FIELDS
                if fence[field] != authority[field]
            ),
            "$.full_fence",
        ),
        code="CONTRACT_DOCUMENT_INVALID",
    )
    verify_document_digest("task-fence/v1", fence)
    _require(
        _json_equal(document["package"], authority["package"])
        and document["authority_digest"] == authority["authority_digest"]
        and document["grant_digest"] == authority["grant"]["grant_digest"]
        and document["task_id"] == authority["task_id"],
        "TASK_VERSION_AUTHORITY_BINDING_INVALID",
        "$.authority_digest",
        code="CONTRACT_DOCUMENT_INVALID",
    )
    return authority


def _task_version_checked_input(
    schema_id: str, value: object
) -> dict[str, object]:
    return (
        verify_document_digest(schema_id, value)
        if isinstance(schema(schema_id).get("x-pullwise-digest"), dict)
        else validate_document(schema_id, value)
    )


def verify_task_control_event_context(
    event: object,
    authority: object,
    previous_record: object,
    task_record: object,
    input_document: object,
) -> dict[str, object]:
    checked = verify_document_digest("task-control-event/v1", event)
    bound_authority = _task_version_authority_binding(checked, authority)
    previous = validate_document("task-record/v1", previous_record)
    current = validate_task_record_transition(previous, task_record)
    kind = checked["event_kind"]
    input_schema = (
        "terminalization-input-snapshot/v1"
        if kind == "terminalization_requested"
        else "task-result/v1"
    )
    input_value = _task_version_checked_input(input_schema, input_document)
    _require(
        checked["previous_task_version"] == previous["task_version"]
        and checked["task_version"] == current["task_version"]
        == previous["task_version"] + 1,
        "TASK_CONTROL_EVENT_VERSION_INVALID",
        "$.task_version",
        code="TASK_VERSION_STALE",
    )
    _require(
        _task_version_ref_matches(
            checked["input_ref"], input_schema, input_value
        )
        and _task_version_ref_matches(
            checked["previous_task_record_ref"], "task-record/v1", previous
        )
        and _task_version_ref_matches(
            checked["task_record_ref"], "task-record/v1", current
        ),
        "TASK_CONTROL_EVENT_REF_INVALID",
        "$.input_ref",
        code="CAS_CORRUPT",
    )
    identity = all(
        previous[field] == current[field] == bound_authority[field]
        for field in _TASK_VERSION_RECORD_AUTHORITY_FIELDS
    )
    identity = (
        identity
        and previous["current_attempt_id"]
        == current["current_attempt_id"]
        == bound_authority["attempt_id"]
        and checked["occurred_at"] == current["updated_at"]
    )
    _require(
        identity,
        "TASK_CONTROL_EVENT_AUTHORITY_INVALID",
        "$.task_id",
        code="AUTHORITY_FENCED",
    )
    if kind == "terminalization_requested":
        terminal_fields = (
            "terminal_kind", "result_ref", "result_digest", "outcome",
            "terminal_at",
        )
        exact = (
            input_value["task_id"] == current["task_id"]
            and input_value["task_version"] == current["task_version"]
            and input_value["deletion_version"] == current["deletion_version"]
            and input_value["attempt_id"] == current["current_attempt_id"]
            and input_value["native_epoch"] == current["native_epoch"]
            and input_value["owner_id"] == current["owner_id"]
            and input_value["owner_epoch"] == current["owner_epoch"]
            and input_value["lease_id"] == current["lease_id"]
            and input_value["lifecycle"] == current["lifecycle"] == "FINALIZING"
            and input_value["desired_state"] == current["desired_state"] == "RUN"
            and all(current[field] is None for field in terminal_fields)
        )
        _require(
            exact,
            "TASK_CONTROL_FINALIZING_INVALID",
            "$.input_ref",
            code="STATE_TRANSITION_INVALID",
        )
    else:
        validate_task_result_publication(previous, current, input_value)
    return checked


def verify_task_version_authority_proof(
    proof: object,
    authority: object,
    task_result: object,
) -> dict[str, object]:
    checked = verify_document_digest(
        "task-version-authority-proof/v1", proof
    )
    bound_authority = _task_version_authority_binding(checked, authority)
    result = validate_document("task-result/v1", task_result)
    _require(
        _task_version_ref_matches(
            checked["task_result_ref"], "task-result/v1", result
        ),
        "TASK_VERSION_AUTHORITY_RESULT_REF_INVALID",
        "$.task_result_ref",
        code="CAS_CORRUPT",
    )
    previous_version = bound_authority["task_version"]
    transition_refs: set[bytes] = set()
    task_refs = {
        canonical_document_bytes(checked["base_task_record_ref"])
    }
    requested_count = 0
    chain = checked["version_chain"]
    for index, link in enumerate(chain):
        path = f"$.version_chain[{index}]"
        _require(
            link["previous_task_version"] == previous_version
            and link["task_version"] == previous_version + 1,
            "TASK_VERSION_AUTHORITY_CHAIN_INVALID",
            path + ".task_version",
            code="TASK_VERSION_STALE",
        )
        transition_ref = canonical_document_bytes(link["transition_ref"])
        task_ref = canonical_document_bytes(link["task_record_ref"])
        _require(
            transition_ref not in transition_refs and task_ref not in task_refs,
            "TASK_VERSION_AUTHORITY_CHAIN_DUPLICATE",
            path,
            code="CAS_CORRUPT",
        )
        transition_refs.add(transition_ref)
        task_refs.add(task_ref)
        kind = link["transition_kind"]
        requested_count += int(kind == "terminalization_requested")
        _require(
            kind != "task_result_published" or index == len(chain) - 1,
            "TASK_VERSION_AUTHORITY_PUBLICATION_ORDER_INVALID",
            path + ".transition_kind",
            code="STATE_TRANSITION_INVALID",
        )
        previous_version = link["task_version"]
    last = chain[-1]
    exact = (
        requested_count >= 1
        and last["transition_kind"] == "task_result_published"
        and checked["published_from_version"] == last["previous_task_version"]
        == result["published_from_version"]
        and checked["terminal_task_version"] == last["task_version"]
        == result["terminal_task_version"]
        and result["task_id"] == checked["task_id"]
    )
    _require(
        exact,
        "TASK_VERSION_AUTHORITY_TERMINAL_BINDING_INVALID",
        "$.terminal_task_version",
        code="CONTRACT_DOCUMENT_INVALID",
    )
    return checked
'''


__all__ = ["PYTHON_TASK_VERSION_AUTHORITY"]
