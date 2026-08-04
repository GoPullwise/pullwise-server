"""Deterministic Node release-gate raw sample semantics."""

from __future__ import annotations


NPM_RELEASE_GATE_SAMPLE_SET = r'''
const RELEASE_SAMPLE_TASK_FIELDS = Object.freeze([
  "task_id", "task_kind", "unknown_family_id", "cluster_id",
  "case_category", "criticality", "profile_id",
  "oracle_in_scope_finding_count", "expected_failure_outcome",
]);

function releaseSampleTaskProjection(sample) {
  return Object.fromEntries(
    RELEASE_SAMPLE_TASK_FIELDS.map((field) => [field, sample[field]]),
  );
}

function ruleReleaseGateSampleSet(value) {
  const samples = value.samples;
  releaseRequire(
    releaseSortedUnique(
      samples,
      (item) => [
        item.cohort,
        item.task_id,
        item.seed.toString().padStart(10, "0"),
      ].join("\0"),
    ),
    "RELEASE_SAMPLE_ORDER_INVALID",
    "$.samples",
  );
  const expectedCohorts = value.release_mode === "BOOTSTRAP"
    ? ["CANDIDATE"] : ["CANDIDATE", "STABLE"];
  releaseRequire(
    releaseSame([...new Set(samples.map((item) => item.cohort))].sort(), expectedCohorts),
    "RELEASE_SAMPLE_MODE_INVALID",
    "$.samples",
  );
  const tasks = new Map();
  samples.forEach((sample, index) => {
    releaseRequire(
      (sample.task_kind === "KNOWN") ===
        (sample.unknown_family_id === null),
      "RELEASE_SAMPLE_TASK_KIND_INVALID",
      "$.samples[" + index + "]",
    );
    const classificationTask = sample.case_category ===
      "ENVIRONMENT_OR_CAPABILITY_FAILURE";
    releaseRequire(
      classificationTask === (sample.expected_failure_outcome !== null),
      "RELEASE_SAMPLE_EXPECTED_OUTCOME_INVALID",
      "$.samples[" + index + "]",
    );
    if (sample.disposition === "INCLUDED") {
      const complete = sample.evidence_issue_codes.length === 0;
      releaseRequire(
        (sample.observation !== null) === complete,
        "RELEASE_SAMPLE_EVIDENCE_INVALID",
        "$.samples[" + index + "]",
      );
    }
    if (sample.observation !== null) {
      const observation = sample.observation;
      const bounds = [
        [
          observation.reported_oracle_in_scope_finding_count,
          sample.oracle_in_scope_finding_count,
        ],
        [
          observation.covered_mandatory_requirement_count,
          observation.mandatory_requirement_count,
        ],
        [
          observation.covered_source_state_proof_count,
          observation.source_state_proof_count,
        ],
      ];
      releaseRequire(
        bounds.every(([numerator, denominator]) => numerator <= denominator),
        "RELEASE_SAMPLE_OBSERVATION_INVALID",
        "$.samples[" + index + "].observation",
      );
    }
    const identity = Object.fromEntries(
      ["cohort", "task_id", "seed"].map((field) => [field, sample[field]]),
    );
    const expectedId = "sample_" + releaseProjectionDigest(
      "pullwise:release-gate-sample-identity:v1",
      identity,
    );
    releaseRequire(
      sample.sample_id === expectedId,
      "RELEASE_SAMPLE_ID_INVALID",
      "$.samples[" + index + "].sample_id",
    );
    const task = releaseSampleTaskProjection(sample);
    const previous = tasks.get(sample.task_id);
    releaseRequire(
      previous === undefined || releaseSame(previous, task),
      "RELEASE_SAMPLE_TASK_DRIFT",
      "$.samples[" + index + "]",
    );
    tasks.set(sample.task_id, task);
  });
  const candidateTaskIds = [...new Set(
    samples
      .filter((item) => item.cohort === "CANDIDATE")
      .map((item) => item.task_id),
  )].sort();
  const taskInventory = candidateTaskIds.map((taskId) => tasks.get(taskId));
  releaseRequire(
    value.task_inventory_digest === releaseProjectionDigest(
      "pullwise:release-gate-task-inventory:v1",
      taskInventory,
    ),
    "RELEASE_SAMPLE_TASK_INVENTORY_INVALID",
    "$.task_inventory_digest",
  );
}
'''


__all__ = ["NPM_RELEASE_GATE_SAMPLE_SET"]
