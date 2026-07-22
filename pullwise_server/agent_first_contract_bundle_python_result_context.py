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


def _result_fragment_identity(value: dict[str, object], manifest_digest: str) -> str:
    identity = {
        "task_id": value["task_id"], "job_id": value["job_id"], "run_id": value["run_id"], "lease_id": value["lease_id"],
        "transport_attempt_id": value["transport_attempt_id"], "transport_epoch": value["transport_epoch"],
        "native_attempt_id": value["native_attempt_id"], "native_epoch": value["native_epoch"],
        "capture_kind": value["capture_kind"], "snapshot_seq": value["snapshot_seq"], "file_manifest_digest": manifest_digest,
    }
    raw = b"pullwise:worker-debug-fragment-id/v1\0" + canonical_document_bytes(identity)
    return "frag_" + hashlib.sha256(raw).hexdigest()


def derive_task_result_core(task_result: object) -> dict[str, object]:
    checked = validate_document("task-result/v1", task_result)
    projected = json.loads(canonical_document_bytes(checked).decode("utf-8"))
    projected["schema_id"] = "task-result-core/v1"
    projected["diagnostics"].pop("worker_debug_fragment", None)
    return validate_document("task-result-core/v1", projected)


def verify_task_result_context(task_result: object, *, worker_debug_descriptor: object = None) -> dict[str, object]:
    checked = validate_document("task-result/v1", task_result)
    debug = checked["diagnostics"]["worker_debug_fragment"]
    if debug["availability"] == "available":
        descriptor = validate_document("worker-debug-fragment-descriptor/v1", worker_debug_descriptor)
        _result_require_ref(debug["ref"], "worker-debug-fragment-descriptor/v1", descriptor, "$.diagnostics.worker_debug_fragment.ref")
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
    if checked["task_result_core"]["availability"] == "available":
        core = validate_document("task-result-core/v1", task_result_core)
        _result_require_ref(checked["task_result_core"]["ref"], "task-result-core/v1", core, "$.task_result_core.ref")
    _result_require_ref(checked["file_manifest_ref"], "worker-debug-file-manifest/v1", manifest, "$.file_manifest_ref")
    _result_require_ref(checked["redaction_report_ref"], "worker-debug-redaction-report/v1", report, "$.redaction_report_ref")
    _seo_require(checked["fragment_id"] == _result_fragment_identity(checked, manifest["manifest_digest"]), "DEBUG_FRAGMENT_ID_INVALID", "$.fragment_id")
    return checked


def verify_worker_debug_descriptor_content(descriptor: object, fragment: object, *, transport_receipt: object = None) -> dict[str, object]:
    checked = validate_document("worker-debug-fragment-descriptor/v1", descriptor)
    fragment_value = validate_document("worker-debug-fragment/v1", fragment)
    fragment_bytes = canonical_document_bytes(fragment_value)
    _result_require_ref(checked["fragment_ref"], "worker-debug-fragment/v1", fragment_value, "$.fragment_ref")
    _seo_require(checked["source_sha256"] == hashlib.sha256(fragment_bytes).hexdigest(), "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID", "$.source_sha256")
    if checked["server_fragment_ref"] is not None:
        _result_require_ref(checked["server_fragment_ref"], "worker-debug-fragment/v1", fragment_value, "$.server_fragment_ref")
    if transport_receipt is not None:
        receipt = _result_checked_receipt(transport_receipt)
        _seo_require(checked["state"] == "uploaded", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _result_require_ref(checked["server_receipt_ref"], "server-transport-receipt/v1", receipt, "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _result_require_ref(receipt["content_ref"], "worker-debug-fragment/v1", fragment_value, "$.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
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
    _seo_require(authority["task_version"] == fence["task_version"] == result["published_from_version"], "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version")
    debug = result["diagnostics"]["worker_debug_fragment"]
    descriptor = checked["worker_debug_descriptor"]
    if descriptor is not None and worker_debug_descriptor is not None:
        descriptor = validate_document("worker-debug-fragment-descriptor/v1", worker_debug_descriptor)
        _seo_require(_json_equal(descriptor, checked["worker_debug_descriptor"]), "TRANSPORT_DEBUG_DESCRIPTOR_CONFLICT", "$.worker_debug_descriptor")
    if debug["availability"] == "available":
        _seo_require(descriptor is not None, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor")
        _result_require_ref(debug["ref"], "worker-debug-fragment-descriptor/v1", descriptor, "$.task_result.diagnostics.worker_debug_fragment.ref")
    else:
        _seo_require(worker_debug_descriptor is None, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor")
    if transport_receipt is not None:
        receipt = _result_checked_receipt(transport_receipt)
        _seo_require(checked["transport_receipt"]["availability"] == "available", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _result_require_ref(checked["transport_receipt"]["ref"], "server-transport-receipt/v1", receipt, "$.transport_receipt.ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(receipt["package"] == checked["package"], "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(all(receipt[key] == authority[key] for key in ("task_id", "attempt_id", "session_id", "owner_id", "lease_id", "task_version", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch")), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        _seo_require(receipt["authority_digest"] == authority["authority_digest"] and receipt["grant_digest"] == authority["grant"]["grant_digest"], "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
        if descriptor is not None:
            target = descriptor["server_fragment_ref"] or descriptor["fragment_ref"]
            _seo_require(_json_equal(receipt["content_ref"], target), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT")
    raw = canonical_document_bytes(checked)
    return {"document": checked, "canonical_bytes": raw, "transport_envelope_digest": hashlib.sha256(raw).hexdigest()}


def verify_task_result_transport_ack(ack: object, envelope: object, *, transport_receipt: object = None) -> dict[str, object]:
    checked = verify_document_digest("task-result-transport-ack/v1", ack)
    verified = verify_task_result_transport_envelope(envelope, derive_task_result_core(validate_document("task-result/v1", envelope["task_result"])), transport_receipt=transport_receipt, worker_debug_descriptor=envelope.get("worker_debug_descriptor"))
    result = verified["document"]["task_result"]
    _seo_require(_json_equal(checked["package"], verified["document"]["package"]), "TRANSPORT_ACK_PACKAGE_INVALID", "$.package")
    for field in ("result_id", "task_id", "outcome", "published_from_version", "terminal_task_version"):
        _seo_require(checked[field] == result[field], "TRANSPORT_ACK_CONTEXT_INVALID", f"$.{field}")
    _seo_require(checked["transport_envelope_digest"] == verified["transport_envelope_digest"], "TRANSPORT_ACK_DIGEST_INVALID", "$.transport_envelope_digest")
    if transport_receipt is None:
        _seo_require(checked["receipt_binding_state"] == "not_applicable" and checked["receipt_digest"] is None, "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state")
    else:
        receipt = _result_checked_receipt(transport_receipt)
        _seo_require(checked["receipt_binding_state"] == "bound" and checked["receipt_digest"] == receipt["receipt_digest"], "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state")
    return checked
'''


__all__ = ["PYTHON_RESULT_CONTEXT"]
