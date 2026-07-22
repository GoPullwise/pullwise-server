"""Executable npm facade semantics for current R0 tool evidence."""

from __future__ import annotations


NPM_TOOL_EVIDENCE = r'''
function verifyToolDigestSync(schemaId, value) {
  const spec = schema(schemaId)["x-pullwise-digest"];
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== spec.field),
  );
  const domain = encoder.encode(spec.domain);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input[domain.length] = 0;
  input.set(document, domain.length + 1);
  if (sha256Sync(input) !== value[spec.field]) {
    fail("CONTRACT_DIGEST_MISMATCH", "$." + spec.field);
  }
}

function validToolSourcePath(value) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\\") ||
      value.startsWith("/") || encoder.encode(value).length > 4096) return false;
  return value.split("/").every((part) => !["", ".", ".."].includes(part));
}

function ruleAgentToolRequest(value) {
  if (!validToolSourcePath(value.tool_input.relative_path)) {
    fail("TOOL_SOURCE_PATH_INVALID", "$.tool_input.relative_path");
  }
}

function ruleLocalToolReceipt(value) {
  verifyToolDigestSync("local-tool-receipt/v1", value);
  const started = observationTimestampMillis(value.started_at);
  const completed = observationTimestampMillis(value.completed_at);
  if (started === null || completed === null || completed < started ||
      value.elapsed_ms !== completed - started) {
    fail("LOCAL_RECEIPT_TIMING_INVALID");
  }
}

function ruleR0ReadPayload(value) {
  verifyToolDigestSync("r0-read-payload/v1", value);
  if (!validToolSourcePath(value.relative_path)) {
    fail("TOOL_SOURCE_PATH_INVALID", "$.relative_path");
  }
}

function ruleR0ReadResult(value) {
  verifyToolDigestSync("r0-read-result/v1", value);
  if (value.source_state_before_id !== value.source_state_after_id) {
    fail("SOURCE_STATE_CHANGED");
  }
}

function decodeToolBase64(value) {
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

function ruleSourceContent(value) {
  const raw = decodeToolBase64(value.data_base64);
  if (raw.length !== value.size_bytes) {
    fail("SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes");
  }
  if (sha256Sync(raw) !== value.byte_sha256) {
    fail("SOURCE_CONTENT_SHA256_MISMATCH", "$.byte_sha256");
  }
  verifyToolDigestSync("source-content/v1", value);
}

function ruleSourceState(value) {
  verifyToolDigestSync("source-state/v1", value);
}

function ruleToolCatalog(value) {
  verifyToolDigestSync("tool-catalog/v1", value);
  const keys = value.tools.map((item) => item.tool_key);
  if (new Set(keys).size !== keys.length ||
      JSON.stringify(keys) !== JSON.stringify([...keys].sort())) {
    fail("TOOL_CATALOG_ORDER_INVALID");
  }
}

function ruleToolDispatchCapability(value) {
  verifyToolDigestSync("tool-dispatch-capability/v1", value);
  if (observationTimestampMillis(value.issued_at) === null) {
    fail("TOOL_CAPABILITY_TIME_INVALID", "$.issued_at");
  }
}

function ruleToolDispatchIntent(value) {
  verifyToolDigestSync("tool-dispatch-intent/v1", value);
  if (!validToolSourcePath(value.tool_input.relative_path)) {
    fail("TOOL_SOURCE_PATH_INVALID", "$.tool_input.relative_path");
  }
  if (observationTimestampMillis(value.created_at) === null) {
    fail("TOOL_INTENT_TIME_INVALID", "$.created_at");
  }
}

function ruleToolInvocation(value) {
  verifyToolDigestSync("tool-invocation/v1", value);
  if (!validToolSourcePath(value.tool_input.relative_path)) {
    fail("TOOL_SOURCE_PATH_INVALID", "$.tool_input.relative_path");
  }
}

function toolEqual(left, right) {
  return canonicalString(left) === canonicalString(right);
}

function toolExact(left, right, fields) {
  return fields.every((field) => toolEqual(left[field], right[field]));
}

export async function validateToolInvocationBinding(request, invocation, catalog) {
  const checkedRequest = validateDocument("agent-tool-request/v1", request);
  const checkedInvocation = await verifyDocumentDigest(
    "tool-invocation/v1", invocation,
  );
  const checkedCatalog = await verifyDocumentDigest("tool-catalog/v1", catalog);
  const descriptor = checkedCatalog.tools.find(
    (item) => item.tool_key === checkedRequest.tool_key,
  );
  if (!descriptor || descriptor.request_schema_id !== checkedRequest.schema_id ||
      !toolExact(checkedRequest, checkedInvocation,
        ["idempotency_key", "tool_key", "tool_input"])) {
    fail("TOOL_INVOCATION_BINDING_INVALID");
  }
  return true;
}

export async function validateToolJournalBegin(invocation, intent, capability) {
  const checkedInvocation = await verifyDocumentDigest(
    "tool-invocation/v1", invocation,
  );
  const checkedIntent = await verifyDocumentDigest(
    "tool-dispatch-intent/v1", intent,
  );
  const checkedCapability = await verifyDocumentDigest(
    "tool-dispatch-capability/v1", capability,
  );
  const exact = [
    "package", "authority_digest", "grant_digest", "invocation_digest",
    "task_id", "idempotency_key", "tool_key", "tool_input",
  ];
  if (!toolExact(checkedIntent, checkedInvocation, exact)) {
    fail("TOOL_INTENT_BINDING_INVALID");
  }
  if (!toolEqual(checkedCapability.package, checkedIntent.package) ||
      checkedCapability.intent_digest !== checkedIntent.intent_digest ||
      checkedCapability.capability_digest !== checkedIntent.capability_digest ||
      checkedCapability.issued_at !== checkedIntent.created_at) {
    fail("TOOL_CAPABILITY_BINDING_INVALID");
  }
  return true;
}

export async function validateToolCapabilityConsumption(
  intent, capability, consumedCapabilityDigests,
) {
  const checkedIntent = await verifyDocumentDigest(
    "tool-dispatch-intent/v1", intent,
  );
  const checkedCapability = await verifyDocumentDigest(
    "tool-dispatch-capability/v1", capability,
  );
  if (!Array.isArray(consumedCapabilityDigests) ||
      !consumedCapabilityDigests.every(
        (item) => typeof item === "string" && /^[0-9a-f]{64}$/.test(item),
      ) || new Set(consumedCapabilityDigests).size !== consumedCapabilityDigests.length ||
      JSON.stringify(consumedCapabilityDigests) !==
        JSON.stringify([...consumedCapabilityDigests].sort())) {
    fail("TOOL_CAPABILITY_CONSUMPTION_INVALID");
  }
  if (checkedCapability.intent_digest !== checkedIntent.intent_digest ||
      checkedCapability.capability_digest !== checkedIntent.capability_digest) {
    fail("TOOL_CAPABILITY_BINDING_INVALID");
  }
  if (consumedCapabilityDigests.includes(checkedCapability.capability_digest)) {
    fail("CAPABILITY_ALREADY_CONSUMED");
  }
  return true;
}

async function verifyToolContentRef(reference, schemaId, document) {
  const checked = validateDocument("content-ref/v1", reference);
  const encoded = canonicalDocumentBytes(document);
  if (checked.content_schema_id !== schemaId ||
      checked.sha256 !== await sha256(encoded) ||
      checked.size_bytes !== encoded.length ||
      checked.media_type !== "application/json" || checked.encoding !== "utf-8") {
    fail("TOOL_CONTENT_REF_BINDING_INVALID");
  }
}

export async function validateToolJournalSettlement(
  invocation, intent, receipt, payload, result, sourceBefore, sourceAfter,
) {
  const checkedInvocation = await verifyDocumentDigest(
    "tool-invocation/v1", invocation,
  );
  const checkedIntent = await verifyDocumentDigest(
    "tool-dispatch-intent/v1", intent,
  );
  const checkedReceipt = await verifyDocumentDigest(
    "local-tool-receipt/v1", receipt,
  );
  const checkedPayload = await verifyDocumentDigest("r0-read-payload/v1", payload);
  const checkedResult = await verifyDocumentDigest("r0-read-result/v1", result);
  const before = await verifyDocumentDigest("source-state/v1", sourceBefore);
  const after = await verifyDocumentDigest("source-state/v1", sourceAfter);
  const exact = [
    "package", "authority_digest", "grant_digest", "invocation_digest",
    "task_id", "idempotency_key", "tool_key", "tool_input",
  ];
  if (!toolExact(checkedIntent, checkedInvocation, exact)) {
    fail("TOOL_SETTLEMENT_BINDING_INVALID");
  }
  const invocationDigest = checkedInvocation.invocation_digest;
  if (checkedReceipt.tool_key !== checkedInvocation.tool_key ||
      checkedReceipt.invocation_digest !== invocationDigest ||
      checkedPayload.invocation_digest !== invocationDigest ||
      checkedResult.invocation_digest !== invocationDigest ||
      checkedPayload.relative_path !== checkedInvocation.tool_input.relative_path ||
      checkedResult.local_receipt_digest !== checkedReceipt.receipt_digest ||
      !toolEqual(checkedReceipt.payload_ref, checkedResult.payload_ref)) {
    fail("TOOL_SETTLEMENT_BINDING_INVALID");
  }
  await verifyToolContentRef(
    checkedReceipt.payload_ref, "r0-read-payload/v1", checkedPayload,
  );
  const identity = ["task_id", "attempt_id", "native_epoch"];
  if (!identity.every(
    (field) => before[field] === checkedInvocation[field] &&
      after[field] === checkedInvocation[field],
  )) fail("SOURCE_STATE_BINDING_INVALID");
  if (before.source_state_id !== after.source_state_id ||
      checkedResult.source_state_before_id !== before.source_state_id ||
      checkedResult.source_state_after_id !== after.source_state_id) {
    fail("SOURCE_STATE_CHANGED");
  }
  return true;
}
'''


__all__ = ["NPM_TOOL_EVIDENCE"]
