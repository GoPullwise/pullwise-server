"""Generated Node document rules for EffectiveExecutionPolicy."""

from __future__ import annotations


NPM_EFFECTIVE_POLICY_RULES = r'''
function rulePolicyCapabilitySets(value) {
  const granted = value.granted_capabilities;
  const deniedIds = value.denied_capabilities.map((item) => item.id);
  if (!sortedUniqueStrings(granted, true) ||
      !sortedUniqueStrings(deniedIds, true)) {
    fail("POLICY_CAPABILITY_ORDER_INVALID");
  }
  if (granted.some((item) => deniedIds.includes(item))) {
    fail("POLICY_CAPABILITY_OVERLAP");
  }
}

function rulePolicyRootsAndOrigins(value) {
  if (!sortedUniqueStrings(value.allowed_read_roots, true) ||
      !sortedUniqueStrings(value.allowed_write_roots, true)) {
    fail("POLICY_ROOT_ORDER_INVALID");
  }
  if (!sortedUniqueStrings(value.agent_tool_network.origins, true)) {
    fail("POLICY_ORIGIN_ORDER_INVALID");
  }
}

function rulePolicyRiskCeiling(value) {
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

function rulePolicyBudgetCeilings(value) {
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

function rulePolicyDigest(value) {
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "digest"),
  );
  const domain = encoder.encode("pullwise:effective-execution-policy/v1");
  const document = canonicalDocumentBytes(unsigned);
  const input = new Uint8Array(domain.length + 1 + document.length);
  input.set(domain);
  input.set(document, domain.length + 1);
  if (sha256Sync(input) !== value.digest) {
    fail("CONTRACT_DIGEST_MISMATCH", "$.digest");
  }
}
'''


__all__ = ["NPM_EFFECTIVE_POLICY_RULES"]
