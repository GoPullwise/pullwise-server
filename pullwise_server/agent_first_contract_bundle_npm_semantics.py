"""Generated JavaScript facade semantic validators."""

from __future__ import annotations

from .agent_first_contract_bundle_npm_budget import NPM_BUDGET
from .agent_first_contract_bundle_npm_change_set_patch import (
    NPM_CHANGE_SET_PATCH_RULE,
)
from .agent_first_contract_bundle_npm_effective_policy import (
    NPM_EFFECTIVE_POLICY_RULES,
)
from .agent_first_contract_bundle_npm_execution_profile import (
    NPM_EXECUTION_PROFILE_RULE,
)
from .agent_first_contract_bundle_npm_gate import NPM_GATE
from .agent_first_contract_bundle_npm_gate_input import NPM_GATE_INPUT
from .agent_first_contract_bundle_npm_gate_preparation import NPM_GATE_PREPARATION
from .agent_first_contract_bundle_npm_observation import NPM_OBSERVATION_RULE
from .agent_first_contract_bundle_npm_source_execution_observation import (
    NPM_SOURCE_EXECUTION_OBSERVATION,
)
from .agent_first_contract_bundle_npm_pre_gate import NPM_PRE_GATE
from .agent_first_contract_bundle_npm_publication import NPM_PUBLICATION
from .agent_first_contract_bundle_npm_quality_policy import NPM_QUALITY_POLICY
from .agent_first_contract_bundle_npm_release_gate import NPM_RELEASE_GATE
from .agent_first_contract_bundle_npm_release_gate_evaluator import (
    NPM_RELEASE_GATE_EVALUATOR,
)
from .agent_first_contract_bundle_npm_release_trust import NPM_RELEASE_TRUST
from .agent_first_contract_bundle_npm_result import NPM_RESULT
from .agent_first_contract_bundle_npm_task_evidence import NPM_TASK_EVIDENCE
from .agent_first_contract_bundle_npm_task_control_helpers import (
    NPM_TASK_CONTROL_HELPERS,
)
from .agent_first_contract_bundle_npm_task_control_rules import (
    NPM_TASK_CONTROL_RULES,
)
from .agent_first_contract_bundle_npm_tool_evidence import NPM_TOOL_EVIDENCE
from .agent_first_contract_bundle_npm_verification import NPM_VERIFICATION


NPM_SEMANTICS_BASE = r'''
function publicErrorCode(detail, explicit) {
  const codes = new Set(bundle().families.flatMap(
    (family) => family.fixtures
      .filter((item) => item.fixture_id === "error_golden_current_registry")
      .flatMap((item) => item.document.entries.map((entry) => entry.code)),
  ));
  const candidate = explicit ?? detail;
  return codes.has(candidate) ? candidate : "CONTRACT_DOCUMENT_INVALID";
}

function canonicalString(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return "[" + value.map(canonicalString).join(",") + "]";
  return "{" + Object.keys(value).sort().map(
    (key) => JSON.stringify(key) + ":" + canonicalString(value[key]),
  ).join(",") + "}";
}

function validateOneOf(options, value, path) {
  let matches = 0;
  for (const option of options) {
    try {
      validateNode(option, value, path);
      matches += 1;
    } catch (error) {
      if (!(error instanceof ContractValidationError)) throw error;
    }
  }
  if (matches !== 1) fail("CONTRACT_ONE_OF_INVALID", path);
}

function patternMatches(pattern, value) {
  const match = new RegExp(pattern).exec(value);
  if (!match) return false;
  if (pattern.startsWith("^") && pattern.endsWith("$")) {
    return match.index === 0 && match[0].length === value.length;
  }
  return true;
}

function validateReferenceAnnotations(rule, value, path) {
  let expected = rule["x-pullwise-content-schema-id"];
  let allowed = rule["x-pullwise-content-schema-ids"];
  if (expected !== undefined || allowed !== undefined) {
    const targets = expected !== undefined ? [expected] : allowed;
    if (!value || !targets.includes(value.content_schema_id)) fail("CONTENT_REF_SCHEMA_INVALID", path);
  }
  expected = rule["x-pullwise-availability-content-schema-id"];
  allowed = rule["x-pullwise-availability-content-schema-ids"];
  if (expected !== undefined || allowed !== undefined) {
    const targets = expected !== undefined ? [expected] : allowed;
    if (value?.availability === "available" &&
        (!value.ref || !targets.includes(value.ref.content_schema_id))) {
      fail("CONTENT_REF_SCHEMA_INVALID", path + ".ref");
    }
  }
}

export function verifyContentRefSet(refs) {
  if (!Array.isArray(refs)) fail("CONTRACT_TYPE_INVALID");
  const validated = refs.map((item) => validateDocument("content-ref/v1", item));
  const identities = new Map();
  const fields = ["content_schema_id", "sha256", "size_bytes", "media_type", "encoding"];
  for (const item of validated) {
    const identity = JSON.stringify(fields.map((field) => item[field]));
    const previous = identities.get(item.artifact_id);
    if (previous !== undefined && previous !== identity) fail("CONTENT_REF_CONFLICT", "$.artifact_id");
    identities.set(item.artifact_id, identity);
  }
  return validated;
}

function sha256Sync(bytes) {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const initial = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bitLength = bytes.length * 8;
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000));
  view.setUint32(paddedLength - 4, bitLength >>> 0);
  const hash = initial.slice();
  const words = new Uint32Array(64);
  const rotate = (value, shift) => (value >>> shift) | (value << (32 - shift));
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) words[index] = view.getUint32(offset + index * 4);
    for (let index = 16; index < 64; index += 1) {
      const left = words[index - 15];
      const right = words[index - 2];
      const sigma0 = rotate(left, 7) ^ rotate(left, 18) ^ (left >>> 3);
      const sigma1 = rotate(right, 17) ^ rotate(right, 19) ^ (right >>> 10);
      words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
      const choice = (e & f) ^ (~e & g);
      const first = (h + sum1 + choice + constants[index] + words[index]) >>> 0;
      const sum0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const second = (sum0 + majority) >>> 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + first) >>> 0, c, b, a, (first + second) >>> 0];
    }
    [a, b, c, d, e, f, g, h].forEach((value, index) => { hash[index] = (hash[index] + value) >>> 0; });
  }
  return hash.map((value) => value.toString(16).padStart(8, "0")).join("");
}

function verifyEmbeddedDigestSync(schemaId, value) {
  const spec = schema(schemaId)["x-pullwise-digest"];
  const unsigned = Object.fromEntries(Object.entries(value).filter(([key]) => key !== spec.field));
  const domain = encoder.encode(spec.domain);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input.set(document, domain.length + 1);
  if (sha256Sync(input) !== value[spec.field]) {
    fail("CONTRACT_DIGEST_MISMATCH", "$." + spec.field);
  }
}

function ruleServerAuthorityEnvelope(value) {
  const grant = value.grant;
  verifyEmbeddedDigestSync("agent-worker-grant/v1", grant);
  const grantBoundFields = [
    "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
    "task_version", "deletion_version", "owner_epoch", "native_epoch",
    "transport_epoch", "absolute_deadline_at", "terminalization_reserve_ms",
  ];
  if (grantBoundFields.some((key) => value[key] !== grant[key])) {
    throw new ContractValidationError(
      "AUTHORITY_INPUT_UNTRUSTED", "AUTHORITY_GRANT_BINDING_MISMATCH", "$",
    );
  }
}

function ruleTransportAbandonmentRecord(value) {
  if (value.abandoned_task_version !== value.previous_task_version + 1) {
    throw new ContractValidationError(
      "AUTHORITY_INPUT_UNTRUSTED", "AUTHORITY_SUCCESSOR_VERSION_INVALID",
      "$.abandoned_task_version",
    );
  }
}

function decodeBase64Canonical(value) {
  try {
    const binary = globalThis.atob(value);
    if (globalThis.btoa(binary) !== value) fail("SOURCE_CONTENT_BASE64_NONCANONICAL", "$.data_base64");
    return Uint8Array.from(binary, (item) => item.charCodeAt(0));
  } catch (error) {
    if (error instanceof ContractValidationError) throw error;
    fail("SOURCE_CONTENT_BASE64_INVALID", "$.data_base64");
  }
}

function validateSemantics(schemaId, value) {
  if (validateDeclaredDocumentRules(schemaId, value)) return;
  if (schemaId === "source-content/v1") {
    const raw = decodeBase64Canonical(value.data_base64);
    if (raw.length !== value.size_bytes) fail("SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes");
    if (sha256Sync(raw) !== value.byte_sha256) fail("SOURCE_CONTENT_SHA256_MISMATCH", "$.byte_sha256");
  } else if (schemaId === "elapsed-budget-ledger/v1") {
    if (value.consumed_ms + value.reserved_ms > value.elapsed_limit_ms) {
      throw new ContractValidationError("BUDGET_EXHAUSTED", "BUDGET_ELAPSED_LIMIT_EXCEEDED", "$");
    }
    if (value.calls_consumed + value.calls_reserved > value.tool_call_limit) {
      throw new ContractValidationError("BUDGET_EXHAUSTED", "BUDGET_CALL_LIMIT_EXCEEDED", "$");
    }
  } else if (schemaId === "elapsed-budget-settlement/v1") {
    if (value.consumed_calls + value.released_calls !== 1) fail("BUDGET_CALL_CONSERVATION_INVALID");
  } else if (schemaId === "agent-claim-abandon-response/v1") {
    const grant = value.grant;
    verifyEmbeddedDigestSync("agent-worker-grant/v1", grant);
    const exact = ["package", "task_id", "attempt_id", "session_id", "owner_id",
      "grant_id", "lease_id", "deletion_version", "owner_epoch", "native_epoch", "transport_epoch"];
    if (exact.some((key) => JSON.stringify(value[key]) !== JSON.stringify(grant[key]))) {
      fail("AUTHORITY_FENCE_MISMATCH");
    }
    if (value.previous_task_version !== grant.task_version) fail("AUTHORITY_PREVIOUS_VERSION_MISMATCH");
    if (value.task_version !== value.previous_task_version + 1) fail("AUTHORITY_SUCCESSOR_VERSION_INVALID");
  } else if (schemaId === "artifact-content-registry/v1") {
    const expected = [
      {artifact_kind: "change_set", content_schema_id: "change-set/v1", media_type: "application/json", encoding: "utf-8"},
      {artifact_kind: "change_set_patch", content_schema_id: "change-set-patch/v1", media_type: "application/json", encoding: "utf-8"},
      {artifact_kind: "r0_read_result", content_schema_id: "r0-read-result/v1", media_type: "application/json", encoding: "utf-8"},
      {artifact_kind: "source_content", content_schema_id: "source-content/v1", media_type: "application/json", encoding: "utf-8"},
      {artifact_kind: "task_report", content_schema_id: "task-report/v1", media_type: "application/json", encoding: "utf-8"},
    ];
    if (JSON.stringify(value.entries) !== JSON.stringify(expected)) fail("ARTIFACT_CONTENT_REGISTRY_INVALID");
  } else if (schemaId === "artifact-content-ref/v1") {
    const registry = fixture("publication_golden_artifact_registry").document;
    verifyEmbeddedDigestSync("artifact-content-registry/v1", registry);
    const entry = registry.entries.find((item) => item.artifact_kind === value.artifact_kind);
    const ref = value.ref;
    if (!entry || ["content_schema_id", "media_type", "encoding"].some(
      (key) => ref[key] !== entry[key],
    )) fail("ARTIFACT_CONTENT_TUPLE_INVALID");
  } else if (schemaId === "budget-summary/v1") {
    if (value.consumed_ms > value.elapsed_limit_ms) {
      throw new ContractValidationError("BUDGET_EXHAUSTED", "BUDGET_SUMMARY_ELAPSED_INVALID", "$");
    }
    if (value.calls_consumed > value.tool_call_limit) {
      throw new ContractValidationError("BUDGET_EXHAUSTED", "BUDGET_SUMMARY_CALLS_INVALID", "$");
    }
  } else if (schemaId === "task-report/v1") {
    const sectionIds = value.sections.map((item) => item.section_id);
    if (JSON.stringify(sectionIds) !== JSON.stringify([...sectionIds].sort())) fail("TASK_REPORT_SECTION_ORDER_INVALID");
    for (const field of ["title", "summary"]) {
      const limit = field === "title" ? 512 : 4096;
      if (encoder.encode(value[field]).length > limit) fail("TASK_REPORT_UTF8_LIMIT_INVALID", "$." + field);
    }
    value.sections.forEach((section, index) => {
      if (encoder.encode(section.title).length > 512 || encoder.encode(section.body).length > 65536) {
        fail("TASK_REPORT_UTF8_LIMIT_INVALID", "$.sections[" + index + "]");
      }
      verifyContentRefSet(section.evidence_refs);
    });
  } else if (schemaId === "waiver-event/v1") {
    if (value.issued_at >= value.expires_at) {
      throw new ContractValidationError("WAIVER_INVALID", "WAIVER_TIME_RANGE_INVALID", "$");
    }
  }
}

export function verifyWaiverAuthorization(waiver, effectivePolicy, now) {
  return verifyWaiverEventAuthority(waiver, effectivePolicy, now);
}

export async function verifyBudgetTransition(previousLedger, reservation, reservedLedger, settlement, resultingLedger) {
  const before = await verifyDocumentDigest("elapsed-budget-ledger/v1", previousLedger);
  const held = await verifyDocumentDigest("elapsed-budget-reservation/v1", reservation);
  const reserved = await verifyDocumentDigest("elapsed-budget-ledger/v1", reservedLedger);
  const settled = await verifyDocumentDigest("elapsed-budget-settlement/v1", settlement);
  const after = await verifyDocumentDigest("elapsed-budget-ledger/v1", resultingLedger);
  if (held.task_id !== before.task_id) fail("BUDGET_TASK_MISMATCH");
  const previous = [
    ["previous_consumed_ms", "consumed_ms"], ["previous_reserved_ms", "reserved_ms"],
    ["previous_calls_consumed", "calls_consumed"], ["previous_calls_reserved", "calls_reserved"],
  ];
  if (previous.some(([left, right]) => held[left] !== before[right])) fail("BUDGET_PREVIOUS_STATE_MISMATCH");
  if (before.consumed_ms + before.reserved_ms + held.reserved_ms > before.elapsed_limit_ms) {
    budgetError("BUDGET_ELAPSED_LIMIT_EXCEEDED", "$", "BUDGET_EXHAUSTED");
  }
  if (before.calls_consumed + before.calls_reserved + held.reserved_calls > before.tool_call_limit) {
    budgetError("BUDGET_CALL_LIMIT_EXCEEDED", "$", "BUDGET_EXHAUSTED");
  }
  const reservedExpected = {
    task_id: before.task_id, grant_digest: before.grant_digest,
    elapsed_limit_ms: before.elapsed_limit_ms, tool_call_limit: before.tool_call_limit,
    consumed_ms: before.consumed_ms, reserved_ms: before.reserved_ms + held.reserved_ms,
    calls_consumed: before.calls_consumed, calls_reserved: before.calls_reserved + held.reserved_calls,
  };
  if (Object.entries(reservedExpected).some(([key, value]) => reserved[key] !== value)) fail("BUDGET_RESERVED_LEDGER_MISMATCH");
  if (settled.reservation_id !== held.reservation_id || settled.invocation_digest !== held.invocation_digest) fail("BUDGET_SETTLEMENT_IDENTITY_MISMATCH");
  if (settled.consumed_ms + settled.released_ms !== held.reserved_ms) fail("BUDGET_ELAPSED_CONSERVATION_INVALID");
  if (settled.consumed_calls + settled.released_calls !== held.reserved_calls) fail("BUDGET_CALL_CONSERVATION_INVALID");
  if (settled.outcome === "settled") {
    if (settled.consumed_calls !== 1 || settled.released_calls !== 0) fail("BUDGET_SETTLED_CALL_INVALID");
    if (settled.elapsed_ms > held.reserved_ms || settled.consumed_ms !== settled.elapsed_ms) fail("BUDGET_SETTLED_ELAPSED_INVALID");
    if (settled.released_ms !== held.reserved_ms - settled.elapsed_ms) fail("BUDGET_SETTLED_RELEASE_INVALID");
  } else if (settled.consumed_calls !== 0 || settled.released_calls !== 1 ||
             settled.consumed_ms !== 0 || settled.released_ms !== held.reserved_ms) {
    fail("BUDGET_ABANDONMENT_RELEASE_INVALID");
  }
  const expected = {
    resulting_consumed_ms: before.consumed_ms + settled.consumed_ms,
    resulting_reserved_ms: before.reserved_ms,
    resulting_calls_consumed: before.calls_consumed + settled.consumed_calls,
    resulting_calls_reserved: before.calls_reserved,
  };
  if (Object.entries(expected).some(([key, value]) => settled[key] !== value)) fail("BUDGET_RESULTING_STATE_MISMATCH");
  const afterExpected = {
    task_id: before.task_id, grant_digest: before.grant_digest,
    elapsed_limit_ms: before.elapsed_limit_ms, tool_call_limit: before.tool_call_limit,
    consumed_ms: expected.resulting_consumed_ms, reserved_ms: expected.resulting_reserved_ms,
    calls_consumed: expected.resulting_calls_consumed, calls_reserved: expected.resulting_calls_reserved,
  };
  if (Object.entries(afterExpected).some(([key, value]) => after[key] !== value)) fail("BUDGET_RESULTING_LEDGER_MISMATCH");
  return true;
}
'''


NPM_DECLARED_DISPATCH = r'''
const DOCUMENT_RULE_HANDLERS = Object.freeze({
  acceptance_source_ids_unique: taskControlRuleRequestAcceptanceSources,
  actor: ruleObservationActor,
  agent_tool_request: ruleAgentToolRequest,
  artifact_content_ref: ruleArtifactContentRef,
  artifact_content_registry: ruleArtifactContentRegistry,
  attempt_state_nullability: taskControlRuleAttemptNullability,
  attempt_transport_binding_all_or_none: taskControlRuleAttemptTransport,
  availability_reason_registry: ruleAvailabilityReasonRegistry,
  availability_ref: ruleAvailabilityRef,
  budget_ceiling_consistency: taskControlRulePolicyBudgets,
  budget_summary: ruleBudgetSummary,
  capability_and_delivery_sets_sorted_unique: taskControlRuleRequestSets,
  capability_sets_disjoint_sorted_unique: taskControlRulePolicyCapabilities,
  change_set: ruleChangeSetComplete,
  change_set_patch: ruleChangeSetPatch,
  completion_proposal: ruleCompletionProposal,
  charter_digest_exact: taskControlRuleCharterDigest,
  debug_redaction_plan: ruleDebugRedactionPlan,
  derived_requirement_shape: taskControlRuleRequirementShape,
  effect_ledger_snapshot: ruleEffectLedgerSnapshot,
  elapsed_budget_ledger: ruleElapsedBudgetLedger,
  elapsed_budget_reservation: ruleElapsedBudgetReservation,
  elapsed_budget_settlement: ruleElapsedBudgetSettlement,
  entries_normative_ingest_then_append_order: taskControlRuleLedgerEntries,
  evidence_closure_manifest: ruleEvidenceClosureManifest,
  execution_profile: ruleExecutionProfile,
  execution_state_manifest: ruleExecutionStateManifest,
  fenced_reason_ownership_loss: taskControlRuleFencedReason,
  gate_decision: ruleGateDecision,
  gate_input_snapshot: ruleGateInputSnapshot,
  gate_predicate_registry: ruleGatePredicateRegistry,
  head_version_ref_pairs: taskControlRuleRecordHeads,
  ledger_digest_exact: taskControlRuleLedgerDigest,
  local_tool_receipt: ruleLocalToolReceipt,
  observation: ruleObservation,
  observation_manifest: ruleObservationManifestComplete,
  owner_state_nullability: taskControlRuleOwnerNullability,
  policy_digest_exact: taskControlRulePolicyDigest,
  pre_gate_evidence_closure_manifest: rulePreGateEvidenceClosureManifest,
  pre_gate_root_set: rulePreGateRootSet,
  pre_verifier_observation_manifest: rulePreVerifierObservationManifest,
  publication_content_manifest: rulePublicationContentManifest,
  quality_policy_plan: ruleQualityPolicyPlan,
  benchmark_bundle: ruleBenchmarkBundle,
  release_gate_attestation: ruleReleaseGateAttestation,
  release_gate_policy: ruleReleaseGatePolicy,
  release_gate_report: ruleReleaseGateReport,
  release_key_revocation: ruleReleaseKeyRevocation,
  release_principal: ruleReleasePrincipal,
  release_signing_key: ruleReleaseSigningKey,
  r0_read_payload: ruleR0ReadPayload,
  r0_read_result: ruleR0ReadResult,
  requirement_id_source_kind_match: taskControlRuleRequirementId,
  risk_ceiling_current_mvp: taskControlRulePolicyMvp,
  root_and_origin_sets_sorted_unique: taskControlRulePolicyRoots,
  server_authority_envelope: ruleServerAuthorityEnvelope,
  sorted_unique_active_requirement_ids: taskControlRuleLedgerActive,
  sorted_unique_charter_sets: taskControlRuleCharterSets,
  sorted_unique_requirement_links: taskControlRuleRequirementLinks,
  source_content: ruleSourceContent,
  source_selection_policy: ruleSourceSelectionPolicyComplete,
  source_tree_manifest: ruleSourceTreeManifest,
  source_state: ruleSourceState,
  task_record_transport_binding_all_or_none: taskControlRuleRecordTransport,
  task_report: ruleTaskReport,
  task_result: ruleTaskResult,
  task_result_core: ruleTaskResultCore,
  task_result_outcome_reason_registry: ruleTaskResultOutcomeReasonRegistry,
  task_result_transport_ack: ruleTaskResultTransportAck,
  task_result_transport_envelope: ruleTaskResultTransportEnvelope,
  terminal_result_shape: taskControlRuleRecordTerminal,
  terminalization_fact: ruleTerminalizationFact,
  terminalization_input_snapshot: ruleTerminalizationInputSnapshot,
  tool_catalog: ruleToolCatalog,
  tool_dispatch_capability: ruleToolDispatchCapability,
  tool_dispatch_intent: ruleToolDispatchIntent,
  tool_invocation: ruleToolInvocation,
  transport_abandonment_record: ruleTransportAbandonmentRecord,
  utf8_nfc_byte_limits: taskControlRuleUtf8,
  verification_attestation: ruleAttestation,
  verification_attestation_manifest: ruleAttestationManifest,
  verifier_input_manifest: ruleVerifierInput,
  verifier_work_report: ruleVerifierWork,
  waiver_time_order: taskControlRuleWaiverTime,
  worker_debug_descriptor: ruleWorkerDebugDescriptor,
  worker_debug_file_manifest: ruleWorkerDebugFileManifest,
  worker_debug_fragment: ruleWorkerDebugFragment,
  worker_debug_redaction_report: ruleWorkerDebugRedactionReport,
});

function ruleUtf8Fields(value) {
  const limits = {
    objective: 16384,
    statement: 16384,
    rationale: 16384,
    objective_restated: 16384,
    reason: 16384,
  };
  for (const [field, limit] of Object.entries(limits)) {
    if (!(field in value)) continue;
    if (value[field].normalize("NFC") !== value[field]) {
      fail("UTF8_NFC_INVALID", "$." + field);
    }
    if (encoder.encode(value[field]).length > limit) {
      fail("UTF8_BYTE_LIMIT_INVALID", "$." + field);
    }
  }
}

function sortedUniqueStrings(values, allowEmpty) {
  return Array.isArray(values) && (allowEmpty || values.length > 0) &&
    values.every((item) => typeof item === "string") &&
    new Set(values).size === values.length &&
    JSON.stringify(values) === JSON.stringify([...values].sort());
}

function validateDeclaredDocumentRules(schemaId, value) {
  const semantics = schema(schemaId)["x-pullwise-semantics"];
  const rules = semantics?.document_rules;
  if (semantics === undefined) return false;
  const signatureContracts = {
    "benchmark-bundle/v1": {algorithm: "Ed25519",
      domain: "pullwise-benchmark-bundle/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
    "release-gate-attestation/v1": {algorithm: "Ed25519",
      domain: "pullwise-release-gate-attestation/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
    "release-gate-policy/v1": {algorithm: "Ed25519",
      domain: "pullwise-release-gate-policy/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
    "waiver-event/v1": {algorithm: "Ed25519", domain: "pullwise-waiver-event/v1",
      domain_separator: "NUL", encoding: "base64url_no_padding",
      signed_projection: "event_without_signature"},
    "release-key-revocation/v1": {algorithm: "Ed25519",
      domain: "pullwise-release-key-revocation/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
    "release-principal/v1": {algorithm: "Ed25519",
      domain: "pullwise-release-principal/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
    "release-signing-key/v1": {algorithm: "Ed25519",
      domain: "pullwise-release-signing-key/v1", domain_separator: "NUL",
      encoding: "base64url_no_padding",
      signed_projection: "document_without_signature_and_digest"},
  };
  const expectedKeys = Object.hasOwn(signatureContracts, schemaId)
    ? ["contextual_helpers", "document_rules", "signature_contract"]
    : ["contextual_helpers", "document_rules"];
  if (!semantics || typeof semantics !== "object" || Array.isArray(semantics) ||
      JSON.stringify(Object.keys(semantics).sort()) !== JSON.stringify(expectedKeys) ||
      !sortedUniqueStrings(rules, false) ||
      !sortedUniqueStrings(semantics.contextual_helpers, true)) {
    fail("CONTRACT_SEMANTICS_INVALID", schemaId);
  }
  if (Object.hasOwn(signatureContracts, schemaId) &&
      canonicalString(semantics.signature_contract) !==
      canonicalString(signatureContracts[schemaId])) {
    fail("CONTRACT_SEMANTICS_INVALID", schemaId);
  }
  for (const ruleId of rules) {
    const handler = DOCUMENT_RULE_HANDLERS[ruleId];
    if (!handler) fail("CONTRACT_SEMANTIC_RULE_UNIMPLEMENTED", ruleId);
    const probe = globalThis.__PULLWISE_DOCUMENT_RULE_PROBE__;
    if (typeof probe === "function") probe({schemaId, ruleId});
    try {
      handler(value);
    } catch (error) {
      if (typeof probe === "function") probe({schemaId, ruleId, rejected: true});
      throw error;
    }
  }
  return true;
}
'''


NPM_SEMANTICS = "\n".join(
    (
        NPM_SEMANTICS_BASE,
        NPM_SOURCE_EXECUTION_OBSERVATION,
        NPM_CHANGE_SET_PATCH_RULE,
        NPM_EFFECTIVE_POLICY_RULES,
        NPM_EXECUTION_PROFILE_RULE,
        NPM_OBSERVATION_RULE,
        NPM_BUDGET,
        NPM_TASK_CONTROL_RULES,
        NPM_TASK_CONTROL_HELPERS,
        NPM_TOOL_EVIDENCE,
        NPM_PUBLICATION,
        NPM_QUALITY_POLICY,
        NPM_RELEASE_GATE,
        NPM_RELEASE_TRUST,
        NPM_RELEASE_GATE_EVALUATOR,
        NPM_PRE_GATE,
        NPM_TASK_EVIDENCE,
        NPM_GATE_INPUT,
        NPM_GATE,
        NPM_GATE_PREPARATION,
        NPM_RESULT,
        NPM_VERIFICATION,
        NPM_DECLARED_DISPATCH,
    )
)


__all__ = ["NPM_SEMANTICS"]
