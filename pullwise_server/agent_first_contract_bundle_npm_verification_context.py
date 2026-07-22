"""Node facade verification-family direct context helpers."""

from __future__ import annotations


NPM_VERIFICATION_CONTEXT = r'''
export async function verifyCompletionProposalContext(proposal, taskSnapshot, attempt, owner, taskRequest, effectivePolicy, requirementLedger, executionCharter, originalSource, finalSource, executionStates, changeSet, preObservationManifest) {
  const checked = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const snapshot = await verificationCheckDocument("task-record/v1", taskSnapshot);
  const currentAttempt = await verificationCheckDocument("attempt-record/v1", attempt);
  const ownerDoc = await verificationCheckDocument("task-owner/v1", owner);
  const request = await verificationCheckDocument("task-request/v1", taskRequest);
  const policy = await verifyDocumentDigest("effective-execution-policy/v1", effectivePolicy);
  const ledger = await verifyDocumentDigest("requirement-ledger/v1", requirementLedger);
  const charter = await verifyDocumentDigest("task-charter/v1", executionCharter);
  const original = await verifyDocumentDigest("source-tree-manifest/v1", originalSource);
  const final = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const states = await verificationCheckDocuments("execution-state-manifest/v1", executionStates, "$.execution_states");
  const pre = await verifyDocumentDigest("pre-verifier-observation-manifest/v1", preObservationManifest);
  const change = changeSet === null ? null : await verifyDocumentDigest("change-set/v1", changeSet);
  verificationRequire(checked.task_id === snapshot.task_id && checked.task_id === currentAttempt.task_id && checked.task_id === ownerDoc.task_id && checked.task_id === request.task_id && checked.task_id === ledger.task_id && checked.task_id === charter.task_id, VERIFICATION_CONTEXT_INVALID, "$.task_id");
  verificationRequire(currentAttempt.attempt_id === ownerDoc.attempt_id && ownerDoc.attempt_id === checked.attempt_id, VERIFICATION_CONTEXT_INVALID, "$.attempt_id");
  verificationRequire(snapshot.current_attempt_id === checked.attempt_id, VERIFICATION_CONTEXT_INVALID, "$.current_attempt_id");
  verificationRequire(snapshot.owner_id === ownerDoc.owner_id && ownerDoc.owner_id === checked.owner_id, VERIFICATION_CONTEXT_INVALID, "$.owner_id");
  verificationRequire(snapshot.owner_epoch === ownerDoc.owner_epoch && ownerDoc.owner_epoch === checked.owner_epoch, VERIFICATION_CONTEXT_INVALID, "$.owner_epoch");
  verificationRequire(snapshot.task_version === checked.proposed_from_task_version, VERIFICATION_CONTEXT_INVALID, "$.proposed_from_task_version");
  verificationRequire(checked.native_epoch === snapshot.native_epoch && checked.native_epoch === currentAttempt.native_epoch && currentAttempt.native_epoch === ownerDoc.native_epoch, VERIFICATION_CONTEXT_INVALID, "$.native_epoch");
  verificationRequire(ownerDoc.session_id === currentAttempt.owner_session_id, VERIFICATION_CONTEXT_INVALID, "$.owner_session_id");
  verificationRequireRef(snapshot.request_ref, "task-request/v1", request, "$.request_ref");
  verificationRequireCompanionDigest(snapshot.request_digest, "task-request/v1", request, "$.request_digest");
  verificationRequireCompanionDigest(checked.request_digest, "task-request/v1", request, "$.request_digest");
  verificationRequireRef(snapshot.policy_ref, "effective-execution-policy/v1", policy, "$.policy_ref");
  verificationRequireCompanionDigest(snapshot.policy_digest, "effective-execution-policy/v1", policy, "$.policy_digest");
  verificationRequireCompanionDigest(checked.policy_digest, "effective-execution-policy/v1", policy, "$.policy_digest");
  verificationRequireCompanionDigest(snapshot.ledger_head_digest, "requirement-ledger/v1", ledger, "$.requirement_ledger_digest");
  verificationRequireCompanionDigest(checked.requirement_ledger_digest, "requirement-ledger/v1", ledger, "$.requirement_ledger_digest");
  verificationRequire(snapshot.charter_ref !== null, VERIFICATION_CONTEXT_INVALID, "$.charter_ref");
  verificationRequireRef(snapshot.charter_ref, "task-charter/v1", charter, "$.charter_ref");
  if (snapshot.completion_proposal_ref !== null) verificationRequireRef(snapshot.completion_proposal_ref, "completion-proposal/v1", checked, "$.completion_proposal_ref");
  verificationRequireCompanionDigest(checked.charter_digest, "task-charter/v1", charter, "$.charter_digest");
  verificationRequire(snapshot.task_type === request.task_type && request.task_type === policy.task_type, VERIFICATION_CONTEXT_INVALID, "$.task_type");
  verificationRequire(checked.original_source_state_id === original.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.original_source_state_id");
  verificationRequire(checked.final_source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.final_source_state_id");
  verificationRequireIdList(checked.execution_state_ids, states.map((item) => item.execution_state_id), "$.execution_state_ids");
  states.forEach((item, index) => verificationRequire(item.source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, `$.execution_state_ids[${index}]`));
  if (change === null) {
    verificationRequire(checked.change_set_ref === null, VERIFICATION_CONTEXT_INVALID, "$.change_set_ref");
  } else {
    verificationRequireRef(checked.change_set_ref, "change-set/v1", change, "$.change_set_ref");
    verificationRequire(change.original_source_state_id === original.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.change_set_ref");
    verificationRequire(change.final_source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.change_set_ref");
  }
  verificationRequire(pre.task_id === checked.task_id && pre.proposal_id === checked.proposal_id && pre.attempt_id === checked.attempt_id && pre.native_epoch === checked.native_epoch, VERIFICATION_CONTEXT_INVALID, "$.pre_observation_manifest");
  verificationRequireIdList(verificationAssessmentIds(checked.requirement_claims), ledger.active_requirement_ids, "$.requirement_claims");
  const preIds = new Set(pre.entries.map((item) => item.observation_id));
  checked.requirement_claims.forEach((item, index) => {
    verificationRequire(item.evidence_ids.every((id) => preIds.has(id)), VERIFICATION_CONTEXT_INVALID, `$.requirement_claims[${index}].evidence_ids`);
  });
  for (const [, timestamp] of [["$.task_snapshot.created_at", snapshot.created_at], ["$.task_snapshot.updated_at", snapshot.updated_at], ["$.attempt.lease_acquired_at", currentAttempt.lease_acquired_at], ["$.attempt.started_at", currentAttempt.started_at], ["$.attempt.ended_at", currentAttempt.ended_at], ["$.owner.started_at", ownerDoc.started_at], ["$.owner.ended_at", ownerDoc.ended_at], ["$.task_request.submitted_at", request.submitted_at], ["$.effective_policy.issued_at", policy.issued_at], ["$.execution_charter.created_at", charter.created_at]]) {
    verificationTimestampLeq(timestamp, checked.created_at, "$.created_at");
  }
  return checked;
}

export async function verifyVerifierInputContext(manifest, proposal, qualityPolicyPlan, taskRequest, effectivePolicy, requirementLedger, executionCharter, originalSource, finalSource, changeSet, preObservationManifest, engineeringRules) {
  const checked = await verifyDocumentDigest("verifier-input-manifest/v1", manifest);
  const proposalDoc = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const plan = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const request = await verificationCheckDocument("task-request/v1", taskRequest);
  const policy = await verifyDocumentDigest("effective-execution-policy/v1", effectivePolicy);
  const ledger = await verifyDocumentDigest("requirement-ledger/v1", requirementLedger);
  const charter = await verifyDocumentDigest("task-charter/v1", executionCharter);
  const original = await verifyDocumentDigest("source-tree-manifest/v1", originalSource);
  const final = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const pre = await verifyDocumentDigest("pre-verifier-observation-manifest/v1", preObservationManifest);
  const change = changeSet === null ? null : await verifyDocumentDigest("change-set/v1", changeSet);
  const rules = await verificationCheckDocuments("source-content/v1", engineeringRules, "$.engineering_rules");
  const slot = verificationFindPlanSlot(plan, checked.slot_id);
  verificationRequire(slot !== null, VERIFICATION_CONTEXT_INVALID, "$.slot_id");
  verificationRequire(checked.task_id === proposalDoc.task_id && checked.task_id === request.task_id && checked.task_id === ledger.task_id && checked.task_id === charter.task_id && checked.task_id === plan.task_id, VERIFICATION_CONTEXT_INVALID, "$.task_id");
  verificationRequire(checked.proposal_id === proposalDoc.proposal_id && checked.proposal_id === plan.proposal_id, VERIFICATION_CONTEXT_INVALID, "$.proposal_id");
  verificationRequire(request.task_type === policy.task_type, VERIFICATION_CONTEXT_INVALID, "$.task_type");
  verificationRequireRef(checked.task_request_ref, "task-request/v1", request, "$.task_request_ref");
  verificationRequireRef(checked.effective_policy_ref, "effective-execution-policy/v1", policy, "$.effective_policy_ref");
  verificationRequireRef(checked.requirement_ledger_ref, "requirement-ledger/v1", ledger, "$.requirement_ledger_ref");
  verificationRequireRef(checked.charter_ref, "task-charter/v1", charter, "$.charter_ref");
  verificationRequireRef(checked.completion_proposal_ref, "completion-proposal/v1", proposalDoc, "$.completion_proposal_ref");
  verificationRequireRef(checked.quality_policy_plan_ref, "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref");
  verificationRequireCompanionDigest(checked.quality_policy_plan_digest, "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest");
  verificationRequireRef(checked.original_source_ref, "source-tree-manifest/v1", original, "$.original_source_ref");
  verificationRequireRef(checked.final_source_ref, "source-tree-manifest/v1", final, "$.final_source_ref");
  verificationRequireRef(checked.pre_verifier_observation_manifest_ref, "pre-verifier-observation-manifest/v1", pre, "$.pre_verifier_observation_manifest_ref");
  verificationRequireCompanionDigest(proposalDoc.request_digest, "task-request/v1", request, "$.request_digest");
  verificationRequireCompanionDigest(proposalDoc.charter_digest, "task-charter/v1", charter, "$.charter_digest");
  verificationRequire(canonicalString(checked.artifact_refs) === canonicalString(proposalDoc.artifact_refs), VERIFICATION_CONTEXT_INVALID, "$.artifact_refs");
  verificationRequire(checked.engineering_rule_refs.length === rules.length, VERIFICATION_CONTEXT_INVALID, "$.engineering_rule_refs");
  checked.engineering_rule_refs.forEach((ref, index) => {
    verificationRequireRef(ref, "source-content/v1", rules[index], `$.engineering_rule_refs[${index}]`);
  });
  if (change === null) {
    verificationRequire(checked.change_set.availability === "not_applicable" && proposalDoc.change_set_ref === null, VERIFICATION_CONTEXT_INVALID, "$.change_set");
  } else {
    verificationRequire(checked.change_set.availability === "available", VERIFICATION_CONTEXT_INVALID, "$.change_set");
    verificationRequireRef(checked.change_set.ref, "change-set/v1", change, "$.change_set.ref");
    verificationRequireRef(proposalDoc.change_set_ref, "change-set/v1", change, "$.completion_proposal_ref");
    verificationRequire(change.original_source_state_id === original.source_state_id && change.final_source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.change_set");
  }
  verificationRequire(plan.proposal_digest === proposalDoc.proposal_digest, VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest");
  verificationRequire(plan.policy_digest === proposalDoc.policy_digest && proposalDoc.policy_digest === policy.digest, VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest");
  verificationRequire(plan.requirement_ledger_digest === ledger.ledger_digest, VERIFICATION_CONTEXT_DIGEST_INVALID, "$.quality_policy_plan_digest");
  verificationRequire(proposalDoc.original_source_state_id === original.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.original_source_ref");
  verificationRequire(proposalDoc.final_source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, "$.final_source_ref");
  verificationRequire(pre.task_id === proposalDoc.task_id && pre.proposal_id === proposalDoc.proposal_id && pre.attempt_id === proposalDoc.attempt_id && pre.native_epoch === proposalDoc.native_epoch, VERIFICATION_CONTEXT_INVALID, "$.pre_verifier_observation_manifest_ref");
  verificationRequire(checked.slot_concern === slot.concern, VERIFICATION_CONTEXT_INVALID, "$.slot_concern");
  verificationRequireIdList(checked.requirement_ids, slot.requirement_ids, "$.requirement_ids");
  verificationTimestampLeq(proposalDoc.created_at, checked.created_at, "$.created_at");
  return checked;
}

export async function verifyVerifierWorkContext(report, verifierInput, proposal, finalObservationManifest) {
  const checked = await verifyDocumentDigest("verifier-work-report/v1", report);
  const inputDoc = await verifyDocumentDigest("verifier-input-manifest/v1", verifierInput);
  const proposalDoc = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const finalManifest = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  const entries = verificationManifestEntries(finalManifest);
  verificationRequireRef(checked.verifier_input_manifest_ref, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_ref");
  verificationRequireCompanionDigest(checked.verifier_input_manifest_digest, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_digest");
  verificationRequire(checked.task_id === inputDoc.task_id && checked.task_id === proposalDoc.task_id && checked.task_id === finalManifest.task_id, VERIFICATION_CONTEXT_INVALID, "$.task_id");
  verificationRequire(checked.proposal_id === inputDoc.proposal_id && checked.proposal_id === proposalDoc.proposal_id && checked.proposal_id === finalManifest.proposal_id, VERIFICATION_CONTEXT_INVALID, "$.proposal_id");
  verificationRequire(checked.slot_id === inputDoc.slot_id, VERIFICATION_CONTEXT_INVALID, "$.slot_id");
  verificationRequireRef(inputDoc.completion_proposal_ref, "completion-proposal/v1", proposalDoc, "$.completion_proposal_ref");
  verificationRequire(finalManifest.attempt_id === proposalDoc.attempt_id && finalManifest.native_epoch === proposalDoc.native_epoch, VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest");
  verificationRequireIdList(verificationAssessmentIds(checked.provisional_requirement_assessments), inputDoc.requirement_ids, "$.provisional_requirement_assessments");
  const finalIds = new Set(Object.keys(entries));
  const ownIds = new Set(checked.own_observation_ids);
  verificationRequire([...ownIds].every((id) => finalIds.has(id)), VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids");
  checked.own_observation_ids.forEach((id) => {
    const actor = entries[id].actor;
    verificationRequire(actor.kind === "quality_verifier" && actor.session_id === checked.verifier_session_id, VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids");
  });
  checked.provisional_requirement_assessments.forEach((item, index) => {
    const evidence = new Set(item.evidence_ids);
    verificationRequire([...evidence].every((id) => finalIds.has(id) && ownIds.has(id)), VERIFICATION_CONTEXT_INVALID, `$.provisional_requirement_assessments[${index}].evidence_ids`);
  });
  verificationTimestampLeq(proposalDoc.created_at, checked.created_at, "$.created_at");
  verificationTimestampLeq(inputDoc.created_at, checked.created_at, "$.created_at");
  return checked;
}

export async function verifyAttestationContext(attestation, verifierInput, verifierWork, proposal, qualityPolicyPlan, finalSource, executionStates, finalObservationManifest) {
  const checked = await verifyDocumentDigest("verification-attestation/v1", attestation);
  const inputDoc = await verifyDocumentDigest("verifier-input-manifest/v1", verifierInput);
  const work = await verifyDocumentDigest("verifier-work-report/v1", verifierWork);
  const proposalDoc = await verifyDocumentDigest("completion-proposal/v1", proposal);
  const plan = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const final = await verifyDocumentDigest("source-tree-manifest/v1", finalSource);
  const states = await verificationCheckDocuments("execution-state-manifest/v1", executionStates, "$.execution_states");
  const manifest = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  const entries = verificationManifestEntries(manifest);
  verificationRequireRef(checked.verifier_input_manifest_ref, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_ref");
  verificationRequireCompanionDigest(checked.verifier_input_manifest_digest, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_digest");
  verificationRequireRef(checked.verifier_work_report_ref, "verifier-work-report/v1", work, "$.verifier_work_report_ref");
  verificationRequireCompanionDigest(checked.verifier_work_report_digest, "verifier-work-report/v1", work, "$.verifier_work_report_digest");
  verificationRequireRef(checked.quality_policy_plan_ref, "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref");
  verificationRequireCompanionDigest(checked.quality_policy_plan_digest, "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest");
  verificationRequireRef(checked.final_observation_manifest_ref, "observation-manifest/v1", manifest, "$.final_observation_manifest_ref");
  verificationRequireCompanionDigest(checked.final_observation_manifest_digest, "observation-manifest/v1", manifest, "$.final_observation_manifest_digest");
  verificationRequireRef(inputDoc.completion_proposal_ref, "completion-proposal/v1", proposalDoc, "$.completion_proposal_ref");
  verificationRequireRef(work.verifier_input_manifest_ref, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_ref");
  verificationRequireCompanionDigest(work.verifier_input_manifest_digest, "verifier-input-manifest/v1", inputDoc, "$.verifier_input_manifest_digest");
  verificationRequire(checked.task_id === inputDoc.task_id && checked.task_id === work.task_id && checked.task_id === proposalDoc.task_id && checked.task_id === plan.task_id && checked.task_id === manifest.task_id, VERIFICATION_CONTEXT_INVALID, "$.task_id");
  verificationRequire(checked.proposal_id === inputDoc.proposal_id && checked.proposal_id === work.proposal_id && checked.proposal_id === proposalDoc.proposal_id && checked.proposal_id === plan.proposal_id && checked.proposal_id === manifest.proposal_id, VERIFICATION_CONTEXT_INVALID, "$.proposal_id");
  verificationRequire(checked.slot_id === inputDoc.slot_id && checked.slot_id === work.slot_id, VERIFICATION_CONTEXT_INVALID, "$.slot_id");
  verificationRequire(checked.verifier_session_id === work.verifier_session_id, VERIFICATION_CONTEXT_INVALID, "$.verifier_session_id");
  verificationRequire(checked.model_identity === work.model_identity, VERIFICATION_CONTEXT_INVALID, "$.model_identity");
  verificationRequire(checked.source_state_id === final.source_state_id && final.source_state_id === proposalDoc.final_source_state_id, VERIFICATION_CONTEXT_INVALID, "$.source_state_id");
  verificationRequireIdList(checked.execution_state_ids, states.map((item) => item.execution_state_id), "$.execution_state_ids");
  states.forEach((item, index) => verificationRequire(item.source_state_id === final.source_state_id, VERIFICATION_CONTEXT_INVALID, `$.execution_state_ids[${index}]`));
  verificationRequireRef(inputDoc.quality_policy_plan_ref, "quality-policy-plan/v1", plan, "$.verifier_input_manifest_ref.quality_policy_plan_ref");
  verificationRequireCompanionDigest(inputDoc.quality_policy_plan_digest, "quality-policy-plan/v1", plan, "$.verifier_input_manifest_digest.quality_policy_plan_digest");
  verificationRequire(plan.proposal_digest === proposalDoc.proposal_digest, VERIFICATION_CONTEXT_DIGEST_INVALID, "$.proposal_digest");
  verificationRequire(manifest.attempt_id === proposalDoc.attempt_id && manifest.native_epoch === proposalDoc.native_epoch, VERIFICATION_CONTEXT_INVALID, "$.final_observation_manifest");
  const attestationSlot = verificationFindPlanSlot(plan, checked.slot_id);
  verificationRequire(attestationSlot !== null && canonicalString(verificationAssessmentIds(checked.requirement_verdicts)) === canonicalString(attestationSlot.requirement_ids), VERIFICATION_CONTEXT_INVALID, "$.requirement_verdicts");
  verificationRequire(canonicalString(checked.own_observation_ids) === canonicalString(work.own_observation_ids), VERIFICATION_CONTEXT_INVALID, "$.own_observation_ids");
  const finalIds = new Set(Object.keys(entries));
  const ownIds = new Set(checked.own_observation_ids);
  checked.own_observation_ids.forEach((id, index) => {
    const actor = entries[id]?.actor;
    verificationRequire(actor !== undefined && actor.kind === "quality_verifier" && actor.session_id === checked.verifier_session_id, VERIFICATION_CONTEXT_INVALID, `$.own_observation_ids[${index}]`);
  });
  manifest.entries.forEach((entry, index) => verificationRequire(entry.actor.session_id !== checked.verifier_session_id || entry.actor.kind === "quality_verifier", VERIFICATION_CONTEXT_INVALID, `$.final_observation_manifest.entries[${index}].actor`));
  verificationRequireIdList(verificationAssessmentIds(checked.requirement_verdicts), inputDoc.requirement_ids, "$.requirement_verdicts");
  checked.requirement_verdicts.forEach((item, index) => {
    const evidence = new Set(item.evidence_ids);
    verificationRequire([...evidence].every((id) => finalIds.has(id) && ownIds.has(id)), VERIFICATION_CONTEXT_INVALID, `$.requirement_verdicts[${index}].evidence_ids`);
  });
  verificationTimestampLeq(proposalDoc.created_at, checked.created_at, "$.created_at");
  verificationTimestampLeq(inputDoc.created_at, checked.created_at, "$.created_at");
  verificationTimestampLeq(work.created_at, checked.created_at, "$.created_at");
  return checked;
}

export async function verifyAttestationManifestContext(manifest, qualityPolicyPlan, finalObservationManifest, attestations) {
  const checked = await verifyDocumentDigest("verification-attestation-manifest/v1", manifest);
  const plan = await verifyDocumentDigest("quality-policy-plan/v1", qualityPolicyPlan);
  const finalManifest = await verifyDocumentDigest("observation-manifest/v1", finalObservationManifest);
  const attestationDocs = await verificationCheckDocuments("verification-attestation/v1", attestations, "$.attestations");
  verificationRequireRef(checked.quality_policy_plan_ref, "quality-policy-plan/v1", plan, "$.quality_policy_plan_ref");
  verificationRequireCompanionDigest(checked.quality_policy_plan_digest, "quality-policy-plan/v1", plan, "$.quality_policy_plan_digest");
  verificationRequireRef(checked.final_observation_manifest_ref, "observation-manifest/v1", finalManifest, "$.final_observation_manifest_ref");
  verificationRequireCompanionDigest(checked.final_observation_manifest_digest, "observation-manifest/v1", finalManifest, "$.final_observation_manifest_digest");
  verificationRequire(checked.task_id === plan.task_id && plan.task_id === finalManifest.task_id, VERIFICATION_CONTEXT_INVALID, "$.task_id");
  verificationRequire(checked.proposal_id === plan.proposal_id && plan.proposal_id === finalManifest.proposal_id, VERIFICATION_CONTEXT_INVALID, "$.proposal_id");
  verificationRequire(checked.attestation_count === attestationDocs.length && checked.attestations.length === attestationDocs.length, VERIFICATION_CONTEXT_INVALID, "$.attestations");
  const attestationById = new Map();
  const attestationBySlot = new Map();
  const planSlots = new Map(plan.slots.map((item) => [item.slot_id, item]));
  attestationDocs.forEach((document, index) => {
    verificationRequire(!attestationById.has(document.attestation_id), VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}]`);
    verificationRequire(document.task_id === checked.task_id && document.proposal_id === checked.proposal_id, VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}]`);
    verificationRequireRef(document.quality_policy_plan_ref, "quality-policy-plan/v1", plan, `$.attestations[${index}].quality_policy_plan_ref`);
    verificationRequireCompanionDigest(document.quality_policy_plan_digest, "quality-policy-plan/v1", plan, `$.attestations[${index}].quality_policy_plan_digest`);
    verificationRequireRef(document.final_observation_manifest_ref, "observation-manifest/v1", finalManifest, `$.attestations[${index}].final_observation_manifest_ref`);
    verificationRequireCompanionDigest(document.final_observation_manifest_digest, "observation-manifest/v1", finalManifest, `$.attestations[${index}].final_observation_manifest_digest`);
    const slot = planSlots.get(document.slot_id);
    verificationRequire(slot !== undefined && !attestationBySlot.has(document.slot_id), VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}].slot_id`);
    verificationRequire(canonicalString(verificationAssessmentIds(document.requirement_verdicts)) === canonicalString(slot.requirement_ids), VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}].requirement_verdicts`);
    attestationById.set(document.attestation_id, document);
    attestationBySlot.set(document.slot_id, document);
  });
  const planSlotSet = new Set(plan.slots.map((item) => item.slot_id));
  const manifestSlots = [];
  checked.attestations.forEach((entry, index) => {
    const document = attestationById.get(entry.attestation_id);
    verificationRequire(document !== undefined, VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}]`);
    verificationRequire(entry.slot_id === document.slot_id && entry.run_status === document.run_status, VERIFICATION_CONTEXT_INVALID, `$.attestations[${index}]`);
    verificationRequireRef(entry.attestation_ref, "verification-attestation/v1", document, `$.attestations[${index}].attestation_ref`);
    manifestSlots.push(entry.slot_id);
  });
  verificationRequire(manifestSlots.every((slotId) => planSlotSet.has(slotId)) && new Set(manifestSlots).size === manifestSlots.length, VERIFICATION_CONTEXT_INVALID, "$.attestations");
  const sessions = attestationDocs.map((item) => item.verifier_session_id);
  verificationRequire(new Set(sessions).size === sessions.length, VERIFICATION_CONTEXT_INVALID, "$.attestations");
  const expectedRequirements = [...new Set(plan.slots.flatMap((slot) => slot.requirement_ids))].sort(verificationTextCompare);
  verificationRequireIdList(checked.requirement_aggregates.map((item) => item.requirement_id), expectedRequirements, "$.requirement_aggregates");
  const bySlot = attestationBySlot;
  checked.requirement_aggregates.forEach((aggregate, index) => {
    const requiredSlotIds = plan.slots.filter((slot) => slot.requirement_ids.includes(aggregate.requirement_id)).map((slot) => slot.slot_id).sort(verificationTextCompare);
    const matched = [];
    const verdicts = [];
    let missing = false;
    requiredSlotIds.forEach((slotId) => {
      const document = bySlot.get(slotId);
      if (document === undefined) {
        missing = true;
        return;
      }
      const verdict = document.requirement_verdicts.find((item) => item.requirement_id === aggregate.requirement_id);
      if (verdict === undefined) {
        missing = true;
        return;
      }
      matched.push(document.attestation_id);
      verdicts.push(verdict.verdict);
    });
    matched.sort(verificationTextCompare);
    const expectedVerdict = missing ? "UNVERIFIABLE" : verdicts.some((item) => item === "POLICY_VIOLATION" || item === "NEEDS_WORK") ? "FAIL" : verdicts.includes("UNVERIFIABLE") ? "UNVERIFIABLE" : "PASS";
    verificationRequire(canonicalString(aggregate.required_slot_ids) === canonicalString(requiredSlotIds), VERIFICATION_CONTEXT_INVALID, `$.requirement_aggregates[${index}].required_slot_ids`);
    verificationRequire(canonicalString(aggregate.attestation_ids) === canonicalString(matched), VERIFICATION_CONTEXT_INVALID, `$.requirement_aggregates[${index}].attestation_ids`);
    verificationRequire(aggregate.verdict === expectedVerdict, VERIFICATION_CONTEXT_INVALID, `$.requirement_aggregates[${index}].verdict`);
  });
  const manifestTime = verificationTimestampMillis(checked.created_at);
  verificationRequire(manifestTime !== null, VERIFICATION_CONTEXT_TIME_INVALID, "$.created_at");
  attestationDocs.forEach((item) => verificationTimestampLeq(item.created_at, checked.created_at, "$.created_at"));
  return checked;
}
'''


__all__ = ["NPM_VERIFICATION_CONTEXT"]
