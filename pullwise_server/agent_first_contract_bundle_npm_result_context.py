"""Node facade result/debug/transport context helpers."""

from __future__ import annotations


NPM_RESULT_CONTEXT = r'''
function resultContextOptions(value) {
  const empty = {terminalGateDecision: null, effectLedgerSnapshot: null, workerDebugDescriptor: null, transportReceipt: null};
  if (value === undefined || value === null) return empty;
  if (!Array.isArray(value) && typeof value === "object" && Object.keys(value).length === 0) return empty;
  if (typeof value === "object" && !Array.isArray(value) && ("terminalGateDecision" in value || "terminal_gate_decision" in value || "effectLedgerSnapshot" in value || "effect_ledger_snapshot" in value || "workerDebugDescriptor" in value || "worker_debug_descriptor" in value || "transportReceipt" in value || "transport_receipt" in value)) return {terminalGateDecision: value.terminalGateDecision ?? value.terminal_gate_decision ?? null, effectLedgerSnapshot: value.effectLedgerSnapshot ?? value.effect_ledger_snapshot ?? null, workerDebugDescriptor: value.workerDebugDescriptor ?? value.worker_debug_descriptor ?? null, transportReceipt: value.transportReceipt ?? value.transport_receipt ?? null};
  return {...empty, workerDebugDescriptor: value};
}

function resultCheckedReceipt(value) {
  seoRequire(value !== null && typeof value === "object" && !Array.isArray(value), "TRANSPORT_RECEIPT_TYPE_INVALID", "$", "TRANSPORT_RECEIPT_TYPE_INVALID");
  seoRequire(value.schema_id === "server-transport-receipt/v1", "TRANSPORT_RECEIPT_TYPE_INVALID", "$.schema_id", "TRANSPORT_RECEIPT_TYPE_INVALID");
  seoRequire(value.receipt_kind === "server_transport", "TRANSPORT_RECEIPT_TYPE_INVALID", "$.receipt_kind", "TRANSPORT_RECEIPT_TYPE_INVALID");
  return verifyDocumentDigest("server-transport-receipt/v1", value);
}

function resultIdentityBytes(identity) {
  const domain = encoder.encode("pullwise:worker-debug-fragment-id/v1\0"), body = canonicalDocumentBytes(identity), bytes = new Uint8Array(domain.length + body.length);
  bytes.set(domain);
  bytes.set(body, domain.length);
  return bytes;
}

export function deriveTaskResultCore(taskResult) {
  return validateDocument("task-result-core/v1", resultTaskResultCoreProjection(validateDocument("task-result/v1", taskResult)));
}

export async function verifyTaskResultContext(taskResult, options = {}) {
  const checked = validateDocument("task-result/v1", taskResult), {terminalGateDecision, effectLedgerSnapshot, workerDebugDescriptor} = resultContextOptions(options);
  seoRequire(terminalGateDecision !== null && typeof terminalGateDecision === "object" && !Array.isArray(terminalGateDecision), "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.ref");
  const decision = await verifyDocumentDigest("gate-decision/v1", terminalGateDecision);
  seoRequire(seoRefMatchesDocument(checked.gate_decision.ref, "gate-decision/v1", decision), "CAS_CORRUPT", "$.gate_decision.ref");
  seoRequire(decision.decision_kind === "terminalization" && decision.selected_lifecycle === "TERMINAL" && decision.passed, "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision");
  seoRequire(decision.task_id === checked.task_id, "TASK_RESULT_CONTEXT_INVALID", "$.task_id");
  seoRequire(decision.task_version === checked.published_from_version, "TASK_RESULT_CONTEXT_INVALID", "$.published_from_version");
  seoRequire(decision.selected_outcome === checked.outcome, "TASK_RESULT_CONTEXT_INVALID", "$.outcome");
  seoRequire(decision.selected_reason === checked.reason_code, "TASK_RESULT_CONTEXT_INVALID", "$.reason_code");
  seoRequire(decision.selector_input_digest === checked.selector_input_digest, "TASK_RESULT_CONTEXT_INVALID", "$.selector_input_digest");
  seoRequire(effectLedgerSnapshot !== null && typeof effectLedgerSnapshot === "object" && !Array.isArray(effectLedgerSnapshot), "TASK_RESULT_CONTEXT_INVALID", "$.effects");
  const ledger = await verifyDocumentDigest("effect-ledger-snapshot/v1", effectLedgerSnapshot);
  const effectAvailability = decision.effect_availability;
  seoRequire(effectAvailability.availability === "available", "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.effect_availability");
  seoRequire(seoRefMatchesDocument(effectAvailability.ref, "effect-ledger-snapshot/v1", ledger), "CAS_CORRUPT", "$.gate_decision.effect_availability.ref");
  seoRequire(ledger.task_id === checked.task_id, "TASK_RESULT_EFFECT_LEDGER_TASK_INVALID", "$.effects");
  const counts = ledger.state_counts;
  seoRequire(counts.prepared === 0 && counts.dispatched === 0, "TASK_RESULT_ACTIVE_EFFECTS", "$.effects");
  seoRequire(canonicalString(counts) === canonicalString(checked.effects), "TASK_RESULT_EFFECT_COUNTS_INVALID", "$.effects");
  const effectState = counts.unknown ? "unknown_post_deadline" : counts.committed ? "committed" : "none";
  seoRequire(decision.effect_state === effectState, "TASK_RESULT_EFFECT_STATE_INVALID", "$.gate_decision.effect_state");
  const debug = checked.diagnostics.worker_debug_fragment;
  if (debug.availability === "available") {
    seoRequire(workerDebugDescriptor !== null && typeof workerDebugDescriptor === "object" && !Array.isArray(workerDebugDescriptor), "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment.ref");
    const descriptor = validateDocument("worker-debug-fragment-descriptor/v1", workerDebugDescriptor);
    seoRequire(seoRefMatchesDocument(debug.ref, "worker-debug-fragment-descriptor/v1", descriptor), "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment.ref");
  } else seoRequire(workerDebugDescriptor === null, "TASK_RESULT_CONTEXT_INVALID", "$.diagnostics.worker_debug_fragment");
  return checked;
}

export async function verifyTaskResultCore(taskResult, core) {
  const checked = validateDocument("task-result-core/v1", core), expected = deriveTaskResultCore(taskResult);
  seoRequire(canonicalString(checked) === canonicalString(expected), "TASK_RESULT_CORE_PROJECTION_INVALID");
  return checked;
}

export async function verifyWorkerDebugFragmentContent(fragment, taskResultCore, fileManifest, redactionReport) {
  const checked = validateDocument("worker-debug-fragment/v1", fragment);
  const manifest = await verifyDocumentDigest("worker-debug-file-manifest/v1", fileManifest), report = await verifyDocumentDigest("worker-debug-redaction-report/v1", redactionReport);
  seoRequire(seoRefMatchesDocument(checked.file_manifest_ref, "worker-debug-file-manifest/v1", manifest), "CAS_CORRUPT", "$.file_manifest_ref");
  seoRequire(seoRefMatchesDocument(checked.redaction_report_ref, "worker-debug-redaction-report/v1", report), "CAS_CORRUPT", "$.redaction_report_ref");
  const identity = {task_id: checked.task_id, job_id: checked.job_id, run_id: checked.run_id, lease_id: checked.lease_id, transport_attempt_id: checked.transport_attempt_id, transport_epoch: checked.transport_epoch, native_attempt_id: checked.native_attempt_id, native_epoch: checked.native_epoch, capture_kind: checked.capture_kind, snapshot_seq: checked.snapshot_seq, file_manifest_digest: manifest.manifest_digest};
  seoRequire(checked.fragment_id === "frag_" + sha256Sync(resultIdentityBytes(identity)), "DEBUG_FRAGMENT_ID_INVALID", "$.fragment_id");
  if (checked.task_result_core.availability === "available") {
    seoRequire(taskResultCore !== null, "DEBUG_TERMINAL_CORE_REQUIRED", "$.task_result_core");
    const core = validateDocument("task-result-core/v1", taskResultCore), attempt = core.attempt_identity;
    seoRequire(seoRefMatchesDocument(checked.task_result_core.ref, "task-result-core/v1", core), "CAS_CORRUPT", "$.task_result_core.ref");
    seoRequire(checked.task_id === core.task_id && attempt.kind === "started", "DEBUG_TERMINAL_CORE_INVALID", "$.task_result_core");
    seoRequire(checked.native_attempt_id === attempt.attempt_id, "DEBUG_TERMINAL_CORE_INVALID", "$.native_attempt_id");
    seoRequire(checked.native_epoch === attempt.native_epoch, "DEBUG_TERMINAL_CORE_INVALID", "$.native_epoch");
    seoRequire(checked.task_version === core.published_from_version, "DEBUG_TERMINAL_CORE_INVALID", "$.task_version");
    seoRequire(checked.checkpoint_generation === core.provenance.checkpoint_generation, "DEBUG_TERMINAL_CORE_INVALID", "$.checkpoint_generation");
    seoRequire(core.final_source_state.availability === "available" && core.final_source_state.ref.sha256 === checked.source_state_id, "DEBUG_TERMINAL_CORE_INVALID", "$.source_state_id");
    const created = resultRfc3339Parts(core.created_at), captured = resultRfc3339Parts(checked.captured_at), terminal = resultRfc3339Parts(core.terminal_at);
    seoRequire(created !== null && captured !== null && terminal !== null && resultCompareKey(created, captured) <= 0 && resultCompareKey(captured, terminal) <= 0, "DEBUG_TERMINAL_CORE_INVALID", "$.captured_at");
  } else seoRequire(taskResultCore === null, "DEBUG_NONTERMINAL_CORE_INVALID", "$.task_result_core");
  return checked;
}

export async function verifyWorkerDebugDescriptorContent(descriptor, fragment, options = {}) {
  const checked = validateDocument("worker-debug-fragment-descriptor/v1", descriptor), {transportReceipt} = resultContextOptions(options);
  const fragmentDoc = validateDocument("worker-debug-fragment/v1", fragment), fragmentBytes = canonicalDocumentBytes(fragmentDoc);
  seoRequire(seoRefMatchesDocument(checked.fragment_ref, "worker-debug-fragment/v1", fragmentDoc), "CAS_CORRUPT", "$.fragment_ref");
  seoRequire(checked.snapshot_seq === fragmentDoc.snapshot_seq, "DEBUG_DESCRIPTOR_BINDING_INVALID", "$.snapshot_seq");
  seoRequire(checked.source_sha256 === sha256Sync(fragmentBytes), "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID", "$.source_sha256");
  if (checked.server_fragment_ref !== null) seoRequire(seoRefMatchesDocument(checked.server_fragment_ref, "worker-debug-fragment/v1", fragmentDoc), "CAS_CORRUPT", "$.server_fragment_ref");
  if (checked.state === "uploaded") {
    seoRequire(transportReceipt !== null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    const receipt = await resultCheckedReceipt(transportReceipt), captured = resultRfc3339Parts(fragmentDoc.captured_at), accepted = resultRfc3339Parts(receipt.accepted_at);
    seoRequire(seoRefMatchesDocument(checked.server_receipt_ref, "server-transport-receipt/v1", receipt), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(resultRefContentTuple(receipt.content_ref).every((value, index) => value === resultRefContentTuple(checked.server_fragment_ref)[index]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(captured !== null && accepted !== null && resultCompareKey(captured, accepted) <= 0, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
  } else seoRequire(transportReceipt === null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
  return checked;
}

export async function verifyTaskResultTransportEnvelope(envelope, core, options = {}) {
  const checked = validateDocument("task-result-transport-envelope/v1", envelope), {transportReceipt, workerDebugDescriptor} = resultContextOptions(options);
  const taskResult = checked.task_result, checkedCore = await verifyTaskResultCore(taskResult, core);
  seoRequire(seoRefMatchesDocument(checked.task_result_core_ref, "task-result-core/v1", checkedCore), "CAS_CORRUPT", "$.task_result_core_ref");
  seoRequire(checked.task_result_core_digest === sha256Sync(canonicalDocumentBytes(checkedCore)), "TRANSPORT_CORE_DIGEST_INVALID", "$.task_result_core_digest");
  const authority = checked.authority, fence = checked.full_fence;
  ["task_id", "attempt_id", "session_id", "owner_id", "lease_id", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"].forEach((key) => seoRequire(authority[key] === fence[key], "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence." + key));
  seoRequire(authority.task_version === fence.task_version, "TRANSPORT_AUTHORITY_FENCE_INVALID", "$.full_fence.task_version");
  seoRequire(authority.task_version === taskResult.published_from_version, "TRANSPORT_RESULT_VERSION_INVALID", "$.task_result.published_from_version");
  const debug = taskResult.diagnostics.worker_debug_fragment;
  let descriptor = checked.worker_debug_descriptor;
  if (debug.availability === "available") {
    seoRequire(descriptor !== null, "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor");
    seoRequire(workerDebugDescriptor !== null && typeof workerDebugDescriptor === "object" && !Array.isArray(workerDebugDescriptor), "TRANSPORT_DEBUG_DESCRIPTOR_REQUIRED", "$.worker_debug_descriptor");
    descriptor = validateDocument("worker-debug-fragment-descriptor/v1", workerDebugDescriptor);
    seoRequire(checked.worker_debug_descriptor !== null && canonicalString(checked.worker_debug_descriptor) === canonicalString(descriptor), "TRANSPORT_DEBUG_DESCRIPTOR_CONFLICT", "$.worker_debug_descriptor");
    seoRequire(seoRefMatchesDocument(debug.ref, "worker-debug-fragment-descriptor/v1", descriptor), "TASK_RESULT_CONTEXT_INVALID", "$.task_result.diagnostics.worker_debug_fragment.ref");
  } else {
    seoRequire(workerDebugDescriptor === null && checked.worker_debug_descriptor === null, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor");
  }
  if (checked.transport_receipt.availability === "available") {
    seoRequire(transportReceipt !== null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    const receipt = await resultCheckedReceipt(transportReceipt);
    seoRequire(seoRefMatchesDocument(checked.transport_receipt.ref, "server-transport-receipt/v1", receipt), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt.ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(descriptor !== null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.worker_debug_descriptor", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(seoRefMatchesDocument(descriptor.server_receipt_ref, "server-transport-receipt/v1", receipt), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.worker_debug_descriptor.server_receipt_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(resultRefContentTuple(receipt.content_ref).every((value, index) => value === resultRefContentTuple(descriptor.fragment_ref)[index]), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt.content_ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(canonicalString(receipt.package) === canonicalString(checked.package) && ["task_id", "attempt_id", "session_id", "owner_id", "lease_id", "task_version", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"].every((key) => receipt[key] === authority[key]) && receipt.authority_digest === authority.authority_digest && receipt.grant_digest === authority.grant.grant_digest, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
  } else {
    seoRequire(transportReceipt === null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(canonicalString(checked.transport_receipt) === canonicalString({availability: "not_applicable", reason_code: "TRANSPORT_RECEIPT_NOT_APPLICABLE"}), "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  }
  const canonicalBytes = canonicalValidatedBytes("task-result-transport-envelope/v1", checked);
  return {document: checked, canonical_bytes: canonicalBytes, transport_envelope_digest: sha256Sync(canonicalBytes)};
}

export async function verifyTaskResultTransportAck(ack, envelope, options = {}) {
  const checked = await verifyDocumentDigest("task-result-transport-ack/v1", ack), {transportReceipt} = resultContextOptions(options);
  const document = validateDocument("task-result-transport-envelope/v1", envelope), taskResult = document.task_result, raw = canonicalDocumentBytes(document);
  seoRequire(canonicalString(checked.package) === canonicalString(document.package), "TRANSPORT_ACK_PACKAGE_INVALID", "$.package");
  ["result_id", "task_id", "outcome", "published_from_version", "terminal_task_version"].forEach((field) => seoRequire(checked[field] === taskResult[field], "TRANSPORT_ACK_CONTEXT_INVALID", "$." + field));
  seoRequire(checked.transport_envelope_digest === sha256Sync(raw), "TRANSPORT_ACK_DIGEST_INVALID", "$.transport_envelope_digest");
  if (document.transport_receipt.availability === "available") {
    seoRequire(transportReceipt !== null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    const receipt = await resultCheckedReceipt(transportReceipt);
    seoRequire(seoRefMatchesDocument(document.transport_receipt.ref, "server-transport-receipt/v1", receipt), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt.ref", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(checked.receipt_binding_state === "bound" && checked.receipt_digest === receipt.receipt_digest, "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state");
    resultTimeLeq(receipt.accepted_at, checked.accepted_at, "$.accepted_at");
  } else {
    seoRequire(transportReceipt === null, "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.transport_receipt", "TRANSPORT_RECEIPT_BINDING_CONFLICT");
    seoRequire(checked.receipt_binding_state === "not_applicable" && checked.receipt_digest === null, "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "$.receipt_binding_state");
  }
  resultTimeLeq(taskResult.terminal_at, checked.accepted_at, "$.accepted_at");
  return checked;
}

export const derive_task_result_core = deriveTaskResultCore;
export const verify_task_result_context = verifyTaskResultContext;
export const verify_task_result_core = verifyTaskResultCore;
export const verify_task_result_transport_ack = verifyTaskResultTransportAck;
export const verify_task_result_transport_envelope = verifyTaskResultTransportEnvelope;
export const verify_worker_debug_descriptor_content = verifyWorkerDebugDescriptorContent;
export const verify_worker_debug_fragment_content = verifyWorkerDebugFragmentContent;
'''


__all__ = ["NPM_RESULT_CONTEXT"]
