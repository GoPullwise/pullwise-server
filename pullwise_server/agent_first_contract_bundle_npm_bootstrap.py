"""Node facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


NPM_BOOTSTRAP = r'''
function ruleAgentTaskAcceptRequest(value) {
  const request = value.task_request;
  validateEffectivePolicyDerivation(request, value.effective_policy);
  const ledger = verifyDocumentDigest(
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
  const authority = value.authority;
  const roots = value.construction_roots;
  const task = roots.task_record;
  const attempt = roots.attempt;
  const owner = roots.owner;
  const sameGeneration = authority.owner_epoch === task.owner_epoch &&
    task.owner_epoch === owner.owner_epoch &&
    authority.native_epoch === task.native_epoch &&
    task.native_epoch === attempt.native_epoch &&
    attempt.native_epoch === owner.native_epoch;
  if (!sameGeneration) {
    fail("BOOTSTRAP_GENERATION_MISMATCH", "$.construction_roots");
  }
}
'''


__all__ = ["NPM_BOOTSTRAP"]
