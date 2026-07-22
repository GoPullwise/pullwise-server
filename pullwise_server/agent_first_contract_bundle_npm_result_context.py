"""Node facade result/debug/transport context helpers."""

from __future__ import annotations


NPM_RESULT_CONTEXT = r'''
function resultContextOptions(value) {
  if (value === undefined || value === null) return {workerDebugDescriptor: null, transportReceipt: null};
  if (typeof value === "object" && !Array.isArray(value) && ("workerDebugDescriptor" in value || "transportReceipt" in value)) return {workerDebugDescriptor: value.workerDebugDescriptor ?? null, transportReceipt: value.transportReceipt ?? null};
  return {workerDebugDescriptor: value, transportReceipt: null};
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
  const checked = validateDocument("task-result/v1", taskResult), {workerDebugDescriptor} = resultContextOptions(options), debug = checked.diagnostics.worker_debug_fragment;
  if (debug.availability === "available") {
    const descriptor = await verifyDocumentDigest("worker-debug-fragment-descriptor/v1", workerDebugDescriptor);
    seoRequire(seoRefMatchesDocument(debug.ref, "worker-debug-fragment-descriptor/v1", descriptor), "CAS_CORRUPT", "$.diagnostics.worker_debug_fragment.ref");
  } else seoRequire(workerDebugDescriptor === null, "TASK_RESULT_DEBUG_DESCRIPTOR_INVALID", "$.diagnostics.worker_debug_fragment");
  return checked;
}

export async function verifyTaskResultCore(taskResult, core) {
  const expected = deriveTaskResultCore(await verifyTaskResultContext(taskResult)), checked = validateDocument("task-result-core/v1", core);
  seoRequire(canonicalString(checked) === canonicalString(expected), "TASK_RESULT_CORE_CONTEXT_INVALID");
  return checked;
}

export async function verifyWorkerDebugFragmentContent(fragment, taskResultCore, fileManifest, redactionReport) {
  const checked = validateDocument("worker-debug-fragment/v1", fragment), core = await verifyDocumentDigest("task-result-core/v1", taskResultCore);
  const manifest = await verifyDocumentDigest("worker-debug-file-manifest/v1", fileManifest), report = await verifyDocumentDigest("worker-debug-redaction-report/v1", redactionReport);
  seoRequire(seoRefMatchesDocument(checked.file_manifest_ref, "worker-debug-file-manifest/v1", manifest), "CAS_CORRUPT", "$.file_manifest_ref");
  seoRequire(seoRefMatchesDocument(checked.redaction_report_ref, "worker-debug-redaction-report/v1", report), "CAS_CORRUPT", "$.redaction_report_ref");
  const identity = {task_id: checked.task_id, job_id: checked.job_id, run_id: checked.run_id, lease_id: checked.lease_id, transport_attempt_id: checked.transport_attempt_id, transport_epoch: checked.transport_epoch, native_attempt_id: checked.native_attempt_id, native_epoch: checked.native_epoch, capture_kind: checked.capture_kind, snapshot_seq: checked.snapshot_seq, file_manifest_digest: manifest.manifest_digest};
  seoRequire(checked.fragment_id === "frag_" + sha256Sync(resultIdentityBytes(identity)), "DEBUG_FRAGMENT_ID_INVALID", "$.fragment_id");
  if (checked.task_result_core.availability === "available") {
    seoRequire(seoRefMatchesDocument(checked.task_result_core.ref, "task-result-core/v1", core), "CAS_CORRUPT", "$.task_result_core.ref");
    seoRequire(checked.task_id === core.task_id && checked.native_attempt_id === core.attempt_identity.attempt_id && checked.native_epoch === core.attempt_identity.native_epoch, "DEBUG_FRAGMENT_CONTEXT_INVALID", "$.task_result_core");
    seoRequire(core.final_source_state.availability === "available" && core.final_source_state.ref.sha256 === checked.source_state_id, "DEBUG_FRAGMENT_CONTEXT_INVALID", "$.source_state_id");
    seoRequire(checked.task_version === core.published_from_version, "DEBUG_FRAGMENT_CONTEXT_INVALID", "$.task_version");
    seoRequire(checked.checkpoint_generation === core.provenance.checkpoint_generation, "DEBUG_FRAGMENT_CONTEXT_INVALID", "$.checkpoint_generation");
    resultTimeLeq(core.created_at, checked.captured_at, "$.captured_at");
    resultTimeLeq(checked.captured_at, core.terminal_at, "$.captured_at");
  }
  return checked;
}

export async function verifyWorkerDebugDescriptorContent(descriptor, fragment, options = {}) {
  const checked = await verifyDocumentDigest("worker-debug-fragment-descriptor/v1", descriptor), {transportReceipt} = resultContextOptions(options);
  const fragmentDoc = validateDocument("worker-debug-fragment/v1", fragment), fragmentBytes = canonicalDocumentBytes(fragmentDoc);
  seoRequire(seoRefMatchesDocument(checked.fragment_ref, "worker-debug-fragment/v1", fragmentDoc), "CAS_CORRUPT", "$.fragment_ref");
  seoRequire(checked.source_sha256 === sha256Sync(fragmentBytes), "DEBUG_DESCRIPTOR_SOURCE_DIGEST_INVALID", "$.source_sha256");
  seoRequire(checked.snapshot_seq === fragmentDoc.snapshot_seq, "DEBUG_DESCRIPTOR_CONTEXT_INVALID", "$.snapshot_seq");
  if (checked.state === "uploaded") {
    const receipt = await verifyDocumentDigest("server-transport-receipt/v1", transportReceipt);
    seoRequire(seoRefMatchesDocument(checked.server_fragment_ref, "worker-debug-fragment/v1", fragmentDoc), "CAS_CORRUPT", "$.server_fragment_ref");
    seoRequire(seoRefMatchesDocument(checked.server_receipt_ref, "server-transport-receipt/v1", receipt), "CAS_CORRUPT", "$.server_receipt_ref");
    seoRequire(seoRefMatchesDocument(receipt.content_ref, "worker-debug-fragment/v1", fragmentDoc), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.server_receipt_ref");
    resultTimeLeq(fragmentDoc.captured_at, receipt.accepted_at, "$.server_receipt_ref");
  } else seoRequire(transportReceipt === null, "DEBUG_DESCRIPTOR_BINDING_INVALID", "$.transportReceipt");
  return checked;
}

export async function verifyTaskResultTransportEnvelope(envelope, core, options = {}) {
  const checked = validateDocument("task-result-transport-envelope/v1", envelope), {transportReceipt, workerDebugDescriptor} = resultContextOptions(options);
  const taskResult = await verifyTaskResultContext(checked.task_result, {workerDebugDescriptor}), checkedCore = await verifyTaskResultCore(taskResult, core);
  seoRequire(seoRefMatchesDocument(checked.task_result_core_ref, "task-result-core/v1", checkedCore), "CAS_CORRUPT", "$.task_result_core_ref");
  seoRequire(checked.task_result_core_digest === sha256Sync(canonicalDocumentBytes(checkedCore)), "TRANSPORT_CORE_DIGEST_INVALID", "$.task_result_core_digest");
  const debug = taskResult.diagnostics.worker_debug_fragment;
  if (debug.availability === "available") {
    const descriptor = await verifyDocumentDigest("worker-debug-fragment-descriptor/v1", workerDebugDescriptor);
    seoRequire(checked.worker_debug_descriptor !== null && canonicalString(checked.worker_debug_descriptor) === canonicalString(descriptor), "TRANSPORT_DEBUG_REF_INVALID", "$.worker_debug_descriptor");
    if (descriptor.state === "uploaded") {
      const receipt = await verifyDocumentDigest("server-transport-receipt/v1", transportReceipt);
      seoRequire(checked.transport_receipt.availability === "available", "TRANSPORT_RECEIPT_REQUIRED", "$.transport_receipt");
      seoRequire(seoRefMatchesDocument(checked.transport_receipt.ref, "server-transport-receipt/v1", receipt), "CAS_CORRUPT", "$.transport_receipt.ref");
      seoRequire(seoRefMatchesDocument(descriptor.server_receipt_ref, "server-transport-receipt/v1", receipt), "TRANSPORT_RECEIPT_BINDING_CONFLICT", "$.worker_debug_descriptor.server_receipt_ref");
    } else seoRequire(transportReceipt === null, "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  } else {
    seoRequire(workerDebugDescriptor === null && checked.worker_debug_descriptor === null, "TRANSPORT_DEBUG_DESCRIPTOR_INVALID", "$.worker_debug_descriptor");
    seoRequire(transportReceipt === null, "TRANSPORT_RECEIPT_MATRIX_INVALID", "$.transport_receipt");
  }
  const canonicalBytes = canonicalValidatedBytes("task-result-transport-envelope/v1", checked);
  return {document: checked, canonical_bytes: canonicalBytes, transport_envelope_digest: sha256Sync(canonicalBytes)};
}

export async function verifyTaskResultTransportAck(ack, envelope, options = {}) {
  const checked = await verifyDocumentDigest("task-result-transport-ack/v1", ack), {transportReceipt} = resultContextOptions(options);
  const verified = await verifyTaskResultTransportEnvelope(envelope, deriveTaskResultCore(envelope.task_result), {transportReceipt, workerDebugDescriptor: envelope.worker_debug_descriptor});
  const taskResult = verified.document.task_result;
  seoRequire(canonicalString(checked.package) === canonicalString(verified.document.package), "CURRENT_PACKAGE_PIN_MISMATCH", "$.package");
  ["result_id", "task_id", "outcome", "published_from_version", "terminal_task_version"].forEach((field) => seoRequire(checked[field] === taskResult[field], "TRANSPORT_ACK_CONTEXT_INVALID", "$." + field));
  seoRequire(checked.transport_envelope_digest === verified.transport_envelope_digest, "TRANSPORT_ENVELOPE_DIGEST_INVALID", "$.transport_envelope_digest");
  if (checked.receipt_binding_state === "bound") {
    const receipt = await verifyDocumentDigest("server-transport-receipt/v1", transportReceipt);
    seoRequire(checked.receipt_digest === receipt.receipt_digest, "TRANSPORT_ACK_CONTEXT_INVALID", "$.receipt_digest");
    resultTimeLeq(receipt.accepted_at, checked.accepted_at, "$.accepted_at");
  } else seoRequire(transportReceipt === null, "TRANSPORT_ACK_CONTEXT_INVALID", "$.receipt_digest");
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
