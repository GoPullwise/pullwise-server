"""Ledger scope and target authorization shared by future D1 routes."""
from __future__ import annotations

from typing import Any, Mapping

from .api_key_dto_rules import parse_api_key_restrictions
from .cloudflare_principal import (
    PrincipalAuthError, _bearer, _header, _principal, _resource_auth_snapshot,
)
import json
import re


READ_SCOPES = frozenset({"profile:read", "projects:read", "categories:read",
                         "expenses:read", "reports:read"})
ROLE_SCOPES = {
    "viewer": READ_SCOPES,
    "editor": READ_SCOPES | {"expenses:write", "suggestions:use"},
    "admin": READ_SCOPES | {"expenses:write", "suggestions:use", "projects:write", "categories:write"},
    "owner": READ_SCOPES | {"expenses:write", "suggestions:use", "projects:write", "categories:write"},
}


def workspace_permissions(role: str) -> dict:
    return {"writeExpenses": role in {"owner", "admin", "editor"},
            "manageProjects": role in {"owner", "admin"},
            "manageCategories": role in {"owner", "admin"},
            "manageMembers": role in {"owner", "admin"},
            "manageAdmins": role == "owner"}


def workspace_payload(owner: dict, role: str, revision: int) -> dict:
    return {"id": owner["id"], "ownerId": owner["id"],
            "name": str(owner.get("name") or owner.get("githubLogin") or "Personal") + " ledger",
            "role": role, "revision": revision,
            "permissions": workspace_permissions(role)}


def target_allowed(restrictions: dict, kind: str, project_id: str | None) -> bool:
    """Project allowlists never grant shared-pool access."""
    if kind == "shared":
        return project_id is None and restrictions.get("shared") is True
    if kind != "project" or not isinstance(project_id, str) or not project_id:
        return False
    project_ids = restrictions.get("projectIds")
    return project_ids is None or project_id in project_ids


async def ledger_principal(*, binding: Any, headers: Mapping[str, object],
                           scope: str, now: int, target_kind: str | None = None,
                           project_id: str | None = None, proof: dict | None = None):
    """Return owner and same-snapshot recheck commands for a ledger operation.

    Resource SQL must be appended to returned statements in one D1 batch,
    followed by validate(...). Cookie owners have all ledger targets.
    """
    actor, raw_restrictions = await _principal(binding, headers, scope=scope, now=now)
    token = _bearer(headers)
    key_token = token.startswith("pwk_") or bool(_header(headers, "X-Pullwise-Api-Key"))
    if key_token:
        try:
            restrictions = parse_api_key_restrictions(raw_restrictions)
        except ValueError:
            raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", "Invalid key restriction") from None
        if restrictions != raw_restrictions:
            raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", "Invalid key restriction")
        if target_kind is not None and not target_allowed(restrictions, target_kind, project_id):
            raise PrincipalAuthError(403, "TARGET_FORBIDDEN", "Target is outside API key restriction")
    else:
        restrictions = {}
    selected = _header(headers, "X-Pullwise-Workspace")
    bound_workspace = restrictions.get("workspaceId", actor["id"]) if key_token else None
    if key_token and selected and selected != bound_workspace:
        raise PrincipalAuthError(403, "WORKSPACE_FORBIDDEN", "API key is bound to another ledger.")
    workspace_id = bound_workspace if key_token else selected or actor["id"]
    if not isinstance(workspace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", workspace_id):
        raise PrincipalAuthError(422, "INVALID_INPUT", "Invalid ledger selection.")
    user, role, revision, member = actor, "owner", 1, None
    extra = []
    if workspace_id != actor["id"]:
        extra = [binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(workspace_id),
            binding.prepare("""SELECT role,revision,removed_at FROM workspace_members
            WHERE workspace_id=? AND user_id=?""").bind(workspace_id, actor["id"])]
        found = await binding.batch(extra)
        owner_rows, member_rows = (part.results for part in found)
        if len(owner_rows) != 1 or len(member_rows) != 1:
            raise PrincipalAuthError(404, "WORKSPACE_NOT_FOUND", "Ledger is unavailable.")
        member = member_rows[0]
        user = json.loads(owner_rows[0]["snapshot"])
        role, revision = member["role"], member["revision"]
        if (not isinstance(user, dict) or user.get("id") != workspace_id
                or member["removed_at"] is not None or role not in {"admin", "editor", "viewer"}):
            raise PrincipalAuthError(404, "WORKSPACE_NOT_FOUND", "Ledger is unavailable.")
        if key_token and restrictions.get("workspaceMemberRevision") != revision:
            raise PrincipalAuthError(403, "WORKSPACE_MEMBERSHIP_CHANGED", "Create a key for your current membership.")
    if scope not in ROLE_SCOPES[role]:
        raise PrincipalAuthError(403, "ROLE_FORBIDDEN", "Your ledger role forbids this operation.")
    proof_target = proof if proof is not None else {}
    statements, validate_actor = _resource_auth_snapshot(
        binding, headers, actor, restrictions, now, scope, proof_target)
    auth_count = len(statements)

    def validate(rows):
        validate_actor(rows[:auth_count])
        owner_snapshot = proof_target["user"]
        if extra:
            owners, members = rows[auth_count:]
            if (len(owners) != 1 or json.loads(owners[0]["snapshot"]) != user
                    or len(members) != 1 or members[0] != member):
                raise PrincipalAuthError(403, "AUTHORIZATION_CHANGED", "Ledger membership changed.")
            owner_snapshot = owners[0]["snapshot"]
        proof_target.update(workspace_id=workspace_id, actor_user_id=actor["id"],
                            owner_user=owner_snapshot, workspace_role=role,
                            workspace_revision=revision, workspace_member=member)

    effective = dict(user)
    effective["_actor"] = actor
    effective["_workspace"] = workspace_payload(user, role, revision)
    return effective, (restrictions if key_token else {"shared": True}), [*statements, *extra], validate
