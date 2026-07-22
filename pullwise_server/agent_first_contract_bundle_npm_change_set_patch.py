"""Generated npm facade semantics for change-set patch documents."""

from __future__ import annotations


NPM_CHANGE_SET_PATCH_RULE = r'''
function decodeChangeSetPatchBase64(value) {
  if (value.length % 4 !== 0) {
    fail("SOURCE_CONTENT_BASE64_INVALID", "$.data_base64");
  }
  let binary;
  try {
    binary = globalThis.atob(value);
  } catch (_error) {
    fail("SOURCE_CONTENT_BASE64_INVALID", "$.data_base64");
  }
  if (globalThis.btoa(binary) !== value) {
    fail("SOURCE_CONTENT_BASE64_NONCANONICAL", "$.data_base64");
  }
  return Uint8Array.from(binary, (item) => item.charCodeAt(0));
}

function ruleChangeSetPatch(value) {
  const raw = decodeChangeSetPatchBase64(value.data_base64);
  if (raw.length !== value.size_bytes) {
    fail("SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes");
  }
  if (sha256Sync(raw) !== value.byte_sha256) {
    fail("SOURCE_CONTENT_SHA256_MISMATCH", "$.byte_sha256");
  }

  const spec = schema("change-set-patch/v1")["x-pullwise-digest"];
  const presented = value[spec.field];
  if (presented === "0".repeat(64)) return;
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== spec.field),
  );
  const domain = encoder.encode(spec.domain);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input[domain.length] = 0;
  input.set(document, domain.length + 1);
  if (sha256Sync(input) !== presented) {
    fail("CONTRACT_DIGEST_MISMATCH", "$." + spec.field);
  }
}
'''


__all__ = ["NPM_CHANGE_SET_PATCH_RULE"]
