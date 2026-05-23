"""Repository checkout support for scan workers."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from urllib.parse import urlparse

from . import github_auth


class CheckoutCancelled(Exception):
    """Raised when a checkout subprocess is cancelled by the scan worker."""


_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_NO_COMMIT = {"", "-", "pending"}


def prepare_checkout(scan_id: str, scan: dict, is_cancelled: Callable[[], bool]) -> str:
    """Clone the scan repository and return the local checkout path."""
    if not github_auth.app_api_configured():
        raise RuntimeError("Repository checkout requires GitHub App API credentials.")

    user_id = str(scan.get("userId") or "")
    if not user_id:
        raise RuntimeError("Scan is missing a user id.")
    repo = validate_repo_full_name(str(scan.get("repo") or ""))
    installation_id = str(scan.get("installationId") or "")
    if not installation_id:
        raise RuntimeError("Scan is missing a GitHub App installation id.")

    token_payload = github_auth.create_installation_access_token(installation_id)
    token = str(token_payload.get("token") or "")
    if not token:
        raise RuntimeError("GitHub App did not return an installation access token.")

    checkout_path = checkout_path_for(user_id, scan_id, repo)
    remove_existing_checkout(checkout_path)

    git_env = git_auth_env(token)
    run_git(
        [
            "git",
            "clone",
            "--quiet",
            "--no-tags",
            "--depth",
            clone_depth(),
            "--branch",
            str(scan.get("branch") or "main"),
            "--single-branch",
            clone_url_for(repo, scan.get("cloneUrl")),
            checkout_path,
        ],
        cwd=None,
        extra_env=git_env,
        is_cancelled=is_cancelled,
        action="clone repository",
    )

    commit = str(scan.get("commit") or "").strip()
    if commit.lower() not in _NO_COMMIT:
        checkout_commit(checkout_path, commit, git_env, is_cancelled)

    return checkout_path


def checkout_commit(
    checkout_path: str,
    commit: str,
    git_env: dict[str, str],
    is_cancelled: Callable[[], bool],
) -> None:
    if not _COMMIT_RE.match(commit):
        raise RuntimeError("Scan commit must be a 7-40 character hexadecimal SHA.")

    try:
        run_git(
            ["git", "checkout", "--quiet", "--detach", commit],
            cwd=checkout_path,
            extra_env=git_env,
            is_cancelled=is_cancelled,
            action="checkout commit",
        )
    except RuntimeError:
        run_git(
            ["git", "fetch", "--quiet", "--depth", "1", "origin", commit],
            cwd=checkout_path,
            extra_env=git_env,
            is_cancelled=is_cancelled,
            action="fetch commit",
        )
        run_git(
            ["git", "checkout", "--quiet", "--detach", commit],
            cwd=checkout_path,
            extra_env=git_env,
            is_cancelled=is_cancelled,
            action="checkout commit",
        )


def run_git(
    cmd: list[str],
    *,
    cwd: str | None,
    extra_env: dict[str, str],
    is_cancelled: Callable[[], bool],
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
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if is_cancelled():
                terminate_process(process)
                raise CheckoutCancelled()

    if process.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"Git {action} failed (exit {process.returncode}): {detail[:500]}")


def terminate_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def git_auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def checkout_path_for(user_id: str, scan_id: str, repo: str) -> str:
    root = checkout_root()
    os.makedirs(root, exist_ok=True)
    workspace = workspace_path_for(user_id, scan_id)
    os.makedirs(workspace, exist_ok=True)
    repo_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo).strip("_")
    return os.path.join(workspace, repo_slug)


def workspace_path_for(user_id: str, scan_id: str) -> str:
    return os.path.join(
        checkout_root(),
        safe_path_segment(user_id, "user"),
        safe_path_segment(scan_id, "scan"),
    )


def path_in_scan_workspace(path: str, user_id: str, scan_id: str) -> bool:
    workspace_abs = os.path.abspath(workspace_path_for(user_id, scan_id))
    path_abs = os.path.abspath(path)
    try:
        common = os.path.commonpath([workspace_abs, path_abs])
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(workspace_abs)


def remove_existing_checkout(path: str) -> None:
    root_abs = os.path.abspath(checkout_root())
    path_abs = os.path.abspath(path)
    if os.path.normcase(os.path.commonpath([root_abs, path_abs])) != os.path.normcase(root_abs):
        raise RuntimeError("Refusing to remove a checkout outside PULLWISE_CHECKOUT_ROOT.")
    if os.path.exists(path_abs):
        shutil.rmtree(path_abs)


def checkout_root() -> str:
    return os.environ.get("PULLWISE_CHECKOUT_ROOT") or os.path.join(tempfile.gettempdir(), "pullwise-scans")


def safe_path_segment(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return slug or fallback


def clone_depth() -> str:
    raw = os.environ.get("PULLWISE_GIT_CLONE_DEPTH", "1").strip()
    try:
        return str(max(1, int(raw)))
    except ValueError:
        return "1"


def clone_url_for(repo: str, configured_url: object | None = None) -> str:
    clone_url = str(configured_url or "").strip() or f"{github_auth.github_web_url()}/{repo}.git"
    parsed = urlparse(clone_url)
    allowed = urlparse(github_auth.github_web_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
    if allowed.netloc and parsed.netloc.lower() != allowed.netloc.lower():
        raise RuntimeError("Repository clone URL host does not match configured GitHub host.")
    return clone_url


def validate_repo_full_name(repo: str) -> str:
    if not _REPO_FULL_NAME_RE.match(repo):
        raise RuntimeError("Repository must be a GitHub full name like owner/repo.")
    return repo
