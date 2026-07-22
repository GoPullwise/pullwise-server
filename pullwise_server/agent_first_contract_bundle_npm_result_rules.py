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

function resultUtf8Compare(left, right) {
  const a = encoder.encode(String(left)), b = encoder.encode(String(right));
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i];
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

function resultOrdered(values, key, compare = resultCompareKey) {
  const keys = values.map(key);
  for (let i = 1; i < keys.length; i += 1) if (compare(keys[i - 1], keys[i]) > 0) return false;
  return true;
}

function resultCanonicalUnique(values) {
  const canonical = values.map((item) => canonicalString(item));
  return new Set(canonical).size === canonical.length;
}

function resultOrderedCanonicalUnique(values, key, compare = resultCompareKey) {
  return resultOrdered(values, key, compare) && resultCanonicalUnique(values);
}

function resultSortedUnique(values) { return resultOrderedUnique(values, (item) => item); }
function resultRefKey(value) { return [value.content_schema_id, value.artifact_id, value.sha256]; }
function resultArtifactKey(value) { const ref = value.ref ?? value; return ref.artifact_id; }
function resultRefContentTuple(value) { return [value.content_schema_id, value.sha256, value.size_bytes, value.media_type, value.encoding]; }
function resultAvailabilityKey(value) { return value.availability === "available" ? ["available", ...resultRefKey(value.ref)] : [value.availability, value.reason_code]; }
function resultUtf8Bytes(value) { return encoder.encode(value).length; }
function resultOutcomeTextValid(value) {
  if (typeof value !== "string" || value.normalize("NFC") !== value) return false;
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      index += 1;
      if (index >= value.length) return false;
      const next = value.charCodeAt(index);
      if (next < 0xdc00 || next > 0xdfff) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    }
  }
  const bytes = resultUtf8Bytes(value);
  return bytes >= 1 && bytes <= 4096;
}
function resultLeap(year) { return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0); }
function resultDaysInMonth(year, month) { return [31, resultLeap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]; }

function resultRfc3339Parts(value) {
  if (typeof value !== "string") return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const hour = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
  const nanos = Number((match[7] ?? "").padEnd(9, "0"));
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > resultDaysInMonth(year, month)) return null;
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

function resultAvailabilityReasons() {
  const branches = schema("availability-ref/v1").oneOf;
  const unavailable = branches[1].properties.reason_code.enum;
  const notApplicable = branches[2].properties.reason_code.enum;
  seoRequire(
    canonicalString(unavailable) === canonicalString(notApplicable),
    "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID",
  );
  return [...unavailable].sort(resultTextCompare);
}

function resultOutcomeReasons() {
  const reasons = new Set();
  schema("task-result/v1").oneOf.forEach((branch) => {
    const rule = schema(branch.$ref).properties.reason_code;
    (Object.hasOwn(rule, "const") ? [rule.const] : rule.enum).forEach((reason) => reasons.add(reason));
  });
  return [...reasons].sort(resultTextCompare);
}

function resultValidateDeliveredScope(items, path) {
  items.forEach((item, index) => {
    seoRequire(resultOutcomeTextValid(item.statement), "TASK_RESULT_OUTCOME_TEXT_INVALID", `${path}[${index}].statement`);
    seoRequire(resultSortedUnique(item.requirement_ids), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}[${index}].requirement_ids`);
    seoRequire(resultOrderedUnique(item.artifact_refs, resultArtifactKey), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}[${index}].artifact_refs`);
  });
  seoRequire(
    resultOrderedCanonicalUnique(items, (item) => item.statement, resultUtf8Compare),
    "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID",
    path,
  );
}

function resultValidateOutcomeDetails(details) {
  const path = "$.outcome_details";
  if (details.delivered_scope !== undefined) resultValidateDeliveredScope(details.delivered_scope, `${path}.delivered_scope`);
  if (details.satisfaction_observation_ids !== undefined) seoRequire(resultSortedUnique(details.satisfaction_observation_ids), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.satisfaction_observation_ids`);
  if (details.waiver_ids !== undefined) seoRequire(resultSortedUnique(details.waiver_ids), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.waiver_ids`);
  if (details.original_verdicts !== undefined) seoRequire(resultOrderedUnique(details.original_verdicts, (item) => item.requirement_id), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.original_verdicts`);
  if (details.gaps !== undefined) seoRequire(resultOrderedUnique(details.gaps, (item) => item.requirement_id), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.gaps`);
  if (details.residual_risks !== undefined) {
    details.residual_risks.forEach((item, index) => {
      seoRequire(resultOutcomeTextValid(item.statement), "TASK_RESULT_OUTCOME_TEXT_INVALID", `${path}.residual_risks[${index}].statement`);
      seoRequire(resultSortedUnique(item.evidence_ids), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.residual_risks[${index}].evidence_ids`);
    });
    seoRequire(resultOrderedUnique(details.residual_risks, (item) => item.risk_id), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.residual_risks`);
  }
  if (details.blockers !== undefined) {
    details.blockers.forEach((item, index) => {
      seoRequire(resultOutcomeTextValid(item.unblock_condition), "TASK_RESULT_OUTCOME_TEXT_INVALID", `${path}.blockers[${index}].unblock_condition`);
      seoRequire(resultSortedUnique(item.requirement_ids), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.blockers[${index}].requirement_ids`);
    });
    seoRequire(
      resultOrderedCanonicalUnique(details.blockers, (item) => [item.code, item.requirement_ids[0] ?? "", item.unblock_condition]),
      "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID",
      `${path}.blockers`,
    );
  }
  if (details.failures !== undefined) {
    details.failures.forEach((item, index) => seoRequire(resultOrderedUnique(item.evidence_refs, resultRefKey), "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID", `${path}.failures[${index}].evidence_refs`));
    seoRequire(
      resultOrderedCanonicalUnique(details.failures, (item) => [item.code, item.evidence_refs[0].artifact_id]),
      "TASK_RESULT_OUTCOME_DETAILS_ORDER_INVALID",
      `${path}.failures`,
    );
  }
}

function ruleAvailabilityReasonRegistry(value) {
  seoVerifyEmbeddedDigest("availability-reason-registry/v1", value);
  seoRequire(canonicalString(value.reasons) === canonicalString(resultAvailabilityReasons()), "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID");
}

function ruleAvailabilityRef(value) {
  seoRequire(
    value.availability === "available" ? canonicalString(Object.keys(value).sort()) === '["availability","ref"]' : canonicalString(Object.keys(value).sort()) === '["availability","reason_code"]',
    "AVAILABILITY_REF_SHAPE_INVALID",
  );
}

function ruleTaskResultOutcomeReasonRegistry(value) {
  seoVerifyEmbeddedDigest("task-result-outcome-reason-registry/v1", value);
  seoRequire(canonicalString(value.reasons) === canonicalString(resultOutcomeReasons()), "TASK_RESULT_OUTCOME_REASON_REGISTRY_BIJECTION_INVALID");
}

function ruleTaskResult(value) {
  seoRequire(resultUtf8Bytes(value.summary) <= 4096, "TASK_RESULT_SUMMARY_LIMIT_INVALID");
  resultValidateOutcomeDetails(value.outcome_details);
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
  value.entries.forEach((item, index) => seoRequire(item.media_type === (ndjson.has(item.path) ? "application/x-ndjson" : "application/json"), "DEBUG_FILE_MEDIA_TYPE_INVALID", `$.entries[${index}].media_type`));
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
    seoRequire(value.server_fragment_ref.sha256 === value.fragment_ref.sha256 && value.server_fragment_ref.size_bytes === value.fragment_ref.size_bytes, "DEBUG_DESCRIPTOR_FRAGMENT_MISMATCH");
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
  seoRequire(value.task_result_digest === resultDigest, "TRANSPORT_ENVELOPE_DIGEST_INVALID", "$");
  const core = validateDocument("task-result-core/v1", resultTaskResultCoreProjection(result)), coreBytes = canonicalDocumentBytes(core), coreDigest = sha256Sync(coreBytes);
  seoRequire(value.task_result_core_digest === coreDigest, "TRANSPORT_CORE_DIGEST_INVALID", "$");
  seoRequire(value.task_result_core_ref.sha256 === coreDigest && value.task_result_core_ref.size_bytes === coreBytes.length, "TRANSPORT_CORE_REF_INVALID", "$.task_result_core_ref");
  const authority = value.authority, fence = value.full_fence;
  ["task_id", "attempt_id", "session_id", "owner_id", "lease_id", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"].forEach((key) => seoRequire(authority[key] === fence[key], "TRANSPORT_AUTHORITY_FENCE_INVALID", `$.full_fence.${key}`));
  seoRequire(authority.task_version === fence.task_version, "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence.task_version");
  seoRequire(authority.task_id === result.task_id, "TRANSPORT_RESULT_TASK_INVALID", "$.authority.task_id");
  seoRequire(authority.task_version === result.published_from_version, "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version");
  seoRequire(canonicalString(value.package) === canonicalString(authority.package), "TRANSPORT_PACKAGE_INVALID", "$.package");
  const debug = result.diagnostics.worker_debug_fragment, descriptor = value.worker_debug_descriptor, receipt = value.transport_receipt;
  if (debug.availability === "available") {
    seoRequire(descriptor !== null, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor");
    validateDocument("worker-debug-fragment-descriptor/v1", descriptor);
    seoRequire(descriptor.state === "uploaded" ? receipt.availability === "available" : canonicalString(receipt) === canonicalString({availability: "not_applicable", reason_code: "TRANSPORT_RECEIPT_NOT_APPLICABLE"}), "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  } else {
    seoRequire(descriptor === null, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor");
    seoRequire(canonicalString(receipt) === canonicalString({availability: "not_applicable", reason_code: "TRANSPORT_RECEIPT_NOT_APPLICABLE"}), "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  }
}
'''


__all__ = ["NPM_RESULT_RULES"]
