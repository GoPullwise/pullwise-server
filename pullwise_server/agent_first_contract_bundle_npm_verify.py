"""Generated npm facade inventory and bundle verification."""

from __future__ import annotations


NPM_VERIFY = r'''
const INTERNAL_CONSTRAINT_SCHEMA_IDS = new Set([
  "task-result-blocked-variant/v1",
  "task-result-cancelled-variant/v1",
  "task-result-cancelled-with-effects-variant/v1",
  "task-result-completed-variant/v1",
  "task-result-completed-with-waivers-variant/v1",
  "task-result-failed-variant/v1",
  "task-result-no-change-needed-variant/v1",
  "task-result-partial-variant/v1",
  "task-result-terminated-with-unknown-effects-variant/v1",
]);

function schemaRole(schemaId) {
  return INTERNAL_CONSTRAINT_SCHEMA_IDS.has(schemaId)
    ? "internal_constraint" : "public_document";
}

export function schemaIds() {
  return rootManifest().schema_registry
    .filter((item) => item.role === "public_document")
    .map((item) => item.schema_id);
}

export function allSchemaIds() {
  return rootManifest().schema_registry.map((item) => item.schema_id);
}

export async function toolCatalog() {
  return verifyDocumentDigest("tool-catalog/v1", fixture("tool_golden_current_catalog").document);
}

export async function gatePredicateRegistry() {
  return verifyDocumentDigest(
    "gate-predicate-registry/v1",
    fixture("gate_golden_independent_registry").document,
  );
}

export async function stableErrorRegistry() {
  return verifyDocumentDigest(
    "stable-error-registry/v1",
    fixture("error_golden_current_registry").document,
  );
}

const SEMANTIC_CYCLE_EXCEPTIONS = [{
  schema_id: "task-charter/v1",
  kind: "content_ref_target",
  path: "$.properties.previous_charter_ref.oneOf[0]",
  target_schema_id: "task-charter/v1",
}, {
  schema_id: "task-record/v1",
  kind: "content_ref_target",
  path: "$.properties.result_ref.oneOf[0]",
  target_schema_id: "task-result/v1",
}];

function compareUnicodeCodePointStrings(left, right) {
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

function compareUnicodeCodePointTuples(left, right) {
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = compareUnicodeCodePointStrings(left[index], right[index]);
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

function schemaEdges(value) {
  const found = [];
  function visit(item, path) {
    if (Array.isArray(item)) {
      item.forEach((child, index) => visit(child, path + "[" + index + "]"));
      return;
    }
    if (!item || typeof item !== "object") return;
    if (typeof item.$ref === "string") {
      found.push({kind: "schema_ref", path, target_schema_id: item.$ref});
    }
    const annotations = [
      ["x-pullwise-content-schema-id", "x-pullwise-content-schema-ids", "content_ref_target"],
      ["x-pullwise-availability-content-schema-id", "x-pullwise-availability-content-schema-ids", "availability_ref_target"],
    ];
    for (const [singular, plural, kind] of annotations) {
      const targets = [];
      if (singular in item) targets.push(item[singular]);
      if (Array.isArray(item[plural])) targets.push(...item[plural]);
      for (const target of targets) {
        if (typeof target === "string") {
          found.push({kind, path, target_schema_id: target});
        }
      }
    }
    for (const [key, child] of Object.entries(item)) visit(child, path + "." + key);
  }
  visit(value, "$");
  const unique = new Map(
    found.map((item) => [canonicalString(canonicalValue(item)), item]),
  );
  return [...unique.values()].sort((left, right) => {
    const a = [left.path, left.kind, left.target_schema_id];
    const b = [right.path, right.kind, right.target_schema_id];
    return compareUnicodeCodePointTuples(a, b);
  });
}

function verifySchemaEdgeDag(edgesBySchema) {
  const visiting = new Set();
  const visited = new Set();
  function visit(schemaId) {
    if (visiting.has(schemaId)) fail("CONTRACT_REFERENCE_CYCLE", schemaId);
    if (visited.has(schemaId)) return;
    visiting.add(schemaId);
    for (const edge of edgesBySchema.get(schemaId)) {
      const exception = {schema_id: schemaId, ...edge};
      if (!SEMANTIC_CYCLE_EXCEPTIONS.some(
        (item) => canonicalString(item) === canonicalString(exception),
      )) visit(edge.target_schema_id);
    }
    visiting.delete(schemaId);
    visited.add(schemaId);
  }
  [...edgesBySchema.keys()].sort(compareUnicodeCodePointStrings).forEach(visit);
}

export async function verifyBundle() {
  const raw = bundleBytes();
  if (await sha256(raw) !== CONTENT_SHA256) fail("CONTRACT_BUNDLE_DIGEST_MISMATCH");
  const document = bundle();
  if (
    document.package_identity !== PACKAGE_IDENTITY ||
    document.package_version !== PACKAGE_VERSION
  ) fail("CURRENT_PACKAGE_PIN_MISMATCH");
  const root = document.root_manifest;
  const {root_sha256: presentedRoot, ...rootBody} = root;
  if (presentedRoot !== ROOT_SHA256 || await sha256(canonicalDocumentBytes(rootBody)) !== ROOT_SHA256) {
    fail("CONTRACT_ROOT_DIGEST_MISMATCH");
  }
  if (
    canonicalString(root.semantic_cycle_exceptions) !==
      canonicalString(SEMANTIC_CYCLE_EXCEPTIONS) ||
    root.semantic_cycle_exceptions_sha256 !==
      await sha256(canonicalDocumentBytes(SEMANTIC_CYCLE_EXCEPTIONS))
  ) fail("CONTRACT_SEMANTIC_CYCLE_EXCEPTION_INVALID");
  if (canonicalString(document.families.map((item) => item.family_id)) !==
      canonicalString(root.required_families)) fail("CONTRACT_FAMILY_CLOSURE_INVALID");

  const schemas = [];
  const fixtures = [];
  const familyEntries = [];
  const known = new Set();
  const edgesBySchema = new Map();
  for (const family of document.families) {
    const localSchemas = [];
    const localFixtures = [];
    for (const item of family.schemas) {
      const edges = schemaEdges(item);
      const entry = {
        schema_id: item.$id,
        family_id: family.family_id,
        role: schemaRole(item.$id),
        references: [...new Set(edges.map((edge) => edge.target_schema_id))]
          .sort(compareUnicodeCodePointStrings),
        edges,
        sha256: await sha256(canonicalDocumentBytes(item)),
      };
      localSchemas.push(entry);
      known.add(item.$id);
      edgesBySchema.set(item.$id, edges);
    }
    for (const item of family.fixtures) {
      localFixtures.push({
        fixture_id: item.fixture_id,
        family_id: family.family_id,
        schema_id: item.schema_id,
        fixture_class: item.fixture_class,
        expected_code: item.expected_code,
        sha256: await sha256(canonicalDocumentBytes(item)),
      });
    }
    if (canonicalString(localSchemas) !== canonicalString(family.schema_registry)) {
      fail("CONTRACT_SCHEMA_REGISTRY_INVALID");
    }
    if (canonicalString(localFixtures) !== canonicalString(family.fixture_registry)) {
      fail("CONTRACT_FIXTURE_REGISTRY_INVALID");
    }
    schemas.push(...localSchemas);
    fixtures.push(...localFixtures);
    familyEntries.push({
      family_id: family.family_id,
      schema_ids: family.schemas.map((item) => item.$id),
      fixture_ids: family.fixtures.map((item) => item.fixture_id),
      sha256: await sha256(canonicalDocumentBytes(family)),
    });
  }
  if (canonicalString(schemas) !== canonicalString(root.schema_registry) ||
      canonicalString(fixtures) !== canonicalString(root.fixture_registry)) {
    fail("CONTRACT_ROOT_REGISTRY_INVALID");
  }
  if (canonicalString(familyEntries) !== canonicalString(root.families)) {
    fail("CONTRACT_FAMILY_DIGEST_INVALID");
  }
  for (const item of schemas) {
    for (const ref of item.references) if (!known.has(ref)) fail("CONTRACT_REFERENCE_UNKNOWN");
  }
  verifySchemaEdgeDag(edgesBySchema);
  const expectedDag = schemas.map((item) => ({
    schema_id: item.schema_id,
    family_id: item.family_id,
    references: item.references,
    edges: item.edges,
  })).sort((left, right) => compareUnicodeCodePointStrings(
    left.schema_id, right.schema_id,
  ));
  if (canonicalString(expectedDag) !== canonicalString(root.reference_dag)) {
    fail("CONTRACT_REFERENCE_DAG_INVALID");
  }
  const classes = new Set(fixtures.map((item) => item.fixture_class));
  if (classes.size !== root.fixture_classes.length ||
      root.fixture_classes.some((item) => !classes.has(item))) {
    fail("CONTRACT_FIXTURE_CLASS_INVALID");
  }
  validateDocument("canonical-json-profile/v1", fixture("core_golden_canonical_profile").document);
  await toolCatalog();
  await gatePredicateRegistry();
  await stableErrorRegistry();
  return true;
}

export function assertPin(identity, version, contentSha256, rootSha256 = null) {
  if (
    identity !== PACKAGE_IDENTITY ||
    version !== PACKAGE_VERSION ||
    contentSha256 !== CONTENT_SHA256 ||
    (rootSha256 !== null && rootSha256 !== ROOT_SHA256)
  ) throw new Error("CURRENT_PACKAGE_PIN_MISMATCH");
}

export const bundle_bytes = bundleBytes;
export const all_schema_ids = allSchemaIds;
export const canonical_document_bytes = canonicalDocumentBytes;
export const canonical_validated_bytes = canonicalValidatedBytes;
export const document_digest = documentDigest;
export const signature_message = signatureMessage;
export const evaluate_success_gate = evaluateSuccessGate;
export const evaluate_terminalization_gate = evaluateTerminalizationGate;
export const fixture_bytes = fixtureBytes;
export const gate_predicate_registry = gatePredicateRegistry;
export const package_tuple = packageTuple;
export const root_manifest = rootManifest;
export const root_manifest_bytes = rootManifestBytes;
export const schema_bytes = schemaBytes;
export const schema_ids = schemaIds;
export const seal_document = sealDocument;
export const stable_error_registry = stableErrorRegistry;
export const tool_catalog = toolCatalog;
export const validate_document = validateDocument;
export const validate_attempt_transition = validateAttemptTransition;
export const validate_claim_write_set = validateClaimWriteSet;
export const validate_effective_policy_derivation = validateEffectivePolicyDerivation;
export const validate_requirement_entry_ingest = validateRequirementEntryIngest;
export const validate_requirement_ledger_transition = validateRequirementLedgerTransition;
export const validate_task_charter_transition = validateTaskCharterTransition;
export const validate_task_owner_transition = validateTaskOwnerTransition;
export const validate_task_record_transition = validateTaskRecordTransition;
export const validate_task_request_acceptance = validateTaskRequestAcceptance;
export const validate_task_result_publication = validateTaskResultPublication;
export const validate_tool_capability_consumption = validateToolCapabilityConsumption;
export const validate_tool_invocation_binding = validateToolInvocationBinding;
export const validate_tool_journal_begin = validateToolJournalBegin;
export const validate_tool_journal_settlement = validateToolJournalSettlement;
export const verify_bundle = verifyBundle;
export const verify_budget_transition = verifyBudgetTransition;
export const verify_content_ref_set = verifyContentRefSet;
export const verify_document_digest = verifyDocumentDigest;
export const verify_change_set_context = verifyChangeSetContext;
export const verify_completion_proposal_context = verifyCompletionProposalContext;
export const verify_execution_state_context = verifyExecutionStateContext;
export const verify_observation_manifest_extension = verifyObservationManifestExtension;
export const verify_source_tree_context = verifySourceTreeContext;
export const verify_evidence_closure_context = verifyEvidenceClosureContext;
export const verify_gate_input_snapshot_context = verifyGateInputSnapshotContext;
export const verify_pre_gate_evidence_closure_context = verifyPreGateEvidenceClosureContext;
export const verify_pre_gate_root_set_context = verifyPreGateRootSetContext;
export const verify_quality_policy_plan_context = verifyQualityPolicyPlanContext;
export const verify_release_gate_policy_context = verifyReleaseGatePolicyContext;
export const verify_release_gate_report_context = verifyReleaseGateReportContext;
export const verify_release_gate_attestation_context = verifyReleaseGateAttestationContext;
export const verify_terminalization_fact_context = verifyTerminalizationFactContext;
export const verify_terminalization_input_snapshot_context = verifyTerminalizationInputSnapshotContext;
export const verify_verifier_input_context = verifyVerifierInputContext;
export const verify_verifier_work_context = verifyVerifierWorkContext;
export const verify_attestation_context = verifyAttestationContext;
export const verify_attestation_manifest_context = verifyAttestationManifestContext;
export const verify_waiver_authorization = verifyWaiverAuthorization;
export const verify_waiver_event_authority = verifyWaiverEventAuthority;
'''


__all__ = ["NPM_VERIFY"]
