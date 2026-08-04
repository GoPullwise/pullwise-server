"""Deterministic Node release-gate evaluator semantics."""

from __future__ import annotations


NPM_RELEASE_GATE_EVALUATOR = r'''
export async function deriveReleaseGateEvaluation(
  benchmarkBundle,
  policy,
  sampleSet,
) {
  const checkedBenchmark = await verifyDocumentDigest(
    "benchmark-bundle/v1", benchmarkBundle,
  );
  const checkedPolicy = await verifyDocumentDigest(
    "release-gate-policy/v1", policy,
  );
  const checkedSampleSet = await verifyDocumentDigest(
    "release-gate-sample-set/v1", sampleSet,
  );
  releaseRequire(
    checkedPolicy.organization_id === checkedBenchmark.organization_id,
    "RELEASE_POLICY_ORGANIZATION_MISMATCH",
    "$.organization_id",
  );
  verifyReleaseGatePolicyBinding(checkedPolicy, checkedBenchmark);
  releaseRequireRef(
    checkedSampleSet.benchmark_ref,
    "benchmark-bundle/v1",
    checkedBenchmark,
    "RELEASE_SAMPLE_REF_INVALID",
    "$.benchmark_ref",
  );
  releaseRequireRef(
    checkedSampleSet.policy_ref,
    "release-gate-policy/v1",
    checkedPolicy,
    "RELEASE_SAMPLE_REF_INVALID",
    "$.policy_ref",
  );
  releaseRequireBindings(
    checkedSampleSet,
    checkedBenchmark,
    ["package", ...RELEASE_POLICY_BENCHMARK_FIELDS],
    "RELEASE_SAMPLE_BENCHMARK_BINDING_INVALID",
  );
  releaseRequire(
    checkedSampleSet.benchmark_digest === checkedBenchmark.bundle_digest,
    "RELEASE_SAMPLE_BENCHMARK_BINDING_INVALID",
    "$.benchmark_digest",
  );
  const candidateSamples = checkedSampleSet.samples.filter(
    (item) => item.cohort === "CANDIDATE",
  );
  const excluded = candidateSamples.filter(
    (item) => item.disposition === "EXCLUDED",
  );
  const reasonCounts = new Map();
  for (const item of excluded) {
    const reason = item.infrastructure_reason_code;
    reasonCounts.set(reason, (reasonCounts.get(reason) ?? 0) + 1);
  }
  return {
    raw_sample_count: candidateSamples.length,
    valid_sample_count: candidateSamples.length - excluded.length,
    excluded_sample_count: excluded.length,
    excluded_reason_counts: [...reasonCounts].sort(
      ([left], [right]) => left < right ? -1 : left > right ? 1 : 0,
    ).map(([reason_code, count]) => ({reason_code, count})),
  };
}

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
export const derive_release_gate_evaluation = deriveReleaseGateEvaluation;
'''


__all__ = ["NPM_RELEASE_GATE_EVALUATOR"]
