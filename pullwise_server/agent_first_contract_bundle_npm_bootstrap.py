"""Node facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


NPM_BOOTSTRAP = r'''
function verifyBootstrapDigest(schemaId, value) {
  const validated = validateDocument(schemaId, value);
  verifyEmbeddedDigestSync(schemaId, validated);
  return validated;
}

function ruleAgentTaskAcceptRequest(value) {
  const request = value.task_request;
  validateEffectivePolicyDerivation(request, value.effective_policy);
  const ledger = verifyBootstrapDigest(
    "requirement-ledger/v1", value.requirement_ledger,
  );
  if (ledger.task_id !== request.task_id) {
    fail(
      "ACCEPT_REQUEST_TASK_BINDING_MISMATCH",
      "$.requirement_ledger.task_id",
    );
  }
}

function ruleAgentTaskRuntimeBootstrap(value) {
  const acceptRequest = verifyBootstrapDigest(
    "agent-task-accept-request/v1", value.accept_request,
  );
  const acceptResponse = verifyBootstrapDigest(
    "agent-task-accept-response/v1", value.accept_response,
  );
  const authority = verifyBootstrapDigest(
    "server-authority-envelope/v1", value.authority,
  );
  const roots = value.construction_roots;
  const task = roots.task_record;
  const attempt = roots.attempt;
  const owner = roots.owner;
  const packages = [
    value.package,
    acceptRequest.package,
    acceptResponse.package,
    authority.package,
    authority.grant.package,
  ];
  if (packages.slice(1).some(
    (item) => canonicalString(item) !== canonicalString(packages[0]),
  )) {
    fail("BOOTSTRAP_PACKAGE_MISMATCH", "$.package");
  }
  const sameGeneration = authority.owner_epoch === task.owner_epoch &&
    task.owner_epoch === owner.owner_epoch &&
    authority.native_epoch === task.native_epoch &&
    task.native_epoch === attempt.native_epoch &&
    attempt.native_epoch === owner.native_epoch;
  if (!sameGeneration) {
    fail("BOOTSTRAP_GENERATION_MISMATCH", "$.construction_roots");
  }
  const request = acceptRequest.task_request;
  const policy = acceptRequest.effective_policy;
  const ledger = acceptRequest.requirement_ledger;
  const taskMatches = request.task_id === ledger.task_id &&
    ledger.task_id === acceptResponse.task_id &&
    acceptResponse.task_id === authority.task_id &&
    authority.task_id === task.task_id && task.task_id === attempt.task_id &&
    attempt.task_id === owner.task_id && request.task_type === task.task_type;
  if (!taskMatches) {
    fail("BOOTSTRAP_TASK_BINDING_MISMATCH", "$.construction_roots");
  }
  const versionMatches = task.task_version === authority.task_version &&
    task.task_version === acceptResponse.task_version + 1 &&
    task.deletion_version === authority.deletion_version &&
    task.deletion_version === acceptResponse.deletion_version;
  if (!versionMatches) {
    fail(
      "BOOTSTRAP_TASK_VERSION_MISMATCH",
      "$.construction_roots.task_record.task_version",
    );
  }
  const binding = value.transport_binding;
  const attemptBinding = attempt.transport_binding;
  const transportFields = [
    "outer_job_id", "run_id", "lease_id", "transport_epoch",
  ];
  const transportMatches =
    acceptRequest.outer_job_id === binding.outer_job_id &&
    acceptRequest.run_id === binding.run_id &&
    transportFields.every((field) => task[field] === binding[field]) &&
    transportFields.every(
      (field) => attemptBinding[field] === binding[field],
    ) && attemptBinding.protocol_mode === task.protocol_mode &&
    authority.lease_id === binding.lease_id &&
    authority.transport_epoch === binding.transport_epoch;
  if (!transportMatches) {
    fail("BOOTSTRAP_TRANSPORT_BINDING_MISMATCH", "$.transport_binding");
  }
  const authorityMatches =
    task.current_attempt_id === authority.attempt_id &&
    authority.attempt_id === attempt.attempt_id &&
    attempt.attempt_id === owner.attempt_id &&
    task.owner_id === authority.owner_id &&
    authority.owner_id === owner.owner_id &&
    authority.session_id === attempt.owner_session_id &&
    attempt.owner_session_id === owner.session_id &&
    task.lifecycle === "ACTIVE" && authority.lifecycle === "ACTIVE" &&
    task.desired_state === "RUN" && authority.desired_state === "RUN" &&
    attempt.state === "LEASED" && owner.state === "STARTING";
  if (!authorityMatches) {
    fail("BOOTSTRAP_AUTHORITY_BINDING_MISMATCH", "$.construction_roots");
  }
  const requestBytes = canonicalDocumentBytes(request);
  const requestSha256 = sha256Sync(requestBytes);
  if (task.request_ref.sha256 !== requestSha256 ||
      task.request_digest !== requestSha256 ||
      task.request_ref.size_bytes !== requestBytes.length) {
    fail(
      "BOOTSTRAP_REQUEST_REF_MISMATCH",
      "$.construction_roots.task_record.request_ref",
    );
  }
  const policyBytes = canonicalDocumentBytes(policy);
  if (task.policy_ref.sha256 !== sha256Sync(policyBytes) ||
      task.policy_ref.size_bytes !== policyBytes.length ||
      task.policy_digest !== policy.digest ||
      task.policy_version !== policy.policy_version) {
    fail(
      "BOOTSTRAP_POLICY_REF_MISMATCH",
      "$.construction_roots.task_record.policy_ref",
    );
  }
  if (task.ledger_version !== ledger.ledger_version ||
      task.ledger_head_digest !== ledger.ledger_digest) {
    fail(
      "BOOTSTRAP_LEDGER_BINDING_MISMATCH",
      "$.construction_roots.task_record.ledger_head_digest",
    );
  }
  if (task.absolute_deadline_at !== authority.absolute_deadline_at ||
      task.terminalization_reserve_ms !==
        authority.terminalization_reserve_ms ||
      task.terminalization_reserve_ms !== policy.terminalization_reserve_ms) {
    fail(
      "BOOTSTRAP_DEADLINE_BINDING_MISMATCH",
      "$.construction_roots.task_record.absolute_deadline_at",
    );
  }
  const acceptedMillis = taskControlTimestampMillis(acceptResponse.accepted_at);
  const deadlineMillis = taskControlTimestampMillis(task.absolute_deadline_at);
  if (acceptedMillis === null || deadlineMillis === null ||
      deadlineMillis !== acceptedMillis + policy.budgets.wall_ms) {
    fail(
      "BOOTSTRAP_DEADLINE_DERIVATION_MISMATCH",
      "$.construction_roots.task_record.absolute_deadline_at",
    );
  }
  const grant = authority.grant;
  const grantedCapabilities = new Set(policy.granted_capabilities);
  if (grant.capability_ids.some((item) => !grantedCapabilities.has(item)) ||
      grant.elapsed_limit_ms > policy.budgets.wall_ms ||
      grant.tool_call_limit > policy.budgets.tool_calls) {
    fail("BOOTSTRAP_GRANT_POLICY_MISMATCH", "$.authority.grant");
  }
  if (acceptResponse.accepted_at !== task.created_at ||
      task.current_checkpoint_generation !== 0 ||
      task.current_checkpoint_hash !== null ||
      attempt.predecessor_checkpoint_generation !== null) {
    fail("BOOTSTRAP_CONSTRUCTION_ROOT_INVALID", "$.construction_roots");
  }
}
'''


__all__ = ["NPM_BOOTSTRAP"]
