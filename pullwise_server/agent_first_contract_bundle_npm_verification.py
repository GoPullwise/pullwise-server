"""Generated Node verification rules and contextual helpers."""

from __future__ import annotations


NPM_VERIFICATION = r'''
function verificationRequire(condition, detail, path = "$") {
  if (!condition) fail(detail, path);
}

function verificationTextCompare(left, right) {
  const a = [...left];
  const b = [...right];
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const diff = a[i].codePointAt(0) - b[i].codePointAt(0);
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function verificationCompareKey(left, right) {
  const a = Array.isArray(left) ? left : [left];
  const b = Array.isArray(right) ? right : [right];
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const diff = verificationTextCompare(String(a[i]), String(b[i]));
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function verificationOrderedUnique(values, key) {
  const keys = values.map(key);
  for (let i = 1; i < keys.length; i += 1) {
    if (verificationCompareKey(keys[i - 1], keys[i]) >= 0) return false;
  }
  return true;
}

function verificationSortedUniqueStrings(values) {
  return verificationOrderedUnique(values, (item) => item);
}

function verificationArtifactKey(value) {
  const ref = value.ref ?? value;
  return ref.artifact_id;
}

function verificationRefKey(value) {
  return [value.content_schema_id, value.artifact_id, value.sha256];
}

function verificationAssessmentsValid(values) {
  return verificationOrderedUnique(values, (item) => item.requirement_id) &&
    values.every((item) =>
      verificationSortedUniqueStrings(item.evidence_ids) &&
      verificationSortedUniqueStrings(item.limitations) &&
      (item.verdict !== "PASS" || item.limitations.length === 0));
}

function verificationTimestampMillis(value) {
  if (typeof value !== "string") return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z$/.exec(value);
  if (!match) return null;
  const [, y, m, d, hh, mm, ss, ms] = match.map(Number);
  const epoch = Date.UTC(y, m - 1, d, hh, mm, ss, ms);
  if (!Number.isFinite(epoch)) return null;
  const roundTrip = new Date(epoch).toISOString();
  return roundTrip === value ? epoch : null;
}

function verificationRequireTimeOrder(values, path) {
  const epochs = values.map(verificationTimestampMillis);
  verificationRequire(epochs.every((item) => item !== null), "VERIFICATION_CONTEXT_TIME_INVALID", path);
  for (let i = 1; i < epochs.length; i += 1) {
    verificationRequire(epochs[i - 1] <= epochs[i], "VERIFICATION_CONTEXT_TIME_INVALID", path);
  }
}

function verificationManifestObservationIds(manifest) {
  return manifest.entries.map((item) => item.observation_id);
}

function verificationManifestObservationMap(manifest) {
  return new Map(manifest.entries.map((item) => [item.observation_id, item]));
}

function verificationChangeBinding(availability, changeSet, path) {
  const checked = validateDocument("availability-ref/v1", availability);
  if (changeSet === null) {
    verificationRequire(
      checked.availability === "not_applicable",
      "VERIFICATION_CONTEXT_CHANGE_SET_INVALID",
      path,
    );
    return checked;
  }
  verificationRequire(
    checked.availability === "available",
    "VERIFICATION_CONTEXT_CHANGE_SET_INVALID",
    path,
  );
  verificationRequire(
    seoRefMatchesDocument(checked.ref, "change-set/v1", changeSet),
    "CAS_CORRUPT",
    path + ".ref",
  );
  return checked;
}

function verificationRequirementIds(values) {
  return values.map((item) => item.requirement_id);
}

function verificationRunStatus(values) {
  const verdicts = new Set(values.map((item) => item.verdict));
  for (const value of ["POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE"]) {
    if (verdicts.has(value)) return value;
  }
  return "PASS";
}

function verificationAggregateVerdict(requiredSlotIds, attestationBySlot, requirementId) {
  const items = [];
  for (const slotId of requiredSlotIds) {
    const attestation = attestationBySlot.get(slotId);
    if (attestation === undefined) {
      return {attestation_ids: [], verdict: "UNVERIFIABLE"};
    }
    const verdict = attestation.requirement_verdicts.find(
      (item) => item.requirement_id === requirementId,
    );
    verificationRequire(
      verdict !== undefined,
      "VERIFICATION_CONTEXT_AGGREGATE_INVALID",
      "$.requirement_aggregates",
    );
    items.push({attestation_id: attestation.attestation_id, verdict: verdict.verdict});
  }
  if (items.some((item) => item.verdict === "POLICY_VIOLATION" || item.verdict === "NEEDS_WORK")) {
    return {attestation_ids: items.map((item) => item.attestation_id), verdict: "FAIL"};
  }
  if (items.some((item) => item.verdict === "UNVERIFIABLE")) {
    return {attestation_ids: items.map((item) => item.attestation_id), verdict: "UNVERIFIABLE"};
  }
  return {attestation_ids: items.map((item) => item.attestation_id), verdict: "PASS"};
}

function ruleCompletionProposal(value) {
  seoVerifyEmbeddedDigest("completion-proposal/v1", value);
  verificationRequire(
    verificationSortedUniqueStrings(value.execution_state_ids),
    "PROPOSAL_EXECUTION_STATE_ORDER_INVALID",
  );
  verificationRequire(
    verificationOrderedUnique(value.artifact_refs, verificationArtifactKey),
    "PROPOSAL_ARTIFACT_ORDER_INVALID",
  );
  verificationRequire(
    verificationOrderedUnique(value.requirement_claims, (item) => item.requirement_id),
    "PROPOSAL_CLAIM_ORDER_INVALID",
  );
  value.requirement_claims.forEach((item) => {
    verificationRequire(verificationSortedUniqueStrings(item.evidence_ids), "PROPOSAL_EVIDENCE_ORDER_INVALID");
  });
  verificationRequire(verificationSortedUniqueStrings(value.known_gaps), "PROPOSAL_GAP_ORDER_INVALID");
  verificationRequire(verificationSortedUniqueStrings(value.residual_risks), "PROPOSAL_RISK_ORDER_INVALID");
  if (value.outcome_requested === "NO_CHANGE_NEEDED") {
    verificationRequire(value.change_set_ref === null, "PROPOSAL_NO_CHANGE_SET_INVALID");
    verificationRequire(
      value.original_source_state_id === value.final_source_state_id,
      "PROPOSAL_NO_CHANGE_STATE_INVALID",
    );
  }
}

function ruleVerifierInput(value) {
  seoVerifyEmbeddedDigest("verifier-input-manifest/v1", value);
  verificationRequire(value.owner_conclusion_excluded === true, "VERIFIER_OWNER_CONCLUSION_INCLUDED");
  verificationRequire(
    verificationOrderedUnique(value.artifact_refs, verificationArtifactKey),
    "VERIFIER_ARTIFACT_ORDER_INVALID",
  );
  verificationRequire(
    verificationOrderedUnique(value.engineering_rule_refs, verificationRefKey),
    "VERIFIER_RULE_ORDER_INVALID",
  );
  verificationRequire(
    verificationSortedUniqueStrings(value.requirement_ids),
    "VERIFIER_REQUIREMENT_ORDER_INVALID",
  );
}

function ruleVerifierWork(value) {
  seoVerifyEmbeddedDigest("verifier-work-report/v1", value);
  verificationRequire(value.sandbox_mode === "read_only_or_cow", "VERIFIER_SANDBOX_INVALID");
  for (const field of ["counterexamples_searched", "own_observation_ids", "limitations"]) {
    verificationRequire(
      verificationSortedUniqueStrings(value[field]),
      "VERIFIER_WORK_ORDER_INVALID",
      "$." + field,
    );
  }
  verificationRequire(value.own_observation_ids.length > 0, "VERIFIER_OBSERVATION_REQUIRED");
  verificationRequire(
    verificationAssessmentsValid(value.provisional_requirement_assessments),
    "VERIFIER_ASSESSMENT_INVALID",
  );
}

function ruleAttestation(value) {
  seoVerifyEmbeddedDigest("verification-attestation/v1", value);
  verificationRequire(
    verificationSortedUniqueStrings(value.execution_state_ids),
    "ATTESTATION_EXECUTION_ORDER_INVALID",
  );
  verificationRequire(
    verificationSortedUniqueStrings(value.own_observation_ids) && value.own_observation_ids.length > 0,
    "ATTESTATION_OBSERVATION_INVALID",
  );
  verificationRequire(
    verificationAssessmentsValid(value.requirement_verdicts),
    "ATTESTATION_VERDICT_INVALID",
  );
  verificationRequire(
    value.run_status === verificationRunStatus(value.requirement_verdicts),
    "ATTESTATION_RUN_STATUS_INVALID",
  );
}

function ruleAttestationManifest(value) {
  seoVerifyEmbeddedDigest("verification-attestation-manifest/v1", value);
  verificationRequire(
    value.attestation_count === value.attestations.length,
    "ATTESTATION_MANIFEST_COUNT_INVALID",
  );
  verificationRequire(
    verificationOrderedUnique(value.attestations, (item) => [item.slot_id, item.attestation_id]),
    "ATTESTATION_MANIFEST_ORDER_INVALID",
  );
  const slotIds = new Set(value.attestations.map((item) => item.slot_id));
  const attestationIds = new Set(value.attestations.map((item) => item.attestation_id));
  verificationRequire(
    verificationOrderedUnique(value.requirement_aggregates, (item) => item.requirement_id),
    "ATTESTATION_AGGREGATE_ORDER_INVALID",
  );
  value.requirement_aggregates.forEach((item) => {
    verificationRequire(verificationSortedUniqueStrings(item.required_slot_ids), "ATTESTATION_REQUIRED_SLOT_ORDER_INVALID");
    verificationRequire(verificationSortedUniqueStrings(item.attestation_ids), "ATTESTATION_ID_ORDER_INVALID");
    verificationRequire(
      item.attestation_ids.every((value) => attestationIds.has(value)),
      "ATTESTATION_ID_UNKNOWN",
    );
    if (item.required_slot_ids.some((value) => !slotIds.has(value))) {
      verificationRequire(item.verdict === "UNVERIFIABLE", "ATTESTATION_MISSING_SLOT_INVALID");
    }
  });
}

export async function verifyCompletionProposalContext(
  proposal, taskSnapshot, attempt, owner, taskRequest, effectivePolicy, requirementLedger,
  executionCharter, originalSource, finalSource, executionStates, changeSet, preObservationManifest,
) {
  const checked = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const snapshot = await verifyDocumentDigest("task-record/v1", taskSnapshot);
  const attemptValue = await verifyDocumentDigest("attempt-record/v1", attempt);
  const ownerValue = await verifyDocumentDigest("task-owner/v1", owner);
  const requestValue = await verifyDocumentDigest("task-request/v1", taskRequest);
  const policyValue = await verifyDocumentDigest("effective-execution-policy/v1", effectivePolicy);
  const ledgerValue = await verifyDocumentDigest("requirement-ledger/v1", requirementLedger);
  const charterValue = await verifyDocumentDigest("task-charter/v1", executionCharter);
  const originalValue = await verifyDocumentDigest("source-tree-manifest/v1", originalSource);
  const finalValue = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const preValue = await verifyDocumentDigest("pre-verifier-observation-manifest/v1", preObservationManifest);
  const states = await Promise.all(executionStates.map(
    (item) => verifyDocumentDigest("execution-state-manifest/v1", item),
  ));
  const changeValue = changeSet === null ? null : await verifyDocumentDigest("change-set/v1", changeSet);

  for (const [left, right, path] of [
    [checked.task_id, snapshot.task_id, "$.task_id"],
    [checked.task_id, attemptValue.task_id, "$.task_id"],
    [checked.task_id, ownerValue.task_id, "$.task_id"],
    [checked.task_id, requestValue.task_id, "$.task_id"],
    [checked.task_id, ledgerValue.task_id, "$.task_id"],
    [checked.task_id, charterValue.task_id, "$.task_id"],
    [checked.task_id, preValue.task_id, "$.task_id"],
    [checked.attempt_id, snapshot.current_attempt_id, "$.attempt_id"],
    [checked.attempt_id, attemptValue.attempt_id, "$.attempt_id"],
    [checked.attempt_id, ownerValue.attempt_id, "$.attempt_id"],
    [checked.attempt_id, preValue.attempt_id, "$.attempt_id"],
    [checked.native_epoch, snapshot.native_epoch, "$.native_epoch"],
    [checked.native_epoch, attemptValue.native_epoch, "$.native_epoch"],
    [checked.native_epoch, ownerValue.native_epoch, "$.native_epoch"],
    [checked.native_epoch, preValue.native_epoch, "$.native_epoch"],
    [checked.owner_id, snapshot.owner_id, "$.owner_id"],
    [checked.owner_id, ownerValue.owner_id, "$.owner_id"],
    [checked.owner_epoch, snapshot.owner_epoch, "$.owner_epoch"],
    [checked.owner_epoch, ownerValue.owner_epoch, "$.owner_epoch"],
    [checked.proposed_from_task_version, snapshot.task_version, "$.proposed_from_task_version"],
    [checked.request_digest, snapshot.request_digest, "$.request_digest"],
    [checked.requirement_ledger_digest, snapshot.ledger_head_digest, "$.requirement_ledger_digest"],
    [checked.requirement_ledger_digest, ledgerValue.ledger_digest, "$.requirement_ledger_digest"],
    [checked.policy_digest, snapshot.policy_digest, "$.policy_digest"],
    [checked.policy_digest, policyValue.digest, "$.policy_digest"],
    [checked.charter_digest, charterValue.digest, "$.charter_digest"],
    [checked.original_source_state_id, originalValue.source_state_id, "$.original_source_state_id"],
    [checked.final_source_state_id, finalValue.source_state_id, "$.final_source_state_id"],
    [checked.task_id, preValue.task_id, "$.task_id"],
    [checked.proposal_id, preValue.proposal_id, "$.proposal_id"],
  ]) {
    verificationRequire(left === right, "VERIFICATION_CONTEXT_BINDING_INVALID", path);
  }
  verificationRequire(snapshot.charter_ref !== null, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.charter_digest");
  verificationRequire(
    seoRefMatchesDocument(snapshot.request_ref, "task-request/v1", requestValue),
    "CAS_CORRUPT",
    "$.request_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(snapshot.policy_ref, "effective-execution-policy/v1", policyValue),
    "CAS_CORRUPT",
    "$.policy_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(snapshot.charter_ref, "task-charter/v1", charterValue),
    "CAS_CORRUPT",
    "$.charter_ref",
  );
  if (snapshot.completion_proposal_ref !== null) {
    verificationRequire(
      seoRefMatchesDocument(snapshot.completion_proposal_ref, "completion-proposal/v1", checked),
      "CAS_CORRUPT",
      "$.completion_proposal_ref",
    );
  }
  if (changeValue === null) {
    verificationRequire(checked.change_set_ref === null, "VERIFICATION_CONTEXT_CHANGE_SET_INVALID", "$.change_set_ref");
  } else {
    verificationRequire(
      checked.change_set_ref !== null &&
      seoRefMatchesDocument(checked.change_set_ref, "change-set/v1", changeValue),
      "CAS_CORRUPT",
      "$.change_set_ref",
    );
  }
  const expectedStateIds = states.map((item) => item.execution_state_id);
  verificationRequire(
    canonicalString(checked.execution_state_ids) === canonicalString(expectedStateIds),
    "VERIFICATION_CONTEXT_BINDING_INVALID",
    "$.execution_state_ids",
  );
  const activeRequirements = ledgerValue.active_requirement_ids;
  verificationRequire(
    canonicalString(verificationRequirementIds(checked.requirement_claims)) === canonicalString(activeRequirements),
    "VERIFICATION_CONTEXT_REQUIREMENT_INVALID",
    "$.requirement_claims",
  );
  const knownEvidence = new Set(verificationManifestObservationIds(preValue));
  checked.requirement_claims.forEach((item, index) => {
    verificationRequire(
      item.evidence_ids.every((value) => knownEvidence.has(value)),
      "VERIFICATION_CONTEXT_OBSERVATION_INVALID",
      "$.requirement_claims[" + index + "].evidence_ids",
    );
  });
  verificationRequireTimeOrder(
    [requestValue.submitted_at, policyValue.issued_at, charterValue.created_at, snapshot.created_at, snapshot.updated_at, checked.created_at],
    "$.created_at",
  );
  return checked;
}

export async function verifyVerifierInputContext(
  manifest, proposal, qualityPolicyPlan, taskRequest, effectivePolicy,
  requirementLedger, executionCharter, originalSource, finalSource, changeSet,
  preObservationManifest, engineeringRules,
) {
  const checked = await verifyDocumentDigest("verifier-input-manifest/v1", manifest);
  const proposalValue = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const planValue = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const requestValue = await verifyDocumentDigest("task-request/v1", taskRequest);
  const policyValue = await verifyDocumentDigest("effective-execution-policy/v1", effectivePolicy);
  const ledgerValue = await verifyDocumentDigest("requirement-ledger/v1", requirementLedger);
  const charterValue = await verifyDocumentDigest("task-charter/v1", executionCharter);
  const originalValue = await verifyDocumentDigest("source-tree-manifest/v1", originalSource);
  const finalValue = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const preValue = await verifyDocumentDigest("pre-verifier-observation-manifest/v1", preObservationManifest);
  const rules = await Promise.all(engineeringRules.map(
    (item) => verifyDocumentDigest("source-content/v1", item),
  ));
  const changeValue = changeSet === null ? null : await verifyDocumentDigest("change-set/v1", changeSet);

  for (const [left, right, path] of [
    [checked.task_id, proposalValue.task_id, "$.task_id"],
    [checked.task_id, planValue.task_id, "$.task_id"],
    [checked.task_id, requestValue.task_id, "$.task_id"],
    [checked.task_id, ledgerValue.task_id, "$.task_id"],
    [checked.task_id, charterValue.task_id, "$.task_id"],
    [checked.task_id, preValue.task_id, "$.task_id"],
    [checked.proposal_id, proposalValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, planValue.proposal_id, "$.proposal_id"],
    [checked.quality_policy_plan_digest, planValue.plan_digest, "$.quality_policy_plan_digest"],
    [checked.created_at >= proposalValue.created_at, true, "$.created_at"],
    [canonicalString(checked.artifact_refs), canonicalString(proposalValue.artifact_refs), "$.artifact_refs"],
    [checked.slot_id, planValue.slots.find((item) => item.slot_id === checked.slot_id)?.slot_id, "$.slot_id"],
    [checked.slot_concern, planValue.slots.find((item) => item.slot_id === checked.slot_id)?.concern, "$.slot_concern"],
    [canonicalString(checked.requirement_ids), canonicalString(planValue.slots.find((item) => item.slot_id === checked.slot_id)?.requirement_ids ?? null), "$.requirement_ids"],
    [canonicalString(checked.requirement_ids), canonicalString(ledgerValue.active_requirement_ids.filter((item) => checked.requirement_ids.includes(item))), "$.requirement_ids"],
  ]) {
    verificationRequire(left === right, "VERIFICATION_CONTEXT_BINDING_INVALID", path);
  }
  verificationRequire(
    seoRefMatchesDocument(checked.task_request_ref, "task-request/v1", requestValue),
    "CAS_CORRUPT",
    "$.task_request_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.effective_policy_ref, "effective-execution-policy/v1", policyValue),
    "CAS_CORRUPT",
    "$.effective_policy_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.requirement_ledger_ref, "requirement-ledger/v1", ledgerValue),
    "CAS_CORRUPT",
    "$.requirement_ledger_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.charter_ref, "task-charter/v1", charterValue),
    "CAS_CORRUPT",
    "$.charter_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.completion_proposal_ref, "completion-proposal/v1", proposalValue),
    "CAS_CORRUPT",
    "$.completion_proposal_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.quality_policy_plan_ref, "quality-policy-plan/v1", planValue),
    "CAS_CORRUPT",
    "$.quality_policy_plan_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.original_source_ref, "source-tree-manifest/v1", originalValue),
    "CAS_CORRUPT",
    "$.original_source_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.final_source_ref, "source-tree-manifest/v1", finalValue),
    "CAS_CORRUPT",
    "$.final_source_ref",
  );
  verificationChangeBinding(checked.change_set, changeValue, "$.change_set");
  verificationRequire(
    seoRefMatchesDocument(
      checked.pre_verifier_observation_manifest_ref,
      "pre-verifier-observation-manifest/v1",
      preValue,
    ),
    "CAS_CORRUPT",
    "$.pre_verifier_observation_manifest_ref",
  );
  verificationRequire(rules.length === checked.engineering_rule_refs.length, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.engineering_rule_refs");
  checked.engineering_rule_refs.forEach((ref, index) => {
    verificationRequire(
      seoRefMatchesDocument(ref, "source-content/v1", rules[index]),
      "CAS_CORRUPT",
      "$.engineering_rule_refs[" + index + "]",
    );
  });
  return checked;
}

export async function verifyVerifierWorkContext(report, verifierInput, proposal, finalObservationManifest) {
  const checked = await verifyDocumentDigest("verifier-work-report/v1", report);
  const inputValue = await verifyDocumentDigest("verifier-input-manifest/v1", verifierInput);
  const proposalValue = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const finalValue = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  for (const [left, right, path] of [
    [checked.task_id, inputValue.task_id, "$.task_id"],
    [checked.task_id, proposalValue.task_id, "$.task_id"],
    [checked.task_id, finalValue.task_id, "$.task_id"],
    [checked.proposal_id, inputValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, proposalValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, finalValue.proposal_id, "$.proposal_id"],
    [checked.slot_id, inputValue.slot_id, "$.slot_id"],
    [checked.verifier_input_manifest_digest, inputValue.input_manifest_digest, "$.verifier_input_manifest_digest"],
    [canonicalString(verificationRequirementIds(checked.provisional_requirement_assessments)), canonicalString(inputValue.requirement_ids), "$.provisional_requirement_assessments"],
  ]) {
    verificationRequire(left === right, "VERIFICATION_CONTEXT_BINDING_INVALID", path);
  }
  verificationRequire(
    seoRefMatchesDocument(checked.verifier_input_manifest_ref, "verifier-input-manifest/v1", inputValue),
    "CAS_CORRUPT",
    "$.verifier_input_manifest_ref",
  );
  const observations = verificationManifestObservationMap(finalValue);
  checked.own_observation_ids.forEach((value, index) => {
    const entry = observations.get(value);
    verificationRequire(entry !== undefined, "VERIFICATION_CONTEXT_OBSERVATION_INVALID", "$.own_observation_ids[" + index + "]");
    verificationRequire(entry.actor.kind === "quality_verifier", "VERIFICATION_CONTEXT_SESSION_INVALID", "$.own_observation_ids[" + index + "]");
    verificationRequire(entry.actor.session_id === checked.verifier_session_id, "VERIFICATION_CONTEXT_SESSION_INVALID", "$.own_observation_ids[" + index + "]");
  });
  checked.provisional_requirement_assessments.forEach((item, index) => {
    verificationRequire(
      item.evidence_ids.every((value) => checked.own_observation_ids.includes(value)),
      "VERIFICATION_CONTEXT_OBSERVATION_INVALID",
      "$.provisional_requirement_assessments[" + index + "].evidence_ids",
    );
  });
  verificationRequireTimeOrder([proposalValue.created_at, inputValue.created_at, checked.created_at], "$.created_at");
  return checked;
}

export async function verifyAttestationContext(
  attestation, verifierInput, verifierWork, proposal, qualityPolicyPlan,
  finalSource, executionStates, finalObservationManifest,
) {
  const checked = await verifyDocumentDigest("verification-attestation/v1", attestation);
  const inputValue = await verifyDocumentDigest("verifier-input-manifest/v1", verifierInput);
  const workValue = await verifyDocumentDigest("verifier-work-report/v1", verifierWork);
  const proposalValue = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const planValue = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const sourceValue = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const finalValue = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  const states = await Promise.all(executionStates.map(
    (item) => verifyDocumentDigest("execution-state-manifest/v1", item),
  ));

  for (const [left, right, path] of [
    [checked.task_id, inputValue.task_id, "$.task_id"],
    [checked.task_id, workValue.task_id, "$.task_id"],
    [checked.task_id, proposalValue.task_id, "$.task_id"],
    [checked.task_id, planValue.task_id, "$.task_id"],
    [checked.task_id, finalValue.task_id, "$.task_id"],
    [checked.proposal_id, inputValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, workValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, proposalValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, planValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, finalValue.proposal_id, "$.proposal_id"],
    [checked.slot_id, inputValue.slot_id, "$.slot_id"],
    [checked.slot_id, workValue.slot_id, "$.slot_id"],
    [checked.verifier_session_id, workValue.verifier_session_id, "$.verifier_session_id"],
    [checked.model_identity, workValue.model_identity, "$.model_identity"],
    [checked.verifier_input_manifest_digest, inputValue.input_manifest_digest, "$.verifier_input_manifest_digest"],
    [checked.verifier_work_report_digest, workValue.report_digest, "$.verifier_work_report_digest"],
    [checked.quality_policy_plan_digest, planValue.plan_digest, "$.quality_policy_plan_digest"],
    [checked.final_observation_manifest_digest, finalValue.manifest_digest, "$.final_observation_manifest_digest"],
    [checked.source_state_id, sourceValue.source_state_id, "$.source_state_id"],
    [canonicalString(checked.execution_state_ids), canonicalString(states.map((item) => item.execution_state_id)), "$.execution_state_ids"],
    [canonicalString(checked.own_observation_ids), canonicalString(workValue.own_observation_ids), "$.own_observation_ids"],
    [canonicalString(checked.requirement_verdicts), canonicalString(workValue.provisional_requirement_assessments), "$.requirement_verdicts"],
  ]) {
    verificationRequire(left === right, "VERIFICATION_CONTEXT_BINDING_INVALID", path);
  }
  for (const [ref, schemaId, document, path] of [
    [checked.verifier_input_manifest_ref, "verifier-input-manifest/v1", inputValue, "$.verifier_input_manifest_ref"],
    [checked.verifier_work_report_ref, "verifier-work-report/v1", workValue, "$.verifier_work_report_ref"],
    [checked.quality_policy_plan_ref, "quality-policy-plan/v1", planValue, "$.quality_policy_plan_ref"],
    [checked.final_observation_manifest_ref, "observation-manifest/v1", finalValue, "$.final_observation_manifest_ref"],
  ]) {
    verificationRequire(seoRefMatchesDocument(ref, schemaId, document), "CAS_CORRUPT", path);
  }
  const observations = verificationManifestObservationMap(finalValue);
  checked.own_observation_ids.forEach((value, index) => {
    const entry = observations.get(value);
    verificationRequire(entry !== undefined, "VERIFICATION_CONTEXT_OBSERVATION_INVALID", "$.own_observation_ids[" + index + "]");
    verificationRequire(entry.actor.kind === "quality_verifier", "VERIFICATION_CONTEXT_SESSION_INVALID", "$.own_observation_ids[" + index + "]");
    verificationRequire(entry.actor.session_id === checked.verifier_session_id, "VERIFICATION_CONTEXT_SESSION_INVALID", "$.own_observation_ids[" + index + "]");
  });
  verificationRequireTimeOrder([proposalValue.created_at, inputValue.created_at, workValue.created_at, checked.created_at], "$.created_at");
  return checked;
}

export async function verifyAttestationManifestContext(
  manifest, qualityPolicyPlan, finalObservationManifest, attestations,
) {
  const checked = await verifyDocumentDigest("verification-attestation-manifest/v1", manifest);
  const planValue = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const finalValue = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  const docs = await Promise.all(attestations.map(
    (item) => verifyDocumentDigest("verification-attestation/v1", item),
  ));
  for (const [left, right, path] of [
    [checked.task_id, planValue.task_id, "$.task_id"],
    [checked.task_id, finalValue.task_id, "$.task_id"],
    [checked.proposal_id, planValue.proposal_id, "$.proposal_id"],
    [checked.proposal_id, finalValue.proposal_id, "$.proposal_id"],
    [checked.quality_policy_plan_digest, planValue.plan_digest, "$.quality_policy_plan_digest"],
    [checked.final_observation_manifest_digest, finalValue.manifest_digest, "$.final_observation_manifest_digest"],
    [checked.attestation_count, docs.length, "$.attestation_count"],
  ]) {
    verificationRequire(left === right, "VERIFICATION_CONTEXT_BINDING_INVALID", path);
  }
  verificationRequire(
    seoRefMatchesDocument(checked.quality_policy_plan_ref, "quality-policy-plan/v1", planValue),
    "CAS_CORRUPT",
    "$.quality_policy_plan_ref",
  );
  verificationRequire(
    seoRefMatchesDocument(checked.final_observation_manifest_ref, "observation-manifest/v1", finalValue),
    "CAS_CORRUPT",
    "$.final_observation_manifest_ref",
  );
  const slotIds = planValue.slots.map((item) => item.slot_id);
  verificationRequire(
    canonicalString(checked.attestations.map((item) => item.slot_id)) === canonicalString(slotIds),
    "VERIFICATION_CONTEXT_SLOT_INVALID",
    "$.attestations",
  );
  const sessionIds = docs.map((item) => item.verifier_session_id);
  verificationRequire(new Set(sessionIds).size === sessionIds.length, "VERIFICATION_CONTEXT_SESSION_INVALID", "$.attestations");
  checked.attestations.forEach((item, index) => {
    const document = docs[index];
    verificationRequire(document !== undefined, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.attestations[" + index + "]");
    verificationRequire(item.attestation_id === document.attestation_id, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.attestations[" + index + "].attestation_id");
    verificationRequire(item.slot_id === document.slot_id, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.attestations[" + index + "].slot_id");
    verificationRequire(item.run_status === document.run_status, "VERIFICATION_CONTEXT_BINDING_INVALID", "$.attestations[" + index + "].run_status");
    verificationRequire(
      seoRefMatchesDocument(item.attestation_ref, "verification-attestation/v1", document),
      "CAS_CORRUPT",
      "$.attestations[" + index + "].attestation_ref",
    );
  });
  const bySlot = new Map(docs.map((item) => [item.slot_id, item]));
  const expectedAggregates = planValue.slots
    .flatMap((slot) => slot.requirement_ids.map((requirementId) => [requirementId, slot.slot_id]))
    .reduce((map, [requirementId, slotId]) => {
      const current = map.get(requirementId) ?? [];
      current.push(slotId);
      map.set(requirementId, current);
      return map;
    }, new Map());
  const aggregates = [...expectedAggregates.entries()].map(([requirementId, requiredSlotIds]) => {
    const result = verificationAggregateVerdict(requiredSlotIds, bySlot, requirementId);
    return {requirement_id: requirementId, required_slot_ids: requiredSlotIds, attestation_ids: result.attestation_ids, verdict: result.verdict};
  }).sort((left, right) => verificationTextCompare(left.requirement_id, right.requirement_id));
  verificationRequire(
    canonicalString(checked.requirement_aggregates) === canonicalString(aggregates),
    "VERIFICATION_CONTEXT_AGGREGATE_INVALID",
    "$.requirement_aggregates",
  );
  const created = docs.map((item) => item.created_at).sort();
  verificationRequireTimeOrder([created[created.length - 1], checked.created_at], "$.created_at");
  return checked;
}
'''


__all__ = ["NPM_VERIFICATION"]
