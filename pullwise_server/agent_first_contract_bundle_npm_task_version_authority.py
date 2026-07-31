"""Node facade helpers for immutable Server/local Task version authority."""

from __future__ import annotations


NPM_TASK_VERSION_AUTHORITY = r'''
const TASK_VERSION_FENCE_FIELDS = [
  "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
  "task_version", "deletion_version", "owner_epoch", "native_epoch",
  "transport_epoch",
];
const TASK_VERSION_RECORD_AUTHORITY_FIELDS = [
  "task_id", "deletion_version", "owner_id", "owner_epoch", "native_epoch",
  "lease_id", "transport_epoch",
];

function ruleTaskControlEvent(value) {
  seoRequire(
    value.task_version === value.previous_task_version + 1,
    "TASK_CONTROL_EVENT_VERSION_INVALID",
    "$.task_version",
    "TASK_VERSION_STALE",
  );
  seoRequire(
    value.task_id === value.full_fence.task_id,
    "TASK_CONTROL_EVENT_FENCE_INVALID",
    "$.task_id",
  );
}

function ruleTaskVersionAuthorityProof(value) {
  seoRequire(
    value.task_id === value.full_fence.task_id,
    "TASK_VERSION_AUTHORITY_FENCE_INVALID",
    "$.task_id",
  );
  const chain = value.version_chain;
  let previousVersion = chain[0].previous_task_version;
  let phase = "checkpoint";
  chain.forEach((link, index) => {
    const path = `$.version_chain[${index}]`;
    seoRequire(
      link.previous_task_version === previousVersion
        && link.task_version === previousVersion + 1,
      "TASK_VERSION_AUTHORITY_CHAIN_INVALID",
      path + ".task_version",
      "TASK_VERSION_STALE",
    );
    const kind = link.transition_kind;
    const allowed = kind === "checkpoint" && phase === "checkpoint"
      || kind === "terminalization_requested" && phase === "checkpoint"
      || kind === "task_result_published"
        && phase === "terminalization_requested"
        && index === chain.length - 1;
    seoRequire(
      allowed,
      "TASK_VERSION_AUTHORITY_PUBLICATION_ORDER_INVALID",
      path + ".transition_kind",
      "STATE_TRANSITION_INVALID",
    );
    if (kind !== "checkpoint") phase = kind;
    previousVersion = link.task_version;
  });
  const last = chain.at(-1);
  seoRequire(
    phase === "task_result_published"
      && value.published_from_version === last.previous_task_version
      && value.terminal_task_version === last.task_version,
    "TASK_VERSION_AUTHORITY_TERMINAL_BINDING_INVALID",
    "$.terminal_task_version",
  );
}

function taskVersionRefMatches(ref, schemaId, document) {
  const raw = canonicalDocumentBytes(document);
  return ref.content_schema_id === schemaId
    && ref.sha256 === sha256Sync(raw)
    && ref.size_bytes === raw.length
    && ref.media_type === "application/json"
    && ref.encoding === "utf-8";
}

async function taskVersionAuthorityBinding(document, authorityValue) {
  const authority = await verifyDocumentDigest(
    "server-authority-envelope/v1", authorityValue,
  );
  const fence = document.full_fence;
  const mismatch = TASK_VERSION_FENCE_FIELDS.find(
    (field) => fence[field] !== authority[field],
  );
  seoRequire(
    mismatch === undefined,
    "TASK_VERSION_AUTHORITY_FENCE_INVALID",
    mismatch === undefined ? "$.full_fence" : "$.full_fence." + mismatch,
  );
  await verifyDocumentDigest("task-fence/v1", fence);
  seoRequire(
    canonicalString(document.package) === canonicalString(authority.package)
      && document.authority_digest === authority.authority_digest
      && document.grant_digest === authority.grant.grant_digest
      && document.task_id === authority.task_id,
    "TASK_VERSION_AUTHORITY_BINDING_INVALID",
    "$.authority_digest",
  );
  return authority;
}

async function taskVersionCheckedInput(schemaId, value) {
  return schema(schemaId)["x-pullwise-digest"]
    ? verifyDocumentDigest(schemaId, value)
    : validateDocument(schemaId, value);
}

export async function verifyTaskControlEventContext(
  event, authority, previousRecord, taskRecord, inputDocument,
) {
  const checked = await verifyDocumentDigest("task-control-event/v1", event);
  const boundAuthority = await taskVersionAuthorityBinding(checked, authority);
  const previous = validateDocument("task-record/v1", previousRecord);
  const current = validateTaskRecordTransition(previous, taskRecord);
  const kind = checked.event_kind;
  const inputSchema = kind === "terminalization_requested"
    ? "terminalization-input-snapshot/v1"
    : "task-result/v1";
  const inputValue = await taskVersionCheckedInput(inputSchema, inputDocument);
  seoRequire(
    checked.previous_task_version === previous.task_version
      && checked.task_version === current.task_version
      && current.task_version === previous.task_version + 1,
    "TASK_CONTROL_EVENT_VERSION_INVALID",
    "$.task_version",
    "TASK_VERSION_STALE",
  );
  seoRequire(
    taskVersionRefMatches(checked.input_ref, inputSchema, inputValue)
      && taskVersionRefMatches(
        checked.previous_task_record_ref, "task-record/v1", previous,
      )
      && taskVersionRefMatches(
        checked.task_record_ref, "task-record/v1", current,
      ),
    "TASK_CONTROL_EVENT_REF_INVALID",
    "$.input_ref",
    "CAS_CORRUPT",
  );
  let identity = TASK_VERSION_RECORD_AUTHORITY_FIELDS.every(
    (field) => previous[field] === current[field]
      && current[field] === boundAuthority[field],
  );
  identity = identity
    && previous.current_attempt_id === current.current_attempt_id
    && current.current_attempt_id === boundAuthority.attempt_id
    && checked.occurred_at === current.updated_at;
  seoRequire(
    identity,
    "TASK_CONTROL_EVENT_AUTHORITY_INVALID",
    "$.task_id",
    "AUTHORITY_FENCED",
  );
  if (kind === "terminalization_requested") {
    const terminalFields = [
      "terminal_kind", "result_ref", "result_digest", "outcome", "terminal_at",
    ];
    const exact = inputValue.task_id === current.task_id
      && inputValue.task_version === current.task_version
      && inputValue.deletion_version === current.deletion_version
      && inputValue.attempt_id === current.current_attempt_id
      && inputValue.native_epoch === current.native_epoch
      && inputValue.owner_id === current.owner_id
      && inputValue.owner_epoch === current.owner_epoch
      && inputValue.lease_id === current.lease_id
      && inputValue.lifecycle === "FINALIZING"
      && current.lifecycle === "FINALIZING"
      && inputValue.desired_state === "RUN"
      && current.desired_state === "RUN"
      && terminalFields.every((field) => current[field] === null);
    seoRequire(
      exact,
      "TASK_CONTROL_FINALIZING_INVALID",
      "$.input_ref",
      "STATE_TRANSITION_INVALID",
    );
  } else {
    validateTaskResultPublication(previous, current, inputValue);
  }
  return checked;
}

async function verifyTaskVersionAuthorityProofChecked(
  checked, authority, taskResult,
) {
  const boundAuthority = await taskVersionAuthorityBinding(checked, authority);
  const result = validateDocument("task-result/v1", taskResult);
  seoRequire(
    taskVersionRefMatches(checked.task_result_ref, "task-result/v1", result),
    "TASK_VERSION_AUTHORITY_RESULT_REF_INVALID",
    "$.task_result_ref",
    "CAS_CORRUPT",
  );
  let previousVersion = boundAuthority.task_version;
  const transitionRefs = new Set();
  const taskRefs = new Set([canonicalString(checked.base_task_record_ref)]);
  let requestedCount = 0;
  checked.version_chain.forEach((link, index) => {
    const path = `$.version_chain[${index}]`;
    seoRequire(
      link.previous_task_version === previousVersion
        && link.task_version === previousVersion + 1,
      "TASK_VERSION_AUTHORITY_CHAIN_INVALID",
      path + ".task_version",
      "TASK_VERSION_STALE",
    );
    const transitionRef = canonicalString(link.transition_ref);
    const taskRef = canonicalString(link.task_record_ref);
    seoRequire(
      !transitionRefs.has(transitionRef) && !taskRefs.has(taskRef),
      "TASK_VERSION_AUTHORITY_CHAIN_DUPLICATE",
      path,
      "CAS_CORRUPT",
    );
    transitionRefs.add(transitionRef);
    taskRefs.add(taskRef);
    requestedCount += Number(link.transition_kind === "terminalization_requested");
    seoRequire(
      link.transition_kind !== "task_result_published"
        || index === checked.version_chain.length - 1,
      "TASK_VERSION_AUTHORITY_PUBLICATION_ORDER_INVALID",
      path + ".transition_kind",
      "STATE_TRANSITION_INVALID",
    );
    previousVersion = link.task_version;
  });
  const last = checked.version_chain.at(-1);
  const exact = requestedCount >= 1
    && last.transition_kind === "task_result_published"
    && checked.published_from_version === last.previous_task_version
    && last.previous_task_version === result.published_from_version
    && checked.terminal_task_version === last.task_version
    && last.task_version === result.terminal_task_version
    && result.task_id === checked.task_id;
  seoRequire(
    exact,
    "TASK_VERSION_AUTHORITY_TERMINAL_BINDING_INVALID",
    "$.terminal_task_version",
  );
  return checked;
}

export async function verifyTaskVersionAuthorityProof(
  proof, authority, taskResult,
) {
  const checked = await verifyDocumentDigest(
    "task-version-authority-proof/v1", proof,
  );
  return verifyTaskVersionAuthorityProofChecked(
    checked, authority, taskResult,
  );
}

export const verify_task_control_event_context = verifyTaskControlEventContext;
export const verify_task_version_authority_proof = verifyTaskVersionAuthorityProof;
'''


__all__ = ["NPM_TASK_VERSION_AUTHORITY"]
