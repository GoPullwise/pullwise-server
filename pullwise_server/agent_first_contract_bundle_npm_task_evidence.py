"""Generated npm facade semantics for final evidence closure."""

from __future__ import annotations


NPM_TASK_EVIDENCE = r'''
const TASK_EVIDENCE_IDENTITY_FIELDS = [
  "content_schema_id", "sha256", "size_bytes", "media_type", "encoding",
];

function taskEvidenceContentIdentity(ref) {
  return canonicalString(
    TASK_EVIDENCE_IDENTITY_FIELDS.map((field) => ref[field]),
  );
}

function taskEvidenceHasContentAlias(refs) {
  const artifactsByContent = new Map();
  for (const ref of refs) {
    const identity = taskEvidenceContentIdentity(ref);
    const previous = artifactsByContent.get(identity);
    if (previous !== undefined && previous !== ref.artifact_id) return true;
    artifactsByContent.set(identity, ref.artifact_id);
  }
  return false;
}

function taskEvidenceCompareText(left, right) {
  const leftPoints = [...left];
  const rightPoints = [...right];
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index].codePointAt(0) -
      rightPoints[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function taskEvidenceRefKey(ref) {
  return [ref.content_schema_id, ref.artifact_id, ref.sha256];
}

function taskEvidenceCompareKey(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    const difference = taskEvidenceCompareText(left[index], right[index]);
    if (difference !== 0) return difference;
  }
  return 0;
}

function taskEvidenceOrderedUnique(refs) {
  const keys = refs.map(taskEvidenceRefKey);
  for (let index = 1; index < keys.length; index += 1) {
    if (taskEvidenceCompareKey(keys[index - 1], keys[index]) >= 0) return false;
  }
  return true;
}

function taskEvidenceExactEdge(entries, schemaIds, expected) {
  const matches = entries.filter(
    (ref) => schemaIds.has(ref.content_schema_id),
  );
  return matches.length === 1 &&
    canonicalString(matches[0]) === canonicalString(expected);
}

function ruleEvidenceClosureManifest(value) {
  const entries = value.entries;
  if (value.entry_count !== entries.length) {
    fail("EVIDENCE_CLOSURE_COUNT_INVALID", "$.entry_count");
  }
  if (!taskEvidenceOrderedUnique(entries)) {
    fail("EVIDENCE_CLOSURE_ORDER_INVALID", "$.entries");
  }
  verifyContentRefSet(entries);
  if (taskEvidenceHasContentAlias(entries)) {
    fail("EVIDENCE_CLOSURE_CONTENT_ALIAS", "$.entries");
  }
  const edges = [
    [new Set(["pre-gate-evidence-closure-manifest/v1"]),
      value.pre_gate_evidence_closure_ref],
    [new Set([
      "gate-input-snapshot/v1", "terminalization-input-snapshot/v1",
    ]), value.input_snapshot_ref],
    [new Set(["gate-decision/v1"]), value.gate_decision_ref],
  ];
  if (!edges.every(
    ([schemaIds, expected]) =>
      taskEvidenceExactEdge(entries, schemaIds, expected),
  )) {
    fail("EVIDENCE_CLOSURE_REQUIRED_EDGE_INVALID", "$.entries");
  }
  const expectedDigest = sha256Sync(canonicalDocumentBytes(entries));
  if (value.evidence_closure_digest !== expectedDigest) {
    fail("EVIDENCE_CLOSURE_DIGEST_INVALID", "$.evidence_closure_digest");
  }
}

function taskEvidenceRefMatchesDocument(ref, schemaId, document) {
  const raw = canonicalDocumentBytes(document);
  return ref.schema_id === "content-ref/v1" &&
    ref.content_schema_id === schemaId &&
    ref.sha256 === sha256Sync(raw) &&
    ref.size_bytes === raw.length &&
    ref.media_type === "application/json" && ref.encoding === "utf-8";
}

function taskEvidenceExpectedEntries(manifest, preGateManifest) {
  const candidates = [
    ...preGateManifest.entries,
    manifest.pre_gate_evidence_closure_ref,
    manifest.input_snapshot_ref,
    manifest.gate_decision_ref,
  ];
  const unique = new Map(
    candidates.map((ref) => [canonicalString(ref), ref]),
  );
  return [...unique.values()].sort(
    (left, right) => taskEvidenceCompareKey(
      taskEvidenceRefKey(left), taskEvidenceRefKey(right),
    ),
  );
}

/** Bind final closure to the direct PreGate manifest document. */
export async function verifyEvidenceClosureContext(manifest, preGateManifest) {
  const validated = await verifyDocumentDigest(
    "evidence-closure-manifest/v1", manifest,
  );
  const preGate = await verifyDocumentDigest(
    "pre-gate-evidence-closure-manifest/v1", preGateManifest,
  );
  if (validated.task_id !== preGate.task_id) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.task_id");
  }
  if (!taskEvidenceRefMatchesDocument(
    validated.pre_gate_evidence_closure_ref,
    "pre-gate-evidence-closure-manifest/v1",
    preGate,
  )) {
    fail("CAS_CORRUPT", "$.pre_gate_evidence_closure_ref");
  }
  const expected = taskEvidenceExpectedEntries(validated, preGate);
  if (canonicalString(validated.entries) !== canonicalString(expected)) {
    fail("EVIDENCE_CLOSURE_INVALID", "$.entries");
  }
  return validated;
}
'''


__all__ = ["NPM_TASK_EVIDENCE"]
