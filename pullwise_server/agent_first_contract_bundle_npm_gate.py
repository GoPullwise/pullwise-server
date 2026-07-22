"""Generated npm facade semantics for GateDecision documents."""

from __future__ import annotations


NPM_GATE = r'''
const GATE_PREDICATE_ENTRIES = [
  {predicate_id: "GATE_TASK_STATE", decision_kind: "success",
    input_schema_ids: ["attempt-record/v1", "gate-input-snapshot/v1", "task-record/v1"],
    failure_codes: ["GATE_INPUT_STALE", "STATE_TRANSITION_INVALID", "TASK_VERSION_STALE"]},
  {predicate_id: "GATE_LEASE_VALID", decision_kind: "success",
    input_schema_ids: ["gate-input-snapshot/v1"],
    failure_codes: ["LEASE_INVALID", "NATIVE_EPOCH_STALE", "OWNER_EPOCH_STALE"]},
  {predicate_id: "GATE_DEADLINE", decision_kind: "success",
    input_schema_ids: ["budget-summary/v1", "gate-input-snapshot/v1"],
    failure_codes: ["ABSOLUTE_DEADLINE_EXCEEDED", "TERMINALIZATION_RESERVE_REACHED"]},
  {predicate_id: "GATE_POLICY", decision_kind: "success",
    input_schema_ids: ["effective-execution-policy/v1"],
    failure_codes: ["POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED"]},
  {predicate_id: "GATE_LEDGER", decision_kind: "success",
    input_schema_ids: ["requirement-ledger/v1"],
    failure_codes: ["CONTRACT_DOCUMENT_INVALID", "REQUIREMENT_ID_COLLISION"]},
  {predicate_id: "GATE_SOURCE_FROZEN", decision_kind: "success",
    input_schema_ids: ["source-tree-manifest/v1"],
    failure_codes: ["SOURCE_STATE_CHANGED", "SOURCE_STATE_MISMATCH"]},
  {predicate_id: "GATE_PROPOSAL_FRESH", decision_kind: "success",
    input_schema_ids: ["completion-proposal/v1", "gate-input-snapshot/v1"],
    failure_codes: ["GATE_INPUT_STALE", "SOURCE_STATE_MISMATCH"]},
  {predicate_id: "GATE_QUALITY_PLAN", decision_kind: "success",
    input_schema_ids: ["quality-policy-plan/v1"],
    failure_codes: ["POLICY_INVARIANT_BROKEN", "ROLE_NOT_ENABLED"]},
  {predicate_id: "GATE_ATTESTATIONS", decision_kind: "success",
    input_schema_ids: ["observation-manifest/v1", "verification-attestation-manifest/v1"],
    failure_codes: ["ATTESTATION_NOT_INDEPENDENT", "OBSERVATION_ACTOR_MISMATCH", "OBSERVATION_MISSING"]},
  {predicate_id: "GATE_REQUIREMENTS", decision_kind: "success",
    input_schema_ids: ["requirement-ledger/v1", "verification-attestation-manifest/v1"],
    failure_codes: ["MANDATORY_REQUIREMENT_FAILED", "MANDATORY_REQUIREMENT_UNVERIFIABLE", "WAIVER_INVALID"]},
  {predicate_id: "GATE_OUTCOME_SHAPE", decision_kind: "success",
    input_schema_ids: ["completion-proposal/v1"],
    failure_codes: ["CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"]},
  {predicate_id: "GATE_EFFECTS_EMPTY", decision_kind: "success",
    input_schema_ids: ["effect-ledger-snapshot/v1"],
    failure_codes: ["EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"]},
  {predicate_id: "GATE_EVIDENCE_CLOSURE", decision_kind: "success",
    input_schema_ids: ["pre-gate-evidence-closure-manifest/v1", "pre-gate-root-set/v1"],
    failure_codes: ["CAS_CORRUPT", "EVIDENCE_CLOSURE_INVALID"]},
  {predicate_id: "GATE_BUDGET", decision_kind: "success",
    input_schema_ids: ["budget-summary/v1"],
    failure_codes: ["BUDGET_EXHAUSTED", "TERMINALIZATION_RESERVE_REACHED"]},
  {predicate_id: "GATE_SECRET_SCAN", decision_kind: "success",
    input_schema_ids: ["debug-redaction-plan/v1", "pre-gate-evidence-closure-manifest/v1", "publication-content-manifest/v1"],
    failure_codes: ["DEBUG_REDACTION_FAILED", "POLICY_INVARIANT_BROKEN"]},
  {predicate_id: "GATE_TERMINAL_AUTHORITY_FACT", decision_kind: "terminalization",
    input_schema_ids: ["terminalization-fact/v1", "terminalization-input-snapshot/v1"],
    failure_codes: ["CONTRACT_DOCUMENT_INVALID", "GATE_INPUT_STALE"]},
  {predicate_id: "GATE_TERMINAL_AVAILABILITY", decision_kind: "terminalization",
    input_schema_ids: ["terminalization-input-snapshot/v1"],
    failure_codes: ["EXECUTION_STATE_UNAVAILABLE", "OBSERVATION_MISSING", "SOURCE_STATE_MISMATCH"]},
  {predicate_id: "GATE_TERMINAL_NO_ACTIVE_EFFECTS", decision_kind: "terminalization",
    input_schema_ids: ["effect-ledger-snapshot/v1", "terminalization-input-snapshot/v1"],
    failure_codes: ["EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"]},
  {predicate_id: "GATE_TERMINAL_OUTCOME_CLASSIFICATION", decision_kind: "terminalization",
    input_schema_ids: ["terminalization-fact/v1", "terminalization-input-snapshot/v1"],
    failure_codes: ["CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"]},
  {predicate_id: "GATE_TERMINAL_ARTIFACT_DELIVERY", decision_kind: "terminalization",
    input_schema_ids: ["publication-content-manifest/v1", "terminalization-fact/v1"],
    failure_codes: ["DEBUG_UPLOAD_FAILED", "EVENT_DELIVERY_UNKNOWN", "PROTOCOL_FAILURE"]},
];

const GATE_TERMINAL_REASONS = {
  PARTIAL: new Set(["BUDGET_EXHAUSTED", "CAPABILITY_UNAVAILABLE", "DEADLINE_REACHED",
    "INTERACTION_UNAVAILABLE", "SAFE_PARTIAL_DELIVERY", "VERIFICATION_INCOMPLETE"]),
  BLOCKED: new Set(["APPROVAL_REQUIRED", "CAPABILITY_UNAVAILABLE", "ENVIRONMENT_UNAVAILABLE",
    "INPUT_REQUIRED", "INTERACTION_UNAVAILABLE", "POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED"]),
  FAILED: new Set(["BUDGET_EXHAUSTED", "CONTRACT_INVALID", "DEADLINE_REACHED",
    "POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED", "PROTOCOL_FAILURE", "QUALITY_GATE_FAILED",
    "RUNTIME_FAILURE", "SOURCE_MUTATION_FORBIDDEN", "STORAGE_FAILURE"]),
  CANCELLED: new Set(["LEASE_CANCELLED", "SERVER_CANCELLED", "USER_CANCELLED"]),
};

function gateVerifyDigest(schemaId, value, field) {
  const presented = value[field];
  if (presented === "0".repeat(64)) return;
  const spec = schema(schemaId)["x-pullwise-digest"];
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== field),
  );
  const domain = encoder.encode(spec.domain);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input.set(document, domain.length + 1);
  if (presented !== sha256Sync(input)) fail("CONTRACT_DIGEST_MISMATCH", "$." + field);
}

function ruleGatePredicateRegistry(value) {
  if (canonicalString(value.predicates) !== canonicalString(GATE_PREDICATE_ENTRIES)) {
    fail("GATE_PREDICATE_REGISTRY_INVALID", "$.predicates");
  }
  gateVerifyDigest("gate-predicate-registry/v1", value, "registry_digest");
}

function gateRegistryDigest() {
  return fixture("gate_golden_independent_registry").document.registry_digest;
}

function gateExpectedEntries(decisionKind) {
  return GATE_PREDICATE_ENTRIES.filter((item) => item.decision_kind === decisionKind);
}

function gateRefKey(value) {
  return [value.content_schema_id, value.artifact_id, value.sha256];
}

function compareGateKeys(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] < right[index]) return -1;
    if (left[index] > right[index]) return 1;
  }
  return 0;
}

function gateValidateRefOrder(refs, path) {
  verifyContentRefSet(refs);
  const keys = refs.map(gateRefKey);
  const ordered = [...keys].sort(compareGateKeys);
  const unique = new Set(keys.map((item) => JSON.stringify(item))).size === keys.length;
  if (!unique || canonicalString(keys) !== canonicalString(ordered)) {
    fail("GATE_PREDICATE_EVIDENCE_INVALID", path);
  }
}

function ruleGateDecision(value) {
  if (value.predicate_registry_digest !== gateRegistryDigest()) {
    fail("GATE_PREDICATE_REGISTRY_DIGEST_INVALID", "$.predicate_registry_digest");
  }
  const expected = gateExpectedEntries(value.decision_kind);
  if (canonicalString(value.predicate_results.map((item) => item.predicate_id)) !==
      canonicalString(expected.map((item) => item.predicate_id))) {
    fail("GATE_PREDICATE_ORDER_INVALID", "$.predicate_results");
  }
  value.predicate_results.forEach((result, index) => {
    const predicate = expected[index];
    const path = "$.predicate_results[" + index + "]";
    if (result.passed !== (result.failure_code === null)) {
      fail("GATE_PREDICATE_RESULT_INVALID", path + ".failure_code");
    }
    if (result.failure_code !== null &&
        !predicate.failure_codes.includes(result.failure_code)) {
      fail("GATE_PREDICATE_FAILURE_CODE_INVALID", path + ".failure_code");
    }
    gateValidateRefOrder(result.evidence_refs, path + ".evidence_refs");
    if (result.evidence_refs.some(
      (item) => !predicate.input_schema_ids.includes(item.content_schema_id),
    )) fail("GATE_PREDICATE_EVIDENCE_INVALID", path + ".evidence_refs");
  });
  if (value.passed !== value.predicate_results.every((item) => item.passed)) {
    fail("GATE_DECISION_PASS_INVALID", "$.passed");
  }
  if (value.decision_kind === "terminalization") {
    if (!GATE_TERMINAL_REASONS[value.selected_outcome].has(value.selected_reason)) {
      fail("GATE_TERMINAL_OUTCOME_INVALID", "$.selected_reason");
    }
    verifyContentRefSet(value.authoritative_fact_refs);
    const keys = value.authoritative_fact_refs.map(gateRefKey);
    const ordered = [...keys].sort(compareGateKeys);
    const unique = new Set(keys.map((item) => JSON.stringify(item))).size === keys.length;
    if (!unique || canonicalString(keys) !== canonicalString(ordered)) {
      fail("GATE_TERMINAL_FACT_ORDER_INVALID", "$.authoritative_fact_refs");
    }
  }
  gateVerifyDigest("gate-decision/v1", value, "decision_digest");
}

function gateContext(context, expectedKeys) {
  if (!context || Array.isArray(context) || typeof context !== "object" ||
      Object.getPrototypeOf(context) !== Object.prototype ||
      canonicalString(Object.keys(context).sort()) !== canonicalString([...expectedKeys].sort())) {
    fail("GATE_EVALUATION_CONTEXT_INVALID", "$.context");
  }
  return JSON.parse(decoder.decode(canonicalDocumentBytes(context)));
}

async function gateSnapshotAndRef(schemaId, snapshotValue, referenceValue) {
  const snapshot = await verifyDocumentDigest(schemaId, snapshotValue);
  const reference = validateDocument("content-ref/v1", referenceValue);
  const raw = canonicalDocumentBytes(snapshot);
  if (reference.content_schema_id !== schemaId ||
      reference.sha256 !== sha256Sync(raw) ||
      reference.size_bytes !== raw.length ||
      reference.media_type !== "application/json" ||
      reference.encoding !== "utf-8") {
    fail("GATE_INPUT_SNAPSHOT_REF_MISMATCH", "$.context.input_snapshot_ref");
  }
  if (snapshot.predicate_registry_digest !== gateRegistryDigest()) {
    fail("GATE_PREDICATE_REGISTRY_DIGEST_INVALID", "$.predicate_registry_digest");
  }
  return [snapshot, reference];
}

export async function evaluateSuccessGate(inputSnapshot, context) {
  const evaluation = gateContext(context, ["input_snapshot_ref", "predicate_results"]);
  const [snapshot, reference] = await gateSnapshotAndRef(
    "gate-input-snapshot/v1", inputSnapshot, evaluation.input_snapshot_ref,
  );
  const results = evaluation.predicate_results;
  return sealDocument("gate-decision/v1", {
    schema_id: "gate-decision/v1",
    decision_kind: "success",
    input_snapshot_ref: reference,
    input_digest: snapshot.input_digest,
    predicate_registry_digest: snapshot.predicate_registry_digest,
    requested_outcome: snapshot.requested_outcome,
    passed: results.every((item) => item?.passed === true),
    predicate_results: results,
  });
}

export async function evaluateTerminalizationGate(inputSnapshot, context) {
  const evaluation = gateContext(context, [
    "input_snapshot_ref", "selected_outcome", "selected_reason",
    "source_availability", "evidence_availability", "effect_availability",
    "predicate_results",
  ]);
  const [snapshot, reference] = await gateSnapshotAndRef(
    "terminalization-input-snapshot/v1", inputSnapshot, evaluation.input_snapshot_ref,
  );
  if (canonicalString(evaluation.source_availability) !==
      canonicalString(snapshot.final_source)) {
    fail("GATE_TERMINAL_AVAILABILITY_MISMATCH", "$.context.source_availability");
  }
  const expectedEffect = {availability: "available", ref: snapshot.effect_ledger_ref};
  if (canonicalString(evaluation.effect_availability) !== canonicalString(expectedEffect)) {
    fail("GATE_TERMINAL_AVAILABILITY_MISMATCH", "$.context.effect_availability");
  }
  const results = evaluation.predicate_results;
  return sealDocument("gate-decision/v1", {
    schema_id: "gate-decision/v1",
    decision_kind: "terminalization",
    input_snapshot_ref: reference,
    input_digest: snapshot.input_digest,
    predicate_registry_digest: snapshot.predicate_registry_digest,
    selected_outcome: evaluation.selected_outcome,
    selected_reason: evaluation.selected_reason,
    authoritative_fact_refs: snapshot.terminalization_fact_refs,
    source_availability: evaluation.source_availability,
    evidence_availability: evaluation.evidence_availability,
    effect_availability: evaluation.effect_availability,
    passed: results.every((item) => item?.passed === true),
    predicate_results: results,
  });
}
'''


__all__ = ["NPM_GATE"]
