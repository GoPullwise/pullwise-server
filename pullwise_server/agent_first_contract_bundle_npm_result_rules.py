"""Node facade result/debug/transport document rules."""

from __future__ import annotations


NPM_RESULT_RULES = r'''
function resultTextCompare(left, right) {
  const a = [...String(left)], b = [...String(right)];
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const diff = a[i].codePointAt(0) - b[i].codePointAt(0);
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function resultCompareKey(left, right) {
  const a = Array.isArray(left) ? left : [left], b = Array.isArray(right) ? right : [right];
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const diff = resultTextCompare(a[i], b[i]);
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function resultOrderedUnique(values, key) {
  const keys = values.map(key);
  for (let i = 1; i < keys.length; i += 1) if (resultCompareKey(keys[i - 1], keys[i]) >= 0) return false;
  return true;
}

function resultSortedUnique(values) { return resultOrderedUnique(values, (item) => item); }
function resultRefKey(value) { return [value.content_schema_id, value.artifact_id, value.sha256]; }
function resultArtifactKey(value) { const ref = value.ref ?? value; return ref.artifact_id; }
function resultAvailabilityKey(value) { return value.availability === "available" ? ["available", ...resultRefKey(value.ref)] : [value.availability, value.reason_code]; }
function resultUtf8Bytes(value) { return encoder.encode(value).length; }
function resultLeap(year) { return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0); }
function resultDaysInMonth(year, month) { return [31, resultLeap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]; }

function resultRfc3339Parts(value) {
  if (typeof value !== "string") return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const hour = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
  const nanos = Number((match[7] ?? "").padEnd(9, "0"));
  if (month < 1 || month > 12 || day < 1 || day > resultDaysInMonth(year, month)) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;
  return [year, month, day, hour, minute, second, nanos];
}

function resultTimeLeq(earlier, later, path) {
  const left = resultRfc3339Parts(earlier), right = resultRfc3339Parts(later);
  let ok = left !== null && right !== null;
  for (let i = 0; ok && i < left.length; i += 1) {
    if (left[i] < right[i]) break;
    if (left[i] > right[i]) ok = false;
  }
  seoRequire(ok, "TASK_RESULT_TIME_ORDER_INVALID", path);
}

function resultTaskResultCoreProjection(value) {
  const projected = JSON.parse(decoder.decode(canonicalDocumentBytes(value)));
  projected.schema_id = "task-result-core/v1";
  projected.diagnostics = {...projected.diagnostics};
  delete projected.diagnostics.worker_debug_fragment;
  return projected;
}

function ruleAvailabilityReasonRegistry(value) {
  seoVerifyEmbeddedDigest("availability-reason-registry/v1", value);
  seoRequire(resultSortedUnique(value.reasons), "REASON_REGISTRY_ORDER_INVALID");
}

function ruleAvailabilityRef(value) {
  seoRequire(
    value.availability === "available" ? canonicalString(Object.keys(value).sort()) === '["availability","ref"]' : canonicalString(Object.keys(value).sort()) === '["availability","reason_code"]',
    "AVAILABILITY_REF_SHAPE_INVALID",
  );
}

function ruleTaskResultOutcomeReasonRegistry(value) {
  seoVerifyEmbeddedDigest("task-result-outcome-reason-registry/v1", value);
  seoRequire(resultSortedUnique(value.reasons), "REASON_REGISTRY_ORDER_INVALID");
}

function ruleTaskResult(value) {
  seoRequire(resultUtf8Bytes(value.summary) <= 4096, "TASK_RESULT_SUMMARY_LIMIT_INVALID");
  seoRequire(value.terminal_task_version === value.published_from_version + 1, "TASK_RESULT_VERSION_SUCCESSOR_INVALID");
  const started = value.attempt_identity.kind === "started", ownerStarted = value.owner_identity.kind === "started";
  seoRequire(started === ownerStarted, "TASK_RESULT_IDENTITY_MATRIX_INVALID");
  if (new Set(["COMPLETED", "NO_CHANGE_NEEDED", "COMPLETED_WITH_WAIVERS", "PARTIAL"]).has(value.outcome)) seoRequire(started && ownerStarted, "TASK_RESULT_IDENTITY_MATRIX_INVALID");
  seoRequire(resultOrderedUnique(value.requirement_results, (item) => item.requirement_id), "TASK_RESULT_REQUIREMENT_ORDER_INVALID");
  value.requirement_results.forEach((item, index) => ["evidence_refs", "attestation_refs", "waiver_refs"].forEach((field) => seoRequire(resultOrderedUnique(item[field], resultRefKey), "TASK_RESULT_REFERENCE_ORDER_INVALID", `$.requirement_results[${index}].${field}`)));
  seoRequire(resultOrderedUnique(value.execution_states, resultAvailabilityKey), "TASK_RESULT_EXECUTION_ORDER_INVALID");
  seoRequire(resultOrderedUnique(value.artifact_refs, resultArtifactKey), "TASK_RESULT_ARTIFACT_ORDER_INVALID");
  seoRequire(resultSortedUnique(value.provenance.attempt_ids), "TASK_RESULT_ATTEMPT_ORDER_INVALID");
  verifyContentRefSet(value.requirement_results.flatMap((result) => ["evidence_refs", "attestation_refs", "waiver_refs"].flatMap((field) => result[field])));
  seoRequire(value.evidence_closure_digest === value.evidence_closure_ref.sha256, "TASK_RESULT_EVIDENCE_CLOSURE_DIGEST_INVALID");
  if (value.outcome === "NO_CHANGE_NEEDED") {
    seoRequire(value.change_set_ref === null, "TASK_RESULT_NO_CHANGE_SET_INVALID");
    seoRequire(value.original_source_state.availability === "available" && value.final_source_state.availability === "available" && canonicalString(value.original_source_state.ref) === canonicalString(value.final_source_state.ref), "TASK_RESULT_NO_CHANGE_STATE_INVALID");
  }
  resultTimeLeq(value.created_at, value.terminal_at, "$.terminal_at");
}

function ruleTaskResultCore(value) {
  ruleTaskResult(value);
  seoRequire(canonicalString(value.diagnostics) === "{}", "TASK_RESULT_CORE_DEBUG_FIELD_INVALID");
}

function ruleWorkerDebugFileManifest(value) {
  seoVerifyEmbeddedDigest("worker-debug-file-manifest/v1", value);
  seoRequire(resultOrderedUnique(value.entries, (item) => item.path), "DEBUG_FILE_MANIFEST_ORDER_INVALID");
  seoRequire(value.entry_count === value.entries.length, "DEBUG_FILE_MANIFEST_COUNT_INVALID");
  seoRequire(value.total_size_bytes === value.entries.reduce((sum, item) => sum + item.size_bytes, 0), "DEBUG_FILE_MANIFEST_SIZE_INVALID");
  const ndjson = new Set(["agent-events.jsonl", "codex-events.jsonl", "gateway-events.jsonl", "progress.log.jsonl", "task-events.jsonl", "worker.log.jsonl"]);
  value.entries.forEach((item, index) => seoRequire(item.media_type === (ndjson.has(item.path) ? "application/x-ndjson" : "application/json"), "DEBUG_FILE_MEDIA_TYPE_INVALID", `$.entries[${index}]`));
}

function ruleWorkerDebugRedactionReport(value) {
  seoVerifyEmbeddedDigest("worker-debug-redaction-report/v1", value);
  seoRequire(value.archive_rescan_detection_count === 0, "DEBUG_REDACTION_RESCAN_FAILED");
  seoRequire(value.structured_pass_detection_count === value.redacted_value_count, "DEBUG_REDACTION_COUNT_INVALID");
  seoRequire(value.status === "clean" ? value.structured_pass_detection_count === 0 : value.structured_pass_detection_count >= 1, "DEBUG_REDACTION_STATUS_INVALID");
}

function ruleWorkerDebugDescriptor(value) {
  if (value.state === "uploaded") {
    seoRequire(value.transport_kind === "server_transport" && value.server_fragment_ref !== null && value.server_receipt_ref !== null && value.reason_code === null, "DEBUG_DESCRIPTOR_BINDING_INVALID");
    seoRequire(value.server_fragment_ref.sha256 === value.fragment_ref.sha256, "DEBUG_DESCRIPTOR_FRAGMENT_MISMATCH");
  } else {
    seoRequire(value.transport_kind === "none" && value.server_fragment_ref === null && value.server_receipt_ref === null && value.reason_code === "DEBUG_UPLOAD_FAILED", "DEBUG_DESCRIPTOR_BINDING_INVALID");
  }
  seoRequire(value.source_sha256 === value.fragment_ref.sha256, "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID");
}

function ruleWorkerDebugFragment(value) {
  seoRequire(value.last_server_acked_event_seq <= value.local_event_seq, "DEBUG_EVENT_SEQUENCE_INVALID");
  if (value.capture_kind === "terminal") seoRequire(value.task_result_core.availability === "available", "DEBUG_TERMINAL_CORE_REQUIRED");
  else seoRequire(canonicalString(value.task_result_core) === canonicalString({availability: "not_applicable", reason_code: "TASK_RESULT_CORE_NOT_APPLICABLE"}), "DEBUG_NONTERMINAL_CORE_INVALID");
  seoRequire(value.status === "complete" ? value.reason_code === null : new Set(["DEBUG_LIMIT_EXCEEDED", "DEBUG_UNAVAILABLE"]).has(value.reason_code), "DEBUG_FRAGMENT_REASON_INVALID");
}

function ruleTaskResultTransportAck(value) {
  seoVerifyEmbeddedDigest("task-result-transport-ack/v1", value);
  seoRequire(value.terminal_task_version === value.published_from_version + 1, "TRANSPORT_ACK_VERSION_INVALID");
  seoRequire((value.receipt_digest !== null) === (value.receipt_binding_state === "bound"), "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID");
}

function ruleTaskResultTransportEnvelope(value) {
  const result = validateDocument("task-result/v1", value.task_result), resultBytes = canonicalDocumentBytes(result), resultDigest = sha256Sync(resultBytes);
  seoRequire(value.task_result_digest === resultDigest, "TRANSPORT_ENVELOPE_DIGEST_INVALID", "$.task_result_digest");
  const core = validateDocument("task-result-core/v1", resultTaskResultCoreProjection(result)), coreBytes = canonicalDocumentBytes(core), coreDigest = sha256Sync(coreBytes);
  seoRequire(value.task_result_core_digest === coreDigest, "TRANSPORT_CORE_DIGEST_INVALID", "$.task_result_core_digest");
  seoRequire(value.task_result_core_ref.sha256 === coreDigest && value.task_result_core_ref.size_bytes === coreBytes.length, "TRANSPORT_CORE_REF_INVALID", "$.task_result_core_ref");
  const authority = value.authority, fence = value.full_fence;
  ["task_id", "attempt_id", "session_id", "owner_id", "lease_id", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"].forEach((key) => seoRequire(authority[key] === fence[key], "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence"));
  seoRequire(authority.task_id === result.task_id, "TRANSPORT_RESULT_TASK_INVALID", "$.authority.task_id");
  seoRequire(authority.task_version === result.published_from_version, "TRANSPORT_RESULT_VERSION_INVALID", "$.authority.task_version");
  seoRequire(canonicalString(value.package) === canonicalString(authority.package), "TRANSPORT_PACKAGE_INVALID", "$.package");
  const debug = result.diagnostics.worker_debug_fragment, descriptor = value.worker_debug_descriptor, receipt = value.transport_receipt;
  if (debug.availability === "available") {
    seoRequire(descriptor !== null, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor");
    seoRequire(descriptor.state === "uploaded" ? receipt.availability === "available" : canonicalString(receipt) === canonicalString({availability: "not_applicable", reason_code: "TRANSPORT_RECEIPT_NOT_APPLICABLE"}), descriptor.state === "uploaded" ? "TRANSPORT_RECEIPT_REQUIRED" : "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  } else {
    seoRequire(descriptor === null, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor");
    seoRequire(canonicalString(receipt) === canonicalString({availability: "not_applicable", reason_code: "TRANSPORT_RECEIPT_NOT_APPLICABLE"}), "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  }
}
'''


__all__ = ["NPM_RESULT_RULES"]
