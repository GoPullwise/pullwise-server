"""Shared resource restrictions for requester-visible manual sync Jobs."""
from __future__ import annotations

from typing import Mapping


def job_resource_allowed(*, job_type: str, resource_id: str,
                         target_repository_id: str | None,
                         restrictions: Mapping[str, object]) -> bool:
    if not restrictions:
        return True
    repositories = restrictions.get("repositoryIds")
    watches = restrictions.get("watchIds")
    if job_type == "sync_repository":
        return isinstance(repositories, list) and resource_id in repositories
    if job_type != "sync_watch":
        return False
    if watches is not None and (not isinstance(watches, list) or resource_id not in watches):
        return False
    if repositories is not None and (not isinstance(repositories, list)
                                     or target_repository_id not in repositories):
        return False
    return watches is not None or target_repository_id is not None and repositories is not None
