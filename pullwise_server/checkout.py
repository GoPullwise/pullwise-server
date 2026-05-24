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

    user_id = clean_scan_text(scan.get("userId")) or ""
    if not user_id:
        raise RuntimeError("Scan is missing a user id.")
    repo = validate_repo_full_name(clean_scan_text(scan.get("repo")) or "")
    installation_id = clean_scan_text(scan.get("installationId"), allow_int=True) or ""
    if not installation_id:
        raise RuntimeError("Scan is missing a GitHub App installation id.")

    token_payload = github_auth.create_installation_access_token(installation_id)
    token = str(token_payload.get("token") or "")
    if not token:
        raise RuntimeError("GitHub App did not return an installation access token.")

    checkout_path = checkout_path_for(user_id, scan_id, repo)
    remove_existing_checkout(checkout_path)
    workspace = workspace_path_for(user_id, scan_id)

    git_env = git_auth_env(token)
    branch = clean_scan_text(scan.get("branch")) or "main"
    run_git(
        [
            "git",
            "clone",
            "--quiet",
            "--no-tags",
            "--depth",
            clone_depth(),
            "--branch",
            branch,
            "--single-branch",
            scan_clone_url_for(repo, scan.get("cloneUrl") or scan.get("clone_url")),
            checkout_path,
        ],
        cwd=workspace,
        extra_env=git_env,
        is_cancelled=is_cancelled,
        action="clone repository",
    )

    commit = clean_scan_text(scan.get("commit")) or "pending"
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
    if not cwd:
        raise RuntimeError("Git command cwd must be inside the checkout root.")
    root_abs = os.path.abspath(checkout_root())
    cwd_abs = os.path.abspath(cwd)
    try:
        common = os.path.commonpath([root_abs, cwd_abs])
    except ValueError:
        common = ""
    if os.path.normcase(common) != os.path.normcase(root_abs):
        raise RuntimeError("Git command cwd must be inside the checkout root.")

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
        encoding="utf-8",
        errors="replace",
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
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
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
    try:
        common = os.path.commonpath([root_abs, path_abs])
    except ValueError:
        common = ""
    if (
        os.path.normcase(common) != os.path.normcase(root_abs)
        or os.path.normcase(path_abs) == os.path.normcase(root_abs)
    ):
        raise RuntimeError("Refusing to remove a checkout outside PULLWISE_CHECKOUT_ROOT.")
    if os.path.exists(path_abs):
        shutil.rmtree(path_abs)


def cleanup_scan_workspace(user_id: str, scan_id: str) -> None:
    workspace = workspace_path_for(user_id, scan_id)
    remove_existing_checkout(workspace)
    remove_empty_checkout_parent(os.path.dirname(workspace))


def remove_empty_checkout_parent(path: str) -> None:
    root_abs = os.path.abspath(checkout_root())
    path_abs = os.path.abspath(path)
    try:
        common = os.path.commonpath([root_abs, path_abs])
    except ValueError:
        return
    if (
        os.path.normcase(common) != os.path.normcase(root_abs)
        or os.path.normcase(path_abs) == os.path.normcase(root_abs)
    ):
        return
    try:
        os.rmdir(path_abs)
    except OSError:
        pass


def checkout_root() -> str:
    return os.environ.get("PULLWISE_CHECKOUT_ROOT") or os.path.join(project_root(), ".pullwise", "checkouts")


def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def safe_path_segment(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return slug or fallback


def clone_depth() -> str:
    raw = os.environ.get("PULLWISE_GIT_CLONE_DEPTH", "1").strip()
    try:
        return str(max(1, int(raw)))
    except ValueError:
        return "1"


def clean_scan_text(value: object, *, allow_int: bool = False) -> str | None:
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char in value for char in "\r\n"):
        return None
    return value


def scan_clone_url_for(repo: str, configured_url: object | None = None) -> str:
    safe_configured_url = clean_scan_text(configured_url)
    if safe_configured_url:
        try:
            return clone_url_for(repo, safe_configured_url)
        except RuntimeError:
            pass
    return clone_url_for(repo)


def clone_url_for(repo: str, configured_url: object | None = None) -> str:
    if configured_url is None:
        clone_url = f"{github_auth.github_web_url()}/{repo}.git"
    elif not isinstance(configured_url, str):
        raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
    else:
        clone_url = configured_url.strip()
        if not clone_url:
            clone_url = f"{github_auth.github_web_url()}/{repo}.git"
        elif any(char in clone_url for char in "\r\n"):
            raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
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
