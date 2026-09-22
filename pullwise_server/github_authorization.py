"""Read-only GitHub authority checks, injected into trusted permission renewal.

The binding resolver belongs to the server credential/account layer. These
portable policy checks neither store credentials nor configure a live worker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .github_transport import GitHubUnavailable
from .product_discovery import DiscoveryAuthorizationProof


@dataclass(frozen=True)
class GitHubAuthorizationBinding:
    """Current server account/credential binding, never constructed from HTTP.

    repository_* identifies the observed upstream/repository. For shared
    Updates, target_repository_* and installation_* identify the enabled target
    RepositoryService; its snapshot revision is fenced by ProductFactSync.
    app_id is the server's configured App, not an upstream payload claim.
    """
    billing_owner_id: str
    github_user_id: str
    repository_id: str
    repository_full_name: str
    user_token: str = field(repr=False)
    app_id: str | None = None
    installation_id: str | None = None
    app_token: str | None = field(default=None, repr=False)
    installation_token: str | None = field(default=None, repr=False)
    target_repository_id: str | None = None
    target_repository_full_name: str | None = None


def _id(value: object) -> str:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return value
    raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")


def _repo_path(full_name: str) -> str:
    if not isinstance(full_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", full_name):
        raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
    if full_name.split("/")[1] in {".", ".."}:
        raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
    return "/repos/" + full_name


class GitHubAuthorizationChecker:
    def __init__(self, *, get_json: Callable, resolve_binding: Callable):
        self.get_json = get_json
        self.resolve_binding = resolve_binding

    def __call__(self, *, target: dict, now: int) -> DiscoveryAuthorizationProof:
        try:
            accessible = self._check(target)
        except GitHubUnavailable as error:
            # Preserve backoff, but never trust an injected adapter's message.
            raise GitHubUnavailable("GITHUB_AUTHORITY_UNAVAILABLE", retry_at=error.retry_at) from None
        except Exception:
            raise GitHubUnavailable("GITHUB_AUTHORITY_UNAVAILABLE") from None
        # Start time bounds the age of the earliest observation. The scheduler
        # additionally fences expiry and the claim after this network work.
        return DiscoveryAuthorizationProof(accessible, now, now + 300 if accessible else now)

    def _get(self, path: str, token: str, shape: type = dict):
        if not isinstance(token, str) or not token:
            raise GitHubUnavailable("GITHUB_AUTHORITY_UNAVAILABLE")
        response = self.get_json(path, token=token)
        if response.status != 200 or not isinstance(response.payload, shape):
            # 401 may be credential expiry; 404 also conceals inaccessible
            # resources. Neither establishes a definitive remote revocation.
            raise GitHubUnavailable("GITHUB_AUTHORITY_UNAVAILABLE")
        return response.payload

    def _repository(self, path: str, repository_id: str, token: str) -> dict | None:
        repo = self._get(path, token)
        return repo if _id(repo.get("id")) == _id(repository_id) else None

    @staticmethod
    def _maintains(repo: dict) -> bool:
        permissions = repo.get("permissions")
        if not isinstance(permissions, dict) or any(type(permissions.get(key)) is not bool for key in ("admin", "maintain")):
            raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        return permissions["admin"] or permissions["maintain"]

    def _installation(self, path: str, binding: GitHubAuthorizationBinding, module: str) -> bool:
        installation = self._get(path + "/installation", binding.app_token)
        if _id(installation.get("id")) != _id(binding.installation_id) or _id(installation.get("app_id")) != _id(binding.app_id):
            return False
        if "suspended_at" not in installation:
            raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        suspended = installation["suspended_at"]
        if suspended is not None:
            if not isinstance(suspended, str) or not suspended:
                raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
            return False
        permissions = installation.get("permissions")
        if not isinstance(permissions, dict):
            raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        required = {"metadata"}
        if module == "pr":
            required.add("pull_requests")
        elif module == "ci":
            required.add("actions")
        if not all(permissions.get(key) in {"read", "write"} for key in required):
            return False
        if module == "pr":
            self._get(path + "/pulls?per_page=1", binding.installation_token, list)
        elif module == "ci":
            runs = self._get(path + "/actions/runs?per_page=1", binding.installation_token)
            if not isinstance(runs.get("workflow_runs"), list):
                raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        return True

    def _check(self, target: dict) -> bool:
        binding = self.resolve_binding(dict(target))
        if binding is None:
            return False
        if not isinstance(binding, GitHubAuthorizationBinding):
            raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        module = target.get("module")
        if module not in {"pr", "ci", "updates"}:
            raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
        if binding.billing_owner_id != target.get("billing_owner_id") or _id(binding.repository_id) != _id(target.get("github_repository_id")):
            return False
        path = _repo_path(binding.repository_full_name)
        user = self._get("/user", binding.user_token)
        if _id(user.get("id")) != _id(binding.github_user_id):
            return False
        repo = self._repository(path, binding.repository_id, binding.user_token)
        if repo is None:
            return False
        if module == "updates":
            if target.get("owner_id") != binding.billing_owner_id:
                return False
            target_repo_id = target.get("target_repository_id")
            if target_repo_id is None:
                self._get(path + "/releases?per_page=1", binding.user_token, list)
                return True
            if type(repo.get("private")) is not bool:
                raise GitHubUnavailable("GITHUB_AUTHORITY_INVALID")
            if repo["private"]:
                return False
            if target_repo_id != "github:" + _id(binding.target_repository_id):
                return False
            if (binding.installation_id != target.get("target_installation_id")
                    or binding.app_id != target.get("app_id")
                    or binding.billing_owner_id != target.get("target_billing_owner_id")):
                return False
            target_path = _repo_path(binding.target_repository_full_name)
            target_repo = self._repository(target_path, binding.target_repository_id, binding.user_token)
            if target_repo is None or not self._maintains(target_repo):
                return False
            # The App belongs to the shared target, never the public upstream.
            if not self._installation(target_path, binding, module):
                return False
            self._get(path + "/releases?per_page=1", binding.user_token, list)
            return True
        if binding.app_id != target.get("app_id") or binding.installation_id != target.get("installation_id"):
            return False
        return self._maintains(repo) and self._installation(path, binding, module)
