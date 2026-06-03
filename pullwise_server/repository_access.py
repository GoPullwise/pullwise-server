from __future__ import annotations

import math
from typing import Any

from . import db


def clean_text(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char in value for char in "\r\n\x00"):
        return None
    return value


def clean_bool(value: object) -> bool:
    return value is True


def github_repo_id_from_item(item: dict[str, Any]) -> str | None:
    explicit = clean_text(item.get("githubRepoId"))
    if explicit:
        return explicit
    fallback = clean_text(item.get("id"))
    full_name = clean_text(item.get("fullName"))
    if fallback and fallback != full_name:
        return fallback
    return None


def repository_record_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    github_repo_id = github_repo_id_from_item(item)
    full_name = clean_text(item.get("fullName"))
    if not github_repo_id or not full_name:
        return None
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    owner_login = clean_text(item.get("ownerLogin")) or clean_text(owner.get("login"))
    owner_id = clean_text(item.get("ownerId")) or clean_text(owner.get("id"))
    parent = item.get("parent") if isinstance(item.get("parent"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    return {
        "id": db.repository_id_for_github_repo(github_repo_id),
        "github_repo_id": github_repo_id,
        "github_node_id": clean_text(item.get("githubNodeId")) or clean_text(item.get("nodeId")),
        "full_name": full_name,
        "owner_login": owner_login or full_name.split("/", 1)[0],
        "owner_id": owner_id,
        "default_branch": clean_text(item.get("defaultBranch")) or "main",
        "private": clean_bool(item.get("private")),
        "fork": clean_bool(item.get("fork")),
        "parent_github_repo_id": (
            clean_text(item.get("parentGithubRepoId"))
            or clean_text(parent.get("id"))
        ),
        "source_github_repo_id": (
            clean_text(item.get("sourceGithubRepoId"))
            or clean_text(source.get("id"))
        ),
        "html_url": clean_text(item.get("htmlUrl")),
        "clone_url": clean_text(item.get("cloneUrl")),
    }


def sync_access_for_user(user: dict[str, Any], github_access: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Sync repository records from the user's GitHub access."""
    if not user or not isinstance(github_access, dict):
        return []

    results: list[dict[str, Any]] = []
    for item in github_access.get("repositoryItems") or []:
        if not isinstance(item, dict):
            continue
        repository_record = repository_record_from_item(item)
        if not repository_record:
            continue
        repository = db.upsert_repository(repository_record)
        item["repoId"] = repository["id"]
        item["githubRepoId"] = repository["github_repo_id"]
        results.append(repository)

    return results


def non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, candidate)
