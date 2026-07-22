"""Python facade document-rule handlers for result/debug transport documents."""

from __future__ import annotations


PYTHON_RESULT_RULES = r'''
def _result_availability_reasons() -> list[str]:
    enums = [
        branch["properties"]["reason_code"]["enum"]
        for branch in schema("availability-ref/v1")["oneOf"]
        if "reason_code" in branch.get("properties", {})
    ]
    _seo_require(
        len(enums) == 2 and enums[0] == enums[1],
        "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID",
        "$",
    )
    return sorted(enums[0])


def _result_outcome_reasons() -> list[str]:
    reasons: set[str] = set()
    for branch in schema("task-result/v1")["oneOf"]:
        rule = schema(branch["$ref"])["properties"]["reason_code"]
        reasons.update([rule["const"]] if "const" in rule else rule["enum"])
    return sorted(reasons)


def _result_outcome_text_valid(value: object) -> bool:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        return False
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return 1 <= size <= 4096


def _result_outcome_order(valid: bool, path: str) -> None:
    _seo_require(valid, "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", path)


def _result_nondecreasing(values: list[object], key) -> bool:
    keys = [key(item) for item in values]
    return keys == sorted(keys)


def _result_ordered_unique(values: list[object], key) -> bool:
    keys = [key(item) for item in values]
    return all(left < right for left, right in zip(keys, keys[1:]))


def _result_sorted_unique(values: list[object]) -> bool:
    return _result_ordered_unique(values, lambda item: item)


def _result_canonical_unique(values: list[object]) -> bool:
    canonical = [canonical_document_bytes(item) for item in values]
    return len(canonical) == len(set(canonical))


def _result_nondecreasing_canonical_unique(values: list[object], key) -> bool:
    return _result_nondecreasing(values, key) and _result_canonical_unique(values)


def _result_validate_delivered_scope(items: list[dict[str, object]], path: str) -> None:
    for index, item in enumerate(items):
        _seo_require(_result_outcome_text_valid(item["statement"]), "TASK_RESULT_OUTCOME_TEXT_INVALID", f"{path}[{index}].statement")
        _result_outcome_order(_result_sorted_unique(item["requirement_ids"]), f"{path}[{index}].requirement_ids")
        _result_outcome_order(_result_ordered_unique(item["artifact_refs"], _artifact_ref_key), f"{path}[{index}].artifact_refs")
    _result_outcome_order(
        _result_nondecreasing_canonical_unique(items, lambda item: item["statement"].encode("utf-8", errors="strict")),
        path,
    )


def _result_validate_outcome_details(details: dict[str, object]) -> None:
    path = "$.outcome_details"
    if "delivered_scope" in details:
        _result_validate_delivered_scope(details["delivered_scope"], f"{path}.delivered_scope")
    for field in ("satisfaction_observation_ids", "waiver_ids"):
        if field in details:
            _result_outcome_order(_result_sorted_unique(details[field]), f"{path}.{field}")
    for field in ("original_verdicts", "gaps"):
        if field in details:
            _result_outcome_order(_result_ordered_unique(details[field], lambda item: item["requirement_id"]), f"{path}.{field}")
    if "residual_risks" in details:
        risks = details["residual_risks"]
        for index, item in enumerate(risks):
            _seo_require(_result_outcome_text_valid(item["statement"]), "TASK_RESULT_OUTCOME_TEXT_INVALID", f"{path}.residual_risks[{index}].statement")
            _result_outcome_order(_result_sorted_unique(item["evidence_ids"]), f"{path}.residual_risks[{index}].evidence_ids")
        _result_outcome_order(_result_ordered_unique(risks, lambda item: item["risk_id"]), f"{path}.residual_risks")
    if "blockers" in details:
        blockers = details["blockers"]
        for index, item in enumerate(blockers):
            _seo_require(_result_outcome_text_valid(item["unblock_condition"]), "TASK_RESULT_OUTCOME_TEXT_INVALID", f"{path}.blockers[{index}].unblock_condition")
            _result_outcome_order(_result_sorted_unique(item["requirement_ids"]), f"{path}.blockers[{index}].requirement_ids")
        _result_outcome_order(
            _result_nondecreasing_canonical_unique(
                blockers,
                lambda item: (item["code"], item["requirement_ids"][0] if item["requirement_ids"] else "", item["unblock_condition"]),
            ),
            f"{path}.blockers",
        )
    if "failures" in details:
        failures = details["failures"]
        for index, item in enumerate(failures):
            _result_outcome_order(_result_ordered_unique(item["evidence_refs"], _ref_key), f"{path}.failures[{index}].evidence_refs")
        _result_outcome_order(
            _result_nondecreasing_canonical_unique(
                failures,
                lambda item: (item["code"], item["evidence_refs"][0]["artifact_id"]),
            ),
            f"{path}.failures",
        )


def _result_instant(value: object) -> tuple[int, ...] | None:
    match = None if not isinstance(value, str) else re.fullmatch(
        r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z",
        value,
    )
    if match is None:
        return None
    parts = [int(match.group(index)) for index in range(1, 7)]
    try:
        datetime(*parts)
    except ValueError:
        return None
    return (*parts, int(((match.group(7) or "") + "000000000")[:9]))


def _result_time_leq(left: object, right: object) -> bool:
    earlier, later = _result_instant(left), _result_instant(right)
    return earlier is not None and later is not None and earlier <= later


def _availability_ref_key(value: dict[str, object]) -> tuple[object, ...]:
    return (value["availability"], value.get("reason_code")) if value["availability"] != "available" else ("available",) + _ref_key(value["ref"])


def _rule_availability_ref(value: dict[str, object]) -> None:
    _seo_require(set(value) == ({"availability", "ref"} if value["availability"] == "available" else {"availability", "reason_code"}), "AVAILABILITY_REF_SHAPE_INVALID")


def _rule_availability_reason_registry(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("availability-reason-registry/v1", value)
    _seo_require(value["reasons"] == _result_availability_reasons(), "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID", "$")


def _rule_task_result_outcome_reason_registry(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("task-result-outcome-reason-registry/v1", value)
    _seo_require(value["reasons"] == _result_outcome_reasons(), "TASK_RESULT_OUTCOME_REASON_REGISTRY_BIJECTION_INVALID", "$")


def _rule_task_result_complete(value: dict[str, object]) -> None:
    _seo_require(len(value["summary"].encode("utf-8")) <= 4096, "TASK_RESULT_SUMMARY_LIMIT_INVALID")
    _result_validate_outcome_details(value["outcome_details"])
    _seo_require(value["terminal_task_version"] == value["published_from_version"] + 1, "TASK_RESULT_VERSION_SUCCESSOR_INVALID")
    started = value["attempt_identity"]["kind"] == "started"
    _seo_require(started == (value["owner_identity"]["kind"] == "started"), "TASK_RESULT_IDENTITY_MATRIX_INVALID")
    if value["outcome"] in {"COMPLETED", "NO_CHANGE_NEEDED", "COMPLETED_WITH_WAIVERS", "PARTIAL"}:
        _seo_require(started, "TASK_RESULT_IDENTITY_MATRIX_INVALID")
    results = value["requirement_results"]
    _seo_require(_ordered_unique(results, lambda item: item["requirement_id"]), "TASK_RESULT_REQUIREMENT_ORDER_INVALID")
    for index, item in enumerate(results):
        for field in ("evidence_refs", "attestation_refs", "waiver_refs"):
            _seo_require(_ordered_unique(item[field], _ref_key), "TASK_RESULT_REFERENCE_ORDER_INVALID", f"$.requirement_results[{index}].{field}")
    _seo_require(_ordered_unique(value["execution_states"], _availability_ref_key), "TASK_RESULT_EXECUTION_ORDER_INVALID")
    _seo_require(_ordered_unique(value["artifact_refs"], _artifact_ref_key), "TASK_RESULT_ARTIFACT_ORDER_INVALID")
    _seo_require(_sorted_unique(value["provenance"]["attempt_ids"]), "TASK_RESULT_ATTEMPT_ORDER_INVALID")
    verify_content_ref_set([item for result in results for field in ("evidence_refs", "attestation_refs", "waiver_refs") for item in result[field]])
    _seo_require(value["evidence_closure_digest"] == value["evidence_closure_ref"]["sha256"], "TASK_RESULT_EVIDENCE_CLOSURE_DIGEST_INVALID")
    if value["outcome"] == "NO_CHANGE_NEEDED":
        _seo_require(value["change_set_ref"] is None, "TASK_RESULT_NO_CHANGE_SET_INVALID")
        _seo_require(value["original_source_state"]["availability"] == "available" and value["final_source_state"]["availability"] == "available" and _json_equal(value["original_source_state"]["ref"], value["final_source_state"]["ref"]), "TASK_RESULT_NO_CHANGE_STATE_INVALID")
    _seo_require(_result_time_leq(value["created_at"], value["terminal_at"]), "TASK_RESULT_TIME_ORDER_INVALID", "$.terminal_at")


def _rule_task_result_core(value: dict[str, object]) -> None:
    _rule_task_result_complete(value)
    _seo_require(value["diagnostics"] == {}, "TASK_RESULT_CORE_DEBUG_FIELD_INVALID")


def _rule_worker_debug_file_manifest(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("worker-debug-file-manifest/v1", value)
    entries = value["entries"]
    _seo_require(_ordered_unique(entries, lambda item: item["path"]), "DEBUG_FILE_MANIFEST_ORDER_INVALID")
    _seo_require(value["entry_count"] == len(entries), "DEBUG_FILE_MANIFEST_COUNT_INVALID")
    _seo_require(value["total_size_bytes"] == sum(item["size_bytes"] for item in entries), "DEBUG_FILE_MANIFEST_SIZE_INVALID")
    for index, item in enumerate(entries):
        expected = "application/x-ndjson" if item["path"] in {"agent-events.jsonl", "codex-events.jsonl", "gateway-events.jsonl", "progress.log.jsonl", "task-events.jsonl", "worker.log.jsonl"} else "application/json"
        _seo_require(item["media_type"] == expected, "DEBUG_FILE_MEDIA_TYPE_INVALID", f"$.entries[{index}].media_type")


def _rule_worker_debug_redaction_report(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("worker-debug-redaction-report/v1", value)
    structured, rescanned, redacted = value["structured_pass_detection_count"], value["archive_rescan_detection_count"], value["redacted_value_count"]
    _seo_require(rescanned == 0, "DEBUG_REDACTION_RESCAN_FAILED")
    _seo_require(structured == redacted, "DEBUG_REDACTION_COUNT_INVALID")
    _seo_require((value["status"] == "clean" and structured == 0) or (value["status"] == "redacted" and structured >= 1), "DEBUG_REDACTION_STATUS_INVALID")


def _rule_worker_debug_descriptor(value: dict[str, object]) -> None:
    uploaded = value["state"] == "uploaded"
    _seo_require(uploaded == (value["transport_kind"] == "server_transport"), "DEBUG_DESCRIPTOR_BINDING_INVALID")
    _seo_require((value["server_fragment_ref"] is not None) == uploaded, "DEBUG_DESCRIPTOR_BINDING_INVALID")
    _seo_require((value["server_receipt_ref"] is not None) == uploaded, "DEBUG_DESCRIPTOR_BINDING_INVALID")
    _seo_require((value["reason_code"] is None) == uploaded, "DEBUG_DESCRIPTOR_BINDING_INVALID")
    if uploaded:
        _seo_require(value["server_fragment_ref"]["sha256"] == value["fragment_ref"]["sha256"] and value["server_fragment_ref"]["size_bytes"] == value["fragment_ref"]["size_bytes"], "DEBUG_DESCRIPTOR_FRAGMENT_MISMATCH")
    _seo_require(value["source_sha256"] == value["fragment_ref"]["sha256"], "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID")


def _rule_worker_debug_fragment(value: dict[str, object]) -> None:
    _seo_require(value["last_server_acked_event_seq"] <= value["local_event_seq"], "DEBUG_EVENT_SEQUENCE_INVALID")
    terminal = value["capture_kind"] == "terminal"
    if terminal:
        _seo_require(value["task_result_core"]["availability"] == "available", "DEBUG_TERMINAL_CORE_REQUIRED")
    else:
        _seo_require(value["task_result_core"] == {"availability": "not_applicable", "reason_code": "TASK_RESULT_CORE_NOT_APPLICABLE"}, "DEBUG_NONTERMINAL_CORE_INVALID")
    _seo_require((value["status"] == "complete" and value["reason_code"] is None) or (value["status"] == "partial" and value["reason_code"] in {"DEBUG_LIMIT_EXCEEDED", "DEBUG_UNAVAILABLE"}), "DEBUG_FRAGMENT_REASON_INVALID")


def _rule_task_result_transport_ack(value: dict[str, object]) -> None:
    _seo_verify_embedded_digest("task-result-transport-ack/v1", value)
    _seo_require(value["terminal_task_version"] == value["published_from_version"] + 1, "TRANSPORT_ACK_VERSION_INVALID")
    _seo_require((value["receipt_binding_state"] == "bound") == (value["receipt_digest"] is not None), "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID")


def _rule_task_result_transport_envelope(value: dict[str, object]) -> None:
    result = validate_document("task-result/v1", value["task_result"])
    result_bytes = canonical_document_bytes(result)
    _seo_require(hashlib.sha256(result_bytes).hexdigest() == value["task_result_digest"], "TRANSPORT_ENVELOPE_DIGEST_INVALID", code="TRANSPORT_ENVELOPE_DIGEST_INVALID")
    core = derive_task_result_core(result)
    core_bytes = canonical_document_bytes(core)
    _seo_require(value["task_result_core_digest"] == hashlib.sha256(core_bytes).hexdigest(), "TRANSPORT_CORE_DIGEST_INVALID")
    _seo_require(value["task_result_core_ref"]["sha256"] == value["task_result_core_digest"] and value["task_result_core_ref"]["size_bytes"] == len(core_bytes), "TRANSPORT_CORE_REF_INVALID")
    authority, fence = value["authority"], value["full_fence"]
    exact = ("task_id", "attempt_id", "session_id", "owner_id", "lease_id", "task_version", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch")
    for key in exact:
        _seo_require(authority[key] == fence[key], "TRANSPORT_AUTHORITY_FENCE_INVALID", f"$.full_fence.{key}")
    _seo_require(authority["task_version"] == result["published_from_version"], "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version")
    _seo_require(authority["task_id"] == result["task_id"], "TRANSPORT_RESULT_TASK_INVALID")
    _seo_require(_json_equal(value["package"], authority["package"]), "TRANSPORT_PACKAGE_INVALID")
    debug = result["diagnostics"]["worker_debug_fragment"]
    descriptor, receipt = value["worker_debug_descriptor"], value["transport_receipt"]
    not_applicable = {"availability": "not_applicable", "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE"}
    if debug["availability"] == "available":
        _seo_require(descriptor is not None, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED")
        receipt_valid = receipt.get("availability") == "available" if descriptor["state"] == "uploaded" else receipt == not_applicable
        _seo_require(receipt_valid, "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt")
    else:
        _seo_require(descriptor is None, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID")
        _seo_require(receipt == not_applicable, "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt")
'''


__all__ = ["PYTHON_RESULT_RULES"]
