"""Generated JavaScript release-trust semantic rules."""

from __future__ import annotations


NPM_RELEASE_TRUST = r'''
function ruleReleasePrincipal(value) {
  releaseRequireTimeOrder(value, "RELEASE_PRINCIPAL_TIME_INVALID");
}

function ruleReleaseSigningKey(value) {
  releaseRequireTimeOrder(value, "RELEASE_SIGNING_KEY_TIME_INVALID");
}

function ruleReleaseKeyRevocation(value) {
  const issuedAt = timestampMillis(value.issued_at);
  const effectiveAt = timestampMillis(value.effective_at);
  releaseRequire(
    issuedAt !== null,
    "RELEASE_KEY_REVOCATION_TIME_INVALID",
    "$.issued_at",
  );
  releaseRequire(
    effectiveAt !== null && issuedAt <= effectiveAt,
    "RELEASE_KEY_REVOCATION_TIME_INVALID",
    "$.effective_at",
  );
}
'''


__all__ = ["NPM_RELEASE_TRUST"]
