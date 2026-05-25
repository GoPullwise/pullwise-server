from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any


_LOCK = threading.Lock()


def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def database_path() -> str:
    configured = os.environ.get("PULLWISE_DB_PATH") or os.environ.get("PULLWISE_SQLITE_PATH")
    if configured:
        return configured

    database_url = os.environ.get("PULLWISE_DATABASE_URL", "")
    if database_url.startswith("sqlite:///"):
        return database_url.removeprefix("sqlite:///")

    return os.path.join(project_root(), ".pullwise", "pullwise.sqlite3")


def connect() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_rate_limits (
                    key TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    route TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_rate_limits_subject
                ON api_rate_limits(subject, route, window_start)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    github_owner_id TEXT,
                    github_owner_login TEXT,
                    github_owner_type TEXT,
                    github_app_installation_id TEXT UNIQUE,
                    plan TEXT NOT NULL DEFAULT 'free',
                    billing_provider TEXT,
                    billing_customer_id TEXT,
                    billing_subscription_id TEXT,
                    billing_subscription_item_id TEXT,
                    billing_status TEXT,
                    billing_interval TEXT,
                    billing_pending_binding INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_members (
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    source TEXT NOT NULL DEFAULT 'github_installation',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    PRIMARY KEY (workspace_id, user_id),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    github_repo_id TEXT NOT NULL UNIQUE,
                    github_node_id TEXT,
                    full_name TEXT NOT NULL,
                    owner_login TEXT,
                    owner_id TEXT,
                    default_branch TEXT,
                    private INTEGER NOT NULL DEFAULT 0,
                    fork INTEGER NOT NULL DEFAULT 0,
                    parent_github_repo_id TEXT,
                    source_github_repo_id TEXT,
                    html_url TEXT,
                    clone_url TEXT,
                    last_synced_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_repositories_full_name
                ON repositories(full_name)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_repositories (
                    workspace_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    github_app_installation_id TEXT,
                    permissions TEXT NOT NULL DEFAULT '{}',
                    repository_selection TEXT,
                    installation_account TEXT,
                    last_authorized_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    PRIMARY KEY (workspace_id, repository_id),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_buckets (
                    id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    quota_limit INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    reset_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE (scope_type, scope_id, period, plan)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    github_repo_id TEXT NOT NULL,
                    scan_id TEXT,
                    requested_by_user_id TEXT NOT NULL,
                    request_id TEXT,
                    bucket_id TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
                    FOREIGN KEY (bucket_id) REFERENCES quota_buckets(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quota_ledger_request
                ON quota_ledger(requested_by_user_id, request_id, reason)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_fingerprints (
                    repository_id TEXT PRIMARY KEY,
                    default_branch TEXT,
                    head_sha TEXT,
                    tree_sha TEXT,
                    lockfile_hash TEXT,
                    manifest_hash TEXT,
                    source_fingerprint TEXT,
                    computed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    last_used_at INTEGER,
                    revoked_at INTEGER,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_keys_user_workspace
                ON api_keys(user_id, workspace_id, revoked_at)
                """
            )


def load_state() -> dict[str, Any]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute("SELECT name, payload FROM app_state").fetchall()
    state: dict[str, Any] = {}
    for name, payload in rows:
        try:
            state[name] = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
    return state


def save_state(state: dict[str, Any]) -> None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.executemany(
                """
                INSERT INTO app_state (name, payload, updated_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(name) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                [
                    (name, json.dumps(to_jsonable(payload, path=f"$.{name}"), ensure_ascii=False, allow_nan=False))
                    for name, payload in state.items()
                ],
            )


def stored_request_count(row: Any) -> int:
    if not row:
        return 0
    try:
        count = int(row[0])
    except (IndexError, TypeError, ValueError, OverflowError):
        return 0
    return max(0, count)


def record_rate_limit_hit(
    subject: str,
    *,
    limit: int,
    window_seconds: int,
    route: str = "api",
    timestamp: int | None = None,
) -> dict[str, Any]:
    initialize()
    current_time = int(timestamp if timestamp is not None else time.time())
    window = max(1, int(window_seconds))
    window_start = current_time - (current_time % window)
    reset_at = window_start + window
    key = f"{subject}:{route}:{window_start}"

    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                "DELETE FROM api_rate_limits WHERE window_start < ?",
                (window_start - window,),
            )
            row = connection.execute(
                "SELECT request_count FROM api_rate_limits WHERE key = ?",
                (key,),
            ).fetchone()
            request_count = stored_request_count(row) + 1
            if row:
                connection.execute(
                    """
                    UPDATE api_rate_limits
                    SET request_count = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (request_count, current_time, key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO api_rate_limits
                        (key, subject, route, window_start, request_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, subject, route, window_start, request_count, current_time),
                )

    allowed = request_count <= limit
    return {
        "allowed": allowed,
        "subject": subject,
        "route": route,
        "limit": limit,
        "remaining": max(0, limit - request_count),
        "resetAt": reset_at,
        "retryAfter": max(0, reset_at - current_time),
        "windowSeconds": window,
        "count": request_count,
    }


def to_jsonable(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"State value at {path} is not a finite JSON number.")
        return value
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"State key at {path} is not a string: {type(key).__name__}.")
            normalized[key] = to_jsonable(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        return [to_jsonable(item, path=f"{path}[{index}]") for index, item in enumerate(value)]

    raise TypeError(f"State value at {path} is not JSON serializable: {type(value).__name__}.")


def stable_id(prefix: str, value: object) -> str:
    text = str(value or "").strip()
    slug = "".join(char.lower() if char.isalnum() else "_" for char in text).strip("_")
    if slug and len(slug) <= 80:
        return f"{prefix}_{slug}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def workspace_id_for_installation(installation_id: object) -> str:
    return stable_id("ws_inst", installation_id)


def legacy_workspace_id_for_user(user_id: object) -> str:
    return stable_id("ws_legacy", user_id)


def repository_id_for_github_repo(github_repo_id: object) -> str:
    return stable_id("repo", github_repo_id)


def upsert_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    initialize()
    workspace_id = str(workspace.get("id") or "").strip()
    if not workspace_id:
        raise ValueError("workspace id is required")
    name = str(workspace.get("name") or workspace.get("github_owner_login") or workspace_id).strip() or workspace_id
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, name, github_owner_id, github_owner_login, github_owner_type,
                    github_app_installation_id, plan, billing_provider, billing_customer_id,
                    billing_subscription_id, billing_subscription_item_id, billing_status,
                    billing_interval, billing_pending_binding, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    github_owner_id = COALESCE(excluded.github_owner_id, workspaces.github_owner_id),
                    github_owner_login = COALESCE(excluded.github_owner_login, workspaces.github_owner_login),
                    github_owner_type = COALESCE(excluded.github_owner_type, workspaces.github_owner_type),
                    github_app_installation_id = COALESCE(excluded.github_app_installation_id, workspaces.github_app_installation_id),
                    plan = CASE
                        WHEN workspaces.plan = 'pro' AND excluded.billing_status IS NULL THEN workspaces.plan
                        ELSE COALESCE(NULLIF(excluded.plan, ''), workspaces.plan)
                    END,
                    billing_provider = COALESCE(excluded.billing_provider, workspaces.billing_provider),
                    billing_customer_id = COALESCE(excluded.billing_customer_id, workspaces.billing_customer_id),
                    billing_subscription_id = COALESCE(excluded.billing_subscription_id, workspaces.billing_subscription_id),
                    billing_subscription_item_id = COALESCE(excluded.billing_subscription_item_id, workspaces.billing_subscription_item_id),
                    billing_status = COALESCE(excluded.billing_status, workspaces.billing_status),
                    billing_interval = COALESCE(excluded.billing_interval, workspaces.billing_interval),
                    billing_pending_binding = excluded.billing_pending_binding,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    name,
                    workspace.get("github_owner_id"),
                    workspace.get("github_owner_login"),
                    workspace.get("github_owner_type"),
                    workspace.get("github_app_installation_id"),
                    workspace.get("plan") or "free",
                    workspace.get("billing_provider"),
                    workspace.get("billing_customer_id"),
                    workspace.get("billing_subscription_id"),
                    workspace.get("billing_subscription_item_id"),
                    workspace.get("billing_status"),
                    workspace.get("billing_interval"),
                    1 if workspace.get("billing_pending_binding") else 0,
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()) or {}


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())


def get_workspace_by_installation(installation_id: object) -> dict[str, Any] | None:
    installation_text = str(installation_id or "").strip()
    if not installation_text:
        return None
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                "SELECT * FROM workspaces WHERE github_app_installation_id = ?",
                (installation_text,),
            ).fetchone()
        )


def list_workspaces_for_user(user_id: str) -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT w.*, wm.role, wm.source
            FROM workspaces w
            JOIN workspace_members wm ON wm.workspace_id = w.id
            WHERE wm.user_id = ?
            ORDER BY w.github_app_installation_id IS NULL, w.name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_workspace_member(workspace_id: str, user_id: str, *, role: str = "member", source: str = "github_installation") -> None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO workspace_members (workspace_id, user_id, role, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                ON CONFLICT(workspace_id, user_id) DO UPDATE SET
                    role = excluded.role,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, user_id, role, source),
            )


def user_is_workspace_member(workspace_id: str, user_id: str) -> bool:
    initialize()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
        return row is not None


def upsert_repository(repository: dict[str, Any]) -> dict[str, Any]:
    initialize()
    github_repo_id = str(repository.get("github_repo_id") or "").strip()
    if not github_repo_id:
        raise ValueError("github_repo_id is required")
    repository_id = str(repository.get("id") or repository_id_for_github_repo(github_repo_id)).strip()
    full_name = str(repository.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("repository full_name is required")
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO repositories (
                    id, github_repo_id, github_node_id, full_name, owner_login, owner_id,
                    default_branch, private, fork, parent_github_repo_id, source_github_repo_id,
                    html_url, clone_url, last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(github_repo_id) DO UPDATE SET
                    github_node_id = COALESCE(excluded.github_node_id, repositories.github_node_id),
                    full_name = excluded.full_name,
                    owner_login = COALESCE(excluded.owner_login, repositories.owner_login),
                    owner_id = COALESCE(excluded.owner_id, repositories.owner_id),
                    default_branch = COALESCE(excluded.default_branch, repositories.default_branch),
                    private = excluded.private,
                    fork = excluded.fork,
                    parent_github_repo_id = COALESCE(excluded.parent_github_repo_id, repositories.parent_github_repo_id),
                    source_github_repo_id = COALESCE(excluded.source_github_repo_id, repositories.source_github_repo_id),
                    html_url = COALESCE(excluded.html_url, repositories.html_url),
                    clone_url = COALESCE(excluded.clone_url, repositories.clone_url),
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    repository_id,
                    github_repo_id,
                    repository.get("github_node_id"),
                    full_name,
                    repository.get("owner_login"),
                    repository.get("owner_id"),
                    repository.get("default_branch") or "main",
                    1 if repository.get("private") else 0,
                    1 if repository.get("fork") else 0,
                    repository.get("parent_github_repo_id"),
                    repository.get("source_github_repo_id"),
                    repository.get("html_url"),
                    repository.get("clone_url"),
                ),
            )
            return row_to_dict(
                connection.execute("SELECT * FROM repositories WHERE github_repo_id = ?", (github_repo_id,)).fetchone()
            ) or {}


def get_repository(repository_id: str) -> dict[str, Any] | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,)).fetchone())


def get_repository_by_github_repo_id(github_repo_id: object) -> dict[str, Any] | None:
    github_repo_id_text = str(github_repo_id or "").strip()
    if not github_repo_id_text:
        return None
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute("SELECT * FROM repositories WHERE github_repo_id = ?", (github_repo_id_text,)).fetchone()
        )


def upsert_workspace_repository(
    workspace_id: str,
    repository_id: str,
    *,
    github_app_installation_id: object = None,
    permissions: dict[str, Any] | None = None,
    repository_selection: object = None,
    installation_account: object = None,
) -> None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO workspace_repositories (
                    workspace_id, repository_id, github_app_installation_id, permissions,
                    repository_selection, installation_account, last_authorized_at
                )
                VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(workspace_id, repository_id) DO UPDATE SET
                    github_app_installation_id = excluded.github_app_installation_id,
                    permissions = excluded.permissions,
                    repository_selection = excluded.repository_selection,
                    installation_account = excluded.installation_account,
                    last_authorized_at = excluded.last_authorized_at
                """,
                (
                    workspace_id,
                    repository_id,
                    str(github_app_installation_id) if github_app_installation_id not in (None, "") else None,
                    json.dumps(permissions or {}, sort_keys=True),
                    str(repository_selection) if repository_selection not in (None, "") else None,
                    str(installation_account) if installation_account not in (None, "") else None,
                ),
            )


def get_workspace_repository(workspace_id: str, repository_id: str) -> dict[str, Any] | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT wr.*, r.github_repo_id, r.full_name
                FROM workspace_repositories wr
                JOIN repositories r ON r.id = wr.repository_id
                WHERE wr.workspace_id = ? AND wr.repository_id = ?
                """,
                (workspace_id, repository_id),
            ).fetchone()
        )


def list_repositories_for_workspace(workspace_id: str) -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                r.*,
                wr.workspace_id,
                wr.github_app_installation_id,
                wr.permissions,
                wr.repository_selection,
                wr.installation_account,
                wr.last_authorized_at
            FROM workspace_repositories wr
            JOIN repositories r ON r.id = wr.repository_id
            WHERE wr.workspace_id = ?
            ORDER BY r.full_name COLLATE NOCASE
            """,
            (workspace_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_repo_fingerprint(repository_id: str, fingerprint: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    repository_id = str(repository_id or "").strip()
    if not repository_id:
        raise ValueError("repository id is required")
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO repo_fingerprints (
                    repository_id, default_branch, head_sha, tree_sha, lockfile_hash,
                    manifest_hash, source_fingerprint, computed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(repository_id) DO UPDATE SET
                    default_branch = COALESCE(excluded.default_branch, repo_fingerprints.default_branch),
                    head_sha = COALESCE(excluded.head_sha, repo_fingerprints.head_sha),
                    tree_sha = COALESCE(excluded.tree_sha, repo_fingerprints.tree_sha),
                    lockfile_hash = COALESCE(excluded.lockfile_hash, repo_fingerprints.lockfile_hash),
                    manifest_hash = COALESCE(excluded.manifest_hash, repo_fingerprints.manifest_hash),
                    source_fingerprint = COALESCE(excluded.source_fingerprint, repo_fingerprints.source_fingerprint),
                    computed_at = excluded.computed_at
                """,
                (
                    repository_id,
                    fingerprint.get("defaultBranch") or fingerprint.get("default_branch"),
                    fingerprint.get("headSha") or fingerprint.get("head_sha"),
                    fingerprint.get("treeSha") or fingerprint.get("tree_sha"),
                    fingerprint.get("lockfileHash") or fingerprint.get("lockfile_hash"),
                    fingerprint.get("manifestHash") or fingerprint.get("manifest_hash"),
                    fingerprint.get("sourceFingerprint") or fingerprint.get("source_fingerprint"),
                ),
            )
            return row_to_dict(
                connection.execute(
                    "SELECT * FROM repo_fingerprints WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone()
            )


def get_repo_fingerprint(repository_id: str) -> dict[str, Any] | None:
    initialize()
    repository_id = str(repository_id or "").strip()
    if not repository_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                "SELECT * FROM repo_fingerprints WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
        )


def find_workspace_repo_fingerprint_match(
    workspace_id: str,
    repository_id: str,
    source_fingerprint: str,
) -> dict[str, Any] | None:
    initialize()
    workspace_id = str(workspace_id or "").strip()
    repository_id = str(repository_id or "").strip()
    source_fingerprint = str(source_fingerprint or "").strip()
    if not workspace_id or not repository_id or not source_fingerprint:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT rf.*, wr.workspace_id
                FROM repo_fingerprints rf
                JOIN workspace_repositories wr ON wr.repository_id = rf.repository_id
                WHERE wr.workspace_id = ?
                  AND rf.repository_id != ?
                  AND rf.source_fingerprint = ?
                ORDER BY rf.computed_at ASC
                LIMIT 1
                """,
                (workspace_id, repository_id, source_fingerprint),
            ).fetchone()
        )


def update_workspace_billing(workspace_id: str, billing_state: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE workspaces
                SET billing_provider = COALESCE(?, billing_provider),
                    billing_customer_id = COALESCE(?, billing_customer_id),
                    billing_subscription_id = COALESCE(?, billing_subscription_id),
                    billing_subscription_item_id = COALESCE(?, billing_subscription_item_id),
                    billing_status = COALESCE(?, billing_status),
                    plan = COALESCE(?, plan),
                    billing_interval = COALESCE(?, billing_interval),
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (
                    billing_state.get("provider"),
                    billing_state.get("customerId"),
                    billing_state.get("subscriptionId"),
                    billing_state.get("subscriptionItemId"),
                    billing_state.get("status"),
                    billing_state.get("plan"),
                    billing_state.get("interval"),
                    workspace_id,
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())


def find_workspace_for_billing_update(update: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    workspace_id = str(update.get("workspaceId") or "").strip()
    customer_id = str(update.get("customerId") or "").strip()
    subscription_id = str(update.get("subscriptionId") or "").strip()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        if workspace_id:
            row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if row:
                return dict(row)
        if customer_id:
            row = connection.execute("SELECT * FROM workspaces WHERE billing_customer_id = ?", (customer_id,)).fetchone()
            if row:
                return dict(row)
        if subscription_id:
            row = connection.execute("SELECT * FROM workspaces WHERE billing_subscription_id = ?", (subscription_id,)).fetchone()
            if row:
                return dict(row)
    return None


def create_api_key(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    api_key_id = str(record.get("id") or "").strip()
    user_id = str(record.get("user_id") or "").strip()
    workspace_id = str(record.get("workspace_id") or "").strip()
    name = str(record.get("name") or "API key").strip() or "API key"
    key_prefix = str(record.get("key_prefix") or "").strip()
    key_hash = str(record.get("key_hash") or "").strip()
    scopes = record.get("scopes")
    if not api_key_id or not user_id or not workspace_id or not key_prefix or not key_hash:
        raise ValueError("api key id, user_id, workspace_id, prefix, and hash are required")
    scopes_text = scopes if isinstance(scopes, str) else json.dumps(scopes or [], sort_keys=True)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO api_keys (
                    id, user_id, workspace_id, name, key_prefix, key_hash, scopes,
                    created_at, last_used_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), NULL, NULL)
                """,
                (api_key_id, user_id, workspace_id, name, key_prefix, key_hash, scopes_text),
            )
            return row_to_dict(connection.execute("SELECT * FROM api_keys WHERE id = ?", (api_key_id,)).fetchone()) or {}


def list_api_keys_for_user(user_id: str, workspace_id: str | None = None) -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        if workspace_id:
            rows = connection.execute(
                """
                SELECT * FROM api_keys
                WHERE user_id = ? AND workspace_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (user_id, workspace_id),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM api_keys
                WHERE user_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def get_api_key_by_hash(key_hash: str) -> dict[str, Any] | None:
    initialize()
    key_hash = str(key_hash or "").strip()
    if not key_hash:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT * FROM api_keys
                WHERE key_hash = ? AND revoked_at IS NULL
                """,
                (key_hash,),
            ).fetchone()
        )


def mark_api_key_used(api_key_id: str) -> None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                "UPDATE api_keys SET last_used_at = strftime('%s', 'now') WHERE id = ? AND revoked_at IS NULL",
                (api_key_id,),
            )


def revoke_api_key(api_key_id: str, user_id: str) -> bool:
    initialize()
    with _LOCK, closing(connect()) as connection:
        with connection:
            updated = connection.execute(
                """
                UPDATE api_keys
                SET revoked_at = COALESCE(revoked_at, strftime('%s', 'now'))
                WHERE id = ? AND user_id = ?
                """,
                (api_key_id, user_id),
            ).rowcount
        return updated > 0


def quota_bucket_id(scope_type: str, scope_id: str, period: str, plan: str) -> str:
    return stable_id("qb", f"{scope_type}:{scope_id}:{period}:{plan}")


def quota_ledger_id(*parts: object) -> str:
    return stable_id("ql", ":".join(str(part or "") for part in parts))
