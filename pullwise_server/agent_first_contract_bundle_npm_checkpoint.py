"""Node facade semantics for dual-layer committed checkpoints."""

from __future__ import annotations


NPM_CHECKPOINT = r'''
function checkpointCurrentPackage(value) {
  if (canonicalString(value.package) !== canonicalString(packageTuple())) {
    fail("CHECKPOINT_PACKAGE_MISMATCH", "$.package");
  }
}

function ruleMachineCheckpoint(value) {
  checkpointCurrentPackage(value);
}

function checkpointSortedUnique(values) {
  return Array.isArray(values) && new Set(values).size === values.length &&
    JSON.stringify(values) === JSON.stringify([...values].sort());
}

function ruleSemanticCheckpoint(value) {
  checkpointCurrentPackage(value);
  const summary = value.owner_summary;
  for (const field of [
    "completed_requirement_ids", "next_requirement_ids",
    "unresolved_question_ids", "residual_risk_ids",
  ]) {
    if (!checkpointSortedUnique(summary[field])) {
      fail(
        "SEMANTIC_CHECKPOINT_SUMMARY_ORDER_INVALID",
        "$.owner_summary." + field,
      );
    }
  }
  const next = new Set(summary.next_requirement_ids);
  if (summary.completed_requirement_ids.some((item) => next.has(item))) {
    fail("SEMANTIC_CHECKPOINT_REQUIREMENT_OVERLAP", "$.owner_summary");
  }
  if (!checkpointSortedUnique(value.pending_interaction_ids) ||
      canonicalString(summary.unresolved_question_ids) !==
        canonicalString(value.pending_interaction_ids)) {
    fail(
      "SEMANTIC_CHECKPOINT_INTERACTION_MISMATCH",
      "$.pending_interaction_ids",
    );
  }
  if (encoder.encode(summary.objective_restated).length > 16384) {
    fail("UTF8_BYTE_LIMIT_INVALID", "$.owner_summary.objective_restated");
  }
  const evidence = verifyContentRefSet(value.evidence_refs);
  const keys = evidence.map((item) => [
    item.content_schema_id, item.artifact_id, item.sha256,
  ].join("\0"));
  if (new Set(keys).size !== keys.length ||
      JSON.stringify(keys) !== JSON.stringify([...keys].sort())) {
    fail("SEMANTIC_CHECKPOINT_EVIDENCE_ORDER_INVALID", "$.evidence_refs");
  }
}

function ruleCommittedCheckpointManifest(value) {
  checkpointCurrentPackage(value);
  const generation = value.generation;
  const predecessorValid = value.previous_generation === generation - 1 &&
    ((generation === 1 && value.previous_manifest_hash === null) ||
     (generation > 1 && value.previous_manifest_hash !== null));
  if (!predecessorValid) {
    fail("CHECKPOINT_PREDECESSOR_INVALID", "$.previous_generation");
  }
  if (value.committed_task_version !== value.committed_from_task_version + 1) {
    fail("CHECKPOINT_TASK_VERSION_INVALID", "$.committed_task_version");
  }
}

function checkpointRefMatches(ref, schemaId, document) {
  const raw = canonicalDocumentBytes(document);
  return ref.content_schema_id === schemaId &&
    ref.sha256 === sha256Sync(raw) && ref.size_bytes === raw.length &&
    ref.media_type === "application/json" && ref.encoding === "utf-8";
}

export async function verifyCommittedCheckpointContext(
  manifest, machineState, semanticState, previousManifest = null,
) {
  const committed = await verifyDocumentDigest(
    "committed-checkpoint-manifest/v1", manifest,
  );
  const machine = await verifyDocumentDigest(
    "machine-checkpoint/v1", machineState,
  );
  const semantic = await verifyDocumentDigest(
    "semantic-checkpoint/v1", semanticState,
  );
  if (canonicalString(committed.package) !== canonicalString(machine.package) ||
      canonicalString(committed.package) !== canonicalString(semantic.package)) {
    fail("CHECKPOINT_PACKAGE_MISMATCH", "$.package");
  }
  if (committed.task_id !== machine.task_id ||
      committed.task_id !== semantic.task_id) {
    fail("CHECKPOINT_TASK_BINDING_MISMATCH", "$.task_id");
  }
  if (committed.generation !== machine.generation ||
      committed.generation !== semantic.generation) {
    fail("CHECKPOINT_GENERATION_MISMATCH", "$.generation");
  }
  if (machine.task_version !== semantic.task_version ||
      machine.task_version !== committed.committed_from_task_version) {
    fail(
      "CHECKPOINT_TASK_VERSION_MISMATCH",
      "$.committed_from_task_version",
    );
  }
  if (committed.attempt_id !== machine.attempt_id ||
      committed.native_epoch !== machine.native_epoch ||
      committed.owner_epoch !== machine.owner_epoch ||
      committed.owner_epoch !== semantic.owner_epoch ||
      machine.owner_id !== semantic.owner_id) {
    fail("CHECKPOINT_AUTHORITY_BINDING_MISMATCH", "$.attempt_id");
  }
  if (!checkpointRefMatches(
    committed.machine_state_ref, "machine-checkpoint/v1", machine,
  )) fail("CHECKPOINT_STATE_REF_MISMATCH", "$.machine_state_ref");
  if (!checkpointRefMatches(
    committed.semantic_state_ref, "semantic-checkpoint/v1", semantic,
  )) fail("CHECKPOINT_STATE_REF_MISMATCH", "$.semantic_state_ref");
  for (const field of [
    "budget_watermark", "effect_watermark", "observation_watermark", "event_seq",
  ]) {
    if (committed[field] !== machine[field]) {
      fail("CHECKPOINT_WATERMARK_MISMATCH", "$." + field);
    }
  }
  if (committed.created_at !== machine.created_at ||
      committed.created_at !== semantic.created_at) {
    fail("CHECKPOINT_TIMESTAMP_MISMATCH", "$.created_at");
  }
  if (committed.generation === 1) {
    if (previousManifest !== null) {
      fail("CHECKPOINT_CHAIN_UNEXPECTED_PREVIOUS", "$.previous_manifest_hash");
    }
    return committed;
  }
  if (previousManifest === null) {
    fail("CHECKPOINT_CHAIN_PREVIOUS_REQUIRED", "$.previous_manifest_hash");
  }
  const previous = await verifyDocumentDigest(
    "committed-checkpoint-manifest/v1", previousManifest,
  );
  if (canonicalString(previous.package) !== canonicalString(committed.package) ||
      previous.task_id !== committed.task_id ||
      previous.generation !== committed.previous_generation ||
      previous.manifest_hash !== committed.previous_manifest_hash) {
    fail("CHECKPOINT_CHAIN_MISMATCH", "$.previous_manifest_hash");
  }
  if (previous.committed_task_version !== committed.committed_from_task_version) {
    fail(
      "CHECKPOINT_CHAIN_TASK_VERSION_MISMATCH",
      "$.committed_from_task_version",
    );
  }
  for (const field of [
    "budget_watermark", "effect_watermark", "observation_watermark", "event_seq",
  ]) {
    if (committed[field] < previous[field]) {
      fail("CHECKPOINT_WATERMARK_REGRESSION", "$.event_seq");
    }
  }
  if (committed.created_at < previous.created_at) {
    fail("CHECKPOINT_TIMESTAMP_REGRESSION", "$.created_at");
  }
  return committed;
}
'''


__all__ = ["NPM_CHECKPOINT"]
