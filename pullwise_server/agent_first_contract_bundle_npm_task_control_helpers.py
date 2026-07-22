"""Node facade contextual helpers for task-control authority semantics."""

from __future__ import annotations


NPM_TASK_CONTROL_HELPERS = r'''
function taskControlFail(detail, path = "$", code = null) {
  throw new ContractValidationError(publicErrorCode(detail, code), detail, path);
}

export function validateTaskRequestAcceptance(
  request, expectedTaskId = null, expectedTaskType = null,
) {
  const checked = validateDocument("task-request/v1", request);
  if (!["user_control", "server_control"].includes(checked.submitted_by.kind)) {
    taskControlFail("TASK_REQUEST_SUBMITTER_INVALID");
  }
  if (expectedTaskId !== null && checked.task_id !== expectedTaskId) {
    taskControlFail("TASK_REQUEST_TASK_ID_INVALID", "$.task_id");
  }
  if (expectedTaskType !== null && checked.task_type !== expectedTaskType) {
    taskControlFail("TASK_REQUEST_TASK_TYPE_INVALID", "$.task_type");
  }
  const interaction = checked.interaction_policy;
  if (interaction.mode === "unavailable" &&
      (interaction.input_deadline_ms !== 0 ||
       interaction.approval_deadline_ms !== 0)) {
    taskControlFail(
      "TASK_REQUEST_INTERACTION_DEADLINE_INVALID", "$.interaction_policy",
    );
  }
  return checked;
}

export function validateEffectivePolicyDerivation(request, policy) {
  const accepted = validateTaskRequestAcceptance(request);
  const effective = validateDocument("effective-execution-policy/v1", policy);
  if (effective.issuer.kind !== "server_control") {
    taskControlFail(
      "POLICY_ISSUER_INVALID", "$.issuer", "POLICY_INVARIANT_BROKEN",
    );
  }
  if (effective.task_type !== accepted.task_type) {
    taskControlFail("POLICY_TASK_TYPE_DERIVATION_INVALID", "$.task_type");
  }
  if (effective.issued_at < accepted.submitted_at) {
    taskControlFail("POLICY_ISSUED_BEFORE_REQUEST", "$.issued_at");
  }
  const requested = new Set(accepted.requested_capabilities);
  const granted = new Set(effective.granted_capabilities);
  const denied = new Set(effective.denied_capabilities.map((item) => item.id));
  if ([...granted].some((item) => !requested.has(item))) {
    taskControlFail(
      "POLICY_UNREQUESTED_CAPABILITY_GRANTED",
      "$.granted_capabilities",
      "POLICY_INVARIANT_BROKEN",
    );
  }
  if ([...requested].some((item) => !granted.has(item) && !denied.has(item))) {
    taskControlFail(
      "POLICY_REQUESTED_CAPABILITY_UNDECIDED",
      "$",
      "POLICY_INVARIANT_BROKEN",
    );
  }
  for (const [field, ceiling] of Object.entries(accepted.requested_budgets)) {
    if (ceiling !== 0 && effective.budgets[field] > ceiling) {
      taskControlFail(
        "POLICY_REQUEST_BUDGET_EXCEEDED",
        `$.budgets.${field}`,
        "POLICY_INVARIANT_BROKEN",
      );
    }
  }
  if (effective.interaction_mode !== accepted.interaction_policy.mode) {
    taskControlFail(
      "POLICY_INTERACTION_DERIVATION_INVALID",
      "$.interaction_mode",
      "POLICY_INVARIANT_BROKEN",
    );
  }
  return effective;
}

export function validateRequirementEntryIngest(entry, previousLedger = null) {
  const checked = validateDocument("requirement-entry/v1", entry);
  if (previousLedger === null) {
    if (checked.ledger_version !== 1) {
      taskControlFail(
        "REQUIREMENT_INGEST_VERSION_INVALID", "$.ledger_version",
      );
    }
    return checked;
  }
  const previous = validateDocument("requirement-ledger/v1", previousLedger);
  if (checked.ledger_version !== previous.ledger_version + 1) {
    taskControlFail("REQUIREMENT_INGEST_VERSION_INVALID", "$.ledger_version");
  }
  const byId = new Map(
    previous.entries.map((item) => [item.requirement_id, item]),
  );
  if (byId.has(checked.requirement_id)) {
    taskControlFail(
      "REQUIREMENT_ID_COLLISION", "$.requirement_id",
      "REQUIREMENT_ID_COLLISION",
    );
  }
  if (checked.parent_requirement_ids.some((item) => !byId.has(item))) {
    taskControlFail("REQUIREMENT_PARENT_UNKNOWN", "$.parent_requirement_ids");
  }
  const active = new Set(previous.active_requirement_ids);
  for (const requirementId of checked.supersedes) {
    const target = byId.get(requirementId);
    if (!active.has(requirementId) || !target || target.source_kind !== "derived") {
      taskControlFail("REQUIREMENT_SUPERSEDES_INVALID", "$.supersedes");
    }
  }
  return checked;
}

export function validateRequirementLedgerTransition(
  previousLedger, candidateLedger,
) {
  const previous = validateDocument("requirement-ledger/v1", previousLedger);
  const candidate = validateDocument("requirement-ledger/v1", candidateLedger);
  if (candidate.task_id !== previous.task_id) {
    taskControlFail("REQUIREMENT_LEDGER_TASK_INVALID", "$.task_id");
  }
  if (candidate.ledger_version !== previous.ledger_version + 1) {
    taskControlFail(
      "REQUIREMENT_LEDGER_SUCCESSOR_INVALID", "$.ledger_version",
    );
  }
  const prefix = candidate.entries.slice(0, previous.entries.length);
  if (canonicalString(prefix) !== canonicalString(previous.entries)) {
    taskControlFail("REQUIREMENT_LEDGER_HISTORY_MUTATED", "$.entries");
  }
  const appended = candidate.entries.slice(previous.entries.length);
  if (appended.length === 0) {
    taskControlFail("REQUIREMENT_LEDGER_APPEND_REQUIRED", "$.entries");
  }
  if (appended.some(
    (item) => item.ledger_version !== candidate.ledger_version,
  )) {
    taskControlFail("REQUIREMENT_INGEST_VERSION_INVALID", "$.entries");
  }
  const previousIds = new Set(
    previous.entries.map((item) => item.requirement_id),
  );
  const appendedIds = appended.map((item) => item.requirement_id);
  if (appendedIds.some((item) => previousIds.has(item)) ||
      new Set(appendedIds).size !== appendedIds.length) {
    taskControlFail(
      "REQUIREMENT_ID_COLLISION", "$.entries", "REQUIREMENT_ID_COLLISION",
    );
  }
  return candidate;
}

export function validateTaskCharterTransition(
  previousCharter, candidateCharter, requirementLedger = null,
) {
  const candidate = validateDocument("task-charter/v1", candidateCharter);
  if (previousCharter === null) {
    if (candidate.charter_version !== 1 ||
        candidate.previous_charter_ref !== null) {
      taskControlFail("CHARTER_PREDECESSOR_INVALID", "$.previous_charter_ref");
    }
  } else {
    const previous = validateDocument("task-charter/v1", previousCharter);
    const predecessor = candidate.previous_charter_ref;
    const previousBytes = canonicalDocumentBytes(previous);
    if (candidate.task_id !== previous.task_id) {
      taskControlFail("CHARTER_TASK_INVALID", "$.task_id");
    }
    if (candidate.charter_version !== previous.charter_version + 1) {
      taskControlFail("CHARTER_VERSION_INVALID", "$.charter_version");
    }
    if (!predecessor || predecessor.sha256 !== sha256Sync(previousBytes) ||
        predecessor.size_bytes !== previousBytes.length) {
      taskControlFail("CHARTER_PREDECESSOR_INVALID", "$.previous_charter_ref");
    }
    if (candidate.created_at < previous.created_at) {
      taskControlFail("CHARTER_TIME_ORDER_INVALID", "$.created_at");
    }
  }
  if (requirementLedger !== null) {
    const ledger = validateDocument("requirement-ledger/v1", requirementLedger);
    if (ledger.task_id !== candidate.task_id) {
      taskControlFail("CHARTER_LEDGER_TASK_INVALID", "$.task_id");
    }
    const active = new Set(ledger.active_requirement_ids);
    if (candidate.requirement_ids.some((item) => !active.has(item))) {
      taskControlFail("CHARTER_REQUIREMENT_INACTIVE", "$.requirement_ids");
    }
  }
  return candidate;
}

function taskControlTimestampMillis(value) {
  if (typeof value !== "string" ||
      !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$/.test(value)) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) && new Date(parsed).toISOString() === value
    ? parsed : null;
}

export function verifyWaiverEventAuthority(waiver, effectivePolicy, now) {
  const event = validateDocument("waiver-event/v1", waiver);
  const policy = validateDocument(
    "effective-execution-policy/v1", effectivePolicy,
  );
  const nowMillis = taskControlTimestampMillis(now);
  const issued = taskControlTimestampMillis(event.issued_at);
  const expires = taskControlTimestampMillis(event.expires_at);
  if (nowMillis === null || issued === null || expires === null ||
      nowMillis < issued || nowMillis >= expires) {
    taskControlFail("WAIVER_TIME_INVALID", "$", "WAIVER_INVALID");
  }
  if (event.policy_version !== policy.policy_version) {
    taskControlFail(
      "WAIVER_POLICY_VERSION_INVALID", "$", "WAIVER_INVALID",
    );
  }
  if (!policy.authorized_waiver_issuers.includes(event.issuer)) {
    taskControlFail(
      "WAIVER_ISSUER_NOT_AUTHORIZED", "$", "WAIVER_INVALID",
    );
  }
  taskControlFail(
    "WAIVER_SIGNATURE_AUTHORITY_UNAVAILABLE", "$", "WAIVER_INVALID",
  );
}

const TASK_CONTROL_ATTEMPT_TRANSITIONS = Object.freeze({
  CREATED: new Set(["LEASED", "FENCED"]),
  LEASED: new Set(["PREPARING", "FAILED", "CANCELLED", "FENCED"]),
  PREPARING: new Set(["RUNNING", "FAILED", "CANCELLED", "FENCED"]),
  RUNNING: new Set([
    "VERIFYING", "SUSPENDING", "FAILED", "CANCELLED", "FENCED",
  ]),
  VERIFYING: new Set([
    "RUNNING", "SUSPENDING", "PUBLISHING", "FAILED", "CANCELLED", "FENCED",
  ]),
  SUSPENDING: new Set(["SUSPENDED"]),
  PUBLISHING: new Set([
    "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "FENCED",
  ]),
  SUCCEEDED: new Set(),
  SUSPENDED: new Set(),
  FAILED: new Set(),
  CANCELLED: new Set(),
  FENCED: new Set(),
});

export function validateAttemptTransition(previousAttempt, candidateAttempt) {
  const previous = validateDocument("attempt-record/v1", previousAttempt);
  const candidate = validateDocument("attempt-record/v1", candidateAttempt);
  const immutable = [
    "attempt_id", "task_id", "native_epoch", "transport_binding",
    "predecessor_checkpoint_generation", "owner_session_id",
  ];
  if (immutable.some((field) =>
    canonicalString(candidate[field]) !== canonicalString(previous[field]))) {
    taskControlFail("ATTEMPT_IDENTITY_MUTATED", "$", "AUTHORITY_FENCED");
  }
  if (candidate.state_version !== previous.state_version + 1) {
    taskControlFail(
      "ATTEMPT_STATE_VERSION_INVALID",
      "$.state_version",
      "STATE_TRANSITION_INVALID",
    );
  }
  if (!TASK_CONTROL_ATTEMPT_TRANSITIONS[previous.state].has(candidate.state)) {
    taskControlFail(
      "ATTEMPT_TRANSITION_INVALID", "$.state", "STATE_TRANSITION_INVALID",
    );
  }
  return candidate;
}

const TASK_CONTROL_OWNER_TRANSITIONS = Object.freeze({
  STARTING: new Set(["ACTIVE", "FENCED"]),
  ACTIVE: new Set(["CLOSED", "FENCED"]),
  CLOSED: new Set(),
  FENCED: new Set(),
});

export function validateTaskOwnerTransition(previousOwner, candidateOwner) {
  const previous = validateDocument("task-owner/v1", previousOwner);
  const candidate = validateDocument("task-owner/v1", candidateOwner);
  const immutable = [
    "task_id", "owner_id", "owner_epoch", "session_id", "attempt_id",
    "native_epoch", "started_at",
  ];
  if (immutable.some((field) =>
    canonicalString(candidate[field]) !== canonicalString(previous[field]))) {
    taskControlFail("OWNER_IDENTITY_MUTATED", "$", "AUTHORITY_FENCED");
  }
  if (candidate.state_version !== previous.state_version + 1) {
    taskControlFail(
      "OWNER_STATE_VERSION_INVALID",
      "$.state_version",
      "STATE_TRANSITION_INVALID",
    );
  }
  if (!TASK_CONTROL_OWNER_TRANSITIONS[previous.state].has(candidate.state)) {
    taskControlFail(
      "OWNER_TRANSITION_INVALID", "$.state", "STATE_TRANSITION_INVALID",
    );
  }
  return candidate;
}

const TASK_CONTROL_RECORD_TRANSITIONS = Object.freeze({
  QUEUED: new Set(["ACTIVE", "FINALIZING"]),
  ACTIVE: new Set([
    "ACTIVE", "WAITING_INPUT", "WAITING_APPROVAL", "FINALIZING",
  ]),
  WAITING_INPUT: new Set(["QUEUED", "FINALIZING"]),
  WAITING_APPROVAL: new Set(["QUEUED", "FINALIZING"]),
  FINALIZING: new Set(["ACTIVE", "QUEUED", "FINALIZING", "TERMINAL"]),
  TERMINAL: new Set(),
});

export function validateTaskRecordTransition(previousRecord, candidateRecord) {
  const previous = validateDocument("task-record/v1", previousRecord);
  const candidate = validateDocument("task-record/v1", candidateRecord);
  if (candidate.task_version !== previous.task_version + 1) {
    taskControlFail(
      "TASK_VERSION_STALE", "$.task_version", "TASK_VERSION_STALE",
    );
  }
  if (!TASK_CONTROL_RECORD_TRANSITIONS[previous.lifecycle]
    .has(candidate.lifecycle)) {
    taskControlFail(
      "TASK_RECORD_TRANSITION_INVALID",
      "$.lifecycle",
      "STATE_TRANSITION_INVALID",
    );
  }
  const immutable = [
    "task_id", "task_type", "request_ref", "request_digest",
    "policy_ref", "policy_digest", "policy_version", "protocol_mode",
    "owner_id", "absolute_deadline_at", "terminalization_reserve_ms",
    "created_at",
  ];
  if (immutable.some((field) =>
    canonicalString(candidate[field]) !== canonicalString(previous[field]))) {
    taskControlFail("TASK_RECORD_IDENTITY_MUTATED", "$", "AUTHORITY_FENCED");
  }
  if (previous.desired_state === "CANCEL" &&
      candidate.desired_state !== "CANCEL") {
    taskControlFail(
      "TASK_RECORD_CANCEL_ROLLBACK_INVALID", "$", "STATE_TRANSITION_INVALID",
    );
  }
  if (candidate.deletion_version < previous.deletion_version) {
    taskControlFail(
      "TASK_RECORD_DELETION_VERSION_INVALID", "$", "STATE_TRANSITION_INVALID",
    );
  }
  if (candidate.native_epoch < previous.native_epoch) {
    taskControlFail(
      "TASK_RECORD_NATIVE_EPOCH_INVALID", "$", "NATIVE_EPOCH_STALE",
    );
  }
  if (candidate.owner_epoch < previous.owner_epoch) {
    taskControlFail(
      "TASK_RECORD_OWNER_EPOCH_INVALID", "$", "OWNER_EPOCH_STALE",
    );
  }
  if (candidate.updated_at < previous.updated_at) {
    taskControlFail("TASK_RECORD_TIME_ORDER_INVALID", "$.updated_at");
  }
  return candidate;
}

export function validateClaimWriteSet(
  previousRecord, claimedRecord, attemptRecord, taskOwner,
) {
  const previous = validateDocument("task-record/v1", previousRecord);
  const claimed = validateTaskRecordTransition(previous, claimedRecord);
  const attempt = validateDocument("attempt-record/v1", attemptRecord);
  const owner = validateDocument("task-owner/v1", taskOwner);
  if (previous.lifecycle !== "QUEUED" || claimed.lifecycle !== "ACTIVE" ||
      claimed.desired_state !== "RUN") {
    taskControlFail("CLAIM_TASK_STATE_INVALID", "$", "TASK_NOT_CLAIMABLE");
  }
  if (claimed.native_epoch !== previous.native_epoch + 1 ||
      claimed.native_epoch !== attempt.native_epoch ||
      claimed.native_epoch !== owner.native_epoch) {
    taskControlFail(
      "CLAIM_NATIVE_EPOCH_INVALID", "$", "NATIVE_EPOCH_STALE",
    );
  }
  if (claimed.owner_epoch !== previous.owner_epoch + 1 ||
      claimed.owner_epoch !== owner.owner_epoch) {
    taskControlFail(
      "CLAIM_OWNER_EPOCH_INVALID", "$", "OWNER_EPOCH_STALE",
    );
  }
  const exact = claimed.task_id === attempt.task_id &&
    claimed.task_id === owner.task_id &&
    claimed.current_attempt_id === attempt.attempt_id &&
    claimed.current_attempt_id === owner.attempt_id &&
    claimed.owner_id === owner.owner_id &&
    attempt.owner_session_id === owner.session_id &&
    attempt.state === "LEASED" && owner.state === "STARTING";
  if (!exact) {
    taskControlFail("CLAIM_WRITE_SET_INVALID", "$", "AUTHORITY_FENCED");
  }
  const binding = attempt.transport_binding;
  if (binding.protocol_mode !== claimed.protocol_mode ||
      ["outer_job_id", "run_id", "lease_id", "transport_epoch"].some(
        (field) => binding[field] !== claimed[field],
      )) {
    taskControlFail(
      "CLAIM_TRANSPORT_BINDING_INVALID", "$", "AUTHORITY_FENCED",
    );
  }
  return claimed;
}

function taskControlNormalizedInstant(value) {
  if (!value.includes(".")) return value;
  const prefix = value.slice(0, -1).split(".")[0];
  const fraction = value.slice(0, -1).split(".")[1].replace(/0+$/, "");
  return prefix + (fraction ? `.${fraction}` : "") + "Z";
}

export function validateTaskResultPublication(
  previousRecord, terminalRecord, taskResult,
) {
  const previous = validateDocument("task-record/v1", previousRecord);
  const terminal = validateTaskRecordTransition(previous, terminalRecord);
  if (!taskResult || typeof taskResult !== "object" || Array.isArray(taskResult)) {
    taskControlFail("TASK_RESULT_CONTEXT_INVALID");
  }
  const result = JSON.parse(decoder.decode(canonicalDocumentBytes(taskResult)));
  const required = [
    "schema_id", "task_id", "task_type", "outcome",
    "published_from_version", "terminal_task_version", "request_ref",
    "policy_ref", "attempt_identity", "owner_identity", "terminal_at",
  ];
  if (result.schema_id !== "task-result/v1" ||
      required.some((field) => !(field in result))) {
    taskControlFail("TASK_RESULT_CONTEXT_INVALID");
  }
  const resultBytes = canonicalDocumentBytes(result);
  const resultDigest = sha256Sync(resultBytes);
  if (previous.lifecycle !== "FINALIZING" ||
      terminal.lifecycle !== "TERMINAL") {
    taskControlFail(
      "TASK_RESULT_PUBLICATION_STATE_INVALID", "$", "STATE_TRANSITION_INVALID",
    );
  }
  if (result.published_from_version !== previous.task_version ||
      result.terminal_task_version !== terminal.task_version) {
    taskControlFail(
      "TASK_RESULT_PUBLICATION_VERSION_INVALID", "$", "TASK_VERSION_STALE",
    );
  }
  if (result.task_id !== terminal.task_id ||
      result.task_type !== terminal.task_type ||
      result.outcome !== terminal.outcome) {
    taskControlFail("TASK_RESULT_PUBLICATION_IDENTITY_INVALID");
  }
  if (terminal.terminal_kind !== "task_result" ||
      terminal.result_digest !== resultDigest ||
      terminal.result_ref.sha256 !== resultDigest ||
      terminal.result_ref.size_bytes !== resultBytes.length) {
    taskControlFail("TASK_RESULT_PUBLICATION_REF_INVALID");
  }
  if (taskControlNormalizedInstant(terminal.terminal_at) !==
      taskControlNormalizedInstant(result.terminal_at)) {
    taskControlFail("TASK_RESULT_PUBLICATION_TIME_INVALID", "$.terminal_at");
  }
  if (canonicalString(result.request_ref) !== canonicalString(terminal.request_ref) ||
      canonicalString(result.policy_ref) !== canonicalString(terminal.policy_ref)) {
    taskControlFail("TASK_RESULT_PUBLICATION_CONTROL_REF_INVALID");
  }
  const attempt = result.attempt_identity;
  const owner = result.owner_identity;
  if (attempt.kind === "started" &&
      (attempt.attempt_id !== terminal.current_attempt_id ||
       attempt.native_epoch !== terminal.native_epoch ||
       owner.owner_id !== terminal.owner_id ||
       owner.owner_epoch !== terminal.owner_epoch)) {
    taskControlFail(
      "TASK_RESULT_PUBLICATION_AUTHORITY_INVALID", "$", "AUTHORITY_FENCED",
    );
  }
  return terminal;
}
'''


__all__ = ["NPM_TASK_CONTROL_HELPERS"]
