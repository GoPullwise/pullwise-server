"""Generated npm facade semantics for GatePreparation documents."""

from __future__ import annotations


NPM_GATE_PREPARATION = r'''
const TERMINALIZATION_REASON_CODES = new Set([
  "BUDGET_EXHAUSTED",
  "CAPABILITY_UNAVAILABLE",
  "DEADLINE_REACHED",
  "INTERACTION_UNAVAILABLE",
  "POLICY_INVARIANT_BROKEN",
  "PROTOCOL_FAILURE",
  "RUNTIME_FAILURE",
  "STORAGE_FAILURE",
]);
const TERMINALIZATION_CONTROL_ACTOR_KINDS = new Set([
  "server_control",
  "system_reconciler",
  "worker_control",
]);
const TERMINALIZATION_REQUEST_LIFECYCLES = new Set([
  "QUEUED",
  "ACTIVE",
  "WAITING_INPUT",
  "WAITING_APPROVAL",
  "FINALIZING",
]);

function gatePreparationCompareText(left, right) {
  const leftCodePoints = [...left];
  const rightCodePoints = [...right];
  const length = Math.min(leftCodePoints.length, rightCodePoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftCodePoints[index].codePointAt(0) -
      rightCodePoints[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return leftCodePoints.length - rightCodePoints.length;
}

function gatePreparationCompareKey(left, right) {
  if (!Array.isArray(left)) return gatePreparationCompareText(left, right);
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = gatePreparationCompareText(left[index], right[index]);
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

function gatePreparationOrderedUnique(values, key) {
  const keys = values.map(key);
  for (let index = 1; index < keys.length; index += 1) {
    if (gatePreparationCompareKey(keys[index - 1], keys[index]) >= 0) {
      return false;
    }
  }
  return true;
}

function ruleDebugRedactionPlan(value) {
  if (!gatePreparationOrderedUnique(
    value.allowed_json_pointers, (item) => item,
  )) {
    fail("DEBUG_REDACTION_POINTER_ORDER_INVALID", "$.allowed_json_pointers");
  }
  if (!gatePreparationOrderedUnique(value.rule_ids, (item) => item)) {
    fail("DEBUG_REDACTION_RULE_ORDER_INVALID", "$.rule_ids");
  }
  if (!gatePreparationOrderedUnique(
    value.debug_input_refs,
    (item) => [item.content_schema_id, item.artifact_id, item.sha256],
  )) {
    fail("DEBUG_REDACTION_INPUT_ORDER_INVALID", "$.debug_input_refs");
  }
  verifyContentRefSet(value.debug_input_refs);
}

function rulePublicationContentManifest(value) {
  if (value.entry_count !== value.entries.length) {
    fail("PUBLICATION_ENTRY_COUNT_INVALID", "$.entry_count");
  }
  if (!gatePreparationOrderedUnique(
    value.entries, (item) => item.json_pointer,
  )) {
    fail("PUBLICATION_ENTRY_ORDER_INVALID", "$.entries");
  }
  value.entries.forEach((entry, index) => {
    const receipt = entry.redaction_receipt;
    const receiptPath = "$.entries[" + index + "].redaction_receipt";
    if (receipt.policy_digest !== value.redaction_policy_digest) {
      fail("PUBLICATION_REDACTION_POLICY_INVALID", receiptPath + ".policy_digest");
    }
    const originalSha256 = entry.content_kind === "artifact_bytes"
      ? entry.source_ref.sha256 : entry.inline_digest;
    if (receipt.original_sha256 !== originalSha256) {
      fail("PUBLICATION_REDACTION_SOURCE_INVALID", receiptPath + ".original_sha256");
    }
  });
}

function ruleTerminalizationFact(value) {
  if (!TERMINALIZATION_REASON_CODES.has(value.reason_code)) {
    fail("TERMINALIZATION_REASON_INVALID", "$.reason_code");
  }
  const expectedKey = "terminalize:" + value.reason_code.toLowerCase() +
    ":" + value.observed_task_version;
  if (value.idempotency_key !== expectedKey) {
    fail("TERMINALIZATION_IDEMPOTENCY_KEY_INVALID", "$.idempotency_key");
  }
  if (!TERMINALIZATION_CONTROL_ACTOR_KINDS.has(value.source.kind)) {
    fail("TERMINALIZATION_ACTOR_INVALID", "$.source.kind");
  }
  if (!gatePreparationOrderedUnique(
    value.evidence_refs,
    (item) => [item.content_schema_id, item.artifact_id, item.sha256],
  )) {
    fail("TERMINALIZATION_EVIDENCE_ORDER_INVALID", "$.evidence_refs");
  }
  verifyContentRefSet(value.evidence_refs);
  if (value.reason_code === "BUDGET_EXHAUSTED" && !value.evidence_refs.some(
    (item) => item.content_schema_id === "budget-summary/v1",
  )) {
    fail("TERMINALIZATION_BUDGET_EVIDENCE_REQUIRED", "$.evidence_refs");
  }
}

/**
 * Bind a terminalization fact to current task state and exact retry bytes.
 * Stable signature: (fact, taskId, currentTaskVersion, lifecycleState,
 * existingFact = null) -> Promise<fact>.
 */
export async function verifyTerminalizationFactContext(
  fact,
  taskId,
  currentTaskVersion,
  lifecycleState,
  existingFact = null,
) {
  const validated = await verifyDocumentDigest("terminalization-fact/v1", fact);
  if (typeof taskId !== "string" || validated.task_id !== taskId) {
    fail("TASK_ID_COLLISION", "$.task_id");
  }
  if (!Number.isSafeInteger(currentTaskVersion) || currentTaskVersion < 1 ||
      validated.observed_task_version !== currentTaskVersion) {
    fail("TASK_VERSION_STALE", "$.observed_task_version");
  }
  if (!TERMINALIZATION_REQUEST_LIFECYCLES.has(lifecycleState)) {
    fail("STATE_TRANSITION_INVALID", "$.lifecycle_state");
  }
  if (existingFact !== null && existingFact !== undefined) {
    const existing = await verifyDocumentDigest(
      "terminalization-fact/v1", existingFact,
    );
    if (existing.idempotency_key === validated.idempotency_key &&
        canonicalString(existing) !== canonicalString(validated)) {
      fail("IDEMPOTENCY_CONFLICT", "$.idempotency_key");
    }
  }
  return validated;
}
'''


__all__ = ["NPM_GATE_PREPARATION"]
