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
    checkedPolicy,
    [
      "package", "candidate_build_id", "candidate_digest",
      "release_mode", "stable_package", "stable_candidate_digest",
      "stable_control_plane_digest", "benchmark_ref",
      "benchmark_digest", ...RELEASE_POLICY_BENCHMARK_FIELDS,
      "organization_id",
    ],
    "RELEASE_SAMPLE_POLICY_BINDING_INVALID",
  );
  releaseRequire(
    checkedSampleSet.policy_digest === checkedPolicy.policy_digest,
    "RELEASE_SAMPLE_POLICY_BINDING_INVALID",
    "$.policy_digest",
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
  releaseRequire(
    excluded.every(
      (item) => checkedPolicy.infrastructure_reason_codes.includes(
        item.infrastructure_reason_code,
      ),
    ),
    "RELEASE_SAMPLE_EXCLUSION_REASON_INVALID",
    "$.samples",
  );
  const reasonCounts = new Map();
  for (const item of excluded) {
    const reason = item.infrastructure_reason_code;
    reasonCounts.set(reason, (reasonCounts.get(reason) ?? 0) + 1);
  }
  const reasons = new Set(
    checkedSampleSet.samples.flatMap((item) => item.evidence_issue_codes),
  );
  if (!releaseInventoryComplete(
    checkedSampleSet.samples,
    checkedBenchmark,
    "CANDIDATE",
  )) reasons.add("SAMPLE_INSUFFICIENT");
  const candidateMetrics = releaseCohortMetrics(
    checkedSampleSet.samples,
    "CANDIDATE",
  );
  let stableMetrics = null;
  if (checkedPolicy.release_mode === "STABLE") {
    const key = (item) => item.task_id + String.fromCharCode(0) + item.seed;
    const candidateKeys = checkedSampleSet.samples
      .filter((item) => item.cohort === "CANDIDATE")
      .map(key);
    const stableKeys = checkedSampleSet.samples
      .filter((item) => item.cohort === "STABLE")
      .map(key);
    if (
      !releaseSame(candidateKeys, stableKeys) ||
      !releaseInventoryComplete(
        checkedSampleSet.samples,
        checkedBenchmark,
        "STABLE",
      )
    ) reasons.add("BASELINE_INCOMPARABLE");
    stableMetrics = releaseCohortMetrics(
      checkedSampleSet.samples,
      "STABLE",
    );
  }
  const metricNames = checkedPolicy.absolute_gates.map(
    (item) => item.gate_id.replace(/^absolute_/, ""),
  );
  if (
    candidateSamples.length === 0 ||
    metricNames.some((name) => candidateMetrics[name] === null) ||
    checkedPolicy.profile_budgets.some(
      (budget) => !(budget.profile_id in candidateMetrics.profile_maxima),
    )
  ) reasons.add("ZERO_DENOMINATOR");
  const absoluteResults = checkedPolicy.absolute_gates.map((gate, index) => {
    const observed = reasons.size === 0
      ? candidateMetrics[metricNames[index]] : null;
    const status = observed === null
      ? "INDETERMINATE"
      : releaseCompare(gate.comparator, observed, gate.threshold)
        ? "PASS" : "FAIL";
    return {
      gate_id: gate.gate_id,
      comparator: gate.comparator,
      threshold: gate.threshold,
      observed_value: observed,
      status,
    };
  });
  const relativeResults = checkedPolicy.relative_gates.map((gate) => {
    let observed = null;
    let status = "NOT_APPLICABLE";
    if (gate.applicability === "REQUIRED") {
      observed = reasons.size > 0 || stableMetrics === null
        ? null
        : releaseRelativeRegressionBps(
          gate.gate_id,
          candidateMetrics,
          stableMetrics,
        );
      if (observed === null) {
        reasons.add("BASELINE_INCOMPARABLE");
        status = "INDETERMINATE";
      } else {
        status = observed <= gate.max_regression_bps ? "PASS" : "FAIL";
      }
    }
    return {
      gate_id: gate.gate_id,
      applicability: gate.applicability,
      max_regression_bps: gate.max_regression_bps,
      observed_regression_bps: observed,
      status,
    };
  });
  const profileResults = checkedPolicy.profile_budgets.map((budget) => {
    const measurements = candidateMetrics.profile_maxima[budget.profile_id];
    if (reasons.size > 0 || measurements === undefined) {
      return {
        profile_id: budget.profile_id,
        wall_ms: null,
        token_count: null,
        cost_microusd: null,
        status: "INDETERMINATE",
      };
    }
    const passed = measurements.wall_ms <= budget.wall_ms &&
      measurements.token_count <= budget.token_limit &&
      measurements.cost_microusd <= budget.cost_microusd;
    return {
      profile_id: budget.profile_id,
      ...measurements,
      status: passed ? "PASS" : "FAIL",
    };
  });
  const statuses = [
    ...absoluteResults.map((item) => item.status),
    ...relativeResults
      .filter((item) => item.status !== "NOT_APPLICABLE")
      .map((item) => item.status),
    ...profileResults.map((item) => item.status),
  ];
  const verdict = statuses.includes("FAIL")
    ? "FAIL"
    : statuses.includes("INDETERMINATE")
      ? "INDETERMINATE" : "PASS";
  return {
    indeterminate_reason_codes: [...reasons].sort(),
    raw_sample_count: candidateSamples.length,
    valid_sample_count: candidateSamples.length - excluded.length,
    excluded_sample_count: excluded.length,
    excluded_reason_counts: [...reasonCounts].sort(
      ([left], [right]) => left < right ? -1 : left > right ? 1 : 0,
    ).map(([reason_code, count]) => ({reason_code, count})),
    absolute_results: absoluteResults,
    relative_results: relativeResults,
    profile_results: profileResults,
    verdict,
    exit_code: {PASS: 0, FAIL: 1, INDETERMINATE: 2}[verdict],
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
