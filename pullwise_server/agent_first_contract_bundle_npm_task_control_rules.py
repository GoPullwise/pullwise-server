"""Node facade rules for task-control and requirement documents."""

from __future__ import annotations


NPM_TASK_CONTROL_RULES = r'''
const TASK_CONTROL_REQUIREMENT_KIND_RANK = Object.freeze({
  user_objective: 0,
  user_acceptance: 1,
  user_constraint: 2,
  delivery: 3,
  policy: 4,
  interaction: 5,
  derived: 5,
});

function taskControlCompareStrings(left, right) {
  const leftPoints = [...left].map((item) => item.codePointAt(0));
  const rightPoints = [...right].map((item) => item.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] < rightPoints[index] ? -1 : 1;
    }
  }
  return leftPoints.length - rightPoints.length;
}

function taskControlSortedUnique(values) {
  return Array.isArray(values) &&
    new Set(values).size === values.length &&
    values.every((item) => typeof item === "string") &&
    values.every((item, index) => index === 0 ||
      taskControlCompareStrings(values[index - 1], item) < 0);
}

function taskControlUtf8Walk(rule, value, path) {
  if (rule.$ref) {
    taskControlUtf8Walk(schema(rule.$ref), value, path);
    return;
  }
  if (rule.oneOf) {
    for (const option of rule.oneOf) {
      try {
        validateNode(option, value, path);
      } catch (error) {
        if (error instanceof ContractValidationError) continue;
        throw error;
      }
      taskControlUtf8Walk(option, value, path);
      break;
    }
  }
  if (typeof value === "string") {
    if (value.normalize("NFC") !== value) fail("UTF8_NFC_INVALID", path);
    if (Number.isSafeInteger(rule.maxLength) &&
        encoder.encode(value).length > rule.maxLength) {
      fail("UTF8_BYTE_LIMIT_INVALID", path);
    }
  } else if (Array.isArray(value) && rule.items) {
    value.forEach((item, index) => {
      taskControlUtf8Walk(rule.items, item, `${path}[${index}]`);
    });
  } else if (value && typeof value === "object" && !Array.isArray(value)) {
    const properties = rule.properties ?? {};
    for (const [key, item] of Object.entries(value)) {
      if (key in properties) {
        taskControlUtf8Walk(properties[key], item, `${path}.${key}`);
      }
    }
  }
}

function taskControlRuleUtf8(value) {
  taskControlUtf8Walk(schema(value.schema_id), value, "$");
}

function taskControlRuleRequestAcceptanceSources(value) {
  const sourceIds = [...value.acceptance_criteria, ...value.constraints]
    .map((item) => item.source_id);
  if (new Set(sourceIds).size !== sourceIds.length) {
    fail("TASK_REQUEST_SOURCE_ID_INVALID");
  }
}

function taskControlRuleRequestSets(value) {
  if (!taskControlSortedUnique(value.requested_capabilities)) {
    fail("TASK_REQUEST_CAPABILITY_ORDER_INVALID");
  }
  if (!taskControlSortedUnique(value.delivery.required_outputs)) {
    fail("TASK_REQUEST_DELIVERY_ORDER_INVALID");
  }
}

function taskControlRulePolicyCapabilities(value) {
  const deniedIds = value.denied_capabilities.map((item) => item.id);
  if (!taskControlSortedUnique(value.granted_capabilities) ||
      !taskControlSortedUnique(deniedIds)) {
    fail("POLICY_CAPABILITY_ORDER_INVALID");
  }
  if (value.granted_capabilities.some((item) => deniedIds.includes(item))) {
    fail("POLICY_CAPABILITY_OVERLAP");
  }
}

function taskControlRulePolicyRoots(value) {
  for (const field of ["allowed_read_roots", "allowed_write_roots"]) {
    if (!taskControlSortedUnique(value[field])) {
      fail("POLICY_ROOT_ORDER_INVALID", `$.${field}`);
    }
  }
  if (!taskControlSortedUnique(value.agent_tool_network.origins)) {
    fail("POLICY_ORIGIN_ORDER_INVALID", "$.agent_tool_network.origins");
  }
}

function taskControlRulePolicyMvp(value) {
  if (!["R0", "R1"].includes(value.capability_risk_ceiling)) {
    fail("POLICY_RISK_CEILING_INVALID");
  }
  if (value.quality_risk_floor !== "Q1") {
    fail("POLICY_QUALITY_RISK_FLOOR_INVALID");
  }
  if (value.source_write_mode !== "read_only") {
    fail("POLICY_SOURCE_WRITE_INVALID");
  }
  if (canonicalString(value.agent_tool_network) !==
      canonicalString({mode: "deny", origins: []})) {
    fail("POLICY_NETWORK_INVALID");
  }
  if (value.dependency_install !== "deny") {
    fail("POLICY_DEPENDENCY_INSTALL_INVALID");
  }
  if (value.interaction_mode !== "unavailable") {
    fail("POLICY_INTERACTION_INVALID");
  }
  if (value.authorized_waiver_issuers.length !== 0) {
    fail("POLICY_WAIVER_ISSUER_INVALID");
  }
}

function taskControlRulePolicyBudgets(value) {
  const budgets = value.budgets;
  if (value.terminalization_reserve_ms > budgets.wall_ms) {
    fail("POLICY_RESERVE_INVALID");
  }
  if (value.max_agent_sessions_total > budgets.agent_sessions) {
    fail("POLICY_SESSION_CEILING_INVALID");
  }
  if (value.max_attempts > budgets.attempts) {
    fail("POLICY_ATTEMPT_CEILING_INVALID");
  }
  if (value.max_agents > value.max_agent_sessions_total) {
    fail("POLICY_AGENT_CEILING_INVALID");
  }
}

function taskControlEmbeddedDigest(value, field, domainValue) {
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== field),
  );
  const domain = encoder.encode(domainValue);
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input.set(document, domain.length + 1);
  if (sha256Sync(input) !== value[field]) {
    fail("CONTRACT_DIGEST_MISMATCH", `$.${field}`);
  }
}

function taskControlRulePolicyDigest(value) {
  taskControlEmbeddedDigest(
    value, "digest", "pullwise:effective-execution-policy/v1",
  );
}

function taskControlRuleRequirementShape(value) {
  if (value.source_kind === "derived" && value.mandatory && !value.rationale) {
    fail("DERIVED_REQUIREMENT_RATIONALE_REQUIRED", "$.rationale");
  }
}

function taskControlRuleRequirementId(value) {
  if (!value.requirement_id.startsWith(`req_${value.source_kind}_`)) {
    fail("REQUIREMENT_ID_KIND_INVALID", "$.requirement_id");
  }
}

function taskControlRuleRequirementLinks(value) {
  for (const field of ["parent_requirement_ids", "supersedes"]) {
    if (!taskControlSortedUnique(value[field])) {
      fail("REQUIREMENT_LINK_ORDER_INVALID", `$.${field}`);
    }
    if (value[field].includes(value.requirement_id)) {
      fail("REQUIREMENT_SELF_LINK_INVALID", `$.${field}`);
    }
  }
}

function taskControlRequirementKey(value) {
  const rank = TASK_CONTROL_REQUIREMENT_KIND_RANK[value.source_kind];
  return [
    rank,
    rank >= 5 ? value.ledger_version : 0,
    value.source_id,
    value.requirement_id,
  ];
}

function taskControlCompareRequirement(left, right) {
  const leftKey = taskControlRequirementKey(left);
  const rightKey = taskControlRequirementKey(right);
  for (let index = 0; index < leftKey.length; index += 1) {
    if (leftKey[index] === rightKey[index]) continue;
    if (typeof leftKey[index] === "number") {
      return leftKey[index] - rightKey[index];
    }
    return taskControlCompareStrings(leftKey[index], rightKey[index]);
  }
  return 0;
}

function taskControlRequirementGraph(entries) {
  const byId = new Map(entries.map((item) => [item.requirement_id, item]));
  if (byId.size !== entries.length) {
    throw new ContractValidationError(
      "REQUIREMENT_ID_COLLISION", "REQUIREMENT_ID_COLLISION", "$",
    );
  }
  const visiting = new Set();
  const visited = new Set();
  function visit(requirementId) {
    if (visiting.has(requirementId)) fail("REQUIREMENT_CYCLE_INVALID");
    if (visited.has(requirementId)) return;
    visiting.add(requirementId);
    for (const parent of byId.get(requirementId).parent_requirement_ids) {
      if (!byId.has(parent)) fail("REQUIREMENT_PARENT_UNKNOWN");
      visit(parent);
    }
    visiting.delete(requirementId);
    visited.add(requirementId);
  }
  for (const requirementId of byId.keys()) visit(requirementId);
  for (const item of entries) {
    for (const superseded of item.supersedes) {
      if (!byId.has(superseded) ||
          byId.get(superseded).source_kind !== "derived") {
        fail("REQUIREMENT_SUPERSEDES_INVALID");
      }
    }
  }
}

function taskControlRuleLedgerEntries(value) {
  const entries = value.entries;
  for (const item of entries) {
    validateDocument("requirement-entry/v1", item);
    if (item.ledger_version > value.ledger_version) {
      fail("REQUIREMENT_LEDGER_VERSION_INVALID");
    }
  }
  const expected = [...entries].sort(taskControlCompareRequirement);
  if (canonicalString(entries) !== canonicalString(expected)) {
    fail("REQUIREMENT_INGEST_ORDER_INVALID");
  }
  if (value.ledger_version === 1 &&
      entries.some((item) => item.ledger_version !== 1)) {
    fail("REQUIREMENT_LEDGER_VERSION_INVALID");
  }
  taskControlRequirementGraph(entries);
}

function taskControlRuleLedgerDigest(value) {
  taskControlEmbeddedDigest(
    value, "ledger_digest", "pullwise:requirement-ledger:v1",
  );
}

function taskControlRuleLedgerActive(value) {
  const superseded = new Set(value.entries.flatMap((item) => item.supersedes));
  const expected = value.entries
    .map((item) => item.requirement_id)
    .filter((requirementId) => !superseded.has(requirementId))
    .sort(taskControlCompareStrings);
  if (canonicalString(value.active_requirement_ids) !== canonicalString(expected)) {
    fail("REQUIREMENT_ACTIVE_SET_INVALID", "$.active_requirement_ids");
  }
}

function taskControlRuleCharterDigest(value) {
  taskControlEmbeddedDigest(value, "digest", "pullwise:task-charter:v1");
}

function taskControlRuleCharterSets(value) {
  for (const field of ["scope_in", "scope_out", "requirement_ids"]) {
    if (!taskControlSortedUnique(value[field])) {
      fail("CHARTER_SET_ORDER_INVALID", `$.${field}`);
    }
  }
  if (!taskControlSortedUnique(value.delivery_plan.required_outputs)) {
    fail("CHARTER_DELIVERY_ORDER_INVALID", "$.delivery_plan.required_outputs");
  }
  const predecessor = value.previous_charter_ref;
  if (!((value.charter_version === 1 && predecessor === null) ||
        (value.charter_version > 1 && predecessor !== null))) {
    fail("CHARTER_PREDECESSOR_INVALID", "$.previous_charter_ref");
  }
}

function taskControlRuleWaiverTime(value) {
  if (value.issued_at >= value.expires_at) {
    throw new ContractValidationError(
      "WAIVER_INVALID", "WAIVER_TIME_RANGE_INVALID", "$",
    );
  }
}

const TASK_CONTROL_ATTEMPT_TERMINAL = new Set([
  "SUCCEEDED", "SUSPENDED", "FAILED", "CANCELLED", "FENCED",
]);

function taskControlRuleAttemptNullability(value) {
  const terminal = TASK_CONTROL_ATTEMPT_TERMINAL.has(value.state);
  if ((value.ended_at !== null) !== terminal ||
      (value.termination_reason !== null) !== terminal) {
    fail("ATTEMPT_STATE_NULLABILITY_INVALID");
  }
}

function taskControlRuleAttemptTransport(value) {
  const binding = value.transport_binding;
  const present = ["outer_job_id", "run_id", "lease_id", "transport_epoch"]
    .map((field) => binding[field] !== null);
  if (!(present.every(Boolean) || present.every((item) => !item))) {
    fail("ATTEMPT_TRANSPORT_BINDING_INVALID", "$.transport_binding");
  }
}

function taskControlRuleFencedReason(value) {
  if (value.state === "FENCED" && value.termination_reason !== "OWNERSHIP_LOST") {
    fail("FENCED_REASON_INVALID", "$.termination_reason");
  }
}

function taskControlRuleOwnerNullability(value) {
  const terminal = ["CLOSED", "FENCED"].includes(value.state);
  if ((value.ended_at !== null) !== terminal ||
      (value.termination_reason !== null) !== terminal) {
    fail("OWNER_STATE_NULLABILITY_INVALID");
  }
}

function taskControlRuleRecordHeads(value) {
  if ((value.charter_version === 0) !== (value.charter_ref === null)) {
    fail("TASK_RECORD_CHARTER_HEAD_INVALID", "$.charter_ref");
  }
  if ((value.current_checkpoint_generation === 0) !==
      (value.current_checkpoint_hash === null)) {
    fail("TASK_RECORD_CHECKPOINT_HEAD_INVALID", "$.current_checkpoint_hash");
  }
}

function taskControlRuleRecordTransport(value) {
  const present = ["outer_job_id", "run_id", "lease_id", "transport_epoch"]
    .map((field) => value[field] !== null);
  if (!(present.every(Boolean) || present.every((item) => !item))) {
    fail("TASK_RECORD_TRANSPORT_BINDING_INVALID");
  }
}

function taskControlRuleRecordTerminal(value) {
  const terminal = value.lifecycle === "TERMINAL";
  const values = [
    "terminal_kind", "result_ref", "result_digest", "outcome", "terminal_at",
  ].map((field) => value[field]);
  if (terminal ? values.some((item) => item === null) :
      values.some((item) => item !== null)) {
    fail("TASK_RECORD_TERMINAL_RESULT_INVALID");
  }
}
'''


__all__ = ["NPM_TASK_CONTROL_RULES"]
