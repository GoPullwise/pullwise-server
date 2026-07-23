"""Executable npm facade semantics for task publication documents."""

from __future__ import annotations


NPM_PUBLICATION = r'''
const ARTIFACT_CONTENT_TUPLES = Object.freeze([
  ["change_set", "change-set/v1", "application/json", "utf-8"],
  ["change_set_patch", "change-set-patch/v1", "application/json", "utf-8"],
  ["r0_read_result", "r0-read-result/v1", "application/json", "utf-8"],
  ["source_content", "source-content/v1", "application/json", "utf-8"],
  ["task_report", "task-report/v1", "application/json", "utf-8"],
]);

function verifyPublicationDigestSync(schemaId, value) {
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

function ruleArtifactContentRegistry(value) {
  verifyPublicationDigestSync("artifact-content-registry/v1", value);
  const actual = value.entries.map((item) => [
    item.artifact_kind, item.content_schema_id, item.media_type, item.encoding,
  ]);
  if (JSON.stringify(actual) !== JSON.stringify(ARTIFACT_CONTENT_TUPLES)) {
    fail("ARTIFACT_CONTENT_REGISTRY_INVALID");
  }
}

function ruleArtifactContentRef(value) {
  const expected = ARTIFACT_CONTENT_TUPLES.find(
    (item) => item[0] === value.artifact_kind,
  );
  const ref = value.ref;
  if (!expected || JSON.stringify([
    ref.content_schema_id, ref.media_type, ref.encoding,
  ]) !== JSON.stringify(expected.slice(1))) {
    fail("ARTIFACT_CONTENT_TUPLE_INVALID");
  }
}

function ruleBudgetSummary(value) {
  verifyPublicationDigestSync("budget-summary/v1", value);
  if (value.consumed_ms > value.elapsed_limit_ms) {
    throw new ContractValidationError(
      "BUDGET_EXHAUSTED", "BUDGET_SUMMARY_ELAPSED_INVALID", "$",
    );
  }
  if (value.calls_consumed > value.tool_call_limit) {
    throw new ContractValidationError(
      "BUDGET_EXHAUSTED", "BUDGET_SUMMARY_CALLS_INVALID", "$",
    );
  }
}

function ruleEffectLedgerSnapshot(value) {
  verifyPublicationDigestSync("effect-ledger-snapshot/v1", value);
  if (value.watermark !== value.rows.length) {
    fail("EFFECT_LEDGER_WATERMARK_INVALID", "$.watermark");
  }
  const effectIds = value.rows.map((item) => item.effect_id);
  if (new Set(effectIds).size !== effectIds.length ||
      canonicalString(effectIds) !== canonicalString([...effectIds].sort())) {
    fail("EFFECT_LEDGER_ROW_ORDER_INVALID", "$.rows");
  }
  const expected = Object.fromEntries(
    Object.keys(value.state_counts).map((state) => [
      state,
      value.rows.filter((item) => item.state === state.toUpperCase()).length,
    ]),
  );
  if (canonicalString(value.state_counts) !== canonicalString(expected)) {
    fail("EFFECT_LEDGER_STATE_COUNTS_INVALID", "$.state_counts");
  }
}

function publicationRefKey(value) {
  return [value.content_schema_id, value.artifact_id, value.sha256];
}

function orderedPublicationRefs(values) {
  const keys = values.map((item) => JSON.stringify(publicationRefKey(item)));
  return new Set(keys).size === keys.length &&
    JSON.stringify(keys) === JSON.stringify([...keys].sort());
}

function ruleTaskReport(value) {
  verifyPublicationDigestSync("task-report/v1", value);
  const sectionIds = value.sections.map((item) => item.section_id);
  if (new Set(sectionIds).size !== sectionIds.length ||
      JSON.stringify(sectionIds) !== JSON.stringify([...sectionIds].sort())) {
    fail("TASK_REPORT_SECTION_ORDER_INVALID", "$.sections");
  }
  for (const [field, limit] of [["title", 512], ["summary", 4096]]) {
    if (encoder.encode(value[field]).length > limit) {
      fail("TASK_REPORT_UTF8_LIMIT_INVALID", "$." + field);
    }
  }
  const allRefs = [];
  value.sections.forEach((section, index) => {
    if (encoder.encode(section.title).length > 512 ||
        encoder.encode(section.body).length > 65536) {
      fail("TASK_REPORT_UTF8_LIMIT_INVALID", `$.sections[${index}]`);
    }
    if (!orderedPublicationRefs(section.evidence_refs)) {
      fail(
        "TASK_REPORT_EVIDENCE_ORDER_INVALID",
        `$.sections[${index}].evidence_refs`,
      );
    }
    allRefs.push(...section.evidence_refs);
  });
  verifyContentRefSet(allRefs);
}
'''


__all__ = ["NPM_PUBLICATION"]
