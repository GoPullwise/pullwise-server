from __future__ import annotations

from typing import Mapping


def _unknown(reason: str) -> dict:
    return {"recovery": None, "recoveryStatus": "unknown", "reason": reason}


def _identifier(value: object) -> str:
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def verified_successor(
    failure: Mapping[str, object],
    success: Mapping[str, object],
    *,
    proof: Mapping[str, object] | None = None,
) -> dict:
    if _identifier(failure.get("conclusion")).lower() != "failure":
        return _unknown("failure_fact_invalid")
    if _identifier(success.get("conclusion")).lower() != "success":
        return {"recovery": None, "recoveryStatus": "not_observed", "reason": "success_not_observed"}
    identity_fields = ("repositoryId", "workflowId")
    if any(
        not _identifier(failure.get(field))
        or _identifier(failure.get(field)) != _identifier(success.get(field))
        for field in identity_fields
    ):
        return _unknown("execution_identity_mismatch")
    if not isinstance(proof, Mapping) or proof.get("verified") is not True:
        return _unknown("identity_proof_missing")
    if (
        not _identifier(failure.get("jobIdentity"))
        or _identifier(proof.get("jobIdentity")) != _identifier(failure.get("jobIdentity"))
        or _identifier(failure.get("jobIdentity")) != _identifier(success.get("jobIdentity"))
    ):
        return _unknown("execution_identity_mismatch")
    rule_version = _identifier(proof.get("matchRuleVersion"))
    evidence = proof.get("evidence")
    if not rule_version or not isinstance(evidence, Mapping):
        return _unknown("identity_proof_invalid")

    failure_run = _identifier(failure.get("runId"))
    success_run = _identifier(success.get("runId"))
    failure_attempt = failure.get("runAttempt")
    success_attempt = success.get("runAttempt")
    if failure_run == success_run:
        if proof.get("kind") != "same_run_job_identity":
            return _unknown("identity_proof_kind_unsupported")
        if (
            isinstance(failure_attempt, bool)
            or isinstance(success_attempt, bool)
            or not isinstance(failure_attempt, int)
            or not isinstance(success_attempt, int)
            or success_attempt <= failure_attempt
            or _identifier(evidence.get("runId")) != failure_run
            or evidence.get("failureAttempt") != failure_attempt
            or evidence.get("successAttempt") != success_attempt
        ):
            return _unknown("identity_proof_invalid")
        kind = "same_run_retry_succeeded"
    else:
        if proof.get("kind") != "verified_run_lineage":
            return _unknown("identity_proof_kind_unsupported")
        if (
            _identifier(evidence.get("failureRunId")) != failure_run
            or _identifier(evidence.get("successRunId")) != success_run
            or not _identifier(evidence.get("lineageKind"))
            or not _identifier(evidence.get("lineageId"))
        ):
            return _unknown("identity_proof_invalid")
        kind = "later_run_succeeded"

    return {
        "recoveryStatus": "verified",
        "reason": None,
        "recovery": {
            "kind": kind,
            "runId": success_run,
            "runAttempt": success_attempt,
            "jobId": _identifier(success.get("jobId")),
            "observedAt": success.get("observedAt"),
            "matchRuleVersion": rule_version,
            "evidence": dict(evidence),
        },
    }
