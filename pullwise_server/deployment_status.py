from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path
from typing import Any


_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_STATUS_BYTES = 8192


def normalize_revision(value: object) -> str | None:
    revision = str(value or "").strip().lower()
    return revision if _REVISION_PATTERN.fullmatch(revision) else None


def normalize_completed_at(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text


def current_git_revision(repository_root: str | Path) -> str | None:
    root = str(Path(repository_root).resolve())
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", root, "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return normalize_revision(result.stdout)


def read_success_status(status_file: str | Path) -> dict[str, str] | None:
    path = Path(status_file)
    try:
        if path.stat().st_size > _MAX_STATUS_BYTES:
            return None
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schemaVersion") != 1 or payload.get("status") != "succeeded":
        return None
    revision = normalize_revision(payload.get("revision"))
    completed_at = normalize_completed_at(payload.get("completedAt"))
    if not revision or not completed_at:
        return None
    return {"revision": revision, "completedAt": completed_at}


def deployment_payload(
    *,
    status_file: str | Path,
    running_revision: object,
    server_started_at: int,
) -> dict[str, object]:
    running_commit = normalize_revision(running_revision)
    success = read_success_status(status_file)
    successful_commit = success["revision"] if success else None
    verified = bool(running_commit and successful_commit and running_commit == successful_commit)
    if verified:
        state = "verified"
    elif successful_commit:
        state = "pending"
    else:
        state = "unreported"
    return {
        "state": state,
        "verified": verified,
        "runningCommit": running_commit,
        "lastSuccessfulCommit": successful_commit,
        "lastSuccessfulAt": success["completedAt"] if success else None,
        "serverStartedAt": int(server_started_at),
    }
