"""Python facade document-rule handlers for TaskResult and debug transport."""

from __future__ import annotations


PYTHON_RESULT = r'''
def _availability_ref_key(value: dict[str, object]) -> tuple[object, ...]:
    if value["availability"] != "available":
        return (value["availability"], value["reason_code"])
    return ("available",) + _ref_key(value["ref"])


def _rule_availability_ref(value: dict[str, object]) -> None:
    if value["availability"] == "available":
        _require(set(value) == {"availability", "ref"}, "AVAILABILITY_REF_SHAPE_INVALID")
    else:
        _require(set(value) == {"availability", "reason_code"}, "AVAILABILITY_REF_SHAPE_INVALID")


def _rule_reason_registry(value: dict[str, object]) -> None:
    _require(_sorted_unique(value["reasons"]), "REASON_REGISTRY_ORDER_INVALID")


def _rule_task_result(value: dict[str, object]) -> None:
    _require(len(value["summary"].encode("utf-8")) <= 4096, "TASK_RESULT_SUMMARY_LIMIT_INVALID")
    _require(
        value["terminal_task_version"] == value["published_from_version"] + 1,
        "TASK_RESULT_VERSION_SUCCESSOR_INVALID",
    )
    started = value["attempt_identity"]["kind"] == "started"
    owner_started = value["owner_identity"]["kind"] == "started"
    _require(started == owner_started, "TASK_RESULT_IDENTITY_MATRIX_INVALID")
    if value["outcome"] in {"COMPLETED", "NO_CHANGE_NEEDED", "COMPLETED_WITH_WAIVERS", "PARTIAL"}:
        _require(started and owner_started, "TASK_RESULT_IDENTITY_MATRIX_INVALID")
    results = value["requirement_results"]
    _require(_ordered_unique(results, lambda item: item["requirement_id"]), "TASK_RESULT_REQUIREMENT_ORDER_INVALID")
    for index, item in enumerate(results):
        for field in ("evidence_refs", "attestation_refs", "waiver_refs"):
            _require(_ordered_unique(item[field], _ref_key), "TASK_RESULT_REFERENCE_ORDER_INVALID", f"$.requirement_results[{index}].{field}")
    _require(_ordered_unique(value["execution_states"], _availability_ref_key), "TASK_RESULT_EXECUTION_ORDER_INVALID")
    _require(_ordered_unique(value["artifact_refs"], _artifact_ref_key), "TASK_RESULT_ARTIFACT_ORDER_INVALID")
    _require(_sorted_unique(value["provenance"]["attempt_ids"]), "TASK_RESULT_ATTEMPT_ORDER_INVALID")
    verify_content_ref_set(
        [
            item
            for result in results
            for field in ("evidence_refs", "attestation_refs", "waiver_refs")
            for item in result[field]
        ]
    )
    _require(
        value["evidence_closure_digest"] == value["evidence_closure_ref"]["sha256"],
        "TASK_RESULT_EVIDENCE_CLOSURE_DIGEST_INVALID",
    )
    if value["outcome"] == "NO_CHANGE_NEEDED":
        _require(value["change_set_ref"] is None, "TASK_RESULT_NO_CHANGE_SET_INVALID")
        _require(
            value["original_source_state"]["availability"] == "available"
            and value["final_source_state"]["availability"] == "available"
            and _json_equal(
                value["original_source_state"]["ref"],
                value["final_source_state"]["ref"],
            ),
            "TASK_RESULT_NO_CHANGE_STATE_INVALID",
        )
    _require(value["created_at"] <= value["terminal_at"], "TASK_RESULT_TIME_ORDER_INVALID")


def _task_result_core_projection(value: dict[str, object]) -> dict[str, object]:
    projected = json.loads(canonical_document_bytes(value).decode("utf-8"))
    projected["schema_id"] = "task-result-core/v1"
    projected["diagnostics"].pop("worker_debug_fragment", None)
    return projected


def _rule_task_result_core(value: dict[str, object]) -> None:
    _rule_task_result(value)
    _require(value["diagnostics"] == {}, "TASK_RESULT_CORE_DEBUG_FIELD_INVALID")


def _rule_worker_debug_file_manifest(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(_ordered_unique(entries, lambda item: item["path"]), "DEBUG_FILE_MANIFEST_ORDER_INVALID")
    _require(value["entry_count"] == len(entries), "DEBUG_FILE_MANIFEST_COUNT_INVALID")
    _require(value["total_size_bytes"] == sum(item["size_bytes"] for item in entries), "DEBUG_FILE_MANIFEST_SIZE_INVALID")
    media = {
        "agent-events.jsonl": "application/x-ndjson",
        "codex-events.jsonl": "application/x-ndjson",
        "gateway-events.jsonl": "application/x-ndjson",
        "progress.log.jsonl": "application/x-ndjson",
        "task-events.jsonl": "application/x-ndjson",
        "worker.log.jsonl": "application/x-ndjson",
    }
    for index, item in enumerate(entries):
        expected = media.get(item["path"], "application/json")
        _require(item["media_type"] == expected, "DEBUG_FILE_MEDIA_TYPE_INVALID", f"$.entries[{index}]")


def _rule_worker_debug_redaction(value: dict[str, object]) -> None:
    structured = value["structured_pass_detection_count"]
    rescanned = value["archive_rescan_detection_count"]
    redacted = value["redacted_value_count"]
    _require(rescanned == 0, "DEBUG_REDACTION_RESCAN_FAILED", code="DEBUG_REDACTION_FAILED")
    _require(structured == redacted, "DEBUG_REDACTION_COUNT_INVALID", code="DEBUG_REDACTION_FAILED")
    if value["status"] == "clean":
        _require(structured == 0, "DEBUG_REDACTION_STATUS_INVALID")
    else:
        _require(structured >= 1, "DEBUG_REDACTION_STATUS_INVALID")


def _worker_debug_fragment_identity(value: dict[str, object]) -> str:
    identity = {
        "task_id": value["task_id"],
        "job_id": value["job_id"],
        "run_id": value["run_id"],
        "lease_id": value["lease_id"],
        "transport_attempt_id": value["transport_attempt_id"],
        "transport_epoch": value["transport_epoch"],
        "native_attempt_id": value["native_attempt_id"],
        "native_epoch": value["native_epoch"],
        "capture_kind": value["capture_kind"],
        "snapshot_seq": value["snapshot_seq"],
        "file_manifest_digest": value["file_manifest_ref"]["sha256"],
    }
    raw = (
        b"pullwise:worker-debug-fragment-id/v1\0"
        + canonical_document_bytes(identity)
    )
    return "frag_" + hashlib.sha256(raw).hexdigest()


def _rule_worker_debug_fragment(value: dict[str, object]) -> None:
    _require(value["fragment_id"] == _worker_debug_fragment_identity(value), "DEBUG_FRAGMENT_ID_INVALID")
    _require(value["last_server_acked_event_seq"] <= value["local_event_seq"], "DEBUG_EVENT_SEQUENCE_INVALID")
    terminal = value["capture_kind"] == "terminal"
    core = value["task_result_core"]
    if terminal:
        _require(core["availability"] == "available", "DEBUG_TERMINAL_CORE_REQUIRED")
    else:
        _require(
            core == {
                "availability": "not_applicable",
                "reason_code": "TASK_RESULT_CORE_NOT_APPLICABLE",
            },
            "DEBUG_NONTERMINAL_CORE_INVALID",
        )
    if value["status"] == "complete":
        _require(value["reason_code"] is None, "DEBUG_FRAGMENT_REASON_INVALID")
    else:
        _require(value["reason_code"] in {"DEBUG_LIMIT_EXCEEDED", "DEBUG_UNAVAILABLE"}, "DEBUG_FRAGMENT_REASON_INVALID")


def _rule_worker_debug_descriptor(value: dict[str, object]) -> None:
    uploaded = value["state"] == "uploaded"
    if uploaded:
        _require(
            value["transport_kind"] == "server_transport"
            and value["server_fragment_ref"] is not None
            and value["server_receipt_ref"] is not None
            and value["reason_code"] is None,
            "DEBUG_DESCRIPTOR_BINDING_INVALID",
        )
        _require(
            value["server_fragment_ref"]["sha256"] == value["fragment_ref"]["sha256"],
            "DEBUG_DESCRIPTOR_FRAGMENT_MISMATCH",
        )
    else:
        _require(
            value["transport_kind"] == "none"
            and value["server_fragment_ref"] is None
            and value["server_receipt_ref"] is None
            and value["reason_code"] == "DEBUG_UPLOAD_FAILED",
            "DEBUG_DESCRIPTOR_BINDING_INVALID",
        )
    _require(value["source_sha256"] == value["fragment_ref"]["sha256"], "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID")


def _rule_transport_ack(value: dict[str, object]) -> None:
    _require(value["terminal_task_version"] == value["published_from_version"] + 1, "TRANSPORT_ACK_VERSION_INVALID")
    bound = value["receipt_binding_state"] == "bound"
    _require((value["receipt_digest"] is not None) == bound, "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID")


def _rule_transport_envelope(value: dict[str, object]) -> None:
    result = validate_document("task-result/v1", value["task_result"])
    result_bytes = canonical_document_bytes(result)
    _require(hashlib.sha256(result_bytes).hexdigest() == value["task_result_digest"], "TRANSPORT_ENVELOPE_DIGEST_INVALID", code="TRANSPORT_ENVELOPE_DIGEST_INVALID")
    core = validate_document("task-result-core/v1", _task_result_core_projection(result))
    core_bytes = canonical_document_bytes(core)
    core_digest = hashlib.sha256(core_bytes).hexdigest()
    _require(value["task_result_core_digest"] == core_digest, "TRANSPORT_CORE_DIGEST_INVALID")
    core_ref = value["task_result_core_ref"]
    _require(
        core_ref["sha256"] == core_digest and core_ref["size_bytes"] == len(core_bytes),
        "TRANSPORT_CORE_REF_INVALID",
    )
    authority, fence = value["authority"], value["full_fence"]
    exact = (
        "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
        "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
    )
    _require(all(authority[key] == fence[key] for key in exact), "TRANSPORT_AUTHORITY_FENCE_INVALID")
    _require(authority["task_id"] == result["task_id"], "TRANSPORT_RESULT_TASK_INVALID")
    _require(authority["task_version"] == result["published_from_version"], "TRANSPORT_RESULT_VERSION_INVALID")
    _require(_json_equal(value["package"], authority["package"]), "TRANSPORT_PACKAGE_INVALID")
    debug = result["diagnostics"]["worker_debug_fragment"]
    descriptor, receipt = value["worker_debug_descriptor"], value["transport_receipt"]
    if debug["availability"] == "available":
        _require(descriptor is not None, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED")
        _require(_json_equal(debug["ref"], descriptor["fragment_ref"]), "TRANSPORT_DEBUG_REF_INVALID")
        if descriptor["state"] == "uploaded":
            _require(receipt["availability"] == "available", "TRANSPORT_RECEIPT_REQUIRED")
            _require(_json_equal(receipt["ref"], descriptor["server_receipt_ref"]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", code="TRANSPORT_RECEIPT_BINDING_CONFLICT")
        else:
            _require(receipt == {"availability": "not_applicable", "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE"}, "TRANSPORT_RECEIPT_MATRIX_INVALID")
    else:
        _require(descriptor is None, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID")
        _require(receipt == {"availability": "not_applicable", "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE"}, "TRANSPORT_RECEIPT_MATRIX_INVALID")
'''


__all__ = ["PYTHON_RESULT"]
