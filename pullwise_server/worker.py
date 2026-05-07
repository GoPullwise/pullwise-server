"""
Background scan worker.

`POST /scans` inserts a queued record then calls `start_scan(scan_id)` here.
The worker walks a fixed phase progression so the frontend can render a
progress bar, calls into `review.run_review()` during the `ai` phase,
appends findings to the ISSUES blob, and marks the scan done.

Concurrency note: this worker mutates the same in-memory blobs (SCANS,
ISSUES) that request handlers read. The worker takes `app.STATE_LOCK` for
each mutation; readers see eventually-consistent state. Pre-existing race
windows in handler-handler interleaving are not addressed here.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback

from . import github_auth, review


class ScanCancelled(Exception):
    """Raised when a scan is cancelled while a blocking subprocess is running."""


# Phase keys must match the SCAN_PHASES table the frontend renders.
PHASES: list[tuple[str, float | None]] = [
    ("clone",   0.6),
    ("index",   0.6),
    ("secrets", 0.4),
    ("deps",    0.6),
    ("ai",      None),   # weighted separately; provider sets the actual duration
    ("report",  0.3),
]
_AI_WEIGHT = 4.0
_CHECKOUT_PROVIDERS = {"claude_code", "codex"}
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_NO_COMMIT = {"", "-", "pending"}


def start_scan(scan_id: str) -> None:
    """Spawn a daemon thread to process the scan with the given id."""
    thread = threading.Thread(
        target=_run, args=(scan_id,), name=f"scan-{scan_id}", daemon=True
    )
    thread.start()


def _run(scan_id: str) -> None:
    # Lazy import to avoid a circular import at module load time.
    from . import app

    started_at = int(time.time())

    snapshot = _start_running(scan_id, started_at)
    if snapshot is None:
        return

    try:
        cumulative = 0.0
        total_weight = sum(w for _, w in PHASES if w is not None) + _AI_WEIGHT

        for phase_key, weight in PHASES:
            if _is_cancelled(scan_id):
                return

            if phase_key == "clone":
                repo_path = _prepare_checkout_if_needed(scan_id, snapshot)
                if repo_path:
                    snapshot["repoPath"] = repo_path
                else:
                    time.sleep(weight or 0)
                cumulative += weight or 0
                patch = {
                    "progress": min(99, int(cumulative / total_weight * 100)),
                    "phase": phase_key,
                }
                if repo_path:
                    patch["repoPath"] = repo_path
                _patch_scan(scan_id, patch)
            elif phase_key == "ai":
                findings = review.run_review(
                    repo=snapshot["repo"],
                    branch=snapshot["branch"],
                    commit=snapshot["commit"],
                    user_id=snapshot["userId"],
                    scan_id=scan_id,
                    repo_path=snapshot.get("repoPath"),
                )
                cumulative += _AI_WEIGHT
                _patch_scan(
                    scan_id,
                    {
                        "issues": _summarize(findings),
                        "progress": min(99, int(cumulative / total_weight * 100)),
                        "phase": phase_key,
                    },
                    extra_findings=findings,
                )
            else:
                time.sleep(weight or 0)
                cumulative += weight or 0
                _patch_scan(
                    scan_id,
                    {
                        "progress": min(99, int(cumulative / total_weight * 100)),
                        "phase": phase_key,
                    },
                )

        completed_at = int(time.time())
        _patch_scan(
            scan_id,
            {
                "status": "done",
                "phase": "report",
                "progress": 100,
                "completedAt": completed_at,
                "durationMs": (completed_at - started_at) * 1000,
            },
        )

    except ScanCancelled:
        return
    except Exception as exc:
        traceback.print_exc()
        _patch_scan(
            scan_id,
            {
                "status": "failed",
                "error": str(exc)[:500],
                "completedAt": int(time.time()),
            },
            allow_after_cancel=True,
        )


def _start_running(scan_id: str, started_at: int) -> dict | None:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        if scan is None or scan.get("status") == "cancelled":
            return None
        scan.update(
            {
                "status": "running",
                "startedAt": started_at,
                "progress": 0,
                "phase": PHASES[0][0],
            }
        )
        app.mark_state_dirty()
        app.persist_state()
        return {
            "userId": scan["userId"],
            "repo": scan.get("repo", ""),
            "branch": scan.get("branch", "main"),
            "commit": scan.get("commit", "pending"),
            "installationId": scan.get("installationId"),
            "repoPath": scan.get("repoPath"),
        }


def _patch_scan(
    scan_id: str,
    patch: dict,
    *,
    extra_findings: list[dict] | None = None,
    allow_after_cancel: bool = False,
) -> None:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        if scan is None:
            return
        if scan.get("status") == "cancelled" and not allow_after_cancel:
            return
        scan.update(patch)
        if extra_findings:
            app.ISSUES.extend(extra_findings)
        app.mark_state_dirty()
        app.persist_state()


def _is_cancelled(scan_id: str) -> bool:
    from . import app

    with app.STATE_LOCK:
        scan = _find_scan(app.SCANS, scan_id)
        return scan is None or scan.get("status") == "cancelled"


def _find_scan(scans: list[dict], scan_id: str) -> dict | None:
    for scan in scans:
        if scan.get("id") == scan_id:
            return scan
    return None


def _prepare_checkout_if_needed(scan_id: str, snapshot: dict) -> str | None:
    if snapshot.get("repoPath"):
        return str(snapshot["repoPath"])
    if not _provider_requires_checkout():
        return None
    return _clone_repository(scan_id, snapshot)


def _provider_requires_checkout() -> bool:
    provider = os.environ.get("PULLWISE_REVIEW_PROVIDER", "mock").strip().lower()
    return provider in _CHECKOUT_PROVIDERS


def _clone_repository(scan_id: str, snapshot: dict) -> str:
    if not github_auth.app_api_configured():
        raise RuntimeError("Real review providers require GitHub App API credentials before cloning.")

    repo = _validate_repo_full_name(str(snapshot.get("repo") or ""))
    branch = _validate_git_ref(str(snapshot.get("branch") or "main"), "branch")
    installation_id = str(snapshot.get("installationId") or "")
    if not installation_id:
        raise RuntimeError("Scan is missing a GitHub App installation id.")

    token_payload = github_auth.create_installation_access_token(installation_id)
    token = str(token_payload.get("token") or "")
    if not token:
        raise RuntimeError("GitHub App did not return an installation access token.")

    checkout_path = _checkout_path(scan_id, repo)
    _remove_existing_checkout(checkout_path)

    clone_url = f"{github_auth.github_web_url()}/{repo}.git"
    git_env = _git_auth_env(token)
    clone_depth = os.environ.get("PULLWISE_GIT_CLONE_DEPTH", "1")
    _run_git(
        [
            "git",
            "clone",
            "--quiet",
            "--no-tags",
            "--depth",
            clone_depth,
            "--branch",
            branch,
            "--single-branch",
            clone_url,
            checkout_path,
        ],
        cwd=None,
        extra_env=git_env,
        scan_id=scan_id,
        action="clone repository",
    )

    commit = str(snapshot.get("commit") or "").strip()
    if commit.lower() not in _NO_COMMIT:
        _checkout_commit(checkout_path, commit, git_env, scan_id)

    return checkout_path


def _checkout_commit(checkout_path: str, commit: str, git_env: dict[str, str], scan_id: str) -> None:
    if not _COMMIT_RE.match(commit):
        raise RuntimeError("Scan commit must be a 7-40 character hexadecimal SHA.")

    try:
        _run_git(
            ["git", "checkout", "--quiet", "--detach", commit],
            cwd=checkout_path,
            extra_env=git_env,
            scan_id=scan_id,
            action="checkout commit",
        )
    except RuntimeError:
        _run_git(
            ["git", "fetch", "--quiet", "--depth", "1", "origin", commit],
            cwd=checkout_path,
            extra_env=git_env,
            scan_id=scan_id,
            action="fetch commit",
        )
        _run_git(
            ["git", "checkout", "--quiet", "--detach", commit],
            cwd=checkout_path,
            extra_env=git_env,
            scan_id=scan_id,
            action="checkout commit",
        )


def _run_git(
    cmd: list[str],
    *,
    cwd: str | None,
    extra_env: dict[str, str],
    scan_id: str,
    action: str,
) -> None:
    env = os.environ.copy()
    env.update(extra_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while process.poll() is None:
        if _is_cancelled(scan_id):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise ScanCancelled()
        time.sleep(0.25)

    stdout, stderr = process.communicate()
    if process.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"Git {action} failed (exit {process.returncode}): {detail[:500]}")


def _git_auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _checkout_path(scan_id: str, repo: str) -> str:
    root = os.environ.get("PULLWISE_CHECKOUT_ROOT") or os.path.join(tempfile.gettempdir(), "pullwise-scans")
    os.makedirs(root, exist_ok=True)
    repo_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo).strip("_")
    return os.path.join(root, f"{scan_id}-{repo_slug}")


def _remove_existing_checkout(path: str) -> None:
    root = os.environ.get("PULLWISE_CHECKOUT_ROOT") or os.path.join(tempfile.gettempdir(), "pullwise-scans")
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    if os.path.normcase(os.path.commonpath([root_abs, path_abs])) != os.path.normcase(root_abs):
        raise RuntimeError("Refusing to remove a checkout outside PULLWISE_CHECKOUT_ROOT.")
    if os.path.exists(path_abs):
        shutil.rmtree(path_abs)


def _validate_repo_full_name(repo: str) -> str:
    if not _REPO_FULL_NAME_RE.match(repo):
        raise RuntimeError("Repository must be a GitHub full name like owner/repo.")
    return repo


def _validate_git_ref(value: str, label: str) -> str:
    if value.startswith("-") or ".." in value or "@{" in value or "\\" in value or not _GIT_REF_RE.match(value):
        raise RuntimeError(f"Invalid git {label}: {value}")
    return value


def _summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity") or "low"
        if severity in summary:
            summary[severity] += 1
    return summary
