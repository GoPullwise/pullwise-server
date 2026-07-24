"""Node facade semantics for release-gate evidence documents."""

from __future__ import annotations


NPM_RELEASE_GATE = r'''
const RELEASE_GATE_PUBLIC_CODE = "CONTRACT_DOCUMENT_INVALID";
const RELEASE_CANARY_STAGE_IDS = Object.freeze([
  "CAPACITY_5", "CAPACITY_25", "FULL_CAPACITY",
]);
const RELEASE_ATTESTATION_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
const RELEASE_POLICY_BENCHMARK_FIELDS = Object.freeze([
  "benchmark_version", "task_inventory_digest", "oracle_rubric_digest",
  "environment_image_digest", "control_plane_digest",
  "evaluation_runtime_digest", "statistical_implementation_version",
]);
const RELEASE_REPORT_POLICY_FIELDS = Object.freeze([
  "package", "candidate_build_id", "candidate_digest", "release_mode",
  "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
  "benchmark_digest", "benchmark_version", "task_inventory_digest",
  "oracle_rubric_digest", "environment_image_digest", "control_plane_digest",
  "evaluation_runtime_digest", "statistical_implementation_version",
  "threshold_table_digest", "profile_budget_digest", "canary_plan_digest",
]);
const RELEASE_ATTESTATION_IDENTITY_FIELDS = Object.freeze([
  "package", "candidate_build_id", "candidate_digest", "release_mode",
  "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
]);

function releaseRequire(condition, detail, path = "$") {
  if (!condition) fail(detail, path, RELEASE_GATE_PUBLIC_CODE);
}

function releaseSortedUnique(values, key = (item) => item) {
  if (!Array.isArray(values)) return false;
  const keys = values.map(key);
  return new Set(keys).size === keys.length &&
    keys.every((item, index) => index === 0 || keys[index - 1] < item);
}

function releaseSame(left, right) {
  return canonicalString(left) === canonicalString(right);
}

function releaseProjectionDigest(domain, projection) {
  const prefix = encoder.encode(domain);
  const document = canonicalDocumentBytes(projection);
  const input = new Uint8Array(prefix.length + 1 + document.length);
  input.set(prefix);
  input.set(document, prefix.length + 1);
  return sha256Sync(input);
}

function releaseTimestampMillis(value) {
  if (typeof value !== "string") return null;
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, millis] = match.map(Number);
  const epoch = Date.UTC(year, month - 1, day, hour, minute, second, millis);
  return Number.isFinite(epoch) && new Date(epoch).toISOString() === value
    ? epoch : null;
}

function releaseRequireTimeOrder(value, detail) {
  const issued = releaseTimestampMillis(value.issued_at);
  releaseRequire(issued !== null, detail, "$.issued_at");
  const expires = releaseTimestampMillis(value.expires_at);
  releaseRequire(expires !== null && issued < expires, detail, "$.expires_at");
  return [issued, expires];
}

function ruleBenchmarkBundle(value) {
  releaseRequire(
    releaseSortedUnique(value.seeds),
    "RELEASE_BENCHMARK_ORDER_INVALID",
    "$.seeds",
  );
  releaseRequire(
    releaseSortedUnique(value.unknown_families, (item) => item.family_id),
    "RELEASE_BENCHMARK_ORDER_INVALID",
    "$.unknown_families",
  );
  releaseRequire(
    releaseSortedUnique(value.core_cluster_ids),
    "RELEASE_BENCHMARK_ORDER_INVALID",
    "$.core_cluster_ids",
  );
  releaseRequire(
    releaseSortedUnique(value.cluster_coverage, (item) => item.cluster_id),
    "RELEASE_BENCHMARK_ORDER_INVALID",
    "$.cluster_coverage",
  );
  releaseRequire(
    releaseSame(
      value.core_cluster_ids,
      value.cluster_coverage.map((item) => item.cluster_id),
    ),
    "RELEASE_BENCHMARK_COVERAGE_INVALID",
    "$.cluster_coverage",
  );
  releaseRequireTimeOrder(value, "RELEASE_BENCHMARK_TIME_INVALID");
}

function ruleReleaseGatePolicy(value) {
  for (const [field, identity] of [
    ["absolute_gates", "gate_id"],
    ["relative_gates", "gate_id"],
    ["profile_budgets", "profile_id"],
    ["infrastructure_reason_codes", null],
  ]) {
    releaseRequire(
      releaseSortedUnique(
        value[field],
        identity === null ? (item) => item : (item) => item[identity],
      ),
      "RELEASE_POLICY_ORDER_INVALID",
      "$." + field,
    );
  }
  releaseRequire(
    releaseSame(
      value.canary_stages.map((item) => item.stage_id),
      RELEASE_CANARY_STAGE_IDS,
    ),
    "RELEASE_POLICY_CANARY_INVALID",
    "$.canary_stages",
  );
  const expectedApplicability = value.release_mode === "BOOTSTRAP"
    ? "NOT_APPLICABLE" : "REQUIRED";
  const stableFields = [
    value.stable_package,
    value.stable_candidate_digest,
    value.stable_control_plane_digest,
  ];
  const stableShapeValid = value.release_mode === "BOOTSTRAP"
    ? stableFields.every((item) => item === null)
    : stableFields.every((item) => item !== null);
  releaseRequire(
    stableShapeValid,
    "RELEASE_POLICY_MODE_INVALID",
    "$.stable_package",
  );
  releaseRequire(
    (value.release_mode === "BOOTSTRAP" || value.release_mode === "STABLE") &&
      value.relative_gates.every(
        (item) => item.applicability === expectedApplicability,
      ),
    "RELEASE_POLICY_MODE_INVALID",
    "$.relative_gates",
  );
  releaseRequireTimeOrder(value, "RELEASE_POLICY_TIME_INVALID");

  const thresholdProjection = {
    absolute_gates: value.absolute_gates,
    relative_gates: value.relative_gates,
    infrastructure_reason_codes: value.infrastructure_reason_codes,
  };
  releaseRequire(
    value.threshold_table_digest === releaseProjectionDigest(
      "pullwise:release-threshold-table:v1",
      thresholdProjection,
    ),
    "RELEASE_POLICY_THRESHOLD_DIGEST_INVALID",
    "$.threshold_table_digest",
  );
  releaseRequire(
    value.profile_budget_digest === releaseProjectionDigest(
      "pullwise:release-profile-budgets:v1",
      value.profile_budgets,
    ),
    "RELEASE_POLICY_PROFILE_DIGEST_INVALID",
    "$.profile_budget_digest",
  );
  const canaryProjection = {
    canary_stages: value.canary_stages,
    canary_platform_failure_rate_max_bps:
      value.canary_platform_failure_rate_max_bps,
    canary_relative_platform_failure_increase_max_bps:
      value.canary_relative_platform_failure_increase_max_bps,
    canary_p95_wall_time_increase_max_bps:
      value.canary_p95_wall_time_increase_max_bps,
    canary_p95_cost_increase_max_bps:
      value.canary_p95_cost_increase_max_bps,
  };
  releaseRequire(
    value.canary_plan_digest === releaseProjectionDigest(
      "pullwise:release-canary-plan:v1",
      canaryProjection,
    ),
    "RELEASE_POLICY_CANARY_DIGEST_INVALID",
    "$.canary_plan_digest",
  );
  const candidateProjection = {
    package: value.package,
    candidate_build_id: value.candidate_build_id,
    control_plane_digest: value.control_plane_digest,
    evaluation_runtime_digest: value.evaluation_runtime_digest,
    benchmark_ref: value.benchmark_ref,
    benchmark_digest: value.benchmark_digest,
    threshold_table_digest: value.threshold_table_digest,
    profile_budget_digest: value.profile_budget_digest,
    canary_plan_digest: value.canary_plan_digest,
  };
  releaseRequire(
    value.candidate_digest === releaseProjectionDigest(
      "pullwise:candidate-digest:v1",
      candidateProjection,
    ),
    "RELEASE_POLICY_CANDIDATE_DIGEST_INVALID",
    "$.candidate_digest",
  );
}

function releaseReportVerdict(value) {
  const statuses = [
    ...value.absolute_results.map((item) => item.status),
    ...value.relative_results
      .map((item) => item.status)
      .filter((item) => item !== "NOT_APPLICABLE"),
    ...value.profile_results.map((item) => item.status),
  ];
  if (statuses.includes("FAIL")) return "FAIL";
  if (statuses.includes("INDETERMINATE")) return "INDETERMINATE";
  return "PASS";
}

function ruleReleaseGateReport(value) {
  releaseValidateAbsoluteResults(value);
  releaseRequire(
    value.raw_sample_count ===
      value.valid_sample_count + value.excluded_sample_count,
    "RELEASE_REPORT_SAMPLE_INVALID",
    "$.raw_sample_count",
  );
  releaseRequire(
    value.excluded_sample_count === value.excluded_reason_counts.reduce(
      (total, item) => total + item.count,
      0,
    ),
    "RELEASE_REPORT_SAMPLE_INVALID",
    "$.excluded_sample_count",
  );
  for (const [field, identity] of [
    ["excluded_reason_counts", "reason_code"],
    ["absolute_results", "gate_id"],
    ["relative_results", "gate_id"],
    ["profile_results", "profile_id"],
  ]) {
    releaseRequire(
      releaseSortedUnique(value[field], (item) => item[identity]),
      "RELEASE_REPORT_ORDER_INVALID",
      "$." + field,
    );
  }
  const modeValid = value.release_mode === "BOOTSTRAP"
    ? value.relative_results.every(
      (item) => item.applicability === "NOT_APPLICABLE" &&
        item.observed_regression_bps === null &&
        item.status === "NOT_APPLICABLE",
    )
    : value.release_mode === "STABLE" && value.relative_results.every(
      (item) => item.applicability === "REQUIRED" &&
        item.status !== "NOT_APPLICABLE" &&
        (
          item.status === "INDETERMINATE" ||
          item.observed_regression_bps !== null
        ),
    );
  releaseRequire(
    modeValid,
    "RELEASE_REPORT_MODE_INVALID",
    "$.relative_results",
  );
  const expectedVerdict = releaseReportVerdict(value);
  releaseRequire(
    value.verdict === expectedVerdict,
    "RELEASE_REPORT_VERDICT_INVALID",
    "$.verdict",
  );
  const expectedExitCode = {PASS: 0, FAIL: 1, INDETERMINATE: 2}[expectedVerdict];
  releaseRequire(
    value.exit_code === expectedExitCode,
    "RELEASE_REPORT_VERDICT_INVALID",
    "$.exit_code",
  );
}

function ruleReleaseGateAttestation(value) {
  releaseRequire(
    value.attested_verdict === "PASS",
    "RELEASE_ATTESTATION_VERDICT_INVALID",
    "$.attested_verdict",
  );
  releaseRequire(
    value.attested_exit_code === 0,
    "RELEASE_ATTESTATION_VERDICT_INVALID",
    "$.attested_exit_code",
  );
  const [issued, expires] = releaseRequireTimeOrder(
    value,
    "RELEASE_ATTESTATION_WINDOW_INVALID",
  );
  releaseRequire(
    expires - issued <= RELEASE_ATTESTATION_MAX_WINDOW_MS,
    "RELEASE_ATTESTATION_WINDOW_INVALID",
    "$.expires_at",
  );
}

function releaseRequireBinding(left, right, fields, detail, prefix = "$") {
  for (const field of fields) {
    releaseRequire(
      releaseSame(left[field], right[field]),
      detail,
      prefix + "." + field,
    );
  }
}

function releaseRequireRef(ref, schemaId, document, detail, path) {
  releaseRequire(seoRefMatchesDocument(ref, schemaId, document), detail, path);
}

function releaseComparisonPasses(comparator, observed, threshold) {
  if (comparator === "EQ") return observed === threshold;
  if (comparator === "GTE") return observed >= threshold;
  if (comparator === "LT") return observed < threshold;
  return observed <= threshold;
}

function releaseVerifyPolicyBenchmarkBinding(policy, benchmarkBundle) {
  releaseRequireRef(
    policy.benchmark_ref,
    "benchmark-bundle/v1",
    benchmarkBundle,
    "RELEASE_POLICY_BENCHMARK_REF_INVALID",
    "$.benchmark_ref",
  );
  releaseRequire(
    releaseSame(policy.package, benchmarkBundle.package),
    "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
    "$.package",
  );
  releaseRequire(
    policy.benchmark_digest === benchmarkBundle.bundle_digest,
    "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
    "$.benchmark_digest",
  );
  releaseRequireBinding(
    policy,
    benchmarkBundle,
    RELEASE_POLICY_BENCHMARK_FIELDS,
    "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
  );
}

export async function verifyReleaseGatePolicyContext(policy, benchmarkBundle) {
  const checked = await verifyDocumentDigest("release-gate-policy/v1", policy);
  const benchmark = await verifyDocumentDigest(
    "benchmark-bundle/v1",
    benchmarkBundle,
  );
  releaseVerifyPolicyBenchmarkBinding(checked, benchmark);
  return checked;
}

export async function verifyReleaseGateReportContext(
  report, benchmarkBundle, policy,
) {
  const checked = await verifyDocumentDigest("release-gate-report/v1", report);
  const benchmark = await verifyDocumentDigest(
    "benchmark-bundle/v1",
    benchmarkBundle,
  );
  const policyValue = await verifyDocumentDigest(
    "release-gate-policy/v1",
    policy,
  );
  releaseRequire(
    seoRefMatchesDocument(
      checked.benchmark_ref,
      "benchmark-bundle/v1",
      benchmark,
    ),
    "RELEASE_REPORT_REF_INVALID",
    "$.benchmark_ref",
  );
  releaseRequire(
    seoRefMatchesDocument(
      checked.policy_ref,
      "release-gate-policy/v1",
      policyValue,
    ),
    "RELEASE_REPORT_REF_INVALID",
    "$.policy_ref",
  );
  releaseRequireBinding(
    checked,
    policyValue,
    [
      "package",
      "candidate_build_id",
      "candidate_digest",
      "release_mode",
      "stable_package",
      "stable_candidate_digest",
      "stable_control_plane_digest",
      "benchmark_ref",
      "benchmark_digest",
      "benchmark_version",
      "task_inventory_digest",
      "oracle_rubric_digest",
      "environment_image_digest",
      "control_plane_digest",
      "evaluation_runtime_digest",
      "statistical_implementation_version",
      "threshold_table_digest",
      "profile_budget_digest",
      "canary_plan_digest",
    ],
    "RELEASE_REPORT_BINDING_INVALID",
  );
  releaseRequireBinding(
    checked,
    benchmark,
    [
      "package",
      "benchmark_version",
      "task_inventory_digest",
      "oracle_rubric_digest",
      "environment_image_digest",
      "control_plane_digest",
      "evaluation_runtime_digest",
      "statistical_implementation_version",
    ],
    "RELEASE_REPORT_BINDING_INVALID",
  );
  releaseRequire(
    checked.benchmark_digest === benchmark.bundle_digest,
    "RELEASE_REPORT_BINDING_INVALID",
    "$.benchmark_digest",
  );
  releaseRequire(
    checked.policy_digest === policyValue.policy_digest,
    "RELEASE_REPORT_BINDING_INVALID",
    "$.policy_digest",
  );
  const expectedAbsolute = policyValue.absolute_gates.map((item) => ({
    gate_id: item.gate_id,
    comparator: item.comparator,
    threshold: item.threshold,
  }));
  const actualAbsolute = checked.absolute_results.map((item) => ({
    gate_id: item.gate_id,
    comparator: item.comparator,
    threshold: item.threshold,
  }));
  const expectedRelative = policyValue.relative_gates.map((item) => ({
    gate_id: item.gate_id,
    applicability: item.applicability,
    max_regression_bps: item.max_regression_bps,
  }));
  const actualRelative = checked.relative_results.map((item) => ({
    gate_id: item.gate_id,
    applicability: item.applicability,
    max_regression_bps: item.max_regression_bps,
  }));
  releaseRequire(
    releaseSame(actualAbsolute, expectedAbsolute),
    "RELEASE_REPORT_POLICY_TABLE_INVALID",
    "$.absolute_results",
  );
  releaseRequire(
    releaseSame(actualRelative, expectedRelative),
    "RELEASE_REPORT_POLICY_TABLE_INVALID",
    "$.relative_results",
  );
  releaseRequire(
    releaseSame(
      checked.profile_results.map((item) => item.profile_id),
      policyValue.profile_budgets.map((item) => item.profile_id),
    ),
    "RELEASE_REPORT_POLICY_TABLE_INVALID",
    "$.profile_results",
  );
  releaseValidateProfileResults(checked, policyValue);
  releaseRequire(
    checked.excluded_reason_counts.every(
      (item) => policyValue.infrastructure_reason_codes.includes(item.reason_code),
    ),
    "RELEASE_REPORT_POLICY_TABLE_INVALID",
    "$.excluded_reason_counts",
  );
  return checked;
}

export async function verifyReleaseGateAttestationContext(
  attestation, policy, report,
) {
  const checked = await verifyDocumentDigest(
    "release-gate-attestation/v1",
    attestation,
  );
  const policyValue = await verifyDocumentDigest(
    "release-gate-policy/v1",
    policy,
  );
  const reportValue = await verifyDocumentDigest(
    "release-gate-report/v1",
    report,
  );
  releaseRequire(
    seoRefMatchesDocument(
      checked.policy_ref,
      "release-gate-policy/v1",
      policyValue,
    ),
    "RELEASE_ATTESTATION_REF_INVALID",
    "$.policy_ref",
  );
  releaseRequire(
    seoRefMatchesDocument(
      checked.report_ref,
      "release-gate-report/v1",
      reportValue,
    ),
    "RELEASE_ATTESTATION_REF_INVALID",
    "$.report_ref",
  );
  releaseRequire(
    seoRefMatchesDocument(
      reportValue.policy_ref,
      "release-gate-policy/v1",
      policyValue,
    ),
    "RELEASE_ATTESTATION_REF_INVALID",
    "$.report.policy_ref",
  );
  releaseRequire(
    reportValue.policy_digest === policyValue.policy_digest,
    "RELEASE_ATTESTATION_BINDING_INVALID",
    "$.report.policy_digest",
  );
  releaseRequireBinding(
    reportValue,
    policyValue,
    RELEASE_REPORT_POLICY_FIELDS,
    "RELEASE_ATTESTATION_BINDING_INVALID",
    "$.report",
  );
  releaseRequireBinding(
    checked,
    policyValue,
    [
      "package",
      "candidate_build_id",
      "candidate_digest",
      "release_mode",
      "stable_package",
      "stable_candidate_digest",
      "stable_control_plane_digest",
      "policy_id",
      "policy_digest",
    ],
    "RELEASE_ATTESTATION_BINDING_INVALID",
  );
  releaseRequireBinding(
    checked,
    reportValue,
    [
      "package",
      "candidate_build_id",
      "candidate_digest",
      "release_mode",
      "stable_package",
      "stable_candidate_digest",
      "stable_control_plane_digest",
      "policy_digest",
      "report_id",
      "report_digest",
    ],
    "RELEASE_ATTESTATION_BINDING_INVALID",
  );
  releaseRequire(
    reportValue.verdict === "PASS" && reportValue.exit_code === 0,
    "RELEASE_ATTESTATION_REPORT_NOT_PASS",
    "$.report.verdict",
  );
  releaseRequire(
    checked.attested_verdict === reportValue.verdict &&
      checked.attested_exit_code === reportValue.exit_code,
    "RELEASE_ATTESTATION_BINDING_INVALID",
    "$.attested_verdict",
  );
  return checked;
}
'''


__all__ = ["NPM_RELEASE_GATE"]
