"""Python facade contextual helpers for result/debug transport documents."""

from __future__ import annotations


PYTHON_RESULT_CONTEXT = r'''
def _result_checked_receipt(transport_receipt: object) -> dict[str, object]:
    _seo_require(isinstance(transport_receipt, dict), "TRANSPORT_RECEIPT_TYPE_INVALID", code="TRANSPORT_RECEIPT_TYPE_INVALID")
    _seo_require(transport_receipt.get("schema_id") == "server-transport-receipt/v1", "TRANSPORT_RECEIPT_TYPE_INVALID", "$.schema_id", "TRANSPORT_RECEIPT_TYPE_INVALID")
    _seo_require(transport_receipt.get("receipt_kind") == "server_transport", "TRANSPORT_RECEIPT_TYPE_INVALID", "$.receipt_kind", "TRANSPORT_RECEIPT_TYPE_INVALID")
    return verify_document_digest("server-transport-receipt/v1", transport_receipt)


def _result_require_ref(ref: dict[str, object], schema_id: str, document: dict[str, object], path: str, detail: str = "CAS_CORRUPT", code: str | None = None) -> None:
    _seo_require(_seo_ref_matches_document(ref, schema_id, document), detail, path, code)


def _result_ref_tuple(value: dict[str, object]) -> tuple[object, ...]:
    return tuple(value.get(key) for key in ("content_schema_id", "sha256", "size_bytes", "media_type", "encoding"))


def _result_fragment_identity(value: dict[str, object], manifest_digest: str) -> str:
    identity = {
        "task_id": value["task_id"], "job_id": value["job_id"], "run_id": value["run_id"], "lease_id": value["lease_id"],
        "transport_attempt_id": value["transport_attempt_id"], "transport_epoch": value["transport_epoch"],
        "native_attempt_id": value["native_attempt_id"], "native_epoch": value["native_epoch"],
        "capture_kind": value["capture_kind"], "snapshot_seq": value["snapshot_seq"], "file_manifest_digest": manifest_digest,
    }
    return "frag_" + hashlib.sha256(b"pullwise:worker-debug-fragment-id/v1\0" + canonical_document_bytes(identity)).hexdigest()


def derive_task_result_core(task_result: object) -> dict[str, object]:
    checked = validate_document("task-result/v1", task_result)
    return validate_document(
        "task-result-core/v1", _result_task_result_core_projection(checked)
    )


def verify_task_result_context(
    task_result: object,
    *,
    terminal_gate_decision: object = None,
    effect_ledger_snapshot: object = None,
    worker_debug_descriptor: object = None,
) -> dict[str, object]:
    checked = validate_document("task-result/v1", task_result)
    _seo_require(isinstance(terminal_gate_decision, dict), "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.ref")
    decision = verify_document_digest("gate-decision/v1", terminal_gate_decision)
    _result_require_ref(checked["gate_decision"]["ref"], "gate-decision/v1", decision, "$.gate_decision.ref")
    _seo_require(decision["decision_kind"] == "terminalization" and decision["selected_lifecycle"] == "TERMINAL" and decision["passed"], "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision")
    _seo_require(decision["task_id"] == checked["task_id"], "TASK_RESULT_CONTEXT_INVALID", "$.task_id")
    _seo_require(decision["task_version"] == checked["published_from_version"], "TASK_RESULT_CONTEXT_INVALID", "$.published_from_version")
    _seo_require(decision["selected_outcome"] == checked["outcome"], "TASK_RESULT_CONTEXT_INVALID", "$.outcome")
    _seo_require(decision["selected_reason"] == checked["reason_code"], "TASK_RESULT_CONTEXT_INVALID", "$.reason_code")
    _seo_require(decision["selector_input_digest"] == checked["selector_input_digest"], "TASK_RESULT_CONTEXT_INVALID", "$.selector_input_digest")
    _seo_require(isinstance(effect_ledger_snapshot, dict), "TASK_RESULT_CONTEXT_INVALID", "$.effects")
    ledger = verify_document_digest("effect-ledger-snapshot/v1", effect_ledger_snapshot)
    effect_availability = decision["effect_availability"]
    _seo_require(effect_availability["availability"] == "available", "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.effect_availability")
    _result_require_ref(effect_availability["ref"], "effect-ledger-snapshot/v1", ledger, "$.gate_decision.effect_availability.ref")
    _seo_require(ledger["task_id"] == checked["task_id"], "TASK_RESULT_EFFECT_LEDGER_TASK_INVALID", "$.effects")
    counts = ledger["state_counts"]
    _seo_require(counts["prepared"] == counts["dispatched"] == 0, "TASK_RESULT_ACTIVE_EFFECTS", "$.effects")
    _seo_require(_json_equal(counts, checked["effects"]), "TASK_RESULT_EFFECT_COUNTS_INVALID", "$.effects")
    effect_state = (
        "unknown_post_deadline"
        if counts["unknown"]
        else "committed"
        if counts["committed"]
        else "none"
    )
    _seo_require(decision["effect_state"] == effect_state, "TASK_RESULT_EFFECT_STATE_INVALID", "$.gate_decision.effect_state")
    debug = checked["diagnostics"]["worker_debug_fragment"]
    if debug["availability"] == "available":
        _seo_require(isinstance(worker_debug_descriptor, dict), "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment.ref")
        descriptor = validate_document("worker-debug-fragment-descriptor/v1", worker_debug_descriptor)
        _seo_require(_seo_ref_matches_document(debug["ref"], "worker-debug-fragment-descriptor/v1", descriptor), "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment.ref")
    else:
        _seo_require(worker_debug_descriptor is None, "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment")
    return checked


def verify_task_result_core(task_result: object, core: object) -> dict[str, object]:
    checked = validate_document("task-result-core/v1", core)
    _seo_require(_json_equal(checked, derive_task_result_core(task_result)), "TASK_RESULT_CORE_PROJECTION_INVALID")
    return checked


def verify_worker_debug_fragment_content(fragment: object, task_result_core: object, file_manifest: object, redaction_report: object) -> dict[str, object]:
    checked = validate_document("worker-debug-fragment/v1", fragment)
    manifest = verify_document_digest("worker-debug-file-manifest/v1", file_manifest)
    report = verify_document_digest("worker-debug-redaction-report/v1", redaction_report)
    _result_require_ref(checked["file_manifest_ref"], "worker-debug-file-manifest/v1", manifest, "$.file_manifest_ref")
    _result_require_ref(checked["redaction_report_ref"], "worker-debug-redaction-report/v1", report, "$.redaction_report_ref")
    _seo_require(checked["fragment_id"] == _result_fragment_identity(checked, manifest["manifest_digest"]), "DEBUG_FRAGMENT_ID_INVALID", "$.fragment_id")
    if checked["task_result_core"]["availability"] == "available":
        _seo_require(task_result_core is not None, "DEBUG_TERMINAL_CORE_REQUIRED", "$.task_result_core")
        core = validate_document("task-result-core/v1", task_result_core)
        _result_require_ref(checked["task_result_core"]["ref"], "task-result-core/v1", core, "$.task_result_core.ref")
        attempt = core["attempt_identity"]
        _seo_require(checked["task_id"] == core["task_id"] and attempt["kind"] == "started", "DEBUG_TERMINAL_CORE_INVALID", "$.task_result_core")
        _seo_require(checked["native_attempt_id"] == attempt["attempt_id"], "DEBUG_TERMINAL_CORE_INVALID", "$.native_attempt_id")
        _seo_require(checked["native_epoch"] == attempt["native_epoch"], "DEBUG_TERMINAL_CORE_INVALID", "$.native_epoch")
        _seo_require(checked["task_version"] == core["published_from_version"], "DEBUG_TERMINAL_CORE_INVALID", "$.task_version")
        _seo_require(checked["checkpoint_generation"] == core["provenance"]["checkpoint_generation"], "DEBUG_TERMINAL_CORE_INVALID", "$.checkpoint_generation")
        _seo_require(core["final_source_state"]["availability"] == "available" and checked["source_state_id"] == core["final_source_state"]["ref"]["sha256"], "DEBUG_TERMINAL_CORE_INVALID", "$.source_state_id")
        _seo_require(_result_time_leq(core["created_at"], checked["captured_at"]) and _result_time_leq(checked["captured_at"], core["terminal_at"]), "DEBUG_TERMINAL_CORE_INVALID", "$.captured_at")
    else:
        _seo_require(task_result_core is None, "DEBUG_NONTERMINAL_CORE_INVALID", "$.task_result_core")
    return checked


def verify_worker_debug_descriptor_content(descriptor: object, fragment: object, *, transport_receipt: object = None) -> dict[str, object]:
    checked = validate_document("worker-debug-fragment-descriptor/v1", descriptor)
    fragment_value = validate_document("worker-debug-fragment/v1", fragment)
    fragment_bytes = canonical_document_bytes(fragment_value)
    _result_require_ref(checked["fragment_ref"], "worker-debug-fragment/v1", fragment_value, "$.fragment_ref")
    _seo_require(checked["snapshot_seq"] == fragment_value["snapshot_seq"], "DEBUG_DESCRIPTOR_BINDING_INVALID", "$.snapshot_seq")
    _seo_require(checked["source_sha256"] == hashlib.sha256(fragment_bytes).hexdigest(), "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID", "$.source_sha256")
    if checked["server_fragment_ref"] is not None:
        _result_require_ref(checked["server_fragment_ref"], "worker-debug-fragment/v1", fragment_value, "$.server_fragment_ref")
    if checked["state"] == "uploaded":
        _seo_require(transport_receipt is not None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        receipt = _result_checked_receipt(transport_receipt)
        _result_require_ref(checked["server_receipt_ref"], "server-transport-receipt/v1", receipt, "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _result_require_ref(receipt["content_ref"], "worker-debug-fragment/v1", fragment_value, "$.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(_result_ref_tuple(receipt["content_ref"]) == _result_ref_tuple(checked["server_fragment_ref"]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(_result_time_leq(fragment_value["captured_at"], receipt["accepted_at"]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
    else:
        _seo_require(transport_receipt is None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
    return checked


def verify_task_result_transport_envelope(envelope: object, core: object, *, transport_receipt: object = None, worker_debug_descriptor: object = None) -> dict[str, object]:
    checked = validate_document("task-result-transport-envelope/v1", envelope)
    core_value = verify_task_result_core(checked["task_result"], core)
    core_bytes = canonical_document_bytes(core_value)
    _result_require_ref(checked["task_result_core_ref"], "task-result-core/v1", core_value, "$.task_result_core_ref")
    _seo_require(checked["task_result_core_digest"] == hashlib.sha256(core_bytes).hexdigest(), "TRANSPORT_CORE_DIGEST_INVALID", "$.task_result_core_digest")
    authority, fence, result = checked["authority"], checked["full_fence"], checked["task_result"]
    for key in ("task_id", "attempt_id", "session_id", "owner_id", "lease_id", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"):
        _seo_require(authority[key] == fence[key], "TRANSPORT_AUTHORITY_FENCE_INVALID", f"$.full_fence.{key}")
    _seo_require(authority["task_version"] == fence["task_version"], "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence.task_version")
    _seo_require(authority["task_version"] == result["published_from_version"], "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version")
    debug = result["diagnostics"]["worker_debug_fragment"]
    descriptor = checked["worker_debug_descriptor"]
    if debug["availability"] == "available":
        _seo_require(descriptor is not None, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor")
        _seo_require(isinstance(worker_debug_descriptor, dict), "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor")
        descriptor = validate_document("worker-debug-fragment-descriptor/v1", worker_debug_descriptor)
        _seo_require(_json_equal(descriptor, checked["worker_debug_descriptor"]), "TRANSPORT_DEBUG_DESCRIPTOR_CONFLICT", "$.worker_debug_descriptor")
        _result_require_ref(debug["ref"], "worker-debug-fragment-descriptor/v1", descriptor, "$.task_result.diagnostics.worker_debug_fragment.ref", "TASK_RESULT_CONTEXT_INVALID")
    else:
        _seo_require(descriptor is None and worker_debug_descriptor is None, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor")
    if checked["transport_receipt"]["availability"] == "available":
        _seo_require(transport_receipt is not None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        receipt = _result_checked_receipt(transport_receipt)
        _result_require_ref(checked["transport_receipt"]["ref"], "server-transport-receipt/v1", receipt, "$.transport_receipt.ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(descriptor is not None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.worker_debug_descriptor", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _result_require_ref(descriptor["server_receipt_ref"], "server-transport-receipt/v1", receipt, "$.worker_debug_descriptor.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(receipt["package"] == checked["package"], "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(all(receipt[key] == authority[key] for key in ("task_id", "attempt_id", "session_id", "owner_id", "lease_id", "task_version", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch")), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(receipt["authority_digest"] == authority["authority_digest"] and receipt["grant_digest"] == authority["grant"]["grant_digest"], "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(_result_ref_tuple(receipt["content_ref"]) == _result_ref_tuple(descriptor["fragment_ref"]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
    else:
        _seo_require(transport_receipt is None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(checked["transport_receipt"] == {"availability": "not_applicable", "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE"}, "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt")
    raw = canonical_document_bytes(checked)
    return {"document": checked, "canonical_bytes": raw, "transport_envelope_digest": hashlib.sha256(raw).hexdigest()}


def verify_task_result_transport_ack(ack: object, envelope: object, *, transport_receipt: object = None) -> dict[str, object]:
    checked = verify_document_digest("task-result-transport-ack/v1", ack)
    document = validate_document("task-result-transport-envelope/v1", envelope)
    result, raw = document["task_result"], canonical_document_bytes(document)
    _seo_require(_json_equal(checked["package"], document["package"]), "TRANSPORT_ACK_PACKAGE_INVALID", "$.package")
    for field in ("result_id", "task_id", "outcome", "published_from_version", "terminal_task_version"):
        _seo_require(checked[field] == result[field], "TRANSPORT_ACK_CONTEXT_INVALID", f"$.{field}")
    _seo_require(checked["transport_envelope_digest"] == hashlib.sha256(raw).hexdigest(), "TRANSPORT_ACK_DIGEST_INVALID", "$.transport_envelope_digest")
    if document["transport_receipt"]["availability"] == "available":
        _seo_require(transport_receipt is not None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        receipt = _result_checked_receipt(transport_receipt)
        _result_require_ref(document["transport_receipt"]["ref"], "server-transport-receipt/v1", receipt, "$.transport_receipt.ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(checked["receipt_binding_state"] == "bound" and checked["receipt_digest"] == receipt["receipt_digest"], "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state")
        _seo_require(_result_time_leq(receipt["accepted_at"], checked["accepted_at"]), "TASK_RESULT_TIME_ORDER_INVALID", "$.accepted_at")
    else:
        _seo_require(transport_receipt is None, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(checked["receipt_binding_state"] == "not_applicable" and checked["receipt_digest"] is None, "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state")
    _seo_require(_result_time_leq(result["terminal_at"], checked["accepted_at"]), "TASK_RESULT_TIME_ORDER_INVALID", "$.accepted_at")
    return checked
'''


__all__ = ["PYTHON_RESULT_CONTEXT"]
