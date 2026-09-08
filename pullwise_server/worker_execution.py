"""Execution liveness is independent of Watcher availability and review progress."""
from datetime import datetime
import re

EXECUTION_TTL_SECONDS = 60
EXECUTION_CLOCK_SKEW_SECONDS = 15
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def proof_timestamp(value: object) -> float:
    if not isinstance(value, dict) or set(value) != {"executor_id", "run_id", "lease_id", "updated_at"}:
        raise ValueError("execution must contain executor_id, run_id, lease_id and updated_at")
    for key in ("executor_id", "run_id", "lease_id"):
        if not isinstance(value[key], str) or not _IDENTIFIER.fullmatch(value[key]):
            raise ValueError(f"execution.{key} is invalid")
    if not isinstance(value["updated_at"], str):
        raise ValueError("execution.updated_at is invalid")
    try:
        timestamp = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("missing timezone")
        return timestamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError("execution.updated_at must be a timezone-bearing timestamp") from None


def lease_deadline(value: object, job: dict, now: int) -> int | None:
    if job.get("timeout_at") is not None and int(job["timeout_at"]) <= now:
        return None
    try:
        timestamp = proof_timestamp(value)
    except ValueError:
        return None
    job_id = job["job_id"]
    attempt = int(job.get("attempt") or 1)
    run_id = f"run_{job_id}" if attempt <= 1 else f"run_{job_id}_attempt_{attempt}"
    if value["run_id"] != run_id or value["lease_id"] != f"lease_{job_id}":
        return None
    if timestamp > now + EXECUTION_CLOCK_SKEW_SECONDS or timestamp + EXECUTION_TTL_SECONDS <= now:
        return None
    return min(now + EXECUTION_TTL_SECONDS, int(timestamp) + EXECUTION_TTL_SECONDS)
