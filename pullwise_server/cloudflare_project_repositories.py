"""Explicit project repository bindings and actor-specific GitHub visibility."""
from __future__ import annotations

from typing import Any

from .cloudflare_github_identity_http import read_repository_access
from .cloudflare_github_gateway import GitHubFailure

MAX_REPOSITORIES = 30
MAX_GITHUB_ID = 9007199254740991


def github_actor(user: dict) -> dict:
    """Ledger membership never lends the ledger owner's GitHub credentials."""
    return user.get("_actor", user)


async def live_repository_access(user: dict, gateway: Any) -> dict:
    try:
        return await read_repository_access(github_actor(user), gateway)
    except (ValueError, UnicodeError, TypeError):
        raise GitHubFailure("GITHUB_RESPONSE_INVALID") from None


def repository_ids(body: dict, *, required: bool = False) -> list[int] | None:
    """Accept the old singular create input, with no implicit repository choice."""
    if "githubRepoId" in body and "githubRepoIds" in body:
        raise ValueError("ambiguous repositories")
    values = body.get("githubRepoIds")
    if "githubRepoId" in body:
        values = [body["githubRepoId"]]
    if values is None and "githubRepoIds" not in body:
        if required:
            raise ValueError("repositories required")
        return None
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_REPOSITORIES
            or any(type(value) is not int or not 1 <= value <= MAX_GITHUB_ID for value in values)
            or len(set(values)) != len(values)):
        raise ValueError("invalid repositories")
    return list(values)


def project_input(body: object, *, creating: bool) -> list[int] | None:
    if (not isinstance(body, dict) or not body
            or set(body) - {"githubRepoId", "githubRepoIds", "name", "description",
                            "githubOrganizationId", *({"status"} if not creating else set())}):
        raise ValueError("invalid fields")
    for field, maximum in (("name", 120), ("description", 2000)):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > maximum):
            raise ValueError("invalid text")
    if "status" in body and (not isinstance(body["status"], str)
                             or body["status"] not in {"active", "archived"}):
        raise ValueError("invalid status")
    organization = body.get("githubOrganizationId")
    if organization is not None and (type(organization) is not int
                                    or not 1 <= organization <= MAX_GITHUB_ID):
        raise ValueError("invalid organization")
    return repository_ids(body, required=creating)


def repository_snapshot_values(item: dict) -> tuple:
    account = item.get("account") or {}
    return (item["fullName"], int(item["installationId"]), account.get("id"),
            account.get("login"), account.get("type"))


def bound_repository_dto(repo_id: int, live: dict[int, dict], unavailable: str) -> dict:
    item = live.get(repo_id)
    return {"githubRepoId": repo_id, "githubFullName": item["fullName"] if item else None,
            "githubAccess": "authorized" if item else unavailable,
            "installationId": item.get("installationId") if item else None,
            "account": item.get("account") if item else None}


async def project_repository_eligibility(binding: Any, user: dict, project_id: str,
                                         gateway: Any) -> dict | None:
    """Return the live authorization evidence an expense write must fence.

    The caller puts project revision and the chosen repository's continued
    binding in the same atomic expense batch. Historical edits do not call this.
    Unknown provider outcomes propagate, so a new target fails closed.
    """
    parts = await binding.batch([
        binding.prepare("""SELECT revision,status FROM ledger_projects
            WHERE owner_id=? AND id=?""").bind(user["id"], project_id),
        binding.prepare("""SELECT github_repo_id FROM ledger_project_repositories
            WHERE owner_id=? AND project_id=? ORDER BY github_repo_id LIMIT 30""").bind(
                user["id"], project_id)])
    projects, repositories = (part.results for part in parts)
    if not projects or projects[0]["status"] != "active":
        return None
    live = await live_repository_access(user, gateway)
    authorized = {item["githubRepoId"] for item in live["items"]}
    for row in repositories:
        if row["github_repo_id"] in authorized:
            return {"revision": projects[0]["revision"], "githubRepoId": row["github_repo_id"]}
    return None
