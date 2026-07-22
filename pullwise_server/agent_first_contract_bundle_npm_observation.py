"""Generated npm facade semantics for task observations."""

from __future__ import annotations


NPM_OBSERVATION_RULE = r'''
function observationTimestampMillis(value) {
  if (typeof value !== "string" ||
      !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$/.test(value) ||
      Number(value.slice(0, 4)) === 0) {
    return null;
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  return new Date(timestamp).toISOString() === value ? timestamp : null;
}

function ruleObservationActor(value) {
  const sessionKinds = new Set([
    "task_owner", "quality_verifier", "domain_reviewer",
    "explorer", "troubleshooter", "implementer",
  ]);
  if (sessionKinds.has(value.kind)) {
    if (typeof value.session_id !== "string" ||
        !/^sess_[0-9a-f]{32}$/.test(value.session_id)) {
      fail("ACTOR_SESSION_INVALID");
    }
  } else if (value.session_id !== null) {
    fail("ACTOR_SESSION_INVALID");
  }
}

function ruleObservation(value) {
  ruleObservationActor(value.actor);
  const status = value.status;
  const started = observationTimestampMillis(value.started_at);
  const completed = observationTimestampMillis(value.completed_at);
  if (status === "policy_denied") {
    if (!["started_at", "completed_at", "duration_ms", "exit_code"].every(
      (key) => value[key] === null,
    )) {
      fail("OBSERVATION_STATUS_MATRIX_INVALID");
    }
    if (value.result_ref.availability !== "available" ||
        value.result_ref.ref.content_schema_id !== "error-response/v1" ||
        value.source_state_before_id !== value.source_state_after_id ||
        value.execution_state_id !== null) {
      fail("OBSERVATION_STATUS_MATRIX_INVALID");
    }
  } else {
    if (started === null || completed === null || completed < started ||
        value.duration_ms !== completed - started) {
      fail("OBSERVATION_TIME_INVALID");
    }
    if (status === "succeeded" && value.result_ref.availability !== "available") {
      fail("OBSERVATION_RESULT_REQUIRED");
    }
  }
  if (value.partial_side_effect !== false) {
    fail("OBSERVATION_PARTIAL_SIDE_EFFECT");
  }
}
'''


__all__ = ["NPM_OBSERVATION_RULE"]
