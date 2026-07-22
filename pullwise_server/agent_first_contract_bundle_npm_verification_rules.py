"""Node facade verification-family document rules and shared utilities."""

from __future__ import annotations


NPM_VERIFICATION_RULES = r'''
const VERIFICATION_CONTEXT_INVALID = "VERIFICATION_CONTEXT_INVALID";
const VERIFICATION_CONTEXT_CAS_CORRUPT = "VERIFICATION_CONTEXT_CAS_CORRUPT";
const VERIFICATION_CONTEXT_DIGEST_INVALID = "VERIFICATION_CONTEXT_DIGEST_INVALID";
const VERIFICATION_CONTEXT_TIME_INVALID = "VERIFICATION_CONTEXT_TIME_INVALID";

function verificationRequire(condition, detail, path = "$") {
  if (!condition) fail(detail, path);
}

function verificationTextCompare(left, right) {
  const a = [...String(left)];
  const b = [...String(right)];
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const diff = a[index].codePointAt(0) - b[index].codePointAt(0);
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function verificationCompareKey(left, right) {
  const a = Array.isArray(left) ? left : [left];
  const b = Array.isArray(right) ? right : [right];
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const diff = verificationTextCompare(a[index], b[index]);
    if (diff !== 0) return diff;
  }
  return a.length - b.length;
}

function verificationOrderedUnique(values, key) {
  const keys = values.map(key);
  for (let index = 1; index < keys.length; index += 1) {
    if (verificationCompareKey(keys[index - 1], keys[index]) >= 0) return false;
  }
  return true;
}

function verificationSortedUniqueStrings(values) {
  return verificationOrderedUnique(values, (item) => item);
}

function verificationDigestField(schemaId) {
  const spec = schema(schemaId)["x-pullwise-digest"];
  return spec && typeof spec.field === "string" ? spec.field : null;
}

async function verificationCheckDocument(schemaId, value) {
  return verificationDigestField(schemaId) === null
    ? validateDocument(schemaId, value)
    : await verifyDocumentDigest(schemaId, value);
}

async function verificationCheckDocuments(schemaId, values, path) {
  verificationRequire(Array.isArray(values), VERIFICATION_CONTEXT_INVALID, path);
  return await Promise.all(
    values.map((item) => verificationCheckDocument(schemaId, item)),
  );
}

function verificationDocumentIdentityDigest(schemaId, document) {
  const field = verificationDigestField(schemaId);
  return field === null ? sha256Sync(canonicalDocumentBytes(document)) : document[field];
}

function verificationRequireRef(ref, schemaId, document, path) {
  verificationRequire(
    seoRefMatchesDocument(ref, schemaId, document),
    VERIFICATION_CONTEXT_CAS_CORRUPT,
    path,
  );
}

function verificationRequireCompanionDigest(actual, schemaId, document, path) {
  verificationRequire(
    actual === verificationDocumentIdentityDigest(schemaId, document),
    VERIFICATION_CONTEXT_DIGEST_INVALID,
    path,
  );
}

function verificationRequireIdList(actual, expected, path) {
  verificationRequire(
    canonicalString(actual) === canonicalString(expected),
    VERIFICATION_CONTEXT_INVALID,
    path,
  );
}

function verificationTimestampMillis(value) {
  if (typeof value !== "string") return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, millis] = match.map(Number);
  const epoch = Date.UTC(year, month - 1, day, hour, minute, second, millis);
  if (!Number.isFinite(epoch)) return null;
  return new Date(epoch).toISOString() === value ? epoch : null;
}

function verificationTimestampLeq(earlier, later, path) {
  if (earlier === null || later === null) return;
  const left = verificationTimestampMillis(earlier);
  const right = verificationTimestampMillis(later);
  verificationRequire(
    left !== null && right !== null && left <= right,
    VERIFICATION_CONTEXT_TIME_INVALID,
    path,
  );
}

function verificationFindPlanSlot(plan, slotId) {
  return plan.slots.find((item) => item.slot_id === slotId) ?? null;
}

function verificationManifestEntries(manifest) {
  return Object.fromEntries(
    manifest.entries.map((item) => [item.observation_id, item]),
  );
}

function verificationAssessmentIds(values) {
  return values.map((item) => item.requirement_id);
}

function verificationArtifactKey(value) {
  const ref = value.ref ?? value;
  return ref.artifact_id;
}

function verificationRefKey(value) {
  return [value.content_schema_id, value.artifact_id, value.sha256];
}

function verificationValidAssessments(values) {
  return verificationOrderedUnique(values, (item) => item.requirement_id) &&
    values.every((item) =>
      verificationSortedUniqueStrings(item.evidence_ids) &&
      verificationSortedUniqueStrings(item.limitations) &&
      (item.verdict !== "PASS" || item.limitations.length === 0));
}

function verificationAggregateRunStatus(values) {
  const present = new Set(values.map((item) => item.verdict));
  for (const verdict of ["POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE"]) {
    if (present.has(verdict)) return verdict;
  }
  return "PASS";
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
  for (const item of value.requirement_claims) {
    verificationRequire(
      verificationSortedUniqueStrings(item.evidence_ids),
      "PROPOSAL_EVIDENCE_ORDER_INVALID",
    );
  }
  verificationRequire(
    verificationSortedUniqueStrings(value.known_gaps),
    "PROPOSAL_GAP_ORDER_INVALID",
  );
  verificationRequire(
    verificationSortedUniqueStrings(value.residual_risks),
    "PROPOSAL_RISK_ORDER_INVALID",
  );
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
  verificationRequire(
    value.owner_conclusion_excluded === true,
    "VERIFIER_OWNER_CONCLUSION_INCLUDED",
  );
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
  verificationRequire(
    value.sandbox_mode === "read_only_or_cow",
    "VERIFIER_SANDBOX_INVALID",
  );
  for (const field of ["counterexamples_searched", "own_observation_ids", "limitations"]) {
    verificationRequire(
      verificationSortedUniqueStrings(value[field]),
      "VERIFIER_WORK_ORDER_INVALID",
      "$." + field,
    );
  }
  verificationRequire(
    value.own_observation_ids.length > 0,
    "VERIFIER_OBSERVATION_REQUIRED",
  );
  verificationRequire(
    verificationValidAssessments(value.provisional_requirement_assessments),
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
    verificationSortedUniqueStrings(value.own_observation_ids) &&
      value.own_observation_ids.length > 0,
    "ATTESTATION_OBSERVATION_INVALID",
  );
  verificationRequire(
    verificationValidAssessments(value.requirement_verdicts),
    "ATTESTATION_VERDICT_INVALID",
  );
  verificationRequire(
    value.run_status === verificationAggregateRunStatus(value.requirement_verdicts),
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
    verificationOrderedUnique(
      value.attestations,
      (item) => [item.slot_id, item.attestation_id],
    ),
    "ATTESTATION_MANIFEST_ORDER_INVALID",
  );
  const slots = new Set(value.attestations.map((item) => item.slot_id));
  const ids = new Set(value.attestations.map((item) => item.attestation_id));
  verificationRequire(
    verificationOrderedUnique(
      value.requirement_aggregates,
      (item) => item.requirement_id,
    ),
    "ATTESTATION_AGGREGATE_ORDER_INVALID",
  );
  for (const item of value.requirement_aggregates) {
    verificationRequire(
      verificationSortedUniqueStrings(item.required_slot_ids),
      "ATTESTATION_REQUIRED_SLOT_ORDER_INVALID",
    );
    verificationRequire(
      verificationSortedUniqueStrings(item.attestation_ids),
      "ATTESTATION_ID_ORDER_INVALID",
    );
    verificationRequire(
      item.attestation_ids.every((id) => ids.has(id)),
      "ATTESTATION_ID_UNKNOWN",
    );
    if (item.required_slot_ids.some((slotId) => !slots.has(slotId))) {
      verificationRequire(
        item.verdict === "UNVERIFIABLE",
        "ATTESTATION_MISSING_SLOT_INVALID",
      );
    }
  }
}
'''


__all__ = ["NPM_VERIFICATION_RULES"]
