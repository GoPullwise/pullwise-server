"""Deterministic Node release-gate evaluator semantics."""

from __future__ import annotations


NPM_RELEASE_GATE_EVALUATOR = r'''
function releaseCompare(comparator, observed, threshold) {
  return {
    EQ: observed === threshold,
    GTE: observed >= threshold,
    LT: observed < threshold,
    LTE: observed <= threshold,
  }[comparator];
}

function releaseValidateAbsoluteResults(value) {
  value.absolute_results.forEach((item, index) => {
    if (item.status === "INDETERMINATE") return;
    const expected = releaseCompare(
      item.comparator,
      item.observed_value,
      item.threshold,
    ) ? "PASS" : "FAIL";
    releaseRequire(
      item.status === expected,
      "RELEASE_EVALUATOR_STATUS_INVALID",
      `$.absolute_results[${index}].status`,
    );
  });
}

function releaseValidateProfileResults(report, policy) {
  report.profile_results.forEach((result, index) => {
    if (result.status === "INDETERMINATE") return;
    const budget = policy.profile_budgets[index];
    const passed = result.wall_ms <= budget.wall_ms &&
      result.token_count <= budget.token_limit &&
      result.cost_microusd <= budget.cost_microusd;
    releaseRequire(
      result.status === (passed ? "PASS" : "FAIL"),
      "RELEASE_EVALUATOR_STATUS_INVALID",
      `$.profile_results[${index}].status`,
    );
  });
}

export async function evaluateReleaseGate(benchmarkBundle, policy, report) {
  const checked = await verifyReleaseGateReportContext(
    report,
    benchmarkBundle,
    policy,
  );
  return {verdict: checked.verdict, exit_code: checked.exit_code};
}

export const evaluate_release_gate = evaluateReleaseGate;
'''


__all__ = ["NPM_RELEASE_GATE_EVALUATOR"]
