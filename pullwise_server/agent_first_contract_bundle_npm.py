"""Render the dependency-free npm current-contract facade."""

from __future__ import annotations

import base64
import json
from .agent_first_contract_bundle_npm_semantics import NPM_SEMANTICS
from .agent_first_contract_bundle_npm_verify import NPM_VERIFY

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
    rendered = _TEMPLATE.replace("@@SEMANTICS@@", NPM_SEMANTICS)
    rendered = rendered.replace("@@VERIFY@@", NPM_VERIFY)
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

export class ContractValidationError extends Error {
  constructor(code, detail, path) {
    super(code + ": " + detail + ": " + path);
    this.code = code;
    this.detail = detail;
    this.path = path;
  }
}

function fail(code, path = "$") {
  throw new ContractValidationError(publicErrorCode(code, null), code, path);
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
  return encoder.encode(canonicalString(canonicalValue(value)));
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
  if (rule.oneOf !== undefined) validateOneOf(rule.oneOf, value, path);
  if (rule.$ref) {
    validateNode(schema(rule.$ref), value, path);
    validateReferenceAnnotations(rule, value, path);
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
    const length = [...value].length;
    if (length < (rule.minLength ?? 0)) fail("CONTRACT_STRING_TOO_SHORT", path);
    if (rule.maxLength !== undefined && length > rule.maxLength) {
      fail("CONTRACT_STRING_TOO_LONG", path);
    }
    if (rule.pattern && !patternMatches(rule.pattern, value)) {
      fail("CONTRACT_PATTERN_INVALID", path);
    }
  }
  if (Number.isSafeInteger(value)) {
    if (rule.minimum !== undefined && value < rule.minimum) fail("CONTRACT_NUMBER_TOO_SMALL", path);
    if (rule.maximum !== undefined && value > rule.maximum) fail("CONTRACT_NUMBER_TOO_LARGE", path);
  }
}

@@SEMANTICS@@

export function validateDocument(schemaId, value) {
  if (schemaRole(schemaId) !== "public_document") {
    fail("CONTRACT_INTERNAL_CONSTRAINT", schemaId);
  }
  const detached = JSON.parse(decoder.decode(canonicalDocumentBytes(value)));
  validateNode(schema(schemaId), detached, "$");
  validateSemantics(schemaId, detached);
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

@@VERIFY@@
'''
