from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_LOCK = threading.Lock()
DEFAULT_STATE_ENCRYPTION_KEY_PATH = "/etc/pullwise/secrets/state-encryption-key"
STATE_ENCRYPTION_KEY_PATH_ENV = "PULLWISE_STATE_ENCRYPTION_KEY_PATH"
STATE_ENCRYPTION_MARKER = "pullwise-state-secret-v1"
STATE_ENCRYPTION_AAD = b"pullwise-server-state-secret-v1"


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
                    repository_id TEXT NOT NULL,
                    github_repo_id TEXT NOT NULL,
                    scan_id TEXT,
                    requested_by_user_id TEXT NOT NULL,
                    request_id TEXT,
                    bucket_id TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
                    FOREIGN KEY (bucket_id) REFERENCES quota_buckets(id) ON DELETE CASCADE
                )
                """
            )
            normalize_quota_ledger_schema(connection)
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
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    last_used_at INTEGER,
                    revoked_at INTEGER
                )
                """
            )
            normalize_api_keys_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_keys_user
                ON api_keys(user_id, revoked_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_tokens (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    last_used_at INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    name TEXT,
                    token_hash TEXT UNIQUE,
                    version TEXT,
                    provider TEXT,
                    provider_chain TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
                    running_jobs INTEGER NOT NULL DEFAULT 0,
                    free_slots INTEGER NOT NULL DEFAULT 0,
                    hostname TEXT,
                    region TEXT,
                    last_error TEXT,
                    doctor_status TEXT,
                    codex_ready INTEGER,
                    ready_providers TEXT,
                    systemd_active INTEGER,
                    doctor_checked_at INTEGER,
                    machine_metrics TEXT,
                    machine_metrics_history TEXT,
                    status TEXT NOT NULL DEFAULT 'online',
                    first_seen_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    last_heartbeat_at INTEGER,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    token_last_used_at INTEGER,
                    disabled_at INTEGER,
                    deleted_at INTEGER
                )
                """
            )
            for table, column, definition in (
                ("workers", "name", "TEXT"),
                ("workers", "token_hash", "TEXT"),
                ("workers", "enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("workers", "provider_chain", "TEXT"),
                ("workers", "region", "TEXT"),
                ("workers", "created_at", "INTEGER"),
                ("workers", "updated_at", "INTEGER"),
                ("workers", "token_last_used_at", "INTEGER"),
                ("workers", "disabled_at", "INTEGER"),
                ("workers", "deleted_at", "INTEGER"),
                ("workers", "doctor_status", "TEXT"),
                ("workers", "codex_ready", "INTEGER"),
                ("workers", "ready_providers", "TEXT"),
                ("workers", "systemd_active", "INTEGER"),
                ("workers", "doctor_checked_at", "INTEGER"),
                ("workers", "machine_metrics", "TEXT"),
                ("workers", "machine_metrics_history", "TEXT"),
            ):
                ensure_column(connection, table, column, definition)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_audit_events (
                    id TEXT PRIMARY KEY,
                    actor_user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    worker_id TEXT,
                    changed_fields TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    success INTEGER NOT NULL DEFAULT 1,
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_commands (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    requested_by_user_id TEXT,
                    request_id TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    started_at INTEGER,
                    completed_at INTEGER,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_worker_commands_worker_status
                ON worker_commands(worker_id, status, created_at)
                """
            )
            reconcile_worker_uninstall_deletes(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_jobs (
                    job_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL UNIQUE,
                    repo TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    "commit" TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    claimed_by_worker_id TEXT,
                    claimed_at INTEGER,
                    started_at INTEGER,
                    completed_at INTEGER,
                    timeout_at INTEGER,
                    error TEXT,
                    result_checksum TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    user_id TEXT,
                    repo_id TEXT,
                    github_repo_id TEXT,
                    installation_id TEXT,
                    clone_url TEXT,
                    progress_phase TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    logs_summary TEXT,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    review_output_language TEXT,
                    provider_chain TEXT,
                    last_attempt_id TEXT
                )
                """
            )
            ensure_column(connection, "scan_jobs", "review_output_language", "TEXT")
            ensure_column(connection, "scan_jobs", "provider_chain", "TEXT")
            ensure_column(connection, "scan_jobs", "last_attempt_id", "TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_claimable
                ON scan_jobs(status, created_at, job_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_results (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    result_checksum TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(job_id, attempt_id),
                    FOREIGN KEY(job_id) REFERENCES scan_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_decision_events (
                    event_id TEXT PRIMARY KEY,
                    protocol TEXT NOT NULL,
                    candidate_observation_key TEXT NOT NULL,
                    scan_id TEXT,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    user_id TEXT,
                    repo_id TEXT,
                    github_repo_id TEXT,
                    repo_full_name TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    base_sha TEXT,
                    head_sha TEXT,
                    candidate_id TEXT,
                    fingerprint TEXT,
                    source TEXT,
                    provider TEXT,
                    model TEXT,
                    category TEXT,
                    severity TEXT,
                    verification_status TEXT,
                    file_path TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    normalized_title TEXT,
                    raw_confidence REAL,
                    calibrated_confidence REAL,
                    source_reliability_mean REAL,
                    source_reliability_lb REAL,
                    source_adjustment REAL,
                    evidence_strength REAL,
                    delta_relevance REAL,
                    category_adjustment REAL,
                    truth_probability REAL,
                    decision_score REAL,
                    decision TEXT NOT NULL,
                    decision_reason TEXT,
                    scoring_protocol TEXT,
                    score_factors_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_decision_events_observation
                ON review_decision_events(candidate_observation_key, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_decision_events_scope
                ON review_decision_events(user_id, repo_id, branch, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_outcome_labels (
                    label_id TEXT PRIMARY KEY,
                    event_id TEXT,
                    candidate_observation_key TEXT NOT NULL,
                    outcome_label TEXT NOT NULL,
                    label_source TEXT NOT NULL,
                    outcome_weight REAL NOT NULL,
                    label_reason TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    created_by TEXT,
                    calibration_success_weight REAL NOT NULL DEFAULT 0,
                    calibration_failure_weight REAL NOT NULL DEFAULT 0
                )
                """
            )
            ensure_column(connection, "review_outcome_labels", "calibration_success_weight", "REAL NOT NULL DEFAULT 0")
            ensure_column(connection, "review_outcome_labels", "calibration_failure_weight", "REAL NOT NULL DEFAULT 0")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_outcome_labels_observation
                ON review_outcome_labels(candidate_observation_key, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_calibration_snapshots (
                    id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    cohort_key TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    effective_samples REAL NOT NULL DEFAULT 0,
                    posterior_alpha REAL,
                    posterior_beta REAL,
                    posterior_mean REAL,
                    posterior_lb REAL,
                    confidence_buckets_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    drift_state TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(scope_key, cohort_key, snapshot_version)
                )
                """
            )
            ensure_column(connection, "review_calibration_snapshots", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_calibration_snapshots_scope
                ON review_calibration_snapshots(scope_key, created_at)
                """
            )
            configured_worker_token = os.environ.get("PULLWISE_WORKER_TOKEN", "").strip()
            if configured_worker_token:
                token_hash = worker_token_hash(configured_worker_token)
                env_worker_id = os.environ.get("PULLWISE_WORKER_ID", "").strip() or "env_worker"
                connection.execute(
                    """
                    INSERT INTO worker_tokens (id, name, token_hash, enabled)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(token_hash) DO UPDATE SET enabled = 1
                    """,
                    (stable_id("wt", token_hash), "env", token_hash),
                )
                connection.execute(
                    """
                    INSERT INTO workers (
                        worker_id, name, token_hash, provider, enabled, status,
                        created_at, updated_at
                    )
                    VALUES (?, 'Environment worker', ?, 'codex', 1, 'offline', strftime('%s', 'now'), strftime('%s', 'now'))
                    ON CONFLICT(worker_id) DO UPDATE SET
                        token_hash = excluded.token_hash,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    WHERE workers.deleted_at IS NULL
                    """,
                    (env_worker_id, token_hash),
                )


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row[1] == column for row in rows):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def reconcile_worker_uninstall_deletes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE workers
        SET enabled = 0,
            deleted_at = COALESCE(
                deleted_at,
                (
                    SELECT MIN(worker_commands.created_at)
                    FROM worker_commands
                    WHERE worker_commands.worker_id = workers.worker_id
                      AND worker_commands.command = 'uninstall'
                      AND worker_commands.status IN ('pending', 'running')
                ),
                strftime('%s', 'now')
            ),
            disabled_at = COALESCE(
                disabled_at,
                (
                    SELECT MIN(worker_commands.created_at)
                    FROM worker_commands
                    WHERE worker_commands.worker_id = workers.worker_id
                      AND worker_commands.command = 'uninstall'
                      AND worker_commands.status IN ('pending', 'running')
                ),
                strftime('%s', 'now')
            ),
            updated_at = strftime('%s', 'now')
        WHERE deleted_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM worker_commands
              WHERE worker_commands.worker_id = workers.worker_id
                AND worker_commands.command = 'uninstall'
                AND worker_commands.status IN ('pending', 'running')
          )
        """
    )


def normalize_quota_ledger_schema(connection: sqlite3.Connection) -> None:
    desired_columns = [
        "id",
        "repository_id",
        "github_repo_id",
        "scan_id",
        "requested_by_user_id",
        "request_id",
        "bucket_id",
        "delta",
        "reason",
        "created_at",
    ]
    rows = connection.execute("PRAGMA table_info(quota_ledger)").fetchall()
    existing_columns = [str(row[1]) for row in rows]
    if not existing_columns:
        return
    foreign_key_tables = {str(row[2]) for row in connection.execute("PRAGMA foreign_key_list(quota_ledger)").fetchall()}
    if existing_columns == desired_columns and "workspaces" not in foreign_key_tables:
        return

    connection.execute("DROP TABLE IF EXISTS quota_ledger_old")
    connection.execute("ALTER TABLE quota_ledger RENAME TO quota_ledger_old")
    connection.execute(
        """
        CREATE TABLE quota_ledger (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            github_repo_id TEXT NOT NULL,
            scan_id TEXT,
            requested_by_user_id TEXT NOT NULL,
            request_id TEXT,
            bucket_id TEXT NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
            FOREIGN KEY (bucket_id) REFERENCES quota_buckets(id) ON DELETE CASCADE
        )
        """
    )
    copy_columns = [column for column in desired_columns if column in existing_columns]
    if copy_columns:
        columns_sql = ", ".join(copy_columns)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO quota_ledger ({columns_sql})
            SELECT {columns_sql}
            FROM quota_ledger_old
            """
        )
    connection.execute("DROP TABLE quota_ledger_old")


def normalize_api_keys_schema(connection: sqlite3.Connection) -> None:
    desired_columns = [
        "id",
        "user_id",
        "name",
        "key_prefix",
        "key_hash",
        "scopes",
        "created_at",
        "last_used_at",
        "revoked_at",
    ]
    rows = connection.execute("PRAGMA table_info(api_keys)").fetchall()
    existing_columns = [str(row[1]) for row in rows]
    if not existing_columns:
        return
    foreign_key_tables = {str(row[2]) for row in connection.execute("PRAGMA foreign_key_list(api_keys)").fetchall()}
    if existing_columns == desired_columns and "workspaces" not in foreign_key_tables:
        return

    connection.execute("DROP TABLE IF EXISTS api_keys_old")
    connection.execute("ALTER TABLE api_keys RENAME TO api_keys_old")
    connection.execute(
        """
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
            last_used_at INTEGER,
            revoked_at INTEGER
        )
        """
    )
    copy_columns = [column for column in desired_columns if column in existing_columns]
    if copy_columns:
        columns_sql = ", ".join(copy_columns)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO api_keys ({columns_sql})
            SELECT {columns_sql}
            FROM api_keys_old
            """
        )
    connection.execute("DROP TABLE api_keys_old")


def state_encryption_key_path() -> str:
    configured = os.environ.get(STATE_ENCRYPTION_KEY_PATH_ENV)
    if configured is not None:
        return configured.strip()
    return DEFAULT_STATE_ENCRYPTION_KEY_PATH


def state_encryption_required() -> bool:
    mode = os.environ.get("PULLWISE_MODE", "").strip().lower()
    if mode == "production":
        return True
    return STATE_ENCRYPTION_KEY_PATH_ENV in os.environ and bool(state_encryption_key_path())


def parse_state_encryption_key(raw: bytes) -> bytes:
    value = raw.strip()
    if len(value) == 32:
        return bytes(value)

    try:
        text = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            "State encryption key must be 32 raw bytes, 64 hex characters, or base64-encoded 32 bytes."
        ) from exc

    if text.startswith("pullwise-state-v1:"):
        text = text.removeprefix("pullwise-state-v1:").strip()
    if len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text):
        return bytes.fromhex(text)

    padded = text + ("=" * (-len(text) % 4))
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(padded.encode("ascii"))
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key

    raise RuntimeError("State encryption key must decode to exactly 32 bytes.")


def load_state_encryption_key(*, required: bool = False) -> bytes | None:
    path = state_encryption_key_path()
    if not path:
        if required:
            raise RuntimeError(f"{STATE_ENCRYPTION_KEY_PATH_ENV} must point to a readable 32-byte key file.")
        return None
    try:
        with open(path, "rb") as key_file:
            raw = key_file.read()
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"{STATE_ENCRYPTION_KEY_PATH_ENV} is not readable: {path}") from None
        return None
    except OSError as exc:
        raise RuntimeError(f"{STATE_ENCRYPTION_KEY_PATH_ENV} is not readable: {path}") from exc

    if not raw.strip():
        raise RuntimeError(f"{STATE_ENCRYPTION_KEY_PATH_ENV} is empty: {path}")
    return parse_state_encryption_key(raw)


def encrypted_state_secret(value: object) -> bool:
    return isinstance(value, dict) and value.get("__encrypted") == STATE_ENCRYPTION_MARKER


def iter_state_secret_slots(state: dict[str, Any]):
    users = state.get("users")
    if not isinstance(users, dict):
        return
    for user_id, user in users.items():
        if not isinstance(user, dict):
            continue
        yield user, "githubAccessToken", f"$.users.{user_id}.githubAccessToken"
        identities = user.get("githubIdentities")
        if isinstance(identities, list):
            for index, identity in enumerate(identities):
                if isinstance(identity, dict):
                    yield identity, "accessToken", f"$.users.{user_id}.githubIdentities[{index}].accessToken"


def state_has_plaintext_secrets(state: dict[str, Any]) -> bool:
    for container, key, _path in iter_state_secret_slots(state):
        value = container.get(key)
        if isinstance(value, str) and value:
            return True
    return False


def state_has_encrypted_secrets(state: dict[str, Any]) -> bool:
    for container, key, _path in iter_state_secret_slots(state):
        if encrypted_state_secret(container.get(key)):
            return True
    return False


def state_secret_kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def encode_state_secret(value: str, key: bytes) -> dict[str, str]:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), STATE_ENCRYPTION_AAD)
    return {
        "__encrypted": STATE_ENCRYPTION_MARKER,
        "alg": "AES-256-GCM",
        "kid": state_secret_kid(key),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def decode_state_secret(value: dict, key: bytes, *, path: str) -> str:
    if value.get("alg") != "AES-256-GCM":
        raise RuntimeError(f"Unsupported encrypted state secret algorithm at {path}.")
    try:
        nonce = base64.urlsafe_b64decode(str(value.get("nonce") or ""))
        ciphertext = base64.urlsafe_b64decode(str(value.get("ciphertext") or ""))
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, STATE_ENCRYPTION_AAD)
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise RuntimeError(
            f"Unable to decrypt encrypted state secret at {path}. Check {STATE_ENCRYPTION_KEY_PATH_ENV}."
        ) from exc


def state_for_storage(state: dict[str, Any]) -> dict[str, Any]:
    normalized = to_jsonable(state)
    if not isinstance(normalized, dict):
        raise TypeError("State root must be a JSON object.")

    key = load_state_encryption_key(required=state_encryption_required()) if state_has_plaintext_secrets(normalized) else None
    if not key:
        return normalized

    for container, slot_key, _path in iter_state_secret_slots(normalized):
        value = container.get(slot_key)
        if isinstance(value, str) and value:
            container[slot_key] = encode_state_secret(value, key)
    return normalized


def state_for_runtime(state: dict[str, Any]) -> dict[str, Any]:
    if not state_has_encrypted_secrets(state):
        return state

    key = load_state_encryption_key(required=True)
    if not key:
        raise RuntimeError(f"{STATE_ENCRYPTION_KEY_PATH_ENV} must be configured to decrypt state secrets.")

    for container, slot_key, path in iter_state_secret_slots(state):
        value = container.get(slot_key)
        if encrypted_state_secret(value):
            container[slot_key] = decode_state_secret(value, key, path=path)
    return state


def migrate_plaintext_state_secrets(state: dict[str, Any]) -> None:
    if not state_has_plaintext_secrets(state):
        return
    key = load_state_encryption_key(required=state_encryption_required())
    if key:
        save_state(state)


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
    migrate_plaintext_state_secrets(state)
    return state_for_runtime(state)


def load_state_item(name: str) -> Any | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            "SELECT payload FROM app_state WHERE name = ?",
            (name,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None


def save_state_item(name: str, payload: Any) -> None:
    initialize()
    storage_payload = to_jsonable(payload)
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO app_state (name, payload, updated_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(name) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (name, json.dumps(storage_payload, ensure_ascii=False, allow_nan=False)),
            )


def save_state(state: dict[str, Any]) -> None:
    initialize()
    storage_state = state_for_storage(state)
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
                    (name, json.dumps(payload, ensure_ascii=False, allow_nan=False))
                    for name, payload in storage_state.items()
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


def worker_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def worker_max_concurrency_cap() -> int:
    return 32


def normalize_worker_capacity(value: Any, *, clamp: bool = True, cap: int | None = None) -> int:
    capacity = max(1, int(value or 1))
    if clamp:
        return min(capacity, max(1, int(cap or worker_max_concurrency_cap())))
    return capacity


WORKER_PROVIDER_VALUES = {"codex"}


def normalize_provider_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
            raw_items = parsed if isinstance(parsed, list) else []
        else:
            raw_items = stripped.split(",")
    elif value is None:
        raw_items = []
    else:
        raw_items = []
    providers: list[str] = []
    for item in raw_items:
        provider = str(item or "").strip().lower()
        if provider in WORKER_PROVIDER_VALUES and provider not in providers:
            providers.append(provider)
    return providers


def provider_list_json(value: Any, *, fallback: Any = None) -> str | None:
    providers = normalize_provider_list(value) or normalize_provider_list(fallback)
    return json.dumps(providers, sort_keys=True) if providers else None


def provider_ready_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ready"}
    return False


def heartbeat_ready_providers_json(record: dict[str, Any]) -> str | None:
    raw_ready = record.get("ready_providers")
    if raw_ready is not None:
        return json.dumps(normalize_provider_list(raw_ready), sort_keys=True)
    if "codex_ready" not in record:
        return None
    providers: list[str] = []
    if provider_ready_flag(record.get("codex_ready")):
        providers.append("codex")
    return json.dumps(providers, sort_keys=True)


WORKER_LIFECYCLE_COMMANDS = {"stop", "uninstall"}
WORKER_COMMAND_ACTIVE_STATUSES = {"pending", "running"}
WORKER_COMMAND_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def normalize_worker_lifecycle_command(command: Any) -> str:
    value = str(command or "").strip().lower()
    if value not in WORKER_LIFECYCLE_COMMANDS:
        allowed = ", ".join(sorted(WORKER_LIFECYCLE_COMMANDS))
        raise ValueError(f"Worker command must be one of: {allowed}.")
    return value


def create_worker_token(name: str = "worker") -> dict[str, Any]:
    initialize()
    token = "pww_" + secrets.token_urlsafe(32)
    token_hash = worker_token_hash(token)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO worker_tokens (id, name, token_hash, enabled)
                VALUES (?, ?, ?, 1)
                """,
                (stable_id("wt", token_hash), str(name or "worker")[:120], token_hash),
            )
            record = row_to_dict(
                connection.execute("SELECT * FROM worker_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            ) or {}
    record["token"] = token
    return record


def create_worker(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    token = "pww_" + secrets.token_urlsafe(32)
    token_hash = worker_token_hash(token)
    worker_id = str(record.get("worker_id") or stable_id("wk", token_hash)).strip()
    timestamp = int(record.get("timestamp") or time.time())
    max_concurrency_cap = record.get("max_concurrency_cap")
    provider = (normalize_provider_list(record.get("provider")) or ["codex"])[0]
    provider_chain = provider_list_json(record.get("provider_chain"), fallback=[provider])
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO workers (
                    worker_id, name, token_hash, provider, provider_chain, enabled, status,
                    max_concurrent_jobs, running_jobs, free_slots, version,
                    hostname, region, last_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, 'offline', ?, 0, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    worker_id,
                    str(record.get("name") or "Worker")[:120],
                    token_hash,
                    provider,
                    provider_chain,
                    normalize_worker_capacity(record.get("max_concurrent_jobs"), cap=max_concurrency_cap),
                    normalize_worker_capacity(record.get("max_concurrent_jobs"), cap=max_concurrency_cap),
                    record.get("version"),
                    record.get("region"),
                    timestamp,
                    timestamp,
                ),
            )
            worker = row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()) or {}
    worker["worker_token"] = token
    return worker


def list_workers(*, include_deleted: bool = False) -> list[dict[str, Any]]:
    initialize()
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT * FROM workers {where} ORDER BY created_at DESC, worker_id ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_worker(worker_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    initialize()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    where_deleted = "" if include_deleted else "AND deleted_at IS NULL"
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                f"SELECT * FROM workers WHERE worker_id = ? {where_deleted}",
                (worker_id,),
            ).fetchone()
        )


def update_worker(
    worker_id: str,
    patch: dict[str, Any],
    *,
    max_concurrency_cap: int | None = None,
) -> dict[str, Any] | None:
    initialize()
    allowed = {
        "name": "name",
        "provider": "provider",
        "provider_chain": "provider_chain",
        "region": "region",
        "version": "version",
        "max_concurrent_jobs": "max_concurrent_jobs",
    }
    assignments = []
    values: list[Any] = []
    for source_key, column in allowed.items():
        if source_key not in patch:
            continue
        value = patch[source_key]
        if column == "max_concurrent_jobs":
            value = normalize_worker_capacity(value, cap=max_concurrency_cap)
        elif column == "provider_chain":
            value = provider_list_json(value)
            if value:
                first_provider = normalize_provider_list(value)[0]
                assignments.append("provider = ?")
                values.append(first_provider)
        elif value is not None:
            value = str(value)[:120]
        assignments.append(f"{column} = ?")
        values.append(value)
    if not assignments:
        return get_worker(worker_id)
    timestamp = int(time.time())
    assignments.append("updated_at = ?")
    values.append(timestamp)
    values.append(worker_id)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                f"UPDATE workers SET {', '.join(assignments)} WHERE worker_id = ? AND deleted_at IS NULL",
                tuple(values),
            )
            return row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone())


def set_worker_enabled(worker_id: str, enabled: bool) -> dict[str, Any] | None:
    initialize()
    timestamp = int(time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE workers
                SET enabled = ?,
                    disabled_at = CASE WHEN ? = 0 THEN ? ELSE NULL END,
                    updated_at = ?
                WHERE worker_id = ? AND deleted_at IS NULL
                """,
                (1 if enabled else 0, 1 if enabled else 0, timestamp, timestamp, worker_id),
            )
            return row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone())


def soft_delete_worker(worker_id: str) -> dict[str, Any] | None:
    initialize()
    timestamp = int(time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE workers
                SET enabled = 0, deleted_at = ?, disabled_at = COALESCE(disabled_at, ?), updated_at = ?
                WHERE worker_id = ? AND deleted_at IS NULL
                """,
                (timestamp, timestamp, timestamp, worker_id),
            )
            return row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone())


def rotate_worker_token(worker_id: str) -> dict[str, Any] | None:
    initialize()
    token = "pww_" + secrets.token_urlsafe(32)
    token_hash = worker_token_hash(token)
    timestamp = int(time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            updated = connection.execute(
                """
                UPDATE workers
                SET token_hash = ?, updated_at = ?
                WHERE worker_id = ? AND deleted_at IS NULL
                """,
                (token_hash, timestamp, worker_id),
            ).rowcount
            if updated != 1:
                return None
            worker = row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()) or {}
    worker["worker_token"] = token
    return worker


def get_enabled_worker_token(token: str) -> dict[str, Any] | None:
    initialize()
    token = str(token or "").strip()
    if not token:
        return None
    token_hash = worker_token_hash(token)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            row = connection.execute(
                """
                SELECT * FROM workers
                WHERE token_hash = ? AND enabled = 1 AND deleted_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE worker_tokens SET last_used_at = strftime('%s', 'now') WHERE token_hash = ?",
                    (token_hash,),
                )
                connection.execute(
                    "UPDATE workers SET token_last_used_at = strftime('%s', 'now'), updated_at = strftime('%s', 'now') WHERE token_hash = ?",
                    (token_hash,),
                )
                return row_to_dict(row)
            return None


def get_worker_by_token(
    token: str,
    *,
    allow_disabled: bool = False,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    initialize()
    token = str(token or "").strip()
    if not token:
        return None
    token_hash = worker_token_hash(token)
    enabled_clause = "" if allow_disabled else "AND enabled = 1"
    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            row = connection.execute(
                f"""
                SELECT * FROM workers
                WHERE token_hash = ? {enabled_clause} {deleted_clause}
                """,
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE workers SET token_last_used_at = strftime('%s', 'now'), updated_at = strftime('%s', 'now') WHERE token_hash = ?",
                (token_hash,),
            )
            return row_to_dict(row)


def upsert_worker_heartbeat(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    worker_id = str(record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    timestamp = int(record.get("timestamp") or time.time())
    max_concurrent_jobs = normalize_worker_capacity(
        record.get("max_concurrent_jobs"),
        cap=record.get("max_concurrency_cap"),
    )
    running_jobs = max(0, min(max_concurrent_jobs, int(record.get("running_jobs") or 0)))
    free_slots = max(0, min(max_concurrent_jobs, int(record.get("free_slots") or 0)))
    provider = str(record.get("provider") or "codex")[:60]
    provider_chain = provider_list_json(record.get("provider_chain"), fallback=[provider])
    ready_providers = heartbeat_ready_providers_json(record)
    machine_metrics = record.get("machine_metrics")
    machine_metrics_history = record.get("machine_metrics_history")
    machine_metrics_text = (
        json.dumps(to_jsonable(machine_metrics), ensure_ascii=False, sort_keys=True)
        if isinstance(machine_metrics, dict)
        else None
    )
    machine_metrics_history_text = (
        json.dumps(to_jsonable(machine_metrics_history), ensure_ascii=False, sort_keys=True)
        if isinstance(machine_metrics_history, list)
        else None
    )
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO workers (
                    worker_id, name, version, provider, provider_chain, enabled, max_concurrent_jobs, running_jobs,
                    free_slots, hostname, region, last_error, status, first_seen_at, last_heartbeat_at,
                    created_at, updated_at, doctor_status, codex_ready, ready_providers,
                    systemd_active, doctor_checked_at, machine_metrics, machine_metrics_history
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    version = excluded.version,
                    provider = excluded.provider,
                    provider_chain = COALESCE(excluded.provider_chain, workers.provider_chain),
                    max_concurrent_jobs = excluded.max_concurrent_jobs,
                    running_jobs = excluded.running_jobs,
                    free_slots = excluded.free_slots,
                    hostname = excluded.hostname,
                    region = COALESCE(NULLIF(excluded.region, ''), workers.region),
                    last_error = excluded.last_error,
                    doctor_status = COALESCE(excluded.doctor_status, workers.doctor_status),
                    codex_ready = COALESCE(excluded.codex_ready, workers.codex_ready),
                    ready_providers = COALESCE(excluded.ready_providers, workers.ready_providers),
                    systemd_active = COALESCE(excluded.systemd_active, workers.systemd_active),
                    doctor_checked_at = COALESCE(excluded.doctor_checked_at, workers.doctor_checked_at),
                    machine_metrics = COALESCE(excluded.machine_metrics, workers.machine_metrics),
                    machine_metrics_history = COALESCE(excluded.machine_metrics_history, workers.machine_metrics_history),
                    status = CASE WHEN workers.enabled = 0 THEN 'disabled' ELSE 'online' END,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    updated_at = excluded.updated_at
                """,
                (
                    worker_id,
                    record.get("name") or worker_id,
                    record.get("version"),
                    provider,
                    provider_chain,
                    max_concurrent_jobs,
                    running_jobs,
                    free_slots,
                    record.get("hostname"),
                    record.get("region"),
                    record.get("last_error"),
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    record.get("doctor_status"),
                    record.get("codex_ready"),
                    ready_providers,
                    record.get("systemd_active"),
                    record.get("doctor_checked_at"),
                    machine_metrics_text,
                    machine_metrics_history_text,
                ),
            )
            row = row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()) or {}
            if row.get("enabled") == 0:
                row["status"] = "disabled"
            return row


def record_worker_audit_event(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    event_id = str(record.get("id") or stable_id("wae", f"{record.get('action')}:{time.time_ns()}"))
    changed_fields = record.get("changed_fields")
    changed_text = changed_fields if isinstance(changed_fields, str) else json.dumps(changed_fields or {}, sort_keys=True)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO worker_audit_events (
                    id, actor_user_id, action, worker_id, changed_fields,
                    request_id, created_at, success, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(record.get("actor_user_id") or ""),
                    str(record.get("action") or ""),
                    record.get("worker_id"),
                    changed_text,
                    record.get("request_id"),
                    int(record.get("created_at") or time.time()),
                    1 if record.get("success", True) else 0,
                    record.get("error"),
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM worker_audit_events WHERE id = ?", (event_id,)).fetchone()) or {}


def list_worker_audit_events(worker_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        if worker_id:
            rows = connection.execute(
                """
                SELECT * FROM worker_audit_events
                WHERE worker_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (worker_id, max(1, min(500, int(limit)))),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM worker_audit_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]


def create_worker_command(record: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    worker_id = str(record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    command = normalize_worker_lifecycle_command(record.get("command"))
    timestamp = int(record.get("created_at") or time.time())
    command_id = str(record.get("id") or stable_id("wcmd", f"{worker_id}:{command}:{time.time_ns()}"))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ? AND deleted_at IS NULL",
                (worker_id,),
            ).fetchone()
            if not worker:
                return None
            active = connection.execute(
                """
                SELECT * FROM worker_commands
                WHERE worker_id = ? AND status IN ('pending', 'running')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            if active:
                raise ValueError("Worker already has an active lifecycle command.")
            connection.execute(
                """
                INSERT INTO worker_commands (
                    id, worker_id, command, status, requested_by_user_id,
                    request_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    command_id,
                    worker_id,
                    command,
                    record.get("requested_by_user_id"),
                    record.get("request_id"),
                    timestamp,
                    timestamp,
                ),
            )
            if command == "uninstall":
                connection.execute(
                    """
                    UPDATE workers
                    SET enabled = 0,
                        deleted_at = COALESCE(deleted_at, ?),
                        disabled_at = COALESCE(disabled_at, ?),
                        updated_at = ?
                    WHERE worker_id = ?
                    """,
                    (timestamp, timestamp, timestamp, worker_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE workers
                    SET enabled = 0,
                        disabled_at = COALESCE(disabled_at, ?),
                        updated_at = ?
                    WHERE worker_id = ?
                    """,
                    (timestamp, timestamp, worker_id),
                )
            return row_to_dict(connection.execute("SELECT * FROM worker_commands WHERE id = ?", (command_id,)).fetchone())


def get_worker_command(command_id: str, *, worker_id: str | None = None) -> dict[str, Any] | None:
    initialize()
    command_id = str(command_id or "").strip()
    if not command_id:
        return None
    worker_clause = "AND worker_id = ?" if worker_id else ""
    values: tuple[Any, ...] = (command_id, worker_id) if worker_id else (command_id,)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                f"SELECT * FROM worker_commands WHERE id = ? {worker_clause}",
                values,
            ).fetchone()
        )


def get_latest_worker_command(worker_id: str) -> dict[str, Any] | None:
    initialize()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT * FROM worker_commands
                WHERE worker_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        )


def get_next_worker_command(worker_id: str) -> dict[str, Any] | None:
    initialize()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT * FROM worker_commands
                WHERE worker_id = ? AND status IN ('pending', 'running')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        )


def update_worker_command_status(record: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    command_id = str(record.get("id") or "").strip()
    worker_id = str(record.get("worker_id") or "").strip()
    status = str(record.get("status") or "").strip().lower()
    if not command_id:
        raise ValueError("command id is required")
    if not worker_id:
        raise ValueError("worker_id is required")
    if status not in WORKER_COMMAND_ACTIVE_STATUSES | WORKER_COMMAND_TERMINAL_STATUSES:
        raise ValueError("Worker command status must be pending, running, succeeded, failed, or cancelled.")
    timestamp = int(record.get("timestamp") or time.time())
    error = str(record.get("error") or "")[:500] if status == "failed" else None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            command = connection.execute(
                "SELECT * FROM worker_commands WHERE id = ? AND worker_id = ?",
                (command_id, worker_id),
            ).fetchone()
            if not command:
                return None
            existing_status = str(command["status"] or "")
            if existing_status in WORKER_COMMAND_TERMINAL_STATUSES:
                return row_to_dict(command)
            started_at = command["started_at"] or (timestamp if status == "running" else None)
            completed_at = timestamp if status in WORKER_COMMAND_TERMINAL_STATUSES else command["completed_at"]
            connection.execute(
                """
                UPDATE worker_commands
                SET status = ?,
                    error = ?,
                    started_at = COALESCE(?, started_at),
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND worker_id = ?
                """,
                (status, error, started_at, completed_at, timestamp, command_id, worker_id),
            )
            if status == "succeeded":
                if command["command"] == "uninstall":
                    connection.execute(
                        """
                        UPDATE workers
                        SET enabled = 0,
                            deleted_at = COALESCE(deleted_at, ?),
                            disabled_at = COALESCE(disabled_at, ?),
                            updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (timestamp, timestamp, timestamp, worker_id),
                    )
                elif command["command"] == "stop":
                    connection.execute(
                        """
                        UPDATE workers
                        SET enabled = 0,
                            disabled_at = COALESCE(disabled_at, ?),
                            updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (timestamp, timestamp, worker_id),
                    )
            return row_to_dict(
                connection.execute(
                    "SELECT * FROM worker_commands WHERE id = ? AND worker_id = ?",
                    (command_id, worker_id),
                ).fetchone()
            )


def cleanup_operational_records(
    *,
    timestamp: int | None = None,
    worker_command_retention_seconds: int = 30 * 24 * 60 * 60,
    worker_audit_retention_seconds: int = 90 * 24 * 60 * 60,
    scan_job_retention_seconds: int = 30 * 24 * 60 * 60,
    removable_scan_ids: set[str] | None = None,
) -> dict[str, int]:
    initialize()
    current_time = int(timestamp if timestamp is not None else time.time())
    command_cutoff = current_time - max(0, int(worker_command_retention_seconds))
    audit_cutoff = current_time - max(0, int(worker_audit_retention_seconds))
    job_cutoff = current_time - max(0, int(scan_job_retention_seconds))
    with _LOCK, closing(connect()) as connection:
        with connection:
            command_deleted = connection.execute(
                """
                DELETE FROM worker_commands
                WHERE status IN ('succeeded', 'failed', 'cancelled')
                  AND COALESCE(completed_at, updated_at, created_at) < ?
                """,
                (command_cutoff,),
            ).rowcount
            audit_deleted = connection.execute(
                """
                DELETE FROM worker_audit_events
                WHERE created_at < ?
                """,
                (audit_cutoff,),
            ).rowcount
            job_deleted = 0
            if removable_scan_ids:
                scan_ids = sorted(str(scan_id).strip() for scan_id in removable_scan_ids if str(scan_id).strip())
                if scan_ids:
                    placeholders = ",".join("?" for _ in scan_ids)
                    job_deleted = connection.execute(
                        f"""
                        DELETE FROM scan_jobs
                        WHERE status IN ('done', 'failed', 'cancelled', 'lost')
                          AND COALESCE(completed_at, updated_at, created_at) < ?
                          AND scan_id IN ({placeholders})
                        """,
                        (job_cutoff, *scan_ids),
                    ).rowcount
    return {
        "worker_commands": max(0, command_deleted),
        "worker_audit_events": max(0, audit_deleted),
        "scan_jobs": max(0, job_deleted),
    }


def create_scan_job(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    job_id = str(record.get("job_id") or stable_id("job", record.get("scan_id"))).strip()
    scan_id = str(record.get("scan_id") or "").strip()
    repo = str(record.get("repo") or "").strip()
    if not job_id or not scan_id or not repo:
        raise ValueError("job_id, scan_id, and repo are required")
    timestamp = int(record.get("created_at") or time.time())
    provider_chain = provider_list_json(record.get("provider_chain"))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO scan_jobs (
                    job_id, scan_id, repo, branch, "commit", status, attempt,
                    claimed_by_worker_id, claimed_at, started_at, completed_at,
                    timeout_at, error, result_checksum, created_at, updated_at,
                    user_id, repo_id, github_repo_id, installation_id, clone_url,
                    progress_phase, progress, progress_message, logs_summary, max_attempts,
                    review_output_language, provider_chain
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?,
                    ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?, ?)
                ON CONFLICT(scan_id) DO NOTHING
                """,
                (
                    job_id,
                    scan_id,
                    repo,
                    str(record.get("branch") or "main"),
                    str(record.get("commit") or "pending"),
                    str(record.get("status") or "queued"),
                    timestamp,
                    timestamp,
                    record.get("user_id"),
                    record.get("repo_id"),
                    record.get("github_repo_id"),
                    record.get("installation_id"),
                    record.get("clone_url"),
                    max(1, int(record.get("max_attempts") or 3)),
                    record.get("review_output_language"),
                    provider_chain,
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone()) or {}


def get_scan_job(job_id: str) -> dict[str, Any] | None:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())


def get_scan_job_for_scan(scan_id: str) -> dict[str, Any] | None:
    initialize()
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone())


def retry_scan_job(scan_id: str, *, timestamp: int | None = None) -> dict[str, Any] | None:
    initialize()
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            cursor = connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'queued',
                    claimed_by_worker_id = NULL,
                    claimed_at = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    timeout_at = NULL,
                    error = NULL,
                    result_checksum = NULL,
                    progress_phase = NULL,
                    progress = 0,
                    progress_message = NULL,
                    logs_summary = NULL,
                    last_attempt_id = NULL,
                    created_at = ?,
                    updated_at = ?
                WHERE scan_id = ?
                  AND status IN ('failed', 'cancelled', 'lost')
                """,
                (current_time, current_time, scan_id),
            )
            if cursor.rowcount != 1:
                return None
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone())


def list_scan_jobs_missing_from_state(scan_ids: list[str] | set[str]) -> list[dict[str, Any]]:
    initialize()
    existing_ids = sorted({str(scan_id or "").strip() for scan_id in scan_ids if str(scan_id or "").strip()})
    query = "SELECT * FROM scan_jobs"
    params: list[Any] = []
    if existing_ids:
        placeholders = ",".join("?" for _ in existing_ids)
        query += f" WHERE scan_id NOT IN ({placeholders})"
        params.extend(existing_ids)
    query += " ORDER BY created_at ASC, job_id ASC"
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return [row_to_dict(row) or {} for row in connection.execute(query, params).fetchall()]


def list_orphan_scan_quota_consumptions(scan_ids: list[str] | set[str]) -> list[dict[str, Any]]:
    initialize()
    existing_ids = sorted({str(scan_id or "").strip() for scan_id in scan_ids if str(scan_id or "").strip()})
    params: list[Any] = []
    existing_clause = ""
    if existing_ids:
        placeholders = ",".join("?" for _ in existing_ids)
        existing_clause = f"AND q.scan_id NOT IN ({placeholders})"
        params.extend(existing_ids)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT
                q.scan_id,
                q.requested_by_user_id,
                q.request_id,
                COUNT(*) AS ledger_rows
            FROM quota_ledger q
            LEFT JOIN scan_jobs sj ON sj.scan_id = q.scan_id
            WHERE q.reason = 'scan_created'
              AND q.delta > 0
              AND q.scan_id IS NOT NULL
              AND q.scan_id != ''
              AND sj.scan_id IS NULL
              {existing_clause}
            GROUP BY q.scan_id, q.requested_by_user_id, q.request_id
            ORDER BY MIN(q.created_at) ASC, q.scan_id ASC
            """,
            params,
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]


def update_scan_job_commit(job_id: str, commit: str) -> dict[str, Any] | None:
    initialize()
    job_id = str(job_id or "").strip()
    commit = str(commit or "").strip()
    if not job_id or not commit:
        return None
    current_time = int(time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE scan_jobs
                SET "commit" = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (commit, current_time, job_id),
            )
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())


def renew_worker_scan_job_leases(
    worker_id: str,
    job_ids: list[str],
    *,
    lease_seconds: int = 3600,
    timestamp: int | None = None,
) -> int:
    initialize()
    worker_id = str(worker_id or "").strip()
    unique_job_ids = []
    seen = set()
    for value in job_ids or []:
        job_id = str(value or "").strip()
        if job_id and job_id not in seen:
            unique_job_ids.append(job_id)
            seen.add(job_id)
    if not worker_id or not unique_job_ids:
        return 0
    current_time = int(timestamp if timestamp is not None else time.time())
    timeout_at = current_time + max(60, int(lease_seconds or 3600))
    placeholders = ",".join("?" for _ in unique_job_ids)
    with _LOCK, closing(connect()) as connection:
        with connection:
            cursor = connection.execute(
                f"""
                UPDATE scan_jobs
                SET timeout_at = CASE
                        WHEN timeout_at IS NULL OR timeout_at < ? THEN ?
                        ELSE timeout_at
                    END,
                    updated_at = ?
                WHERE claimed_by_worker_id = ?
                  AND status IN ('claimed', 'running', 'uploading_result')
                  AND job_id IN ({placeholders})
                """,
                (timeout_at, timeout_at, current_time, worker_id, *unique_job_ids),
            )
            return max(0, cursor.rowcount)


def list_worker_task_activity(worker_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    initialize()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return []
    safe_limit = max(1, min(500, int(limit or 50)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM scan_jobs
            WHERE claimed_by_worker_id = ?
              AND (
                claimed_at IS NOT NULL
                OR started_at IS NOT NULL
                OR completed_at IS NOT NULL
              )
            ORDER BY MAX(
                       COALESCE(completed_at, 0),
                       COALESCE(started_at, 0),
                       COALESCE(claimed_at, 0),
                       COALESCE(updated_at, 0),
                       COALESCE(created_at, 0)
                     ) DESC,
                     job_id ASC
            LIMIT ?
            """,
            (worker_id, safe_limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_completed_scan_job_results() -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                sj.*,
                jr.attempt_id AS result_attempt_id,
                jr.result_checksum AS result_result_checksum,
                jr.status AS result_status,
                jr.payload AS result_payload,
                jr.created_at AS result_created_at
            FROM scan_jobs sj
            JOIN job_results jr ON jr.job_id = sj.job_id
            WHERE sj.status IN ('done', 'failed')
              AND jr.attempt_id = sj.last_attempt_id
            ORDER BY sj.completed_at ASC, sj.job_id ASC
            """
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row) or {}
        try:
            item["result_payload"] = json.loads(str(item.get("result_payload") or "{}"))
        except (TypeError, json.JSONDecodeError):
            item["result_payload"] = {}
        results.append(item)
    return results


def claim_next_scan_jobs(
    worker_id: str,
    *,
    max_jobs: int = 1,
    lease_seconds: int = 3600,
    per_user_running_limit: int = 1,
    worker_heartbeat_timeout_seconds: int = 120,
    ready_providers: list[str] | None = None,
    timestamp: int | None = None,
) -> list[dict[str, Any]]:
    initialize()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    current_time = int(timestamp if timestamp is not None else time.time())
    timeout_at = current_time + max(60, int(lease_seconds))
    requested = max(1, int(max_jobs or 1))
    per_user_limit = max(1, int(per_user_running_limit or 1))
    ready_provider_set = set(normalize_provider_list(ready_providers)) if ready_providers is not None else None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            worker = connection.execute(
                "SELECT enabled, deleted_at FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker and (int(worker["enabled"] or 0) == 0 or worker["deleted_at"] is not None):
                connection.commit()
                return []
            offline_after = max(60, int(worker_heartbeat_timeout_seconds or 120))
            connection.execute(
                """
                UPDATE workers
                SET status = 'offline'
                WHERE status = 'online' AND last_heartbeat_at < ?
                """,
                (current_time - offline_after,),
            )
            _requeue_expired_jobs_locked(connection, current_time)
            _requeue_stale_worker_jobs_locked(connection, current_time, offline_after)
            claimed: list[dict[str, Any]] = []
            running_rows = connection.execute(
                """
                SELECT user_id, COUNT(*) AS count
                FROM scan_jobs
                WHERE status IN ('claimed', 'running', 'uploading_result')
                GROUP BY user_id
                """
            ).fetchall()
            running_by_user = {str(row["user_id"] or ""): int(row["count"]) for row in running_rows}
            rows = connection.execute(
                """
                SELECT * FROM scan_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC, job_id ASC
                """
            ).fetchall()
            if not rows:
                connection.commit()
                return []
            for row in rows:
                if len(claimed) >= requested:
                    break
                if ready_provider_set is not None:
                    job_provider_chain = normalize_provider_list(row["provider_chain"])
                    if not ready_provider_set or (job_provider_chain and ready_provider_set.isdisjoint(job_provider_chain)):
                        continue
                user_id = str(row["user_id"] or "")
                if running_by_user.get(user_id, 0) >= per_user_limit:
                    continue
                job_id = row["job_id"]
                updated = connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'claimed',
                        attempt = attempt + 1,
                        claimed_by_worker_id = ?,
                        claimed_at = ?,
                        timeout_at = ?,
                        error = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (worker_id, current_time, timeout_at, current_time, job_id),
                ).rowcount
                if updated != 1:
                    continue
                claimed_job = row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())
                if claimed_job:
                    claimed.append(claimed_job)
                running_by_user[user_id] = running_by_user.get(user_id, 0) + 1
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise


def claim_next_scan_job(worker_id: str, *, lease_seconds: int = 3600, timestamp: int | None = None) -> dict[str, Any] | None:
    jobs = claim_next_scan_jobs(worker_id, lease_seconds=lease_seconds, timestamp=timestamp)
    return jobs[0] if jobs else None


def recover_expired_scan_jobs(
    timestamp: int | None = None,
    *,
    worker_heartbeat_timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    initialize()
    current_time = int(timestamp if timestamp is not None else time.time())
    offline_after = max(60, int(worker_heartbeat_timeout_seconds or 120))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            recovered = _requeue_expired_jobs_locked(connection, current_time)
            recovered.extend(_requeue_stale_worker_jobs_locked(connection, current_time, offline_after))
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise


def requeue_interrupted_scan_job(scan_id: str, *, reason: str = "server_restart", timestamp: int | None = None) -> dict[str, Any] | None:
    initialize()
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'queued',
                    claimed_by_worker_id = NULL,
                    claimed_at = NULL,
                    started_at = NULL,
                    timeout_at = NULL,
                    error = ?,
                    updated_at = ?
                WHERE scan_id = ?
                  AND status IN ('claimed', 'running', 'uploading_result')
                """,
                (reason, current_time, scan_id),
            )
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone())


def _requeue_expired_jobs_locked(connection: sqlite3.Connection, current_time: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT job_id, scan_id, attempt, max_attempts
        FROM scan_jobs
        WHERE status IN ('claimed', 'running', 'uploading_result') AND timeout_at IS NOT NULL AND timeout_at <= ?
        """,
        (current_time,),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        if int(row["attempt"]) < int(row["max_attempts"]):
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'retrying',
                    error = 'timed_out',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, row["job_id"]),
            )
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'queued',
                    claimed_by_worker_id = NULL,
                    claimed_at = NULL,
                    started_at = NULL,
                    timeout_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, row["job_id"]),
            )
            recovered.append(
                {
                    "job_id": row["job_id"],
                    "scan_id": row["scan_id"],
                    "status": "queued",
                    "reason": "timed_out",
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                }
            )
        else:
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'lost',
                    completed_at = ?,
                    timeout_at = NULL,
                    error = 'timed_out',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, current_time, row["job_id"]),
            )
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'failed',
                    completed_at = ?,
                    timeout_at = NULL,
                    error = 'timed_out',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, current_time, row["job_id"]),
            )
            recovered.append(
                {
                    "job_id": row["job_id"],
                    "scan_id": row["scan_id"],
                    "status": "failed",
                    "reason": "timed_out",
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                }
            )
    return recovered


def _requeue_stale_worker_jobs_locked(
    connection: sqlite3.Connection,
    current_time: int,
    offline_after: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT sj.job_id, sj.scan_id, sj.attempt, sj.max_attempts
        FROM scan_jobs sj
        JOIN workers w ON w.worker_id = sj.claimed_by_worker_id
        WHERE sj.status IN ('claimed', 'running', 'uploading_result')
          AND w.last_heartbeat_at IS NOT NULL
          AND w.last_heartbeat_at < ?
        """,
        (current_time - max(60, int(offline_after)),),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        if int(row["attempt"]) < int(row["max_attempts"]):
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'queued',
                    claimed_by_worker_id = NULL,
                    claimed_at = NULL,
                    started_at = NULL,
                    timeout_at = NULL,
                    error = 'worker_heartbeat_timed_out',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, row["job_id"]),
            )
            recovered.append(
                {
                    "job_id": row["job_id"],
                    "scan_id": row["scan_id"],
                    "status": "queued",
                    "reason": "worker_heartbeat_timed_out",
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                }
            )
        else:
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'failed',
                    completed_at = ?,
                    timeout_at = NULL,
                    error = 'worker_heartbeat_timed_out',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, current_time, row["job_id"]),
            )
            recovered.append(
                {
                    "job_id": row["job_id"],
                    "scan_id": row["scan_id"],
                    "status": "failed",
                    "reason": "worker_heartbeat_timed_out",
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                }
            )
    return recovered


def update_scan_job_progress(job_id: str, progress: dict[str, Any]) -> dict[str, Any] | None:
    initialize()
    current_time = int(time.time())
    raw_timeout_at = progress.get("timeout_at")
    try:
        timeout_at = int(raw_timeout_at) if raw_timeout_at is not None else None
    except (TypeError, ValueError):
        timeout_at = None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            cursor = connection.execute(
                """
                UPDATE scan_jobs
                SET progress_phase = ?,
                    progress = ?,
                    progress_message = ?,
                    status = 'running',
                    started_at = COALESCE(started_at, ?),
                    timeout_at = CASE
                        WHEN ? IS NULL THEN timeout_at
                        WHEN timeout_at IS NULL OR timeout_at < ? THEN ?
                        ELSE timeout_at
                    END,
                    logs_summary = ?,
                    updated_at = ?
                WHERE job_id = ? AND status IN ('claimed', 'running')
                """,
                (
                    progress.get("phase"),
                    max(0, min(100, int(progress.get("progress") or 0))),
                    progress.get("message"),
                    int(progress.get("started_at") or current_time),
                    timeout_at,
                    timeout_at,
                    timeout_at,
                    progress.get("logs_summary"),
                    current_time,
                    job_id,
                ),
            )
            if cursor.rowcount <= 0:
                return None
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())


def cancel_scan_job_for_scan(scan_id: str) -> None:
    initialize()
    current_time = int(time.time())
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'cancelled',
                    completed_at = COALESCE(completed_at, ?),
                    timeout_at = NULL,
                    updated_at = ?
                WHERE scan_id = ? AND status IN ('queued', 'claimed', 'running', 'uploading_result')
                """,
                (current_time, current_time, scan_id),
            )


def record_scan_job_result(
    job_id: str,
    *,
    attempt_id: str,
    status: str,
    result_checksum: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    initialize()
    job_id = str(job_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    status = str(status or "").strip().lower()
    result_checksum = str(result_checksum or "").strip()
    if not job_id or not attempt_id or not result_checksum:
        raise ValueError("job_id, attempt_id, and result_checksum are required")
    if status not in {"done", "failed"}:
        raise ValueError("status must be done or failed")
    current_time = int(time.time())
    payload_text = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            job = connection.execute(
                "SELECT status, last_attempt_id, claimed_by_worker_id, attempt FROM scan_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not job:
                connection.commit()
                return {"accepted": False, "duplicate": False, "conflict": True}
            existing = connection.execute(
                "SELECT * FROM job_results WHERE job_id = ? AND attempt_id = ?",
                (job_id, attempt_id),
            ).fetchone()
            if existing:
                if existing["result_checksum"] == result_checksum:
                    connection.commit()
                    return {"accepted": True, "duplicate": True, "conflict": False}
                connection.commit()
                return {"accepted": False, "duplicate": True, "conflict": True}
            if job["status"] not in {"claimed", "running", "uploading_result"}:
                connection.commit()
                return {"accepted": False, "duplicate": False, "conflict": True}
            claimed_worker_id = str(job["claimed_by_worker_id"] or "")
            try:
                attempt = int(job["attempt"] or 0)
            except (TypeError, ValueError):
                attempt = 0
            expected_attempt_id = f"{claimed_worker_id}-{attempt}" if claimed_worker_id and attempt else ""
            if not expected_attempt_id or attempt_id != expected_attempt_id:
                connection.commit()
                return {"accepted": False, "duplicate": False, "conflict": True}
            connection.execute(
                """
                INSERT INTO job_results (id, job_id, attempt_id, result_checksum, status, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stable_id("jr", f"{job_id}:{attempt_id}"), job_id, attempt_id, result_checksum, status, payload_text),
            )
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'uploading_result',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (current_time, job_id),
            )
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = ?,
                    completed_at = ?,
                    timeout_at = NULL,
                    error = ?,
                    result_checksum = ?,
                    last_attempt_id = ?,
                    progress = CASE WHEN ? = 'done' THEN 100 ELSE progress END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    current_time,
                    payload.get("error"),
                    result_checksum,
                    attempt_id,
                    status,
                    current_time,
                    job_id,
                ),
            )
            connection.commit()
            return {"accepted": True, "duplicate": False, "conflict": False}
        except Exception:
            connection.rollback()
            raise


def record_review_decision_events(events: list[dict[str, Any]]) -> dict[str, int]:
    initialize()
    sanitized = [event for event in events if isinstance(event, dict)]
    if not sanitized:
        return {"inserted": 0, "duplicates": 0}
    inserted = 0
    duplicates = 0
    with _LOCK, closing(connect()) as connection:
        with connection:
            for event in sanitized:
                event_id = str(event.get("event_id") or "").strip()
                observation_key = str(event.get("candidate_observation_key") or "").strip()
                job_id = str(event.get("job_id") or "").strip()
                attempt_id = str(event.get("attempt_id") or "").strip()
                decision = str(event.get("decision") or "").strip()
                protocol = str(event.get("protocol") or "").strip()
                if not event_id or not observation_key or not job_id or not attempt_id or not decision or not protocol:
                    continue
                before = connection.total_changes
                connection.execute(
                    """
                    INSERT OR IGNORE INTO review_decision_events (
                        event_id, protocol, candidate_observation_key, scan_id, job_id, attempt_id,
                        user_id, repo_id, github_repo_id, repo_full_name, branch, commit_sha,
                        base_sha, head_sha, candidate_id, fingerprint, source, provider, model,
                        category, severity, verification_status, file_path, line_start, line_end,
                        normalized_title, raw_confidence, calibrated_confidence,
                        source_reliability_mean, source_reliability_lb, source_adjustment,
                        evidence_strength, delta_relevance, category_adjustment, truth_probability,
                        decision_score, decision, decision_reason, scoring_protocol,
                        score_factors_json, created_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        event_id,
                        protocol,
                        observation_key,
                        event.get("scan_id"),
                        job_id,
                        attempt_id,
                        event.get("user_id"),
                        event.get("repo_id"),
                        event.get("github_repo_id"),
                        event.get("repo_full_name"),
                        event.get("branch"),
                        event.get("commit_sha"),
                        event.get("base_sha"),
                        event.get("head_sha"),
                        event.get("candidate_id"),
                        event.get("fingerprint"),
                        event.get("source"),
                        event.get("provider"),
                        event.get("model"),
                        event.get("category"),
                        event.get("severity"),
                        event.get("verification_status"),
                        event.get("file_path"),
                        event.get("line_start"),
                        event.get("line_end"),
                        event.get("normalized_title"),
                        event.get("raw_confidence"),
                        event.get("calibrated_confidence"),
                        event.get("source_reliability_mean"),
                        event.get("source_reliability_lb"),
                        event.get("source_adjustment"),
                        event.get("evidence_strength"),
                        event.get("delta_relevance"),
                        event.get("category_adjustment"),
                        event.get("truth_probability"),
                        event.get("decision_score"),
                        decision,
                        event.get("decision_reason"),
                        event.get("scoring_protocol"),
                        json.dumps(to_jsonable(event.get("score_factors") or {}), ensure_ascii=False, sort_keys=True),
                        int(event.get("created_at") or time.time()),
                    ),
                )
                if connection.total_changes > before:
                    inserted += 1
                else:
                    duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates}


def list_review_decision_events(*, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    initialize()
    max_rows = max(1, min(500, int(limit or 100)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        if job_id:
            rows = connection.execute(
                """
                SELECT * FROM review_decision_events
                WHERE job_id = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (str(job_id), max_rows),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM review_decision_events
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (max_rows,),
            ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def list_review_decision_events_for_scope(
    *,
    user_id: str,
    repo_key: str,
    branch: str,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    initialize()
    user_id = str(user_id or "").strip()
    repo_key = str(repo_key or "").strip()
    branch = str(branch or "").strip()
    if not user_id or not repo_key or not branch:
        return []
    max_rows = max(1, min(20000, int(limit or 5000)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM review_decision_events
            WHERE user_id = ?
              AND lower(branch) = lower(?)
              AND (
                lower(repo_id) = lower(?)
                OR lower(github_repo_id) = lower(?)
                OR lower(repo_full_name) = lower(?)
              )
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            (user_id, branch, repo_key, repo_key, repo_key, max_rows),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def list_review_decision_events_for_observation(candidate_observation_key: str) -> list[dict[str, Any]]:
    initialize()
    observation_key = str(candidate_observation_key or "").strip()
    if not observation_key:
        return []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM review_decision_events
            WHERE candidate_observation_key = ?
            ORDER BY created_at DESC, event_id DESC
            """,
            (observation_key,),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def upsert_review_outcome_label(label: dict[str, Any]) -> dict[str, Any]:
    initialize()
    label_id = str(label.get("label_id") or "").strip()
    observation_key = str(label.get("candidate_observation_key") or "").strip()
    outcome_label = str(label.get("outcome_label") or "").strip()
    label_source = str(label.get("label_source") or "").strip()
    if not label_id or not observation_key or not outcome_label or not label_source:
        raise ValueError("label_id, candidate_observation_key, outcome_label, and label_source are required")
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO review_outcome_labels (
                    label_id, event_id, candidate_observation_key, outcome_label,
                    label_source, outcome_weight, label_reason, created_at, created_by,
                    calibration_success_weight, calibration_failure_weight
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    candidate_observation_key = excluded.candidate_observation_key,
                    outcome_label = excluded.outcome_label,
                    label_source = excluded.label_source,
                    outcome_weight = excluded.outcome_weight,
                    label_reason = excluded.label_reason,
                    created_at = excluded.created_at,
                    created_by = excluded.created_by,
                    calibration_success_weight = CASE
                        WHEN review_outcome_labels.label_source = 'user_explicit'
                            AND review_outcome_labels.outcome_label != excluded.outcome_label
                        THEN review_outcome_labels.calibration_success_weight + excluded.calibration_success_weight
                        ELSE excluded.calibration_success_weight
                    END,
                    calibration_failure_weight = CASE
                        WHEN review_outcome_labels.label_source = 'user_explicit'
                            AND review_outcome_labels.outcome_label != excluded.outcome_label
                        THEN review_outcome_labels.calibration_failure_weight + excluded.calibration_failure_weight
                        ELSE excluded.calibration_failure_weight
                    END
                """,
                (
                    label_id,
                    label.get("event_id"),
                    observation_key,
                    outcome_label,
                    label_source,
                    float(label.get("outcome_weight") or 0.0),
                    label.get("label_reason"),
                    int(label.get("created_at") or time.time()),
                    label.get("created_by"),
                    float(label.get("calibration_success_weight") or 0.0),
                    float(label.get("calibration_failure_weight") or 0.0),
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_outcome_labels WHERE label_id = ?",
                (label_id,),
            ).fetchone()
    return row_to_dict(row) or {}


def list_review_outcome_labels(candidate_observation_key: str) -> list[dict[str, Any]]:
    initialize()
    observation_key = str(candidate_observation_key or "").strip()
    if not observation_key:
        return []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM review_outcome_labels
            WHERE candidate_observation_key = ?
            ORDER BY created_at DESC, label_id DESC
            """,
            (observation_key,),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def upsert_review_calibration_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    initialize()
    scope_key = str(snapshot.get("scope_key") or "").strip()
    cohort_key = str(snapshot.get("cohort_key") or "").strip()
    version = str(snapshot.get("snapshot_version") or "").strip()
    if not scope_key or not cohort_key or not version:
        raise ValueError("scope_key, cohort_key, and snapshot_version are required")
    snapshot_id = str(snapshot.get("id") or stable_id("rcs", f"{scope_key}:{cohort_key}:{version}"))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO review_calibration_snapshots (
                    id, scope_key, cohort_key, snapshot_version, effective_samples,
                    posterior_alpha, posterior_beta, posterior_mean, posterior_lb,
                    confidence_buckets_json, metadata_json, drift_state, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key, cohort_key, snapshot_version) DO UPDATE SET
                    effective_samples = excluded.effective_samples,
                    posterior_alpha = excluded.posterior_alpha,
                    posterior_beta = excluded.posterior_beta,
                    posterior_mean = excluded.posterior_mean,
                    posterior_lb = excluded.posterior_lb,
                    confidence_buckets_json = excluded.confidence_buckets_json,
                    metadata_json = excluded.metadata_json,
                    drift_state = excluded.drift_state,
                    created_at = excluded.created_at
                """,
                (
                    snapshot_id,
                    scope_key,
                    cohort_key,
                    version,
                    float(snapshot.get("effective_samples") or 0.0),
                    snapshot.get("posterior_alpha"),
                    snapshot.get("posterior_beta"),
                    snapshot.get("posterior_mean"),
                    snapshot.get("posterior_lb"),
                    json.dumps(to_jsonable(snapshot.get("confidence_buckets") or {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(to_jsonable(snapshot.get("metadata") or {}), ensure_ascii=False, sort_keys=True),
                    snapshot.get("drift_state"),
                    int(snapshot.get("created_at") or time.time()),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM review_calibration_snapshots
                WHERE scope_key = ? AND cohort_key = ? AND snapshot_version = ?
                """,
                (scope_key, cohort_key, version),
            ).fetchone()
    return row_to_dict(row) or {}


def list_review_calibration_snapshots(scope_key: str) -> list[dict[str, Any]]:
    initialize()
    scope_key = str(scope_key or "").strip()
    if not scope_key:
        return []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM review_calibration_snapshots
            WHERE scope_key = ?
            ORDER BY created_at DESC, cohort_key ASC
            """,
            (scope_key,),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def repository_id_for_github_repo(github_repo_id: object) -> str:
    return stable_id("repo", github_repo_id)


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


def find_repo_fingerprint_match(
    repository_id: str,
    source_fingerprint: str,
) -> dict[str, Any] | None:
    initialize()
    repository_id = str(repository_id or "").strip()
    source_fingerprint = str(source_fingerprint or "").strip()
    if not repository_id or not source_fingerprint:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT rf.*
                FROM repo_fingerprints rf
                WHERE rf.repository_id != ?
                  AND rf.source_fingerprint = ?
                ORDER BY rf.computed_at ASC
                LIMIT 1
                """,
                (repository_id, source_fingerprint),
            ).fetchone()
        )


def create_api_key(record: dict[str, Any]) -> dict[str, Any]:
    initialize()
    api_key_id = str(record.get("id") or "").strip()
    user_id = str(record.get("user_id") or "").strip()
    name = str(record.get("name") or "API key").strip() or "API key"
    key_prefix = str(record.get("key_prefix") or "").strip()
    key_hash = str(record.get("key_hash") or "").strip()
    scopes = record.get("scopes")
    if not api_key_id or not user_id or not key_prefix or not key_hash:
        raise ValueError("api key id, user_id, prefix, and hash are required")
    scopes_text = scopes if isinstance(scopes, str) else json.dumps(scopes or [], sort_keys=True)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO api_keys (
                    id, user_id, name, key_prefix, key_hash, scopes,
                    created_at, last_used_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'), NULL, NULL)
                """,
                (api_key_id, user_id, name, key_prefix, key_hash, scopes_text),
            )
            return row_to_dict(connection.execute("SELECT * FROM api_keys WHERE id = ?", (api_key_id,)).fetchone()) or {}


def list_api_keys_for_user(user_id: str) -> list[dict[str, Any]]:
    initialize()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
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


def delete_user_related_records(user_id: str, scan_ids: list[str] | set[str] | None = None) -> dict[str, int]:
    initialize()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return {}
    target_scan_ids = [str(scan_id) for scan_id in (scan_ids or []) if str(scan_id or "").strip()]
    counts: dict[str, int] = {}
    with _LOCK, closing(connect()) as connection:
        with connection:
            counts["apiKeys"] = connection.execute(
                "DELETE FROM api_keys WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["quotaLedger"] = connection.execute(
                "DELETE FROM quota_ledger WHERE requested_by_user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["rateLimits"] = connection.execute(
                "DELETE FROM api_rate_limits WHERE subject = ?",
                (f"user:{target_user_id}",),
            ).rowcount
            counts["reviewOutcomeLabels"] = connection.execute(
                "DELETE FROM review_outcome_labels WHERE created_by = ?",
                (target_user_id,),
            ).rowcount
            counts["reviewDecisionEvents"] = connection.execute(
                "DELETE FROM review_decision_events WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["reviewCalibrationSnapshots"] = connection.execute(
                "DELETE FROM review_calibration_snapshots WHERE scope_key LIKE ?",
                (f"%user:{target_user_id}%",),
            ).rowcount
            counts["scanJobs"] = connection.execute(
                "DELETE FROM scan_jobs WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            if target_scan_ids:
                placeholders = ",".join("?" for _ in target_scan_ids)
                counts["scanJobs"] += connection.execute(
                    f"DELETE FROM scan_jobs WHERE scan_id IN ({placeholders})",
                    target_scan_ids,
                ).rowcount
    return counts


def quota_bucket_id(scope_type: str, scope_id: str, period: str, plan: str) -> str:
    return stable_id("qb", f"{scope_type}:{scope_id}:{period}:{plan}")


def quota_ledger_id(*parts: object) -> str:
    return stable_id("ql", ":".join(str(part or "") for part in parts))
