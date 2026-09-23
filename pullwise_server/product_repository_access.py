"""Conservative saved GitHub App repository entitlement for Worker reads."""
from __future__ import annotations

from typing import Mapping


def account_can_read_repository_service(user: Mapping[str, object],
                                        service: Mapping[str, object]) -> bool:
    access = user.get("githubRepositoryAccess")
    if not isinstance(access, dict) or access.get("mode") != "github-app":
        return False
    if (access.get("authorizedUserId") != user.get("id")
            or access.get("repositoriesNeedSync") is True
            or isinstance(user.get("githubRepositoryAccessPending"), dict)):
        return False
    for access_field, user_field in (("authorizedGithubId", "githubId"),
                                     ("authorizedGithubLogin", "githubLogin")):
        authorized = str(access.get(access_field) or "").casefold()
        if authorized and authorized != str(user.get(user_field) or "").casefold():
            return False
    items = access.get("repositoryItems")
    if not isinstance(items, list):
        return False
    repository_id = str(service["repository_id"])
    installation_id = str(service["installation_id"])
    for item in items:
        if not isinstance(item, dict):
            continue
        ids = {str(item.get(name) or "") for name in ("id", "repoId", "githubRepoId")}
        if repository_id not in ids:
            continue
        item_installation = item.get("installationId") or access.get("installationId")
        if str(item_installation or "") == installation_id:
            return True
    return False
