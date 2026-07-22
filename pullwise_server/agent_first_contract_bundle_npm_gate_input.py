"""Generated npm facade semantics for GateInput snapshots."""

from __future__ import annotations


NPM_GATE_INPUT = r'''
const GATE_INPUT_KEYS = new Set([
  "schema_id", "task_id", "attempt_id", "native_epoch", "owner_id",
  "owner_epoch", "task_version", "lifecycle", "desired_state", "lease_id",
  "outer_lease_expires_at", "outer_lease_grace_expires_at",
  "authoritative_cancel_received", "absolute_deadline_at",
  "trusted_wall_time_at", "monotonic_deadline_remaining_ms",
  "terminal_budget_reserved_ms", "predicate_registry_digest", "request_ref",
  "policy_ref", "requirement_ledger_ref", "completion_proposal_ref",
  "quality_policy_plan_ref", "original_source_ref", "final_source_ref",
  "execution_state_refs", "change_set", "pre_observation_manifest_ref",
  "final_observation_manifest_ref", "verification_attestation_manifest_ref",
  "effect_ledger_ref", "budget_summary_ref", "publication_content_manifest_ref",
  "debug_redaction_plan_ref", "pre_gate_root_set_ref",
  "pre_gate_evidence_closure_ref", "pre_gate_closure_digest",
  "requested_outcome", "input_digest",
]);
const TERMINALIZATION_INPUT_KEYS = new Set([
  "schema_id", "task_id", "attempt_id", "native_epoch", "owner_id",
  "owner_epoch", "task_version", "lifecycle", "desired_state", "lease_id",
  "outer_lease_expires_at", "outer_lease_grace_expires_at",
  "absolute_deadline_at", "trusted_wall_time_at",
  "monotonic_deadline_remaining_ms", "terminal_budget_reserved_ms",
  "predicate_registry_digest", "request_ref", "policy_ref",
  "requirement_ledger_ref", "original_source", "final_source",
  "final_observation_manifest", "effect_ledger_ref", "budget_summary_ref",
  "publication_content_manifest_ref", "debug_redaction_plan_ref",
  "terminalization_fact_refs", "pre_gate_root_set_ref",
  "pre_gate_evidence_closure_ref", "pre_gate_closure_digest", "input_digest",
]);

function gateInputTimestampMillis(value) {
  if (typeof value !== "string" ||
      !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$/.test(value) ||
      Number(value.slice(0, 4)) === 0) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  return new Date(timestamp).toISOString() === value ? timestamp : null;
}

function ruleGateInputSnapshot(value) {
  if (!preGateSameKeys(value, GATE_INPUT_KEYS)) {
    fail("GATE_INPUT_CLOSURE_DIRECTION_INVALID");
  }
  if (!preGateOrderedUnique(
    value.execution_state_refs,
    (item) => [item.content_schema_id, item.artifact_id, item.sha256],
  )) {
    fail("GATE_INPUT_EXECUTION_ORDER_INVALID", "$.execution_state_refs");
  }
  verifyContentRefSet(value.execution_state_refs);
  const leaseExpires = gateInputTimestampMillis(value.outer_lease_expires_at);
  const graceExpires = gateInputTimestampMillis(
    value.outer_lease_grace_expires_at,
  );
  if (leaseExpires === null || graceExpires === null ||
      leaseExpires > graceExpires) {
    fail("GATE_INPUT_LEASE_TIME_INVALID", "$.outer_lease_grace_expires_at");
  }
  const wallTime = gateInputTimestampMillis(value.trusted_wall_time_at);
  const deadline = gateInputTimestampMillis(value.absolute_deadline_at);
  if (wallTime === null || deadline === null || wallTime > deadline) {
    fail("GATE_INPUT_DEADLINE_INVALID", "$.trusted_wall_time_at");
  }
  if (value.terminal_budget_reserved_ms >
      value.monotonic_deadline_remaining_ms) {
    fail("GATE_INPUT_TERMINAL_RESERVE_INVALID", "$.terminal_budget_reserved_ms");
  }
}

function ruleTerminalizationInputSnapshot(value) {
  if (!preGateSameKeys(value, TERMINALIZATION_INPUT_KEYS)) {
    fail("GATE_INPUT_CLOSURE_DIRECTION_INVALID");
  }
  const facts = value.terminalization_fact_refs;
  if (!facts.length || !preGateOrderedUnique(
    facts,
    (item) => [item.content_schema_id, item.artifact_id, item.sha256],
  )) {
    fail("TERMINALIZATION_FACT_ORDER_INVALID", "$.terminalization_fact_refs");
  }
  verifyContentRefSet(facts);
  const hasAttempt = value.attempt_id !== null;
  const attemptBindingValid = hasAttempt
    ? value.native_epoch >= 1 && value.owner_epoch >= 1 &&
      value.lease_id !== null && value.outer_lease_expires_at !== null &&
      value.outer_lease_grace_expires_at !== null
    : value.native_epoch === 0 && value.owner_epoch === 0 &&
      value.lease_id === null && value.outer_lease_expires_at === null &&
      value.outer_lease_grace_expires_at === null;
  if (!attemptBindingValid) {
    fail("TERMINALIZATION_ATTEMPT_BINDING_INVALID", "$.attempt_id");
  }
  if (hasAttempt) {
    const leaseExpires = gateInputTimestampMillis(value.outer_lease_expires_at);
    const graceExpires = gateInputTimestampMillis(
      value.outer_lease_grace_expires_at,
    );
    if (leaseExpires === null || graceExpires === null ||
        leaseExpires > graceExpires) {
      fail("GATE_INPUT_LEASE_TIME_INVALID", "$.outer_lease_grace_expires_at");
    }
  }
  if (gateInputTimestampMillis(value.absolute_deadline_at) === null ||
      gateInputTimestampMillis(value.trusted_wall_time_at) === null) {
    fail("GATE_INPUT_DEADLINE_INVALID", "$.trusted_wall_time_at");
  }
}

function requireGateInputProjection(actual, expected, path) {
  if (canonicalString(actual) !== canonicalString(expected)) {
    fail("GATE_INPUT_STALE", path);
  }
}

function verifySnapshotPreGateContext(snapshot, rootSet, preGateManifest) {
  if (snapshot.task_id !== rootSet.task_id ||
      snapshot.task_id !== preGateManifest.task_id) {
    fail("GATE_INPUT_STALE", "$.task_id");
  }
  if (canonicalString(snapshot.pre_gate_root_set_ref) !==
      canonicalString(preGateManifest.pre_gate_root_set_ref)) {
    fail("GATE_INPUT_STALE", "$.pre_gate_root_set_ref");
  }
  if (!contentRefMatchesDirectDocument(
    snapshot.pre_gate_root_set_ref, "pre-gate-root-set/v1", rootSet,
  )) {
    fail("CAS_CORRUPT", "$.pre_gate_root_set_ref");
  }
  if (!contentRefMatchesDirectDocument(
    snapshot.pre_gate_evidence_closure_ref,
    "pre-gate-evidence-closure-manifest/v1",
    preGateManifest,
  )) {
    fail("CAS_CORRUPT", "$.pre_gate_evidence_closure_ref");
  }
  if (snapshot.pre_gate_closure_digest !==
      preGateManifest.pre_gate_closure_digest) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.pre_gate_closure_digest");
  }
}

/** Bind a success snapshot to direct PreGate documents and projections. */
export async function verifyGateInputSnapshotContext(
  snapshot,
  rootSet,
  preGateManifest,
) {
  const validated = await verifyDocumentDigest("gate-input-snapshot/v1", snapshot);
  const roots = await verifyDocumentDigest("pre-gate-root-set/v1", rootSet);
  const manifest = await verifyPreGateEvidenceClosureContext(
    preGateManifest, roots,
  );
  verifySnapshotPreGateContext(validated, roots, manifest);
  if (!PRE_GATE_SUCCESS_OUTCOMES.has(roots.outcome_candidate) ||
      validated.requested_outcome !== roots.outcome_candidate) {
    fail("GATE_INPUT_STALE", "$.requested_outcome");
  }
  const projections = {
    request_ref: roots.request.ref,
    policy_ref: roots.policy.ref,
    requirement_ledger_ref: roots.ledger.ref,
    completion_proposal_ref: roots.proposal.ref,
    original_source_ref: roots.original_source.ref,
    final_source_ref: roots.final_source.ref,
    pre_observation_manifest_ref: roots.pre_observation_manifest.ref,
    final_observation_manifest_ref: roots.final_observation_manifest.ref,
    verification_attestation_manifest_ref: roots.attestations.ref,
    effect_ledger_ref: roots.effect_ledger.ref,
    budget_summary_ref: roots.budget_summary.ref,
    publication_content_manifest_ref: roots.publication_content_manifest.ref,
    debug_redaction_plan_ref: roots.debug_redaction_plan.ref,
    change_set: roots.change_set,
    execution_state_refs: roots.execution_states
      .filter((item) => item.availability === "available")
      .map((item) => item.ref),
  };
  for (const [field, expected] of Object.entries(projections)) {
    requireGateInputProjection(validated[field], expected, "$." + field);
  }
  if (!manifest.entries.some(
    (item) => canonicalString(item) ===
      canonicalString(validated.quality_policy_plan_ref),
  )) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.quality_policy_plan_ref");
  }
  return validated;
}

/** Bind a terminal snapshot to direct PreGate and authority-fact documents. */
export async function verifyTerminalizationInputSnapshotContext(
  snapshot,
  rootSet,
  preGateManifest,
  terminalizationFacts,
) {
  const validated = await verifyDocumentDigest(
    "terminalization-input-snapshot/v1", snapshot,
  );
  const roots = await verifyDocumentDigest("pre-gate-root-set/v1", rootSet);
  const manifest = await verifyPreGateEvidenceClosureContext(
    preGateManifest, roots,
  );
  verifySnapshotPreGateContext(validated, roots, manifest);
  if (!(PRE_GATE_TERMINAL_OUTCOMES.has(roots.outcome_candidate) ||
        roots.outcome_candidate === "PARTIAL")) {
    fail("GATE_INPUT_STALE", "$.outcome_candidate");
  }
  const projections = {
    request_ref: roots.request.ref,
    policy_ref: roots.policy.ref,
    requirement_ledger_ref: roots.ledger.ref,
    original_source: roots.original_source,
    final_source: roots.final_source,
    final_observation_manifest: roots.final_observation_manifest,
    effect_ledger_ref: roots.effect_ledger.ref,
    budget_summary_ref: roots.budget_summary.ref,
    publication_content_manifest_ref: roots.publication_content_manifest.ref,
    debug_redaction_plan_ref: roots.debug_redaction_plan.ref,
    terminalization_fact_refs: roots.termination_facts
      .filter((item) => item.availability === "available")
      .map((item) => item.ref),
  };
  for (const [field, expected] of Object.entries(projections)) {
    requireGateInputProjection(validated[field], expected, "$." + field);
  }
  if (!Array.isArray(terminalizationFacts) ||
      terminalizationFacts.length !== validated.terminalization_fact_refs.length) {
    fail("GATE_INPUT_STALE", "$.terminalization_fact_refs");
  }
  const facts = [];
  for (const item of terminalizationFacts) {
    facts.push(await verifyDocumentDigest("terminalization-fact/v1", item));
  }
  facts.forEach((fact, index) => {
    if (fact.task_id !== validated.task_id ||
        fact.observed_task_version >= validated.task_version) {
      fail("GATE_INPUT_STALE", "$.terminalization_fact_refs[" + index + "]");
    }
  });
  if (Math.max(...facts.map((fact) => fact.observed_task_version)) !==
      validated.task_version - 1) {
    fail("GATE_INPUT_STALE", "$.terminalization_fact_refs");
  }
  const unmatched = facts.slice();
  validated.terminalization_fact_refs.forEach((ref, index) => {
    const matches = unmatched.filter((fact) => contentRefMatchesDirectDocument(
      ref, "terminalization-fact/v1", fact,
    ));
    if (matches.length !== 1) {
      fail("CAS_CORRUPT", "$.terminalization_fact_refs[" + index + "]");
    }
    unmatched.splice(unmatched.indexOf(matches[0]), 1);
  });
  return validated;
}
'''


__all__ = ["NPM_GATE_INPUT"]
