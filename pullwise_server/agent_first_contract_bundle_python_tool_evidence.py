"""Executable Python facade semantics for current R0 tool evidence."""

from __future__ import annotations


PYTHON_TOOL_EVIDENCE = r'''
def _valid_tool_source_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    if value.startswith("/") or len(value.encode("utf-8")) > 4096:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _rule_agent_tool_request(value: dict[str, object]) -> None:
    _require(
        _valid_tool_source_path(value["tool_input"]["relative_path"]),
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )


def _rule_local_tool_receipt(value: dict[str, object]) -> None:
    _verify_embedded_digest("local-tool-receipt/v1", value)
    started = _timestamp_millis(value["started_at"])
    completed = _timestamp_millis(value["completed_at"])
    _require(
        started is not None
        and completed is not None
        and completed >= started
        and value["elapsed_ms"] == completed - started,
        "LOCAL_RECEIPT_TIMING_INVALID",
    )


def _rule_r0_read_payload(value: dict[str, object]) -> None:
    _verify_embedded_digest("r0-read-payload/v1", value)
    _require(
        _valid_tool_source_path(value["relative_path"]),
        "TOOL_SOURCE_PATH_INVALID",
        "$.relative_path",
    )


def _rule_r0_read_result(value: dict[str, object]) -> None:
    _verify_embedded_digest("r0-read-result/v1", value)
    _require(
        value["source_state_before_id"] == value["source_state_after_id"],
        "SOURCE_STATE_CHANGED",
    )


def _rule_source_content(value: dict[str, object]) -> None:
    raw = _decode_canonical_base64(value["data_base64"], "$.data_base64")
    _require(
        len(raw) == value["size_bytes"],
        "SOURCE_CONTENT_SIZE_MISMATCH",
        "$.size_bytes",
    )
    _require(
        hashlib.sha256(raw).hexdigest() == value["byte_sha256"],
        "SOURCE_CONTENT_SHA256_MISMATCH",
        "$.byte_sha256",
    )
    _verify_embedded_digest("source-content/v1", value)


def _rule_source_state(value: dict[str, object]) -> None:
    _verify_embedded_digest("source-state/v1", value)


def _rule_tool_catalog(value: dict[str, object]) -> None:
    _verify_embedded_digest("tool-catalog/v1", value)
    _require(
        _ordered_unique(value["tools"], lambda item: item["tool_key"]),
        "TOOL_CATALOG_ORDER_INVALID",
    )


def _rule_tool_dispatch_capability(value: dict[str, object]) -> None:
    _verify_embedded_digest("tool-dispatch-capability/v1", value)
    _require(
        _timestamp_millis(value["issued_at"]) is not None,
        "TOOL_CAPABILITY_TIME_INVALID",
        "$.issued_at",
    )


def _rule_tool_dispatch_intent(value: dict[str, object]) -> None:
    _verify_embedded_digest("tool-dispatch-intent/v1", value)
    _require(
        _valid_tool_source_path(value["tool_input"]["relative_path"]),
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )
    _require(
        _timestamp_millis(value["created_at"]) is not None,
        "TOOL_INTENT_TIME_INVALID",
        "$.created_at",
    )


def _rule_tool_invocation(value: dict[str, object]) -> None:
    _verify_embedded_digest("tool-invocation/v1", value)
    _require(
        _valid_tool_source_path(value["tool_input"]["relative_path"]),
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )


def _tool_exact(left: dict[str, object], right: dict[str, object], fields) -> bool:
    return all(_json_equal(left[field], right[field]) for field in fields)


def validate_tool_invocation_binding(
    request: object, invocation: object, catalog: object
) -> bool:
    checked_request = validate_document("agent-tool-request/v1", request)
    checked_invocation = verify_document_digest("tool-invocation/v1", invocation)
    checked_catalog = verify_document_digest("tool-catalog/v1", catalog)
    descriptor = next(
        (
            item
            for item in checked_catalog["tools"]
            if item["tool_key"] == checked_request["tool_key"]
        ),
        None,
    )
    if descriptor is None:
        _fail("TOOL_INVOCATION_BINDING_INVALID")
    _require(
        descriptor["request_schema_id"] == checked_request["schema_id"]
        and _tool_exact(
            checked_request,
            checked_invocation,
            ("idempotency_key", "tool_key", "tool_input"),
        ),
        "TOOL_INVOCATION_BINDING_INVALID",
    )
    return True


def validate_tool_journal_begin(
    invocation: object, intent: object, capability: object
) -> bool:
    checked_invocation = verify_document_digest("tool-invocation/v1", invocation)
    checked_intent = verify_document_digest("tool-dispatch-intent/v1", intent)
    checked_capability = verify_document_digest(
        "tool-dispatch-capability/v1", capability
    )
    exact = (
        "package", "authority_digest", "grant_digest", "invocation_digest",
        "task_id", "idempotency_key", "tool_key", "tool_input",
    )
    _require(
        _tool_exact(checked_intent, checked_invocation, exact),
        "TOOL_INTENT_BINDING_INVALID",
    )
    _require(
        _json_equal(checked_capability["package"], checked_intent["package"])
        and checked_capability["intent_digest"] == checked_intent["intent_digest"]
        and checked_capability["capability_digest"]
        == checked_intent["capability_digest"]
        and checked_capability["issued_at"] == checked_intent["created_at"],
        "TOOL_CAPABILITY_BINDING_INVALID",
    )
    return True


def validate_tool_capability_consumption(
    intent: object, capability: object, consumed_capability_digests: object
) -> bool:
    checked_intent = verify_document_digest("tool-dispatch-intent/v1", intent)
    checked_capability = verify_document_digest(
        "tool-dispatch-capability/v1", capability
    )
    _require(
        isinstance(consumed_capability_digests, list)
        and all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
            for item in consumed_capability_digests
        )
        and _sorted_unique(consumed_capability_digests),
        "TOOL_CAPABILITY_CONSUMPTION_INVALID",
    )
    _require(
        checked_capability["intent_digest"] == checked_intent["intent_digest"]
        and checked_capability["capability_digest"]
        == checked_intent["capability_digest"],
        "TOOL_CAPABILITY_BINDING_INVALID",
    )
    _require(
        checked_capability["capability_digest"]
        not in consumed_capability_digests,
        "CAPABILITY_ALREADY_CONSUMED",
    )
    return True


def _verify_tool_content_ref(
    reference: object, schema_id: str, document: dict[str, object]
) -> None:
    checked = validate_document("content-ref/v1", reference)
    encoded = canonical_document_bytes(document)
    _require(
        checked["content_schema_id"] == schema_id
        and checked["sha256"] == hashlib.sha256(encoded).hexdigest()
        and checked["size_bytes"] == len(encoded)
        and checked["media_type"] == "application/json"
        and checked["encoding"] == "utf-8",
        "TOOL_CONTENT_REF_BINDING_INVALID",
    )


def validate_tool_journal_settlement(
    invocation: object,
    intent: object,
    receipt: object,
    payload: object,
    result: object,
    source_before: object,
    source_after: object,
) -> bool:
    checked_invocation = verify_document_digest("tool-invocation/v1", invocation)
    checked_intent = verify_document_digest("tool-dispatch-intent/v1", intent)
    checked_receipt = verify_document_digest("local-tool-receipt/v1", receipt)
    checked_payload = verify_document_digest("r0-read-payload/v1", payload)
    checked_result = verify_document_digest("r0-read-result/v1", result)
    before = verify_document_digest("source-state/v1", source_before)
    after = verify_document_digest("source-state/v1", source_after)
    _require(
        checked_intent["invocation_digest"] == checked_invocation["invocation_digest"]
        and checked_intent["task_id"] == checked_invocation["task_id"]
        and checked_intent["idempotency_key"] == checked_invocation["idempotency_key"]
        and checked_intent["tool_key"] == checked_invocation["tool_key"]
        and _json_equal(checked_intent["tool_input"], checked_invocation["tool_input"]),
        "TOOL_SETTLEMENT_BINDING_INVALID",
    )
    invocation_digest = checked_invocation["invocation_digest"]
    _require(
        checked_receipt["tool_key"] == checked_invocation["tool_key"]
        and checked_receipt["invocation_digest"] == invocation_digest
        and checked_payload["invocation_digest"] == invocation_digest
        and checked_result["invocation_digest"] == invocation_digest
        and checked_payload["relative_path"]
        == checked_invocation["tool_input"]["relative_path"]
        and checked_result["local_receipt_digest"]
        == checked_receipt["receipt_digest"]
        and _json_equal(checked_receipt["payload_ref"], checked_result["payload_ref"]),
        "TOOL_SETTLEMENT_BINDING_INVALID",
    )
    _verify_tool_content_ref(
        checked_receipt["payload_ref"], "r0-read-payload/v1", checked_payload
    )
    identity = ("task_id", "attempt_id", "native_epoch")
    _require(
        all(
            before[field] == checked_invocation[field]
            and after[field] == checked_invocation[field]
            for field in identity
        ),
        "SOURCE_STATE_BINDING_INVALID",
    )
    _require(
        before["source_state_id"] == after["source_state_id"]
        and checked_result["source_state_before_id"] == before["source_state_id"]
        and checked_result["source_state_after_id"] == after["source_state_id"],
        "SOURCE_STATE_CHANGED",
    )
    return True
'''


__all__ = ["PYTHON_TOOL_EVIDENCE"]
