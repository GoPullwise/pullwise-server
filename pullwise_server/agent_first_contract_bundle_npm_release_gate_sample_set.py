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
    if (sample.disposition === "INCLUDED") {
      const complete = sample.evidence_issue_codes.length === 0;
      releaseRequire(
        (sample.observation !== null) === complete,
        "RELEASE_SAMPLE_EVIDENCE_INVALID",
        "$.samples[" + index + "]",
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
