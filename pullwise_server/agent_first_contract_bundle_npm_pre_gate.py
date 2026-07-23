"""Generated npm facade semantics for PreGate documents."""

from __future__ import annotations


NPM_PRE_GATE = r'''
const PRE_GATE_ROOT_FIELDS = [
  "request", "policy", "charter", "ledger", "waiver_events", "proposal",
  "original_source", "final_source", "execution_states", "change_set",
  "pre_observation_manifest", "final_observation_manifest", "verifier_inputs",
  "verifier_work", "attestations", "artifacts", "report", "effect_ledger",
  "budget_summary", "termination_facts", "publication_content_manifest",
  "debug_redaction_plan",
];
const PRE_GATE_ROOT_KEYS = new Set([
  "schema_id", "task_id", "root_set_digest",
  ...PRE_GATE_ROOT_FIELDS,
]);
const PRE_GATE_ALWAYS_AVAILABLE = [
  "request", "policy", "ledger", "effect_ledger", "budget_summary",
  "publication_content_manifest", "debug_redaction_plan",
];
const PRE_GATE_SUCCESS_OUTCOMES = new Set([
  "COMPLETED", "COMPLETED_WITH_WAIVERS", "NO_CHANGE_NEEDED",
]);
const PRE_GATE_SUCCESS_AVAILABLE = [
  "charter", "proposal", "original_source", "final_source",
  "pre_observation_manifest", "final_observation_manifest", "attestations",
  "report",
];
const PRE_GATE_FORBIDDEN_CLOSURE_TARGETS = new Set([
  "error-response/v1", "evidence-closure-manifest/v1", "gate-decision/v1",
  "gate-input-snapshot/v1", "server-debug-assembly/v1",
  "server-debug-snapshot/v1", "task-result-core/v1", "task-result/v1",
  "terminalization-input-snapshot/v1", "worker-debug-fragment/v1",
]);

function preGateCompareText(left, right) {
  const leftPoints = [...left];
  const rightPoints = [...right];
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index].codePointAt(0) -
      rightPoints[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function preGateCompareKey(left, right) {
  if (!Array.isArray(left)) return preGateCompareText(left, right);
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = preGateCompareText(left[index], right[index]);
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

function preGateOrderedUnique(values, key) {
  const keys = values.map(key);
  for (let index = 1; index < keys.length; index += 1) {
    if (preGateCompareKey(keys[index - 1], keys[index]) >= 0) return false;
  }
  return true;
}

function preGateSameKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

function preGateAvailability(value) {
  return value.availability;
}

function rulePreGateRootSet(value) {
  if (!preGateSameKeys(value, PRE_GATE_ROOT_KEYS)) {
    fail("PRE_GATE_ROOT_FIELDS_INVALID");
  }
  for (const field of PRE_GATE_ALWAYS_AVAILABLE) {
    if (preGateAvailability(value[field]) !== "available") {
      fail("PRE_GATE_REQUIRED_ROOT_UNAVAILABLE", "$." + field);
    }
  }
}

function rulePreGateEvidenceClosureManifest(value) {
  const entries = value.entries;
  if (value.entry_count !== entries.length) {
    fail("PRE_GATE_CLOSURE_ENTRY_COUNT_INVALID", "$.entry_count");
  }
  if (!preGateOrderedUnique(
    entries,
    (item) => [item.content_schema_id, item.artifact_id, item.sha256],
  )) {
    fail("PRE_GATE_CLOSURE_ENTRY_ORDER_INVALID", "$.entries");
  }
  verifyContentRefSet(entries);
  if (!entries.some(
    (item) => canonicalString(item) === canonicalString(value.pre_gate_root_set_ref),
  )) {
    fail("PRE_GATE_CLOSURE_ROOT_MISSING", "$.entries");
  }
  if (entries.some(
    (item) => PRE_GATE_FORBIDDEN_CLOSURE_TARGETS.has(item.content_schema_id),
  )) {
    fail("PRE_GATE_CLOSURE_DIRECTION_INVALID", "$.entries");
  }
  const expectedDigest = sha256Sync(canonicalDocumentBytes(entries));
  if (value.pre_gate_closure_digest !== expectedDigest) {
    fail("PRE_GATE_CLOSURE_DIGEST_INVALID", "$.pre_gate_closure_digest");
  }
}

function contentRefMatchesDirectDocument(ref, schemaId, document) {
  const raw = canonicalDocumentBytes(document);
  return ref.content_schema_id === schemaId &&
    ref.sha256 === sha256Sync(raw) &&
    ref.size_bytes === raw.length &&
    ref.media_type === "application/json" && ref.encoding === "utf-8";
}

function preGateAvailableRootRefs(rootSet) {
  const refs = [];
  for (const field of PRE_GATE_ROOT_FIELDS) {
    const value = rootSet[field];
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) {
      if (item.availability === "available") refs.push(item.ref);
    }
  }
  return refs;
}

/** Verify an outcome-neutral root set against direct task context. */
export async function verifyPreGateRootSetContext(
  rootSet,
  taskId,
) {
  const validated = await verifyDocumentDigest("pre-gate-root-set/v1", rootSet);
  if (typeof taskId !== "string" || validated.task_id !== taskId) {
    fail("PRE_GATE_TASK_BINDING_INVALID", "$.task_id");
  }
  return validated;
}

/** Bind a PreGate closure to the direct root-set document. */
export async function verifyPreGateEvidenceClosureContext(manifest, rootSet) {
  const validated = await verifyDocumentDigest(
    "pre-gate-evidence-closure-manifest/v1", manifest,
  );
  const roots = await verifyDocumentDigest("pre-gate-root-set/v1", rootSet);
  if (validated.task_id !== roots.task_id) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.task_id");
  }
  if (!contentRefMatchesDirectDocument(
    validated.pre_gate_root_set_ref, "pre-gate-root-set/v1", roots,
  )) {
    fail("CAS_CORRUPT", "$.pre_gate_root_set_ref");
  }
  const entries = new Set(validated.entries.map((item) => canonicalString(item)));
  if (!preGateAvailableRootRefs(roots).every(
    (item) => entries.has(canonicalString(item)),
  )) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.entries");
  }
  return validated;
}
'''


__all__ = ["NPM_PRE_GATE"]
