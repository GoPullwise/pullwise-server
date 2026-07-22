"""Generated npm facade inventory and bundle verification."""

from __future__ import annotations


NPM_VERIFY = r'''
export function schemaIds() {
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

function references(value) {
  const found = new Set();
  if (Array.isArray(value)) {
    for (const item of value) for (const ref of references(item)) found.add(ref);
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (key === "$ref" && typeof item === "string") found.add(item);
      for (const ref of references(item)) found.add(ref);
    }
  }
  return found;
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
  if (JSON.stringify(document.families.map((item) => item.family_id)) !==
      JSON.stringify(root.required_families)) fail("CONTRACT_FAMILY_CLOSURE_INVALID");

  const schemas = [];
  const fixtures = [];
  const familyEntries = [];
  const known = new Set();
  for (const family of document.families) {
    const localSchemas = [];
    const localFixtures = [];
    for (const item of family.schemas) {
      const entry = {
        schema_id: item.$id,
        family_id: family.family_id,
        references: [...references(item)].sort(),
        sha256: await sha256(canonicalDocumentBytes(item)),
      };
      localSchemas.push(entry);
      known.add(item.$id);
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
    if (JSON.stringify(localSchemas) !== JSON.stringify(family.schema_registry)) {
      fail("CONTRACT_SCHEMA_REGISTRY_INVALID");
    }
    if (JSON.stringify(localFixtures) !== JSON.stringify(family.fixture_registry)) {
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
  if (JSON.stringify(schemas) !== JSON.stringify(root.schema_registry) ||
      JSON.stringify(fixtures) !== JSON.stringify(root.fixture_registry)) {
    fail("CONTRACT_ROOT_REGISTRY_INVALID");
  }
  if (JSON.stringify(familyEntries) !== JSON.stringify(root.families)) {
    fail("CONTRACT_FAMILY_DIGEST_INVALID");
  }
  for (const item of schemas) {
    for (const ref of item.references) if (!known.has(ref)) fail("CONTRACT_REFERENCE_UNKNOWN");
  }
  const expectedDag = schemas.map((item) => ({
    schema_id: item.schema_id,
    family_id: item.family_id,
    references: item.references,
  })).sort((left, right) => left.schema_id.localeCompare(right.schema_id));
  if (JSON.stringify(expectedDag) !== JSON.stringify(root.reference_dag)) {
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
export const canonical_document_bytes = canonicalDocumentBytes;
export const canonical_validated_bytes = canonicalValidatedBytes;
export const document_digest = documentDigest;
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
export const verify_bundle = verifyBundle;
export const verify_budget_transition = verifyBudgetTransition;
export const verify_content_ref_set = verifyContentRefSet;
export const verify_document_digest = verifyDocumentDigest;
'''


__all__ = ["NPM_VERIFY"]
