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
    explicit = clean_text(item.get("githubRepoId")) or clean_text(item.get("github_repo_id"))
    if explicit:
        return explicit
    fallback = clean_text(item.get("id"))
    full_name = clean_text(item.get("fullName")) or clean_text(item.get("full_name"))
    if fallback and fallback != full_name:
        return fallback
    return None


def repository_record_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    github_repo_id = github_repo_id_from_item(item)
    full_name = clean_text(item.get("fullName")) or clean_text(item.get("full_name"))
    if not github_repo_id or not full_name:
        return None
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    owner_login = clean_text(item.get("ownerLogin")) or clean_text(item.get("owner_login")) or clean_text(owner.get("login"))
    owner_id = clean_text(item.get("ownerId")) or clean_text(item.get("owner_id")) or clean_text(owner.get("id"))
    parent = item.get("parent") if isinstance(item.get("parent"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    return {
        "id": db.repository_id_for_github_repo(github_repo_id),
        "github_repo_id": github_repo_id,
        "github_node_id": clean_text(item.get("githubNodeId")) or clean_text(item.get("nodeId")) or clean_text(item.get("node_id")),
        "full_name": full_name,
        "owner_login": owner_login or full_name.split("/", 1)[0],
        "owner_id": owner_id,
        "default_branch": clean_text(item.get("defaultBranch")) or clean_text(item.get("default_branch")) or "main",
        "private": clean_bool(item.get("private")),
        "fork": clean_bool(item.get("fork")),
        "parent_github_repo_id": (
            clean_text(item.get("parentGithubRepoId"))
            or clean_text(item.get("parent_github_repo_id"))
            or clean_text(parent.get("id"))
        ),
        "source_github_repo_id": (
            clean_text(item.get("sourceGithubRepoId"))
            or clean_text(item.get("source_github_repo_id"))
            or clean_text(source.get("id"))
        ),
        "html_url": clean_text(item.get("htmlUrl")) or clean_text(item.get("html_url")),
        "clone_url": clean_text(item.get("cloneUrl")) or clean_text(item.get("clone_url")),
    }


def workspace_record_for_installation(
    installation_id: object,
    *,
    installation_account: object = None,
    installation_target_type: object = None,
    owner_id: object = None,
    existing_billing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    installation_id_text = clean_text(installation_id)
    if not installation_id_text:
        return None
    account = clean_text(installation_account) or f"installation-{installation_id_text}"
    billing_state = existing_billing or {}
    return {
        "id": db.workspace_id_for_installation(installation_id_text),
        "name": account,
        "github_owner_id": clean_text(owner_id),
        "github_owner_login": account,
        "github_owner_type": clean_text(installation_target_type),
        "github_app_installation_id": installation_id_text,
        "plan": billing_state.get("plan") or "free",
        "billing_provider": billing_state.get("provider"),
        "billing_customer_id": billing_state.get("customerId"),
        "billing_subscription_id": billing_state.get("subscriptionId"),
        "billing_subscription_item_id": billing_state.get("subscriptionItemId"),
        "billing_status": billing_state.get("status"),
        "billing_interval": billing_state.get("interval"),
    }


def legacy_workspace_for_user(user: dict[str, Any]) -> dict[str, Any]:
    user_id = clean_text(user.get("id")) or "unknown"
    billing_state = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    name = clean_text(user.get("githubLogin")) or clean_text(user.get("name")) or "Personal workspace"
    return db.upsert_workspace(
        {
            "id": db.legacy_workspace_id_for_user(user_id),
            "name": name,
            "github_owner_login": clean_text(user.get("githubLogin")),
            "github_owner_type": "User" if user.get("githubLogin") else "Legacy",
            "plan": billing_state.get("plan") or "free",
            "billing_provider": billing_state.get("provider"),
            "billing_customer_id": billing_state.get("customerId"),
            "billing_subscription_id": billing_state.get("subscriptionId"),
            "billing_subscription_item_id": billing_state.get("subscriptionItemId"),
            "billing_status": billing_state.get("status"),
            "billing_interval": billing_state.get("interval"),
        }
    )


def ensure_legacy_membership(user: dict[str, Any]) -> dict[str, Any]:
    workspace = legacy_workspace_for_user(user)
    user_id = clean_text(user.get("id")) or ""
    if user_id:
        db.upsert_workspace_member(workspace["id"], user_id, role="owner", source="legacy_migration")
    return workspace


def sync_access_for_user(user: dict[str, Any], github_access: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not user or not isinstance(github_access, dict):
        return []
    user_id = clean_text(user.get("id"))
    if not user_id:
        return []

    workspaces_by_installation: dict[str, dict[str, Any]] = {}
    for item in github_access.get("repositoryItems") or []:
        if not isinstance(item, dict):
            continue
        installation_id = clean_text(item.get("installationId")) or clean_text(github_access.get("installationId"))
        if not installation_id:
            continue
        if installation_id not in workspaces_by_installation:
            record = workspace_record_for_installation(
                installation_id,
                installation_account=item.get("installationAccount") or github_access.get("installationAccount"),
                installation_target_type=item.get("installationTargetType") or github_access.get("installationTargetType"),
                owner_id=item.get("ownerId") or item.get("owner_id"),
                existing_billing=user.get("billing") if isinstance(user.get("billing"), dict) else None,
            )
            if record:
                workspace = db.upsert_workspace(record)
                db.upsert_workspace_member(workspace["id"], user_id, role="admin", source="github_installation")
                workspaces_by_installation[installation_id] = workspace
        workspace = workspaces_by_installation.get(installation_id)
        if not workspace:
            continue
        repository_record = repository_record_from_item(item)
        if not repository_record:
            continue
        repository = db.upsert_repository(repository_record)
        db.upsert_workspace_repository(
            workspace["id"],
            repository["id"],
            github_app_installation_id=installation_id,
            permissions=item.get("permissions") if isinstance(item.get("permissions"), dict) else {},
            repository_selection=item.get("repositorySelection") or github_access.get("repositorySelection"),
            installation_account=item.get("installationAccount") or github_access.get("installationAccount"),
        )
        item["repoId"] = repository["id"]
        item["githubRepoId"] = repository["github_repo_id"]
        item["workspaceId"] = workspace["id"]

    if workspaces_by_installation:
        return list(workspaces_by_installation.values())
    return [ensure_legacy_membership(user)]


def current_workspace_for_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    github_access = user.get("githubRepositoryAccess") if isinstance(user.get("githubRepositoryAccess"), dict) else None
    synced = sync_access_for_user(user, github_access)
    if synced:
        return synced[0]
    existing = db.list_workspaces_for_user(clean_text(user.get("id")) or "")
    if existing:
        return existing[0]
    return ensure_legacy_membership(user)


def workspaces_for_user(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not user:
        return []
    current_workspace_for_user(user)
    return db.list_workspaces_for_user(clean_text(user.get("id")) or "")


def workspace_public_payload(workspace: dict[str, Any] | None, *, role: str | None = None) -> dict[str, Any] | None:
    if not workspace:
        return None
    payload = {
        "id": clean_text(workspace.get("id")),
        "name": clean_text(workspace.get("name")) or "Workspace",
        "githubOwnerLogin": clean_text(workspace.get("github_owner_login")),
        "githubOwnerType": clean_text(workspace.get("github_owner_type")),
        "githubAppInstallationId": clean_text(workspace.get("github_app_installation_id")),
    }
    if role or workspace.get("role"):
        payload["role"] = clean_text(role or workspace.get("role"))
    return payload


def billing_state_from_workspace(workspace: dict[str, Any] | None) -> dict[str, Any]:
    if not workspace:
        return {}
    return {
        "provider": workspace.get("billing_provider"),
        "customerId": workspace.get("billing_customer_id"),
        "subscriptionId": workspace.get("billing_subscription_id"),
        "subscriptionItemId": workspace.get("billing_subscription_item_id"),
        "status": workspace.get("billing_status"),
        "plan": workspace.get("plan"),
        "interval": workspace.get("billing_interval"),
    }


def billing_subject_for_workspace(user: dict[str, Any], workspace: dict[str, Any] | None) -> dict[str, Any]:
    subject = {
        "id": clean_text(user.get("id")) or "",
        "email": clean_text(user.get("email")),
        "billing": billing_state_from_workspace(workspace),
    }
    if workspace:
        subject["workspaceId"] = workspace.get("id")
    return subject


def non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, candidate)
