"""Resolve local fact targets from saved account and watch identity only."""
from __future__ import annotations

from .github_authorization import _id, _repo_path
from .github_transport import GitHubUnavailable


def repository_for_target(store, account: dict, repository_id: str, target: dict,
                          role: str = "upstream") -> dict:
    repository_id = _id(repository_id)
    target_repository_id = target.get("target_repository_id")
    if role == "target" and target.get("module") == "updates" and isinstance(target_repository_id, str) and target_repository_id.startswith("github:"):
        expected_id = target_repository_id[7:]
    elif role == "upstream":
        expected_id = target.get("github_repository_id")
    else:
        raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
    if (account.get("id") != target.get("billing_owner_id")
            or _id(expected_id) != repository_id):
        raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
    if (target.get("module") == "updates" and target.get("resource_kind") == "watch"
            and role == "upstream"):
        watch = store.get_watch(target.get("resource_id"))
        if (watch is None or target.get("owner_id") != account["id"]
                or watch["id"] != target["resource_id"]
                or watch["ownerId"] != account["id"]
                or watch["billingOwnerId"] != account["id"]
                or watch["upstreamRepositoryId"] != "github:" + repository_id):
            raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
        name = watch["upstream"]
    elif (target.get("module") in {"pr", "ci"} and target.get("resource_kind") == "repository") or (target.get("module") == "updates" and role == "target"):
        access = account.get("githubRepositoryAccess")
        if (not isinstance(access, dict) or access.get("mode") != "github-app"
                or access.get("authorizedUserId") != account["id"]
                or access.get("repositoriesNeedSync") is True
                or isinstance(account.get("githubRepositoryAccessPending"), dict)):
            raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
        for field, account_field in (("authorizedGithubId", "githubId"),
                                     ("authorizedGithubLogin", "githubLogin")):
            saved = access.get(field)
            if saved and str(saved).casefold() != str(account.get(account_field) or "").casefold():
                raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
        installation_id = target.get("installation_id")
        if target.get("module") == "updates":
            installation_id = target.get("target_installation_id")
        if not installation_id or not isinstance(access.get("repositoryItems"), list):
            raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
        matches = [item for item in access["repositoryItems"]
                   if isinstance(item, dict)
                   and str(item.get("githubRepoId") or "") == repository_id
                   and str(item.get("installationId") or access.get("installationId") or "") == str(installation_id)]
        if len(matches) != 1:
            raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
        name = matches[0].get("fullName")
    else:
        raise GitHubUnavailable("GITHUB_CREDENTIAL_UNAVAILABLE")
    _repo_path(name)
    return {"id": repository_id, "fullName": name}
