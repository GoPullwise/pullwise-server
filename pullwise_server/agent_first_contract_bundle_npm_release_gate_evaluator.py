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

function releaseValidateIndeterminateShape(value) {
  const reasons = value.indeterminate_reason_codes;
  releaseRequire(
    releaseSortedUnique(reasons),
    "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
    "$.indeterminate_reason_codes",
  );
  const results = [
    ...value.absolute_results,
    ...value.relative_results,
    ...value.profile_results,
  ];
  releaseRequire(
    Boolean(reasons.length) === results.some(
      (item) => item.status === "INDETERMINATE",
    ),
    "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
    "$.indeterminate_reason_codes",
  );
  value.absolute_results.forEach((item, index) => {
    releaseRequire(
      (item.observed_value === null) === (item.status === "INDETERMINATE"),
      "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
      `$.absolute_results[${index}]`,
    );
  });
  value.relative_results.forEach((item, index) => {
    const missing = item.observed_regression_bps === null;
    const expectedMissing = ["INDETERMINATE", "NOT_APPLICABLE"]
      .includes(item.status);
    releaseRequire(
      missing === expectedMissing,
      "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
      `$.relative_results[${index}]`,
    );
  });
  value.profile_results.forEach((item, index) => {
    const measurements = [item.wall_ms, item.token_count, item.cost_microusd];
    const valid = item.status === "INDETERMINATE"
      ? measurements.every((value) => value === null)
      : measurements.every((value) => value !== null);
    releaseRequire(
      valid,
      "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
      `$.profile_results[${index}]`,
    );
  });
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

function releaseValidateRelativeResults(value) {
  value.relative_results.forEach((item, index) => {
    if (["INDETERMINATE", "NOT_APPLICABLE"].includes(item.status)) return;
    const expected = item.observed_regression_bps <= item.max_regression_bps
      ? "PASS" : "FAIL";
    releaseRequire(
      item.status === expected,
      "RELEASE_EVALUATOR_STATUS_INVALID",
      `$.relative_results[${index}].status`,
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

function releaseValidateSampleInventory(report, benchmark) {
  const taskCount = benchmark.known_gold_task_count +
    benchmark.unknown_families.reduce(
      (total, item) => total + item.task_count,
      0,
    );
  const expected = taskCount * benchmark.repeats_per_task;
  const reasons = new Set(report.indeterminate_reason_codes);
  const valid = reasons.has("SAMPLE_INSUFFICIENT") ===
      (report.raw_sample_count !== expected) &&
    reasons.has("ZERO_DENOMINATOR") === (report.valid_sample_count === 0);
  releaseRequire(
    valid,
    "RELEASE_EVALUATOR_SAMPLE_INVALID",
    "$.indeterminate_reason_codes",
  );
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
