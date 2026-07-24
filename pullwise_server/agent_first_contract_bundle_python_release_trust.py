"""Generated Python release-trust semantic rules."""

from __future__ import annotations


PYTHON_RELEASE_TRUST = r'''
def _rule_release_principal(value: dict[str, object]) -> None:
    issued_at = _timestamp_millis(value["issued_at"])
    expires_at = _timestamp_millis(value["expires_at"])
    _release_require(
        issued_at is not None,
        "RELEASE_PRINCIPAL_TIME_INVALID",
        "$.issued_at",
    )
    _release_require(
        expires_at is not None and expires_at > issued_at,
        "RELEASE_PRINCIPAL_TIME_INVALID",
        "$.expires_at",
    )


def _rule_release_signing_key(value: dict[str, object]) -> None:
    _rule_release_principal(value)


def _rule_release_key_revocation(value: dict[str, object]) -> None:
    issued_at = _timestamp_millis(value["issued_at"])
    effective_at = _timestamp_millis(value["effective_at"])
    _release_require(
        issued_at is not None,
        "RELEASE_KEY_REVOCATION_TIME_INVALID",
        "$.issued_at",
    )
    _release_require(
        effective_at is not None and issued_at <= effective_at,
        "RELEASE_KEY_REVOCATION_TIME_INVALID",
        "$.effective_at",
    )
'''


__all__ = ["PYTHON_RELEASE_TRUST"]
