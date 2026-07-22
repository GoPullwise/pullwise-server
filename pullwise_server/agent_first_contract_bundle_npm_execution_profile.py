"""Generated npm facade semantics for execution profiles."""

from __future__ import annotations


NPM_EXECUTION_PROFILE_RULE = r'''
function ruleExecutionProfile(value) {
  if (value.operating_system !== "linux") fail("EXECUTION_PROFILE_OS_INVALID");
  if (!new Set(["aarch64", "x86_64"]).has(value.cpu_architecture)) {
    fail("EXECUTION_PROFILE_ARCH_INVALID");
  }
  if (!/^sha256:[0-9a-f]{64}$/.test(value.image_identity)) {
    fail("EXECUTION_PROFILE_IMAGE_MUTABLE");
  }
}
'''


__all__ = ["NPM_EXECUTION_PROFILE_RULE"]
