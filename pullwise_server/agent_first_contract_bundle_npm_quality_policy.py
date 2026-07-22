"""Generated Node QualityPolicyPlan rules and contextual verification."""

from __future__ import annotations


NPM_QUALITY_POLICY = r'''
const QUALITY_POLICY_INPUT_FIELDS = Object.freeze([
  "proposal_digest",
  "policy_digest",
  "task_type",
  "requirement_ledger_digest",
  "change_set_classification_digest",
  "capability_usage_digest",
]);

const QUALITY_POLICY_SLOT_TABLE = Object.freeze({
  Q1: [["slot_11111111111111111111111111111111", "contract_and_data"]],
  Q2: [
    ["slot_11111111111111111111111111111111", "contract_and_data"],
    ["slot_22222222222222222222222222222222", "security_and_concurrency"],
  ],
  Q3: [],
});

const QUALITY_RISK_RANK = Object.freeze({Q0: 0, Q1: 1, Q2: 2, Q3: 3});

function qualitySortedUniqueStrings(values, allowEmpty) {
  return Array.isArray(values) && (allowEmpty || values.length > 0) &&
    values.every((item) => typeof item === "string") &&
    new Set(values).size === values.length &&
    JSON.stringify(values) === JSON.stringify([...values].sort());
}

function ruleQualityPolicyPlan(value) {
  const inputProjection = Object.fromEntries(
    QUALITY_POLICY_INPUT_FIELDS.map((field) => [field, value[field]]),
  );
  if (value.input_digest !== sha256Sync(canonicalDocumentBytes(inputProjection))) {
    fail("QUALITY_POLICY_INPUT_DIGEST_INVALID", "$.input_digest");
  }
  const expectedSlots = QUALITY_POLICY_SLOT_TABLE[value.quality_risk];
  const actualSlots = value.slots.map((item) => [item.slot_id, item.concern]);
  if (expectedSlots === undefined ||
      canonicalString(actualSlots) !== canonicalString(expectedSlots)) {
    fail("QUALITY_POLICY_SLOT_TABLE_INVALID", "$.slots");
  }
  if (value.self_attestation_allowed !== false) {
    fail("QUALITY_POLICY_SELF_ATTESTATION_INVALID", "$.self_attestation_allowed");
  }
  value.slots.forEach((slot, index) => {
    if (!qualitySortedUniqueStrings(slot.requirement_ids, false)) {
      fail(
        "QUALITY_POLICY_REQUIREMENT_ORDER_INVALID",
        "$.slots[" + index + "].requirement_ids",
      );
    }
  });
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "plan_digest"),
  );
  const domain = encoder.encode("pullwise:quality-policy-plan:v1");
  const document = canonicalDocumentBytes(unsigned);
  const digestInput = new Uint8Array(domain.length + 1 + document.length);
  digestInput.set(domain);
  digestInput.set(document, domain.length + 1);
  if (value.plan_digest !== sha256Sync(digestInput)) {
    fail("CONTRACT_DIGEST_MISMATCH", "$.plan_digest");
  }
}

function qualityContextObject(value, path) {
  if (value === null || Array.isArray(value) || typeof value !== "object" ||
      Object.getPrototypeOf(value) !== Object.prototype) {
    fail("QUALITY_POLICY_CONTEXT_INVALID", path);
  }
  return value;
}

function qualityContextField(value, field, path) {
  if (!Object.prototype.hasOwnProperty.call(value, field)) {
    fail("QUALITY_POLICY_CONTEXT_INVALID", path + "." + field);
  }
  return value[field];
}

export async function verifyQualityPolicyPlanContext(
  plan, proposal, policy, taskRequest, requirementLedger, changeSet,
) {
  const checked = await verifyDocumentDigest("quality-policy-plan/v1", plan);
  const proposalValue = qualityContextObject(proposal, "$.proposal");
  const policyValue = qualityContextObject(policy, "$.policy");
  const requestValue = qualityContextObject(taskRequest, "$.task_request");
  const ledgerValue = qualityContextObject(
    requirementLedger, "$.requirement_ledger",
  );
  const changeValue = qualityContextObject(changeSet, "$.change_set");
  const bindings = [
    ["task_id", proposalValue, "task_id", "$.proposal"],
    ["proposal_id", proposalValue, "proposal_id", "$.proposal"],
    ["proposal_digest", proposalValue, "proposal_digest", "$.proposal"],
    ["policy_digest", proposalValue, "policy_digest", "$.proposal"],
    [
      "requirement_ledger_digest", proposalValue,
      "requirement_ledger_digest", "$.proposal",
    ],
    ["policy_digest", policyValue, "digest", "$.policy"],
    ["task_type", policyValue, "task_type", "$.policy"],
    ["task_id", requestValue, "task_id", "$.task_request"],
    ["task_type", requestValue, "task_type", "$.task_request"],
    ["task_id", ledgerValue, "task_id", "$.requirement_ledger"],
    [
      "requirement_ledger_digest", ledgerValue,
      "ledger_digest", "$.requirement_ledger",
    ],
    [
      "change_set_classification_digest", changeValue,
      "change_set_classification_digest", "$.change_set",
    ],
    [
      "capability_usage_digest", changeValue,
      "capability_usage_digest", "$.change_set",
    ],
  ];
  for (const [planField, context, contextField, path] of bindings) {
    if (checked[planField] !== qualityContextField(context, contextField, path)) {
      fail("QUALITY_POLICY_CONTEXT_BINDING_INVALID", path + "." + contextField);
    }
  }

  const floor = qualityContextField(policyValue, "quality_risk_floor", "$.policy");
  if (typeof floor !== "string" ||
      !Object.prototype.hasOwnProperty.call(QUALITY_RISK_RANK, floor) ||
      QUALITY_RISK_RANK[checked.quality_risk] < QUALITY_RISK_RANK[floor]) {
    fail("QUALITY_POLICY_RISK_FLOOR_INVALID", "$.policy.quality_risk_floor");
  }

  const active = qualityContextField(
    ledgerValue, "active_requirement_ids", "$.requirement_ledger",
  );
  const entries = qualityContextField(
    ledgerValue, "entries", "$.requirement_ledger",
  );
  if (!qualitySortedUniqueStrings(active, false)) {
    fail(
      "QUALITY_POLICY_ACTIVE_REQUIREMENTS_INVALID",
      "$.requirement_ledger.active_requirement_ids",
    );
  }
  if (!Array.isArray(entries)) {
    fail("QUALITY_POLICY_LEDGER_ENTRIES_INVALID", "$.requirement_ledger.entries");
  }
  const entriesById = new Map();
  entries.forEach((entry, index) => {
    const path = "$.requirement_ledger.entries[" + index + "]";
    if (entry === null || Array.isArray(entry) || typeof entry !== "object" ||
        Object.getPrototypeOf(entry) !== Object.prototype ||
        typeof entry.requirement_id !== "string" ||
        typeof entry.mandatory !== "boolean") {
      fail("QUALITY_POLICY_LEDGER_ENTRIES_INVALID", path);
    }
    if (entriesById.has(entry.requirement_id)) {
      fail("QUALITY_POLICY_LEDGER_ENTRIES_INVALID", path + ".requirement_id");
    }
    entriesById.set(entry.requirement_id, entry.mandatory);
  });
  const activeSet = new Set(active);
  if (active.some((requirementId) => !entriesById.has(requirementId))) {
    fail(
      "QUALITY_POLICY_ACTIVE_REQUIREMENTS_INVALID",
      "$.requirement_ledger.active_requirement_ids",
    );
  }
  const mandatoryActive = new Set(
    active.filter((requirementId) => entriesById.get(requirementId)),
  );
  checked.slots.forEach((slot, index) => {
    const covered = new Set(slot.requirement_ids);
    const path = "$.slots[" + index + "].requirement_ids";
    if ([...covered].some((requirementId) => !activeSet.has(requirementId))) {
      fail("QUALITY_POLICY_SLOT_REQUIREMENT_INACTIVE", path);
    }
    if ([...mandatoryActive].some((requirementId) => !covered.has(requirementId))) {
      fail("QUALITY_POLICY_MANDATORY_COVERAGE_INVALID", path);
    }
  });
  return checked;
}
'''


__all__ = ["NPM_QUALITY_POLICY"]
