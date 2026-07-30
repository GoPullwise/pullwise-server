"""Node facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


NPM_BOOTSTRAP = r'''
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
