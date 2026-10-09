"""Explicit project repository bindings and actor-specific GitHub visibility."""
from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv6Address
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .cloudflare_github_identity_http import read_repository_access
from .cloudflare_github_gateway import GitHubFailure

MAX_REPOSITORIES = 30
MAX_GITHUB_ID = 9007199254740991
MAX_PROJECT_URL_BYTES = 2048


def project_url(value: object) -> str | None:
    """Validate optional user links without borrowing provider redirect rules."""
    if value is None:
        return None
    if (not isinstance(value, str)
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError("invalid project URL")
    url = value.strip()
    if (not url or len(url.encode("utf-8")) > MAX_PROJECT_URL_BYTES
            or "\\" in url or any(char.isspace() for char in url)
            or not re.match(r"https?://", url, re.I)):
        raise ValueError("invalid project URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or "@" in parsed.netloc or parsed.username is not None
                or parsed.password is not None):
            raise ValueError("invalid project URL")
        # urllib validates IPv6 brackets and port ranges. Require the whole
        # authority to match too: an IPv6 suffix or malformed port is not a URL.
        authority = r"\[[^\[\]]+\](?::[0-9]+)?" if ":" in host else r"[^:\[\]]+(?::[0-9]+)?"
        if not re.fullmatch(authority, parsed.netloc):
            raise ValueError("invalid project URL")
        port = parsed.port
        if ":" in host:
            if "%" in host:
                raise ValueError("invalid project URL")
            canonical_host = "[" + IPv6Address(host).compressed + "]"
        else:
            ascii_host = host.encode("idna").decode("ascii")
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+", ascii_host)
                    or ".." in ascii_host or ascii_host.startswith(".")):
                raise ValueError("invalid project URL")
            canonical_host = ascii_host.lower()
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", canonical_host):
                canonical_host = str(IPv4Address(canonical_host))
        canonical_authority = canonical_host
        if port is not None and port != (80 if parsed.scheme == "http" else 443):
            canonical_authority += ":" + str(port)
        # Match browser URL serialization for Unicode hosts/components so an
        # accepted link cannot become oversized and disappear in Web's reader.
        url = urlunsplit((parsed.scheme, canonical_authority,
            quote(parsed.path or "/", safe="/!$&'()*+,-.:;=@_%~[]"),
            quote(parsed.query, safe="/?!$&()*+,-.:;=@_%~[]"),
            quote(parsed.fragment, safe="/?!$&'()*+,-.:;=@_%~[]")))
        if len(url.encode("utf-8")) > MAX_PROJECT_URL_BYTES:
            raise ValueError("invalid project URL")
    except (ValueError, UnicodeError):
        raise ValueError("invalid project URL") from None
    return url


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
    if (not isinstance(values, list) or not 0 <= len(values) <= MAX_REPOSITORIES
            or any(type(value) is not int or not 1 <= value <= MAX_GITHUB_ID for value in values)
            or len(set(values)) != len(values)):
        raise ValueError("invalid repositories")
    return list(values)


def project_input(body: object, *, creating: bool) -> list[int] | None:
    if (not isinstance(body, dict) or not body
            or set(body) - {"githubRepoId", "githubRepoIds", "name", "description",
                            "githubOrganizationId", "developmentUrl", "productUrl",
                            *({"status"} if not creating else set())}):
        raise ValueError("invalid fields")
    for field, maximum in (("name", 120), ("description", 2000)):
        if field in body and (not isinstance(body[field], str)
                or len(body[field].strip() if field == "name" else body[field]) > maximum):
            raise ValueError("invalid text")
    for field in ("developmentUrl", "productUrl"):
        if field in body:
            project_url(body[field])
    if "status" in body and (not isinstance(body["status"], str)
                             or body["status"] not in {"active", "archived"}):
        raise ValueError("invalid status")
    organization = body.get("githubOrganizationId")
    if organization is not None and (type(organization) is not int
                                    or not 1 <= organization <= MAX_GITHUB_ID):
        raise ValueError("invalid organization")
    selected = repository_ids(body)
    if creating and selected is None:
        selected = []
    if selected == []:
        if organization is not None:
            raise ValueError("standalone organization")
        if creating and not body.get("name", "").strip():
            raise ValueError("standalone name required")
    return selected


def standalone_project(project: dict, repositories: list[dict]) -> bool:
    """An explicit empty association never substitutes for a lost GitHub grant."""
    return "github_repo_id" in project and project["github_repo_id"] is None and not repositories


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
        binding.prepare("""SELECT revision,status,github_repo_id FROM ledger_projects
            WHERE owner_id=? AND id=? AND deleted_at IS NULL""").bind(user["id"], project_id),
        binding.prepare("""SELECT github_repo_id FROM ledger_project_repositories
            WHERE owner_id=? AND project_id=? ORDER BY github_repo_id LIMIT 30""").bind(
                user["id"], project_id)])
    projects, repositories = (part.results for part in parts)
    if not projects or projects[0]["status"] != "active":
        return None
    if standalone_project(projects[0], repositories):
        return {"revision": projects[0]["revision"], "githubRepoId": None}
    if not repositories:
        return None
    live = await live_repository_access(user, gateway)
    authorized = {item["githubRepoId"] for item in live["items"]}
    for row in repositories:
        if row["github_repo_id"] in authorized:
            return {"revision": projects[0]["revision"], "githubRepoId": row["github_repo_id"]}
    return None
