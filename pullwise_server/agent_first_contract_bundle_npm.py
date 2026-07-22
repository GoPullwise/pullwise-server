"""Render the dependency-free npm current-contract facade."""

from __future__ import annotations

import base64
import json


def render_npm_wrapper(
    identity: str,
    version: str,
    root_sha256: str,
    content_sha256: str,
    canonical: bytes,
) -> bytes:
    replacements = {
        "@@IDENTITY@@": json.dumps(identity),
        "@@VERSION@@": json.dumps(version),
        "@@ROOT@@": json.dumps(root_sha256),
        "@@CONTENT@@": json.dumps(content_sha256),
        "@@PAYLOAD@@": json.dumps(base64.b64encode(canonical).decode("ascii")),
    }
    rendered = _TEMPLATE
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered.encode("utf-8")


_TEMPLATE = r'''// Generated from the Server-owned Agent-First bundle; do not edit.
export const PACKAGE_IDENTITY = @@IDENTITY@@;
export const PACKAGE_VERSION = @@VERSION@@;
export const ROOT_SHA256 = @@ROOT@@;
export const CONTENT_SHA256 = @@CONTENT@@;
export const PACKAGE_TUPLE = Object.freeze([
  PACKAGE_IDENTITY, PACKAGE_VERSION, CONTENT_SHA256, ROOT_SHA256,
]);
export const BUNDLE_BASE64 = @@PAYLOAD@@;
const SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const encoder = new TextEncoder();
const decoder = new TextDecoder();

export class ContractValidationError extends Error {}

function fail(code, path = "$") {
  throw new ContractValidationError(`${code}: ${path}`);
}

function canonicalValue(value, path = "$") {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || Math.abs(value) > SAFE_INTEGER) {
      fail("CANONICAL_INTEGER_UNSAFE", path);
    }
    return value;
  }
  if (typeof value === "string") {
    if (value.normalize("NFC") !== value) fail("CANONICAL_STRING_NOT_NFC", path);
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => canonicalValue(item, `${path}[${index}]`));
  }
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      if (!/^[\x00-\x7f]*$/.test(key)) fail("CANONICAL_KEY_INVALID", path);
      result[key] = canonicalValue(value[key], `${path}.${key}`);
    }
    return result;
  }
  fail("CANONICAL_TYPE_INVALID", path);
}

export function canonicalDocumentBytes(value) {
  return encoder.encode(JSON.stringify(canonicalValue(value)));
}

export function bundleBytes() {
  const decoded = atob(BUNDLE_BASE64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

export function bundle() {
  return JSON.parse(decoder.decode(bundleBytes()));
}

export function rootManifest() {
  return bundle().root_manifest;
}

export function rootManifestBytes() {
  return canonicalDocumentBytes(rootManifest());
}

export function packageTuple() {
  return {
    schema_id: "current-package-ref/v1",
    package_identity: PACKAGE_IDENTITY,
    package_version: PACKAGE_VERSION,
    content_sha256: CONTENT_SHA256,
    root_sha256: ROOT_SHA256,
  };
}

function findDocument(collection, identityKey, identity) {
  for (const family of bundle().families) {
    const found = family[collection].find((item) => item[identityKey] === identity);
    if (found) return JSON.parse(decoder.decode(canonicalDocumentBytes(found)));
  }
  throw new Error(`UNKNOWN_CONTRACT_DOCUMENT: ${identity}`);
}

export function schema(schemaId) {
  return findDocument("schemas", "$id", schemaId);
}

export function schemaBytes(schemaId) {
  return canonicalDocumentBytes(schema(schemaId));
}

export function fixture(fixtureId) {
  return findDocument("fixtures", "fixture_id", fixtureId);
}

export function fixtureBytes(fixtureId) {
  return canonicalDocumentBytes(fixture(fixtureId));
}

function typeMatches(typeName, value) {
  if (typeName === "object") {
    return value !== null && !Array.isArray(value) &&
      typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype;
  }
  if (typeName === "array") return Array.isArray(value);
  if (typeName === "string") return typeof value === "string";
  if (typeName === "integer") return Number.isSafeInteger(value);
  if (typeName === "boolean") return typeof value === "boolean";
  if (typeName === "null") return value === null;
  return false;
}

function validateNode(rule, value, path) {
  if (rule.$ref) {
    validateNode(schema(rule.$ref), value, path);
    const expectedSchema = rule["x-pullwise-content-schema-id"];
    if (expectedSchema !== undefined &&
        (!typeMatches("object", value) || value.content_schema_id !== expectedSchema)) {
      fail("CONTENT_REF_SCHEMA_INVALID", path);
    }
    const allowedSchemas = rule["x-pullwise-content-schema-ids"];
    if (allowedSchemas !== undefined &&
        (!typeMatches("object", value) ||
         !allowedSchemas.includes(value.content_schema_id))) {
      fail("CONTENT_REF_SCHEMA_INVALID", path);
    }
    return;
  }
  if ("const" in rule && JSON.stringify(value) !== JSON.stringify(rule.const)) {
    fail("CONTRACT_CONST_INVALID", path);
  }
  if (rule.enum && !rule.enum.some((item) => JSON.stringify(item) === JSON.stringify(value))) {
    fail("CONTRACT_ENUM_INVALID", path);
  }
  if (rule.type !== undefined) {
    const choices = Array.isArray(rule.type) ? rule.type : [rule.type];
    if (!choices.some((choice) => typeMatches(choice, value))) {
      fail("CONTRACT_TYPE_INVALID", path);
    }
  }
  if (rule.type === "object" && typeMatches("object", value)) {
    for (const key of rule.required ?? []) {
      if (!(key in value)) fail("CONTRACT_REQUIRED_MISSING", `${path}.${key}`);
    }
    const properties = rule.properties ?? {};
    if (rule.additionalProperties === false) {
      const unknown = Object.keys(value).filter((key) => !(key in properties)).sort();
      if (unknown.length) fail("CONTRACT_FIELD_UNKNOWN", `${path}.${unknown[0]}`);
    }
    for (const [key, item] of Object.entries(value)) {
      if (key in properties) validateNode(properties[key], item, `${path}.${key}`);
    }
  }
  if (rule.type === "array" && Array.isArray(value)) {
    if (value.length < (rule.minItems ?? 0)) fail("CONTRACT_ARRAY_TOO_SHORT", path);
    if (rule.maxItems !== undefined && value.length > rule.maxItems) {
      fail("CONTRACT_ARRAY_TOO_LONG", path);
    }
    if (rule.uniqueItems) {
      const encoded = value.map((item) => decoder.decode(canonicalDocumentBytes(item)));
      if (new Set(encoded).size !== encoded.length) fail("CONTRACT_ARRAY_NOT_UNIQUE", path);
    }
    if (rule.items) {
      value.forEach((item, index) => validateNode(rule.items, item, `${path}[${index}]`));
    }
  }
  if (typeof value === "string") {
    if (value.length < (rule.minLength ?? 0)) fail("CONTRACT_STRING_TOO_SHORT", path);
    if (rule.maxLength !== undefined && value.length > rule.maxLength) {
      fail("CONTRACT_STRING_TOO_LONG", path);
    }
    if (rule.pattern && !(new RegExp(rule.pattern).test(value))) {
      fail("CONTRACT_PATTERN_INVALID", path);
    }
  }
  if (Number.isSafeInteger(value)) {
    if (rule.minimum !== undefined && value < rule.minimum) fail("CONTRACT_NUMBER_TOO_SMALL", path);
    if (rule.maximum !== undefined && value > rule.maximum) fail("CONTRACT_NUMBER_TOO_LARGE", path);
  }
}

export function validateDocument(schemaId, value) {
  const detached = JSON.parse(decoder.decode(canonicalDocumentBytes(value)));
  validateNode(schema(schemaId), detached, "$");
  return detached;
}

export function canonicalValidatedBytes(schemaId, value) {
  return canonicalDocumentBytes(validateDocument(schemaId, value));
}

async function sha256(bytes) {
  let subtle = globalThis.crypto?.subtle;
  if (!subtle) subtle = (await import("node:crypto")).webcrypto.subtle;
  const digest = new Uint8Array(await subtle.digest("SHA-256", bytes));
  return [...digest].map((item) => item.toString(16).padStart(2, "0")).join("");
}

function digestSpec(schemaId) {
  const spec = schema(schemaId)["x-pullwise-digest"];
  if (!spec || typeof spec.field !== "string" || typeof spec.domain !== "string") {
    fail("CONTRACT_DIGEST_UNDECLARED", schemaId);
  }
  return spec;
}

export async function documentDigest(schemaId, unsignedValue) {
  const {field, domain} = digestSpec(schemaId);
  const unsigned = JSON.parse(decoder.decode(canonicalDocumentBytes(unsignedValue)));
  if (field in unsigned) fail("CONTRACT_DIGEST_FIELD_PRESENT", field);
  validateDocument(schemaId, {...unsigned, [field]: "0".repeat(64)});
  const domainBytes = encoder.encode(domain);
  const documentBytes = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domainBytes.length + 1 + documentBytes.length);
  input.set(domainBytes);
  input[domainBytes.length] = 0;
  input.set(documentBytes, domainBytes.length + 1);
  return sha256(input);
}

export async function sealDocument(schemaId, unsignedValue) {
  const {field} = digestSpec(schemaId);
  const unsigned = JSON.parse(decoder.decode(canonicalDocumentBytes(unsignedValue)));
  return validateDocument(schemaId, {
    ...unsigned,
    [field]: await documentDigest(schemaId, unsigned),
  });
}

export async function verifyDocumentDigest(schemaId, completeValue) {
  const complete = validateDocument(schemaId, completeValue);
  const {field} = digestSpec(schemaId);
  const presented = complete[field];
  const unsigned = Object.fromEntries(
    Object.entries(complete).filter(([key]) => key !== field),
  );
  if (presented !== await documentDigest(schemaId, unsigned)) {
    fail("CONTRACT_DIGEST_MISMATCH", field);
  }
  return complete;
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
export const package_tuple = packageTuple;
export const root_manifest = rootManifest;
export const root_manifest_bytes = rootManifestBytes;
export const schema_bytes = schemaBytes;
export const seal_document = sealDocument;
export const validate_document = validateDocument;
export const verify_bundle = verifyBundle;
export const verify_document_digest = verifyDocumentDigest;
'''
