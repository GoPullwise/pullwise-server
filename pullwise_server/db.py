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
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_LOCK = threading.Lock()
_INITIALIZE_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[str] = set()
SQLITE_BUSY_TIMEOUT_SECONDS = 30
DEFAULT_STATE_ENCRYPTION_KEY_PATH = "/etc/pullwise/secrets/state-encryption-key"
STATE_ENCRYPTION_KEY_PATH_ENV = "PULLWISE_STATE_ENCRYPTION_KEY_PATH"
STATE_ENCRYPTION_MARKER = "pullwise-state-secret-v1"
STATE_ENCRYPTION_AAD = b"pullwise-server-state-secret-v1"
REVIEW_ARTIFACT_STORAGE_DIR_ENV = "PULLWISE_REVIEW_ARTIFACT_STORAGE_DIR"
LEGACY_ARTIFACT_STORAGE_DIR_ENV = "PULLWISE_ARTIFACT_STORAGE_DIR"


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
    connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_initialized() -> None:
    path = os.path.abspath(database_path())
    if path in _INITIALIZED_DATABASES and os.path.exists(path):
        return
    with _INITIALIZE_LOCK:
        if path in _INITIALIZED_DATABASES and os.path.exists(path):
            return
        initialize()
        if os.path.exists(path):
            _INITIALIZED_DATABASES.add(path)


def reset_initialization_cache() -> None:
    with _INITIALIZE_LOCK:
        _INITIALIZED_DATABASES.clear()


def initialize() -> None:
    with _LOCK, closing(connect()) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
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
                    reserved INTEGER NOT NULL DEFAULT 0,
                    reset_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE (scope_type, scope_id, period, plan)
                )
                """
            )
            ensure_column(connection, "quota_buckets", "reserved", "INTEGER NOT NULL DEFAULT 0")
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
                CREATE INDEX IF NOT EXISTS idx_quota_ledger_user_created
                ON quota_ledger(requested_by_user_id, created_at)
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
                    worker_scope TEXT NOT NULL DEFAULT 'shared',
                    owner_user_id TEXT,
                    version TEXT,
                    provider TEXT,
                    provider_chain TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    running_jobs INTEGER NOT NULL DEFAULT 0,
                    hostname TEXT,
                    region TEXT,
                    last_error TEXT,
                    doctor_status TEXT,
                    codex_ready INTEGER,
                    ready_providers TEXT,
                    codex_quota TEXT,
                    systemd_active INTEGER,
                    doctor_checked_at INTEGER,
                    machine_metrics TEXT,
                    machine_metrics_history TEXT,
                    protocol_version TEXT,
                    worker_group TEXT,
                    worker_capabilities TEXT,
                    worker_isolation TEXT,
                    worker_platform TEXT,
                    registration_json TEXT,
                    registered_at INTEGER,
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
                ("workers", "worker_scope", "TEXT NOT NULL DEFAULT 'shared'"),
                ("workers", "owner_user_id", "TEXT"),
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
                ("workers", "codex_quota", "TEXT"),
                ("workers", "systemd_active", "INTEGER"),
                ("workers", "doctor_checked_at", "INTEGER"),
                ("workers", "machine_metrics", "TEXT"),
                ("workers", "machine_metrics_history", "TEXT"),
                ("workers", "protocol_version", "TEXT"),
                ("workers", "worker_group", "TEXT"),
                ("workers", "worker_capabilities", "TEXT"),
                ("workers", "worker_isolation", "TEXT"),
                ("workers", "worker_platform", "TEXT"),
                ("workers", "registration_json", "TEXT"),
                ("workers", "registered_at", "INTEGER"),
                ("workers", "last_heartbeat_at", "INTEGER"),
            ):
                ensure_column(connection, table, column, definition)
            normalize_workers_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workers_scope_owner
                ON workers(worker_scope, owner_user_id, enabled, deleted_at, last_heartbeat_at)
                """
            )
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
                    telemetry_received_at INTEGER,
                    completed_at INTEGER,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            ensure_column(connection, "worker_commands", "telemetry_received_at", "INTEGER")
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
                    worker_scope TEXT NOT NULL DEFAULT 'shared',
                    worker_owner_user_id TEXT,
                    repo_id TEXT,
                    github_repo_id TEXT,
                    installation_id TEXT,
                    clone_url TEXT,
                    progress_phase TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    logs_summary TEXT,
                    review_output_language TEXT,
                    provider_chain TEXT,
                    last_attempt_id TEXT,
                    cancel_requested_at INTEGER,
                    cancel_reason TEXT,
                    projection_pending INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            ensure_column(connection, "scan_jobs", "review_output_language", "TEXT")
            ensure_column(connection, "scan_jobs", "provider_chain", "TEXT")
            ensure_column(connection, "scan_jobs", "last_attempt_id", "TEXT")
            ensure_column(connection, "scan_jobs", "worker_scope", "TEXT NOT NULL DEFAULT 'shared'")
            ensure_column(connection, "scan_jobs", "worker_owner_user_id", "TEXT")
            ensure_column(connection, "scan_jobs", "cancel_requested_at", "INTEGER")
            ensure_column(connection, "scan_jobs", "cancel_reason", "TEXT")
            scan_job_projection_pending_added = not any(
                row[1] == "projection_pending"
                for row in connection.execute("PRAGMA table_info(scan_jobs)").fetchall()
            )
            ensure_column(
                connection,
                "scan_jobs",
                "projection_pending",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_claimable
                ON scan_jobs(status, created_at, job_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_claimable_scope
                ON scan_jobs(status, worker_scope, user_id, created_at, job_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_worker_status
                ON scan_jobs(claimed_by_worker_id, status)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_user_status_created
                ON scan_jobs(user_id, status, created_at DESC, job_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_user_created
                ON scan_jobs(user_id, created_at DESC, job_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_user_repo_created
                ON scan_jobs(user_id, repo, created_at DESC, job_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_jobs_projection_pending
                ON scan_jobs(updated_at, job_id)
                WHERE projection_pending = 1
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_job_attempts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    claimed_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    error TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(job_id, attempt),
                    UNIQUE(job_id, worker_id),
                    FOREIGN KEY(job_id) REFERENCES scan_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_job_attempts_job
                ON scan_job_attempts(job_id, attempt)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_job_attempts_worker
                ON scan_job_attempts(worker_id, status)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    job_id TEXT,
                    repo TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scans_user_status_created
                ON scans(user_id, status, created_at DESC, scan_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scans_user_created
                ON scans(user_id, created_at DESC, scan_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scans_user_repo_created
                ON scans(user_id, repo, created_at DESC, scan_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scans_job_id
                ON scans(job_id)
                """
            )
            if scan_job_projection_pending_added:
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET projection_pending = 1
                    WHERE status IN ('failed', 'cancelled')
                      AND error IN (
                          'scan_attempts_exhausted',
                          'timed_out',
                          'worker_heartbeat_timed_out',
                          'worker_job_startup_lost',
                          'server_restart',
                          'cancel_timed_out'
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM scans s
                          WHERE s.scan_id = scan_jobs.scan_id
                            AND (
                                s.status != scan_jobs.status
                                OR COALESCE(
                                    CASE
                                        WHEN json_valid(s.payload)
                                        THEN json_extract(s.payload, '$.quotaState')
                                    END,
                                    ''
                                ) = 'reserved'
                                OR COALESCE(
                                    CASE
                                        WHEN json_valid(s.payload)
                                        THEN json_extract(s.payload, '$.recoveryReason')
                                    END,
                                    ''
                                ) != scan_jobs.error
                            )
                      )
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
            ensure_column(connection, "job_results", "payload_artifact_id", "TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_result_artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(job_id, attempt_id, kind),
                    FOREIGN KEY(job_id) REFERENCES scan_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_result_artifacts_job_attempt
                ON job_result_artifacts(job_id, attempt_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    worker_id TEXT,
                    status TEXT NOT NULL,
                    overall_risk TEXT,
                    result_status TEXT,
                    started_at INTEGER,
                    completed_at INTEGER,
                    duration_ms INTEGER,
                    protocol_version TEXT,
                    worker_version TEXT,
                    engine_type TEXT,
                    codex_thread_id TEXT,
                    summary_json TEXT,
                    quality_gate_json TEXT,
                    usage_json TEXT,
                    progress_json TEXT,
                    error_json TEXT,
                    raw_result_envelope_json TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )
            for table, column, definition in (
                ("review_runs", "overall_risk", "TEXT"),
                ("review_runs", "result_status", "TEXT"),
                ("review_runs", "started_at", "INTEGER"),
                ("review_runs", "completed_at", "INTEGER"),
                ("review_runs", "duration_ms", "INTEGER"),
                ("review_runs", "protocol_version", "TEXT"),
                ("review_runs", "worker_version", "TEXT"),
                ("review_runs", "engine_type", "TEXT"),
                ("review_runs", "codex_thread_id", "TEXT"),
                ("review_runs", "summary_json", "TEXT"),
                ("review_runs", "quality_gate_json", "TEXT"),
                ("review_runs", "usage_json", "TEXT"),
                ("review_runs", "progress_json", "TEXT"),
                ("review_runs", "error_json", "TEXT"),
                ("review_runs", "raw_result_envelope_json", "TEXT"),
            ):
                ensure_column(connection, table, column, definition)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_runs_job
                ON review_runs(job_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_runs_worker_status
                ON review_runs(worker_id, status, updated_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_run_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    phase TEXT,
                    severity TEXT,
                    status TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    event_timestamp TEXT,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(run_id, sequence)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_run_events_run_sequence
                ON review_run_events(run_id, sequence)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_run_events_job_created
                ON review_run_events(job_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_artifacts (
                    id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT,
                    media_type TEXT,
                    schema_id TEXT,
                    schema_version TEXT,
                    required INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    storage_url TEXT NOT NULL DEFAULT '',
                    storage_json TEXT,
                    inline_json TEXT,
                    content_path TEXT,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    UNIQUE(run_id, artifact_id)
                )
                """
            )
            for table, column, definition in (
                ("review_artifacts", "job_id", "TEXT NOT NULL DEFAULT ''"),
                ("review_artifacts", "attempt_id", "TEXT NOT NULL DEFAULT ''"),
                ("review_artifacts", "required", "INTEGER NOT NULL DEFAULT 0"),
                ("review_artifacts", "storage_url", "TEXT NOT NULL DEFAULT ''"),
                ("review_artifacts", "storage_json", "TEXT"),
                ("review_artifacts", "inline_json", "TEXT"),
                ("review_artifacts", "content_path", "TEXT"),
                ("review_artifacts", "payload_json", "TEXT"),
                ("review_artifacts", "updated_at", "INTEGER"),
            ):
                ensure_column(connection, table, column, definition)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_artifacts_run
                ON review_artifacts(run_id, kind, artifact_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_artifacts_job_attempt
                ON review_artifacts(job_id, attempt_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    scan_id TEXT,
                    job_id TEXT,
                    repo TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    severity TEXT,
                    category TEXT,
                    title TEXT,
                    file_path TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_user_status_created
                ON issues(user_id, status, created_at DESC, issue_id ASC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_user_created
                ON issues(user_id, created_at DESC, issue_id ASC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_user_severity_created
                ON issues(user_id, severity, created_at DESC, issue_id ASC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_user_status_severity_created
                ON issues(user_id, status, severity, created_at DESC, issue_id ASC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_user_scan_created
                ON issues(user_id, scan_id, created_at DESC, issue_id ASC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_issues_scan_job
                ON issues(scan_id, job_id)
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
                    created_by TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_outcome_labels_observation
                ON review_outcome_labels(candidate_observation_key, created_at)
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
            disabled_at = COALESCE(
                disabled_at,
                (
                    SELECT MIN(COALESCE(worker_commands.created_at, worker_commands.updated_at))
                    FROM worker_commands
                    WHERE worker_commands.worker_id = workers.worker_id
                      AND worker_commands.command = 'uninstall'
                      AND worker_commands.status IN ('pending', 'running', 'succeeded', 'failed')
                ),
                strftime('%s', 'now')
            ),
            updated_at = strftime('%s', 'now')
        WHERE EXISTS (
              SELECT 1
              FROM worker_commands
              WHERE worker_commands.worker_id = workers.worker_id
                AND worker_commands.command = 'uninstall'
                AND worker_commands.status IN ('pending', 'running', 'succeeded', 'failed')
          )
        """
    )
    connection.execute(
        """
        UPDATE workers
        SET enabled = 0,
            deleted_at = COALESCE(
                deleted_at,
                (
                    SELECT MIN(COALESCE(worker_commands.completed_at, worker_commands.updated_at, worker_commands.created_at))
                    FROM worker_commands
                    WHERE worker_commands.worker_id = workers.worker_id
                      AND worker_commands.command = 'uninstall'
                      AND worker_commands.status = 'succeeded'
                ),
                strftime('%s', 'now')
            ),
            disabled_at = COALESCE(
                disabled_at,
                (
                    SELECT MIN(COALESCE(worker_commands.completed_at, worker_commands.updated_at, worker_commands.created_at))
                    FROM worker_commands
                    WHERE worker_commands.worker_id = workers.worker_id
                      AND worker_commands.command = 'uninstall'
                      AND worker_commands.status = 'succeeded'
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
                AND worker_commands.status = 'succeeded'
          )
        """
    )


def normalize_workers_schema(connection: sqlite3.Connection) -> None:
    desired_columns = [
        "worker_id",
        "name",
        "token_hash",
        "worker_scope",
        "owner_user_id",
        "version",
        "provider",
        "provider_chain",
        "enabled",
        "running_jobs",
        "hostname",
        "region",
        "last_error",
        "doctor_status",
        "codex_ready",
        "ready_providers",
        "codex_quota",
        "systemd_active",
        "doctor_checked_at",
        "machine_metrics",
        "machine_metrics_history",
        "protocol_version",
        "worker_group",
        "worker_capabilities",
        "worker_isolation",
        "worker_platform",
        "registration_json",
        "registered_at",
        "status",
        "first_seen_at",
        "last_heartbeat_at",
        "created_at",
        "updated_at",
        "token_last_used_at",
        "disabled_at",
        "deleted_at",
    ]
    rows = connection.execute("PRAGMA table_info(workers)").fetchall()
    existing_columns = [str(row[1]) for row in rows]
    if not existing_columns or existing_columns == desired_columns:
        return
    deprecated_columns = {"max_concurrent_jobs", "free_slots"}
    if not deprecated_columns.intersection(existing_columns):
        return

    connection.execute("DROP TABLE IF EXISTS workers_old")
    connection.execute("ALTER TABLE workers RENAME TO workers_old")
    connection.execute(
        """
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY,
            name TEXT,
            token_hash TEXT UNIQUE,
            worker_scope TEXT NOT NULL DEFAULT 'shared',
            owner_user_id TEXT,
            version TEXT,
            provider TEXT,
            provider_chain TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            running_jobs INTEGER NOT NULL DEFAULT 0,
            hostname TEXT,
            region TEXT,
            last_error TEXT,
            doctor_status TEXT,
            codex_ready INTEGER,
            ready_providers TEXT,
            codex_quota TEXT,
            systemd_active INTEGER,
            doctor_checked_at INTEGER,
            machine_metrics TEXT,
            machine_metrics_history TEXT,
            protocol_version TEXT,
            worker_group TEXT,
            worker_capabilities TEXT,
            worker_isolation TEXT,
            worker_platform TEXT,
            registration_json TEXT,
            registered_at INTEGER,
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
    copy_columns = [column for column in desired_columns if column in existing_columns]
    if copy_columns:
        columns_sql = ", ".join(copy_columns)
        value_expressions = []
        for column in copy_columns:
            if column == "enabled":
                value_expressions.append("COALESCE(enabled, 1)")
            elif column == "worker_scope":
                value_expressions.append("COALESCE(NULLIF(worker_scope, ''), 'shared')")
            elif column == "running_jobs":
                value_expressions.append("CASE WHEN COALESCE(running_jobs, 0) > 0 THEN 1 ELSE 0 END")
            elif column == "status":
                value_expressions.append("COALESCE(NULLIF(status, ''), 'online')")
            elif column in {"first_seen_at", "created_at", "updated_at"}:
                value_expressions.append(f"COALESCE({column}, strftime('%s', 'now'))")
            else:
                value_expressions.append(column)
        values_sql = ", ".join(value_expressions)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO workers ({columns_sql})
            SELECT {values_sql}
            FROM workers_old
            """
        )
    connection.execute("DROP TABLE workers_old")


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
        "expires_at",
        "restrictions",
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
            expires_at INTEGER,
            restrictions TEXT NOT NULL DEFAULT '{}',
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
    return True


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
    if isinstance(users, dict):
        for user_id, user in users.items():
            if not isinstance(user, dict):
                continue
            yield user, "githubAccessToken", f"$.users.{user_id}.githubAccessToken"
            identities = user.get("githubIdentities")
            if isinstance(identities, list):
                for index, identity in enumerate(identities):
                    if isinstance(identity, dict):
                        yield identity, "accessToken", f"$.users.{user_id}.githubIdentities[{index}].accessToken"

    system_config = state.get("system_config")
    if isinstance(system_config, dict):
        alerts = system_config.get("alerts")
        email = alerts.get("email") if isinstance(alerts, dict) else None
        if isinstance(email, dict):
            yield email, "smtpPassword", "$.system_config.alerts.email.smtpPassword"


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
    ensure_initialized()
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
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            "SELECT payload FROM app_state WHERE name = ?",
            (name,),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    state = {str(name): payload}
    migrate_plaintext_state_secrets(state)
    return state_for_runtime(state).get(str(name))


def delete_state_items(names: list[str] | tuple[str, ...] | set[str]) -> int:
    ensure_initialized()
    unique_names = list(dict.fromkeys(str(name or "").strip() for name in names if str(name or "").strip()))
    if not unique_names:
        return 0
    with _LOCK, closing(connect()) as connection:
        with connection:
            removed = 0
            for start in range(0, len(unique_names), 400):
                chunk = unique_names[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                removed += connection.execute(
                    f"DELETE FROM app_state WHERE name IN ({placeholders})",
                    tuple(chunk),
                ).rowcount
    return max(0, removed)


def save_state_item(name: str, payload: Any) -> None:
    ensure_initialized()
    state_name = str(name)
    storage_payload = state_for_storage({state_name: payload})[state_name]
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
                (state_name, json.dumps(storage_payload, ensure_ascii=False, allow_nan=False)),
            )


def save_state(state: dict[str, Any]) -> None:
    ensure_initialized()
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
    ensure_initialized()
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


WORKER_PROVIDER_VALUES = {"codex"}
WORKER_SCOPE_SHARED = "shared"
WORKER_SCOPE_VALUES = {WORKER_SCOPE_SHARED}


def normalize_worker_scope(value: Any, *, default: str = WORKER_SCOPE_SHARED) -> str:
    scope = str(value or "").strip().lower().replace("-", "_")
    if scope in WORKER_SCOPE_VALUES:
        return scope
    return default if default in WORKER_SCOPE_VALUES else WORKER_SCOPE_SHARED


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



def worker_quota_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(100.0, number))


def worker_quota_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def worker_quota_text(value: Any, limit: int = 200) -> str | None:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit] if text else None


def worker_quota_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "ready"}:
            return True
        if lowered in {"0", "false", "no", "not_ready"}:
            return False
    return None


def worker_codex_quota_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    payload: dict[str, Any] = {}
    for key, limit in (("name", 80), ("windowKind", 80), ("label", 120)):
        text = worker_quota_text(value.get(key), limit)
        if text:
            payload[key] = text
    for key in ("usedPercent", "remainingPercent"):
        number = worker_quota_float(value.get(key))
        if number is not None:
            payload[key] = round(number, 3)
    for key in ("windowDurationMins", "resetsAt"):
        number = worker_quota_int(value.get(key))
        if number is not None:
            payload[key] = number
    return payload if payload else None


def worker_codex_quota_json(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    payload: dict[str, Any] = {"provider": "codex"}
    for key, limit in (
        ("limitId", 80),
        ("limitName", 120),
        ("planType", 80),
        ("status", 40),
        ("reason", 120),
        ("rateLimitReachedType", 120),
        ("lastError", 500),
    ):
        text = worker_quota_text(value.get(key), limit)
        if text:
            payload[key] = text
    ready = worker_quota_bool(value.get("ready"))
    if ready is not None:
        payload["ready"] = ready
    for key in ("checkedAt", "nextCheckAt"):
        number = worker_quota_int(value.get(key))
        if number is not None:
            payload[key] = number
    for key in ("thresholdPercent", "usedPercent", "remainingPercent"):
        number = worker_quota_float(value.get(key))
        if number is not None:
            payload[key] = round(number, 3)
    windows = [window for item in value.get("windows") or [] if (window := worker_codex_quota_window(item))]
    if windows:
        payload["windows"] = windows[:4]
    blocked_windows = [window for item in value.get("blockedWindows") or [] if (window := worker_codex_quota_window(item))]
    if blocked_windows:
        payload["blockedWindows"] = blocked_windows[:4]
    reset_credits = value.get("rateLimitResetCredits") if isinstance(value.get("rateLimitResetCredits"), dict) else {}
    available_count = worker_quota_int(reset_credits.get("availableCount")) if reset_credits else None
    if available_count is not None:
        payload["rateLimitResetCredits"] = {"availableCount": available_count}
    credits = value.get("credits") if isinstance(value.get("credits"), dict) else {}
    if credits:
        credits_payload: dict[str, Any] = {}
        for key in ("hasCredits", "unlimited"):
            flag = worker_quota_bool(credits.get(key))
            if flag is not None:
                credits_payload[key] = flag
        balance = worker_quota_text(credits.get("balance"), 80)
        if balance is not None:
            credits_payload["balance"] = balance
        if credits_payload:
            payload["credits"] = credits_payload
    return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)

WORKER_LIFECYCLE_COMMANDS = {"stop", "uninstall"}
WORKER_TELEMETRY_COMMANDS = {"refresh_codex_quota"}
WORKER_COMMANDS = WORKER_LIFECYCLE_COMMANDS | WORKER_TELEMETRY_COMMANDS
WORKER_COMMAND_ACTIVE_STATUSES = {"pending", "running"}
WORKER_COMMAND_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def normalize_worker_command(command: Any) -> str:
    value = str(command or "").strip().lower()
    if value not in WORKER_COMMANDS:
        allowed = ", ".join(sorted(WORKER_COMMANDS))
        raise ValueError(f"Worker command must be one of: {allowed}.")
    return value


def normalize_worker_lifecycle_command(command: Any) -> str:
    value = str(command or "").strip().lower()
    if value not in WORKER_LIFECYCLE_COMMANDS:
        allowed = ", ".join(sorted(WORKER_LIFECYCLE_COMMANDS))
        raise ValueError(f"Worker lifecycle command must be one of: {allowed}.")
    return value


def create_worker_token(name: str = "worker") -> dict[str, Any]:
    ensure_initialized()
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
    ensure_initialized()
    token = "pww_" + secrets.token_urlsafe(32)
    token_hash = worker_token_hash(token)
    worker_id = str(record.get("worker_id") or stable_id("wk", token_hash)).strip()
    timestamp = int(record.get("timestamp") or time.time())
    provider = (normalize_provider_list(record.get("provider")) or ["codex"])[0]
    provider_chain = provider_list_json(record.get("provider_chain"), fallback=[provider])
    requested_scope = str(record.get("worker_scope") or record.get("scope") or "").strip().lower().replace("-", "_")
    if requested_scope == "private":
        raise ValueError("Private workers are not supported.")
    worker_scope = WORKER_SCOPE_SHARED
    owner_user_id = ""
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO workers (
                    worker_id, name, token_hash, worker_scope, owner_user_id,
                    provider, provider_chain, enabled, status,
                    running_jobs, version,
                    hostname, region, last_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'offline', 0, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    worker_id,
                    str(record.get("name") or "Worker")[:120],
                    token_hash,
                    worker_scope,
                    owner_user_id or None,
                    provider,
                    provider_chain,
                    record.get("version"),
                    record.get("region"),
                    timestamp,
                    timestamp,
                ),
            )
            worker = row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()) or {}
    worker["worker_token"] = token
    return worker


def worker_visibility_where_clause(
    *,
    include_deleted: bool = False,
    activated_only: bool = False,
    worker_scope: str | None = None,
) -> str:
    filters = []
    if not include_deleted:
        filters.append("deleted_at IS NULL")
    if activated_only:
        filters.append(
            "(last_heartbeat_at IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM worker_commands "
            "WHERE worker_commands.worker_id = workers.worker_id "
            "AND worker_commands.command = 'uninstall' "
            "AND worker_commands.status IN ('pending', 'running', 'failed', 'cancelled')"
            "))"
        )
    if worker_scope is not None:
        scope = normalize_worker_scope(worker_scope)
        filters.append(f"COALESCE(worker_scope, '{WORKER_SCOPE_SHARED}') = '{scope}'")
    return f"WHERE {' AND '.join(filters)}" if filters else ""


def list_workers(
    *,
    include_deleted: bool = False,
    activated_only: bool = False,
    worker_scope: str | None = None,
) -> list[dict[str, Any]]:
    ensure_initialized()
    where = worker_visibility_where_clause(
        include_deleted=include_deleted,
        activated_only=activated_only,
        worker_scope=worker_scope,
    )
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT * FROM workers {where} ORDER BY created_at DESC, worker_id ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def count_workers(
    *,
    include_deleted: bool = False,
    activated_only: bool = False,
    worker_scope: str | None = None,
) -> int:
    ensure_initialized()
    where = worker_visibility_where_clause(
        include_deleted=include_deleted,
        activated_only=activated_only,
        worker_scope=worker_scope,
    )
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM workers {where}").fetchone()
    return max(0, int(row[0] if row else 0))


def list_workers_page(
    *,
    include_deleted: bool = False,
    activated_only: bool = False,
    worker_scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    ensure_initialized()
    where = worker_visibility_where_clause(
        include_deleted=include_deleted,
        activated_only=activated_only,
        worker_scope=worker_scope,
    )
    safe_limit = max(1, min(100, int(limit or 50)))
    safe_offset = max(0, int(offset or 0))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        total = int(connection.execute(f"SELECT COUNT(*) FROM workers {where}").fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT *
            FROM workers
            {where}
            ORDER BY created_at DESC, worker_id ASC
            LIMIT ? OFFSET ?
            """,
            (safe_limit, safe_offset),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": max(0, total),
        "limit": safe_limit,
        "offset": safe_offset,
    }


def get_worker(
    worker_id: str,
    *,
    include_deleted: bool = False,
    worker_scope: str | None = None,
) -> dict[str, Any] | None:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    where_deleted = "" if include_deleted else "AND deleted_at IS NULL"
    scope_clause = ""
    params: list[Any] = [worker_id]
    if worker_scope is not None:
        scope = normalize_worker_scope(worker_scope)
        scope_clause = "AND COALESCE(worker_scope, ?) = ?"
        params.extend([WORKER_SCOPE_SHARED, scope])
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                f"SELECT * FROM workers WHERE worker_id = ? {where_deleted} {scope_clause}",
                tuple(params),
            ).fetchone()
        )


def get_worker_heartbeat(worker_id: str) -> dict[str, Any] | None:
    return get_worker(worker_id)


def update_worker(
    worker_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    ensure_initialized()
    allowed = {
        "name": "name",
        "provider": "provider",
        "provider_chain": "provider_chain",
        "region": "region",
        "version": "version",
    }
    assignments = []
    values: list[Any] = []
    for source_key, column in allowed.items():
        if source_key not in patch:
            continue
        value = patch[source_key]
        if column == "provider_chain":
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
    ensure_initialized()
    timestamp = int(time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            if not enabled:
                telemetry_commands = sorted(WORKER_TELEMETRY_COMMANDS)
                placeholders = ",".join("?" for _ in telemetry_commands)
                connection.execute(
                    f"""
                    UPDATE worker_commands
                    SET status = 'cancelled',
                        error = NULL,
                        completed_at = ?,
                        updated_at = ?
                    WHERE worker_id = ?
                      AND command IN ({placeholders})
                      AND status IN ('pending', 'running')
                    """,
                    (timestamp, timestamp, worker_id, *telemetry_commands),
                )
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
    ensure_initialized()
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


def cleanup_stale_worker_uninstall_commands(
    *,
    timestamp: int | None = None,
    pending_timeout_seconds: int = 24 * 60 * 60,
    running_timeout_seconds: int = 24 * 60 * 60,
) -> int:
    ensure_initialized()
    current_time = int(timestamp if timestamp is not None else time.time())
    pending_cutoff = current_time - max(0, int(pending_timeout_seconds))
    running_cutoff = current_time - max(0, int(running_timeout_seconds))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT wc.id, wc.worker_id, wc.status
                FROM worker_commands wc
                JOIN workers w ON w.worker_id = wc.worker_id
                WHERE wc.command = 'uninstall'
                  AND w.deleted_at IS NULL
                  AND (
                    (
                      wc.status = 'pending'
                      AND COALESCE(wc.created_at, wc.updated_at, wc.started_at, 0) < ?
                    )
                    OR (
                      wc.status = 'running'
                      AND COALESCE(wc.started_at, wc.updated_at, wc.created_at, 0) < ?
                    )
                  )
                ORDER BY wc.created_at ASC, wc.id ASC
                """,
                (pending_cutoff, running_cutoff),
            ).fetchall()
            command_ids_by_status = {
                status: [
                    str(row["id"])
                    for row in rows
                    if str(row["status"] or "").strip() == status and str(row["id"] or "").strip()
                ]
                for status in ("pending", "running")
            }
            command_ids = [command_id for ids in command_ids_by_status.values() for command_id in ids]
            worker_ids = sorted({str(row["worker_id"]) for row in rows if str(row["worker_id"] or "").strip()})
            if not command_ids or not worker_ids:
                return 0
            timeout_errors = {
                "pending": "cleanup pending exceeded timeout; host cleanup was not confirmed",
                "running": "cleanup running exceeded timeout; host cleanup was not confirmed",
            }
            for status, status_command_ids in command_ids_by_status.items():
                if not status_command_ids:
                    continue
                command_placeholders = ",".join("?" for _ in status_command_ids)
                connection.execute(
                    f"""
                    UPDATE worker_commands
                    SET status = 'cancelled',
                        error = COALESCE(NULLIF(error, ''), ?),
                        completed_at = COALESCE(completed_at, ?),
                        updated_at = ?
                    WHERE id IN ({command_placeholders})
                      AND command = 'uninstall'
                      AND status = ?
                    """,
                    (timeout_errors[status], current_time, current_time, *status_command_ids, status),
                )
            worker_placeholders = ",".join("?" for _ in worker_ids)
            affected_count = connection.execute(
                f"""
                UPDATE workers
                SET enabled = 0,
                    deleted_at = COALESCE(deleted_at, ?),
                    disabled_at = COALESCE(disabled_at, ?),
                    updated_at = ?
                WHERE worker_id IN ({worker_placeholders})
                  AND deleted_at IS NULL
                """,
                (current_time, current_time, current_time, *worker_ids),
            ).rowcount
            return max(0, int(affected_count or 0))


def rotate_worker_token(worker_id: str) -> dict[str, Any] | None:
    ensure_initialized()
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


def worker_token_last_used_stale(row: sqlite3.Row, timestamp: int, *, interval_seconds: int = 60) -> bool:
    try:
        last_used_at = int(row["token_last_used_at"] or 0)
    except (KeyError, TypeError, ValueError):
        last_used_at = 0
    return last_used_at <= 0 or last_used_at <= timestamp - max(1, int(interval_seconds or 60))


def get_enabled_worker_token(token: str, *, update_last_used: bool = True) -> dict[str, Any] | None:
    ensure_initialized()
    token = str(token or "").strip()
    if not token:
        return None
    token_hash = worker_token_hash(token)
    timestamp = int(time.time())
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
                if update_last_used and worker_token_last_used_stale(row, timestamp):
                    connection.execute(
                        "UPDATE worker_tokens SET last_used_at = ? WHERE token_hash = ?",
                        (timestamp, token_hash),
                    )
                    connection.execute(
                        "UPDATE workers SET token_last_used_at = ?, updated_at = ? WHERE token_hash = ?",
                        (timestamp, timestamp, token_hash),
                    )
                return row_to_dict(row)
            return None


def get_worker_by_token(
    token: str,
    *,
    allow_disabled: bool = False,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    ensure_initialized()
    token = str(token or "").strip()
    if not token:
        return None
    token_hash = worker_token_hash(token)
    enabled_clause = "" if allow_disabled else "AND enabled = 1"
    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    timestamp = int(time.time())
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
            if worker_token_last_used_stale(row, timestamp):
                connection.execute(
                    "UPDATE workers SET token_last_used_at = ?, updated_at = ? WHERE token_hash = ?",
                    (timestamp, timestamp, token_hash),
                )
            return row_to_dict(row)


def touch_worker_presence(worker_id: str, *, timestamp: int | None = None) -> dict[str, Any] | None:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            updated = connection.execute(
                """
                UPDATE workers
                SET status = 'online',
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE worker_id = ? AND enabled = 1 AND deleted_at IS NULL
                """,
                (current_time, current_time, worker_id),
            ).rowcount
            if updated <= 0:
                return None
            return row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone())

def _upsert_worker_heartbeat_locked(connection: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    timestamp = int(record.get("timestamp") or time.time())
    running_jobs = 1 if int(record.get("running_jobs") or 0) > 0 else 0
    provider = str(record.get("provider") or "codex")[:60]
    provider_chain = provider_list_json(record.get("provider_chain"), fallback=[provider])
    ready_providers = heartbeat_ready_providers_json(record)
    codex_quota_text = worker_codex_quota_json(record.get("codex_quota"))
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
    connection.execute(
        """
        INSERT INTO workers (
            worker_id, name, version, provider, provider_chain, enabled, running_jobs,
            hostname, region, last_error, status, first_seen_at, last_heartbeat_at,
            created_at, updated_at, doctor_status, codex_ready, ready_providers,
            codex_quota, systemd_active, doctor_checked_at, machine_metrics, machine_metrics_history
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(worker_id) DO UPDATE SET
            version = excluded.version,
            provider = excluded.provider,
            provider_chain = COALESCE(excluded.provider_chain, workers.provider_chain),
            running_jobs = excluded.running_jobs,
            hostname = excluded.hostname,
            region = COALESCE(NULLIF(excluded.region, ''), workers.region),
            last_error = excluded.last_error,
            doctor_status = COALESCE(excluded.doctor_status, workers.doctor_status),
            codex_ready = COALESCE(excluded.codex_ready, workers.codex_ready),
            ready_providers = COALESCE(excluded.ready_providers, workers.ready_providers),
            codex_quota = COALESCE(excluded.codex_quota, workers.codex_quota),
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
            running_jobs,
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
            codex_quota_text,
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


def upsert_worker_heartbeat(record: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            return _upsert_worker_heartbeat_locked(connection, record)

def register_worker_protocol(record: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
    worker = record.get("worker") if isinstance(record.get("worker"), dict) else {}
    worker_id = str(worker.get("worker_id") or record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    protocol_version = str(record.get("protocol_version") or "").strip()
    if not protocol_version:
        raise ValueError("protocol_version is required")
    timestamp = int(record.get("timestamp") or time.time())
    capabilities = worker.get("capabilities") if isinstance(worker.get("capabilities"), dict) else {}
    isolation = worker.get("isolation") if isinstance(worker.get("isolation"), dict) else {}
    platform = worker.get("platform") if isinstance(worker.get("platform"), dict) else {}
    registration_text = json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True)
    capabilities_text = json.dumps(to_jsonable(capabilities), ensure_ascii=False, sort_keys=True)
    isolation_text = json.dumps(to_jsonable(isolation), ensure_ascii=False, sort_keys=True)
    platform_text = json.dumps(to_jsonable(platform), ensure_ascii=False, sort_keys=True)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE workers
                SET
                    version = COALESCE(NULLIF(?, ''), version),
                    hostname = COALESCE(NULLIF(?, ''), hostname),
                    protocol_version = ?,
                    worker_group = COALESCE(NULLIF(?, ''), worker_group),
                    worker_capabilities = ?,
                    worker_isolation = ?,
                    worker_platform = ?,
                    registration_json = ?,
                    registered_at = COALESCE(registered_at, ?),
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (
                    str(worker.get("worker_version") or "")[:120],
                    str(worker.get("hostname") or "")[:255],
                    protocol_version,
                    str(worker.get("worker_group") or "")[:120],
                    capabilities_text,
                    isolation_text,
                    platform_text,
                    registration_text,
                    timestamp,
                    timestamp,
                    worker_id,
                ),
            )
            row = row_to_dict(connection.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone())
            if not row:
                raise ValueError("worker not found")
            return row


def protocol_timestamp(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def run_json_text(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)


def _upsert_review_run_claimed_locked(
    connection: sqlite3.Connection,
    job: dict[str, Any],
    *,
    protocol_version: str = "review-worker-protocol/v1",
    timestamp: int | None = None,
) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "").strip()
    run_id = str(job.get("run_id") or "").strip()
    if not run_id and job_id:
        try:
            attempt = int(job.get("attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        run_id = f"run_{job_id}" if attempt <= 1 else f"run_{job_id}_attempt_{attempt}"
    if not run_id or not job_id:
        raise ValueError("run_id and job_id are required")
    current_time = int(timestamp if timestamp is not None else time.time())
    worker_id = str(job.get("claimed_by_worker_id") or "").strip()
    started_at = protocol_timestamp(job.get("claimed_at")) or current_time
    connection.execute(
        """
        INSERT INTO review_runs (
            run_id, job_id, worker_id, status, started_at,
            protocol_version, progress_json, created_at, updated_at
        )
        VALUES (?, ?, ?, 'leased', ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            job_id = excluded.job_id,
            worker_id = COALESCE(NULLIF(excluded.worker_id, ''), review_runs.worker_id),
            status = CASE
                WHEN review_runs.status IN ('completed', 'failed', 'cancelled', 'partial_completed') THEN review_runs.status
                ELSE excluded.status
            END,
            started_at = COALESCE(review_runs.started_at, excluded.started_at),
            protocol_version = COALESCE(excluded.protocol_version, review_runs.protocol_version),
            progress_json = COALESCE(review_runs.progress_json, excluded.progress_json),
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            job_id,
            worker_id,
            started_at,
            protocol_version,
            run_json_text({"status": "leased", "overall_percent": 0}),
            current_time,
            current_time,
        ),
    )
    return row_to_dict(connection.execute("SELECT * FROM review_runs WHERE run_id = ?", (run_id,)).fetchone()) or {}


def upsert_review_run_claimed(job: dict[str, Any], *, protocol_version: str = "review-worker-protocol/v1") -> dict[str, Any]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            return _upsert_review_run_claimed_locked(connection, job, protocol_version=protocol_version)

def _review_run_progress_values(event: dict[str, Any]) -> tuple[str, str, str, int, int | None, str, str | None, str | None]:
    run_id = str(event.get("run_id") or "").strip()
    job_id = str(event.get("job_id") or "").strip()
    worker_id = str(event.get("worker_id") or "").strip()
    if not run_id or not job_id:
        raise ValueError("run_id and job_id are required")
    timestamp = int(event.get("created_at") or time.time())
    event_type = str(event.get("event_type") or "").strip()
    progress_payload = {
        "sequence": event.get("sequence"),
        "event_type": event_type,
        "phase": event.get("phase"),
        "severity": event.get("severity"),
        "status": event.get("status"),
        "overall_percent": max(0, min(100, int(event.get("progress") or 0))),
        "timestamp": event.get("timestamp"),
    }
    if isinstance(event.get("steps"), list):
        progress_payload["steps"] = event.get("steps")
    terminal_status = {
        "run_completed": "completed",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
        "run_partial_completed": "partial_completed",
    }.get(event_type)
    cancellation_pending = event_type == "run_cancel_requested" or (
        event_type == "run_cancelled" and event.get("defer_terminal") is True
    )
    if cancellation_pending:
        terminal_status = None
    status = "cancelling" if cancellation_pending else terminal_status or "running"
    completed_at = timestamp if terminal_status else None
    if not terminal_status and isinstance(event.get("estimate"), dict):
        progress_payload["estimate"] = event.get("estimate")
    return run_id, job_id, worker_id, timestamp, completed_at, status, run_json_text(progress_payload), terminal_status


def _upsert_review_run_progress_locked(connection: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    run_id, job_id, worker_id, timestamp, completed_at, status, progress_text, _terminal_status = _review_run_progress_values(event)
    connection.execute(
        """
        INSERT INTO review_runs (
            run_id, job_id, worker_id, status, completed_at,
            progress_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            job_id = excluded.job_id,
            worker_id = COALESCE(NULLIF(excluded.worker_id, ''), review_runs.worker_id),
            status = CASE
                WHEN review_runs.status IN ('completed', 'failed', 'cancelled', 'partial_completed')
                     AND excluded.status IN ('running', 'cancelling')
                THEN review_runs.status
                ELSE excluded.status
            END,
            completed_at = COALESCE(excluded.completed_at, review_runs.completed_at),
            progress_json = CASE
                WHEN review_runs.status IN ('completed', 'failed', 'cancelled', 'partial_completed')
                     AND excluded.status IN ('running', 'cancelling')
                THEN review_runs.progress_json
                ELSE excluded.progress_json
            END,
            updated_at = CASE
                WHEN review_runs.status IN ('completed', 'failed', 'cancelled', 'partial_completed')
                     AND excluded.status IN ('running', 'cancelling')
                THEN review_runs.updated_at
                ELSE excluded.updated_at
            END
        """,
        (
            run_id,
            job_id,
            worker_id,
            status,
            completed_at,
            progress_text,
            timestamp,
            timestamp,
        ),
    )
    return row_to_dict(connection.execute("SELECT * FROM review_runs WHERE run_id = ?", (run_id,)).fetchone()) or {}


def update_review_run_progress(event: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            return _upsert_review_run_progress_locked(connection, event)

def finalize_review_run_result(job: dict[str, Any], body: dict[str, Any], *, status: str) -> dict[str, Any]:
    ensure_initialized()
    envelope = body.get("reviewWorkerProtocol") if isinstance(body.get("reviewWorkerProtocol"), dict) else body.get("review_worker_protocol")
    if not isinstance(envelope, dict):
        raise ValueError("reviewWorkerProtocol is required")
    envelope_job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    envelope_worker = envelope.get("worker") if isinstance(envelope.get("worker"), dict) else {}
    execution = envelope.get("execution") if isinstance(envelope.get("execution"), dict) else {}
    summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
    quality_gate = envelope.get("quality_gate") if isinstance(envelope.get("quality_gate"), dict) else {}
    progress_final = envelope.get("progress_final") if isinstance(envelope.get("progress_final"), dict) else None
    if progress_final is None:
        progress_final = execution.get("progress_final") if isinstance(execution.get("progress_final"), dict) else None
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else None
    worker_engine = envelope_worker.get("engine") if isinstance(envelope_worker.get("engine"), dict) else {}
    run_id = str(envelope_job.get("run_id") or job.get("run_id") or f"run_{job.get('job_id')}").strip()
    job_id = str(envelope_job.get("job_id") or job.get("job_id") or "").strip()
    if not run_id or not job_id:
        raise ValueError("run_id and job_id are required")
    timestamp = int(time.time())
    execution_status = str(execution.get("status") or "").strip() or ("completed" if status == "done" else "failed")
    overall_risk = str(summary.get("overall_risk") or "").strip().lower() or None
    started_at = protocol_timestamp(execution.get("started_at")) or protocol_timestamp(job.get("started_at")) or protocol_timestamp(job.get("claimed_at"))
    completed_at = protocol_timestamp(execution.get("completed_at")) or protocol_timestamp(job.get("completed_at")) or timestamp
    try:
        duration_ms = int(execution.get("duration_ms")) if execution.get("duration_ms") is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    if duration_ms is None and started_at and completed_at:
        duration_ms = max(0, int((completed_at - started_at) * 1000))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            existing = row_to_dict(
                connection.execute(
                    """
                    SELECT status, completed_at, duration_ms, error_json
                    FROM review_runs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
            ) or {}
            existing_error: dict[str, Any] = {}
            try:
                decoded_existing_error = json.loads(
                    str(existing.get("error_json") or "{}")
                )
                if isinstance(decoded_existing_error, dict):
                    existing_error = decoded_existing_error
            except (TypeError, ValueError, json.JSONDecodeError):
                existing_error = {}
            preserve_server_cancellation = (
                status == "cancelled"
                and str(existing.get("status") or "").strip() == "cancelled"
                and str(existing_error.get("source") or "").strip()
                == "server_lease_reaper"
            )
            effective_completed_at = (
                int(existing["completed_at"])
                if preserve_server_cancellation
                and existing.get("completed_at") is not None
                else completed_at
            )
            effective_error_json = (
                str(existing.get("error_json") or "")
                if preserve_server_cancellation
                else run_json_text(error)
            )
            effective_duration_ms = (
                int(existing["duration_ms"])
                if preserve_server_cancellation
                and existing.get("duration_ms") is not None
                else duration_ms
            )
            connection.execute(
                """
                INSERT INTO review_runs (
                    run_id, job_id, worker_id, status, overall_risk, result_status,
                    started_at, completed_at, duration_ms, protocol_version,
                    worker_version, engine_type, codex_thread_id, summary_json,
                    quality_gate_json, usage_json, progress_json, error_json,
                    raw_result_envelope_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    worker_id = COALESCE(NULLIF(excluded.worker_id, ''), review_runs.worker_id),
                    status = excluded.status,
                    overall_risk = excluded.overall_risk,
                    result_status = excluded.result_status,
                    started_at = COALESCE(review_runs.started_at, excluded.started_at),
                    completed_at = excluded.completed_at,
                    duration_ms = excluded.duration_ms,
                    protocol_version = excluded.protocol_version,
                    worker_version = excluded.worker_version,
                    engine_type = excluded.engine_type,
                    codex_thread_id = excluded.codex_thread_id,
                    summary_json = excluded.summary_json,
                    quality_gate_json = excluded.quality_gate_json,
                    usage_json = excluded.usage_json,
                    progress_json = COALESCE(excluded.progress_json, review_runs.progress_json),
                    error_json = excluded.error_json,
                    raw_result_envelope_json = excluded.raw_result_envelope_json,
                    updated_at = excluded.updated_at
                WHERE review_runs.job_id IS NOT excluded.job_id
                   OR review_runs.worker_id IS NOT COALESCE(NULLIF(excluded.worker_id, ''), review_runs.worker_id)
                   OR review_runs.status IS NOT excluded.status
                   OR review_runs.overall_risk IS NOT excluded.overall_risk
                   OR review_runs.result_status IS NOT excluded.result_status
                   OR review_runs.started_at IS NOT COALESCE(review_runs.started_at, excluded.started_at)
                   OR review_runs.completed_at IS NOT excluded.completed_at
                   OR review_runs.duration_ms IS NOT excluded.duration_ms
                   OR review_runs.protocol_version IS NOT excluded.protocol_version
                   OR review_runs.worker_version IS NOT excluded.worker_version
                   OR review_runs.engine_type IS NOT excluded.engine_type
                   OR review_runs.codex_thread_id IS NOT excluded.codex_thread_id
                   OR review_runs.summary_json IS NOT excluded.summary_json
                   OR review_runs.quality_gate_json IS NOT excluded.quality_gate_json
                   OR review_runs.usage_json IS NOT excluded.usage_json
                   OR review_runs.progress_json IS NOT COALESCE(excluded.progress_json, review_runs.progress_json)
                   OR review_runs.error_json IS NOT excluded.error_json
                   OR review_runs.raw_result_envelope_json IS NOT excluded.raw_result_envelope_json
                """,
                (
                    run_id,
                    job_id,
                    str(envelope_worker.get("worker_id") or job.get("claimed_by_worker_id") or "").strip(),
                    execution_status,
                    overall_risk,
                    status,
                    started_at,
                    effective_completed_at,
                    effective_duration_ms,
                    str(envelope.get("protocol_version") or "").strip(),
                    str(envelope_worker.get("worker_version") or "").strip(),
                    str(worker_engine.get("type") or "").strip(),
                    str(worker_engine.get("codex_thread_id") or "").strip(),
                    run_json_text(summary),
                    run_json_text(quality_gate),
                    run_json_text(envelope.get("usage") if isinstance(envelope.get("usage"), dict) else None),
                    run_json_text(progress_final),
                    effective_error_json,
                    run_json_text(envelope),
                    timestamp,
                    timestamp,
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM review_runs WHERE run_id = ?", (run_id,)).fetchone()) or {}


def get_review_run(run_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM review_runs WHERE run_id = ?", (normalized,)).fetchone())


def get_latest_review_run_for_job(job_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    normalized = str(job_id or "").strip()
    if not normalized:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                """
                SELECT * FROM review_runs
                WHERE job_id = ?
                ORDER BY updated_at DESC, started_at DESC, run_id DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        )


def _insert_review_run_event_locked(connection: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    run_id = str(event.get("run_id") or "").strip()
    job_id = str(event.get("job_id") or "").strip()
    worker_id = str(event.get("worker_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    if not job_id:
        raise ValueError("job_id is required")
    if not worker_id:
        raise ValueError("worker_id is required")
    raw_sequence = event.get("sequence")
    if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
        raise ValueError("event sequence is required")
    sequence = raw_sequence
    if sequence <= 0:
        raise ValueError("event sequence must be positive")
    event_type = str(event.get("event_type") or "").strip()
    if not event_type:
        raise ValueError("event_type is required")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    payload_text = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    created_at = int(event.get("created_at") or time.time())
    event_id = stable_id("rve", f"{run_id}:{sequence}")
    latest = connection.execute(
        "SELECT MAX(sequence) AS latest_sequence FROM review_run_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    latest_sequence = int(latest["latest_sequence"]) if latest and latest["latest_sequence"] is not None else 0
    if sequence <= latest_sequence:
        raise ValueError("event sequence must be monotonic")
    connection.execute(
        """
        INSERT INTO review_run_events (
            id, run_id, job_id, worker_id, sequence, event_type, phase,
            severity, status, progress, event_timestamp, payload, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            run_id,
            job_id,
            worker_id,
            sequence,
            event_type,
            event.get("phase"),
            event.get("severity"),
            event.get("status"),
            max(0, min(100, int(event.get("progress") or 0))),
            event.get("timestamp"),
            payload_text,
            created_at,
        ),
    )
    return row_to_dict(connection.execute("SELECT * FROM review_run_events WHERE id = ?", (event_id,)).fetchone()) or {}


def store_review_run_event(event: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            return _insert_review_run_event_locked(connection, event)


def store_review_run_event_and_progress(
    event: dict[str, Any],
    progress_event: dict[str, Any] | None = None,
    scan_job_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            stored_event = _insert_review_run_event_locked(connection, event)
            _upsert_review_run_progress_locked(connection, progress_event or event)
            if scan_job_progress is not None:
                job_id = str(scan_job_progress.get("job_id") or event.get("job_id") or "").strip()
                scan_job = None
                if job_id:
                    current_time = int(time.time())
                    raw_timeout_at = scan_job_progress.get("timeout_at")
                    try:
                        timeout_at = int(raw_timeout_at) if raw_timeout_at is not None else None
                    except (TypeError, ValueError):
                        timeout_at = None
                    target_job_status = (
                        "cancelling"
                        if str(scan_job_progress.get("status") or "").strip().lower() == "cancelling"
                        else "running"
                    )
                    cursor = connection.execute(
                        """
                        UPDATE scan_jobs
                        SET progress_phase = ?,
                            progress = ?,
                            progress_message = ?,
                            status = ?,
                            started_at = COALESCE(started_at, ?),
                            timeout_at = CASE
                                WHEN ? IS NULL THEN timeout_at
                                WHEN timeout_at IS NULL OR timeout_at < ? THEN ?
                                ELSE timeout_at
                            END,
                            logs_summary = ?,
                            updated_at = ?
                        WHERE job_id = ?
                          AND (
                              (? = 'running' AND status IN ('claimed', 'running'))
                              OR (? = 'cancelling' AND status IN ('cancel_requested', 'cancelling'))
                          )
                        """,
                        (
                            scan_job_progress.get("phase"),
                            max(0, min(100, int(scan_job_progress.get("progress") or 0))),
                            scan_job_progress.get("message"),
                            target_job_status,
                            int(scan_job_progress.get("started_at") or current_time),
                            timeout_at,
                            timeout_at,
                            timeout_at,
                            scan_job_progress.get("logs_summary"),
                            current_time,
                            job_id,
                            target_job_status,
                            target_job_status,
                        ),
                    )
                    if cursor.rowcount <= 0 and target_job_status == "cancelling":
                        raise ValueError("Run is no longer accepting cancellation events.")
                    if cursor.rowcount > 0:
                        scan_job = row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())
                stored_event["_scan_job"] = scan_job
            return stored_event


def review_run_event_is_latest(run_id: str, sequence: int) -> bool:
    ensure_initialized()
    target_run_id = str(run_id or "").strip()
    if not target_run_id:
        return False
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            "SELECT MAX(sequence) FROM review_run_events WHERE run_id = ?",
            (target_run_id,),
        ).fetchone()
    return row is not None and row[0] is not None and int(row[0]) == int(sequence)

def list_review_run_events(run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    ensure_initialized()
    normalized = str(run_id or "").strip()
    if not normalized:
        return []
    safe_limit = max(1, min(1000, int(limit or 200)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM review_run_events
            WHERE run_id = ?
            ORDER BY sequence ASC
            LIMIT ?
            """,
            (normalized, safe_limit),
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]


def scan_job_has_progress_phase_evidence(
    job_id: str,
    phases: Iterable[str],
) -> bool:
    ensure_initialized()
    normalized_job_id = str(job_id or "").strip()
    normalized_phases = sorted(
        {
            str(phase or "").strip()
            for phase in phases
            if str(phase or "").strip()
        }
    )
    if not normalized_job_id or not normalized_phases:
        return False
    placeholders = ", ".join("?" for _ in normalized_phases)
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            f"""
            SELECT 1
            FROM review_run_events AS event
            WHERE event.job_id = ?
              AND (
                  (
                      event.phase IN ({placeholders})
                      AND event.event_type IN (
                          'phase_started',
                          'progress_updated',
                          'phase_completed'
                      )
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM json_tree(
                          CASE
                              WHEN json_valid(event.payload)
                              THEN event.payload
                              ELSE '{{}}'
                          END
                      ) AS step
                      WHERE step.type = 'object'
                        AND step.path IN (
                            '$.progress.steps',
                            '$.data.progress_steps',
                            '$.data.progressSteps',
                            '$.progressSteps',
                            '$.progress_steps'
                        )
                        AND json_extract(step.value, '$.id') IN ({placeholders})
                        AND (
                            LOWER(
                                CAST(
                                    COALESCE(
                                        json_extract(step.value, '$.status'),
                                        ''
                                    )
                                    AS TEXT
                                )
                            ) NOT IN (
                                '',
                                'pending',
                                'queued',
                                'skipped',
                                'not_started'
                            )
                            OR CAST(
                                COALESCE(
                                    json_extract(step.value, '$.percent'),
                                    0
                                )
                                AS REAL
                            ) > 0
                        )
                  )
              )
            LIMIT 1
            """,
            (
                normalized_job_id,
                *normalized_phases,
                *normalized_phases,
            ),
        ).fetchone()
    return row is not None


def record_worker_audit_event(record: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
    worker_id = str(record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    command = normalize_worker_command(record.get("command"))
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
                active_command = str(active["command"] or "").strip().lower()
                if command in WORKER_LIFECYCLE_COMMANDS and active_command in WORKER_TELEMETRY_COMMANDS:
                    connection.execute(
                        """
                        UPDATE worker_commands
                        SET status = 'cancelled',
                            error = NULL,
                            completed_at = ?,
                            updated_at = ?
                        WHERE id = ? AND worker_id = ?
                        """,
                        (timestamp, timestamp, active["id"], worker_id),
                    )
                else:
                    raise ValueError("Worker already has an active command.")
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
            if command in WORKER_LIFECYCLE_COMMANDS:
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
    ensure_initialized()
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
    ensure_initialized()
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
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        )


def latest_worker_commands(worker_ids: list[str] | set[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    ensure_initialized()
    unique_worker_ids = list(dict.fromkeys(str(value or "").strip() for value in worker_ids or [] if str(value or "").strip()))
    if not unique_worker_ids:
        return {}
    rows: list[sqlite3.Row] = []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        for start in range(0, len(unique_worker_ids), 400):
            chunk = unique_worker_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"""
                    SELECT wc.*
                    FROM worker_commands wc
                    JOIN (
                        SELECT worker_id, MAX(created_at) AS latest_created_at
                        FROM worker_commands
                        WHERE worker_id IN ({placeholders})
                        GROUP BY worker_id
                    ) latest
                      ON latest.worker_id = wc.worker_id
                     AND latest.latest_created_at = wc.created_at
                    WHERE wc.worker_id IN ({placeholders})
                    ORDER BY wc.worker_id ASC, wc.created_at DESC, wc.rowid DESC
                    """,
                    (*chunk, *chunk),
                ).fetchall()
            )
    commands: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        worker_id = str(item.get("worker_id") or "").strip()
        if worker_id and worker_id not in commands:
            commands[worker_id] = item
    return commands


def _get_next_worker_command_locked(connection: sqlite3.Connection, worker_id: str) -> dict[str, Any] | None:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
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

def get_next_worker_command(worker_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return _get_next_worker_command_locked(connection, worker_id)

def update_worker_command_status(record: dict[str, Any]) -> dict[str, Any] | None:
    ensure_initialized()
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
            if (
                status == "succeeded"
                and command["command"] == "refresh_codex_quota"
                and command["telemetry_received_at"] is None
            ):
                raise ValueError("Codex quota refresh must heartbeat refreshed telemetry before succeeding.")
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


def mark_running_worker_quota_refresh_telemetry(
    worker_id: str,
    *,
    timestamp: int | None = None,
) -> dict[str, Any] | None:
    ensure_initialized()
    target_worker_id = str(worker_id or "").strip()
    if not target_worker_id:
        return None
    recorded_at = int(timestamp if timestamp is not None else time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                UPDATE worker_commands
                SET telemetry_received_at = COALESCE(telemetry_received_at, ?),
                    updated_at = ?
                WHERE worker_id = ?
                  AND command = 'refresh_codex_quota'
                  AND status = 'running'
                """,
                (recorded_at, recorded_at, target_worker_id),
            )
            row = connection.execute(
                """
                SELECT * FROM worker_commands
                WHERE worker_id = ?
                  AND command = 'refresh_codex_quota'
                  AND status = 'running'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (target_worker_id,),
            ).fetchone()
    return row_to_dict(row)


def cleanup_operational_records(
    *,
    timestamp: int | None = None,
    worker_command_retention_seconds: int = 30 * 24 * 60 * 60,
    worker_audit_retention_seconds: int = 90 * 24 * 60 * 60,
    scan_job_retention_seconds: int = 30 * 24 * 60 * 60,
    removable_scan_ids: set[str] | None = None,
) -> dict[str, int]:
    ensure_initialized()
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
                        WHERE status IN ('done', 'failed', 'cancelled', 'partial_completed', 'lost')
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


def cleanup_user_scan_issue_records(
    *,
    timestamp: int | None = None,
    retention_seconds: int = 90 * 24 * 60 * 60,
) -> dict[str, int]:
    ensure_initialized()
    current_time = int(timestamp if timestamp is not None else time.time())
    cutoff = current_time - max(0, int(retention_seconds))
    terminal_job_statuses = ("done", "failed", "cancelled", "partial_completed", "lost")
    terminal_scan_statuses = ("done", "failed", "cancelled", "partial_completed", "lost")
    terminal_job_placeholders = ",".join("?" for _ in terminal_job_statuses)
    terminal_scan_placeholders = ",".join("?" for _ in terminal_scan_statuses)
    old_scan_ids: set[str] = set()
    counts = {"issues": 0, "scans": 0, "scan_jobs": 0}
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            job_rows = connection.execute(
                f"""
                SELECT scan_id
                FROM scan_jobs
                WHERE created_at < ?
                  AND lower(COALESCE(status, '')) IN ({terminal_job_placeholders})
                  AND NULLIF(scan_id, '') IS NOT NULL
                """,
                (cutoff, *terminal_job_statuses),
            ).fetchall()
            old_scan_ids.update(str(row["scan_id"] or "").strip() for row in job_rows)

            scan_rows = connection.execute(
                f"""
                SELECT scans.scan_id
                FROM scans
                WHERE scans.created_at < ?
                  AND lower(COALESCE(scans.status, '')) IN ({terminal_scan_placeholders})
                  AND NULLIF(scans.scan_id, '') IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scan_jobs retained_job
                      WHERE retained_job.scan_id = scans.scan_id
                        AND lower(COALESCE(retained_job.status, '')) NOT IN ({terminal_job_placeholders})
                      LIMIT 1
                  )
                """,
                (cutoff, *terminal_scan_statuses, *terminal_job_statuses),
            ).fetchall()
            old_scan_ids.update(str(row["scan_id"] or "").strip() for row in scan_rows)
            old_scan_ids.discard("")

            scan_ids = sorted(old_scan_ids)
            for start in range(0, len(scan_ids), 400):
                chunk = scan_ids[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                counts["issues"] += connection.execute(
                    f"DELETE FROM issues WHERE scan_id IN ({placeholders})",
                    tuple(chunk),
                ).rowcount
                counts["scans"] += connection.execute(
                    f"DELETE FROM scans WHERE scan_id IN ({placeholders})",
                    tuple(chunk),
                ).rowcount
                counts["scan_jobs"] += connection.execute(
                    f"""
                    DELETE FROM scan_jobs
                    WHERE scan_id IN ({placeholders})
                      AND lower(COALESCE(status, '')) IN ({terminal_job_placeholders})
                    """,
                    (*chunk, *terminal_job_statuses),
                ).rowcount

            counts["issues"] += connection.execute(
                f"""
                DELETE FROM issues
                WHERE created_at < ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scan_jobs retained_job
                      WHERE retained_job.scan_id = issues.scan_id
                        AND lower(COALESCE(retained_job.status, '')) NOT IN ({terminal_job_placeholders})
                      LIMIT 1
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scans retained_scan
                      WHERE retained_scan.scan_id = issues.scan_id
                        AND lower(COALESCE(retained_scan.status, '')) NOT IN ({terminal_scan_placeholders})
                      LIMIT 1
                  )
                """,
                (cutoff, *terminal_job_statuses, *terminal_scan_statuses),
            ).rowcount
    return {key: max(0, value) for key, value in counts.items()}


def scan_payload_for_storage(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): scan_payload_for_storage(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scan_payload_for_storage(item) for item in value]
    return str(value)


def timestamp_field(value: Any, default: int) -> int:
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))


def scan_storage_fields(record: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    payload = scan_payload_for_storage(record)
    if not isinstance(payload, dict):
        return None
    scan_id = str(payload.get("id") or payload.get("scanId") or payload.get("scan_id") or "").strip()
    user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
    if not scan_id or not user_id:
        return None
    created_at = timestamp_field(
        payload.get("createdAt") or payload.get("created_at") or payload.get("queuedAt") or payload.get("queued_at"),
        current_time,
    )
    updated_at = timestamp_field(
        payload.get("updatedAt")
        or payload.get("updated_at")
        or payload.get("completedAt")
        or payload.get("completed_at")
        or payload.get("startedAt")
        or payload.get("started_at"),
        created_at,
    )
    status = str(payload.get("status") or "queued").strip().lower() or "queued"
    return {
        "scan_id": scan_id,
        "user_id": user_id,
        "job_id": str(payload.get("jobId") or payload.get("job_id") or "").strip(),
        "repo": str(payload.get("repo") or "").strip(),
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "payload": payload,
    }


def scan_from_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload_text = row["payload"] if isinstance(row, sqlite3.Row) else row.get("payload")
    try:
        payload = json.loads(str(payload_text or "{}"))
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def upsert_scan(record: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any] | None:
    ensure_initialized()
    fields = scan_storage_fields(record, timestamp=timestamp)
    if not fields:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO scans (scan_id, user_id, job_id, repo, status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    job_id = excluded.job_id,
                    repo = excluded.repo,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    fields["scan_id"],
                    fields["user_id"],
                    fields["job_id"],
                    fields["repo"],
                    fields["status"],
                    fields["created_at"],
                    fields["updated_at"],
                    json.dumps(fields["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True),
                ),
            )
            row = connection.execute("SELECT * FROM scans WHERE scan_id = ?", (fields["scan_id"],)).fetchone()
    return scan_from_row(row)


def get_user_scan_snapshot(user_id: str, scan_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_scan_id = str(scan_id or "").strip()
    if not target_user_id or not target_scan_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM scans WHERE user_id = ? AND scan_id = ?",
            (target_user_id, target_scan_id),
        ).fetchone()
    return scan_from_row(row)


def list_scan_snapshots_for_scan_ids(scan_ids: list[str] | set[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    ensure_initialized()
    unique_scan_ids = list(dict.fromkeys(str(value or "").strip() for value in scan_ids or [] if str(value or "").strip()))
    if not unique_scan_ids:
        return []
    rows: list[sqlite3.Row] = []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        for start in range(0, len(unique_scan_ids), 400):
            chunk = unique_scan_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"SELECT * FROM scans WHERE scan_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
            )
    return [scan for row in rows if (scan := scan_from_row(row)) is not None]


def list_scan_snapshots(*, limit: int = 1000) -> list[dict[str, Any]]:
    ensure_initialized()
    safe_limit = max(1, min(10000, int(limit or 1000)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM scans
            ORDER BY updated_at DESC, scan_id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [scan for row in rows if (scan := scan_from_row(row)) is not None]


def count_scan_snapshots() -> int:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM scans").fetchone()
    return max(0, int(row[0] if row else 0))


def delete_scan_snapshots(
    scan_ids: list[str] | set[str] | tuple[str, ...],
    *,
    user_id: str = "",
) -> dict[str, int]:
    ensure_initialized()
    unique_scan_ids = list(dict.fromkeys(str(value or "").strip() for value in scan_ids or [] if str(value or "").strip()))
    if not unique_scan_ids:
        return {"scans": 0, "issues": 0}
    target_user_id = str(user_id or "").strip()
    counts = {"scans": 0, "issues": 0}
    with _LOCK, closing(connect()) as connection:
        with connection:
            for start in range(0, len(unique_scan_ids), 400):
                chunk = unique_scan_ids[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                issue_clauses = [f"scan_id IN ({placeholders})"]
                scan_clauses = [f"scan_id IN ({placeholders})"]
                params: list[Any] = list(chunk)
                if target_user_id:
                    issue_clauses.append("user_id = ?")
                    scan_clauses.append("user_id = ?")
                    params.append(target_user_id)
                counts["issues"] += connection.execute(
                    f"DELETE FROM issues WHERE {' AND '.join(issue_clauses)}",
                    tuple(params),
                ).rowcount
                counts["scans"] += connection.execute(
                    f"DELETE FROM scans WHERE {' AND '.join(scan_clauses)}",
                    tuple(params),
                ).rowcount
    return {key: max(0, value) for key, value in counts.items()}


def find_user_scan_snapshot_by_request_id(user_id: str, request_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_request_id = str(request_id or "").strip()
    if not target_user_id or not target_request_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM scans
            WHERE user_id = ?
            ORDER BY updated_at DESC, scan_id DESC
            """,
            (target_user_id,),
        ).fetchall()
    for row in rows:
        scan = scan_from_row(row)
        if isinstance(scan, dict) and str(scan.get("requestId") or "").strip() == target_request_id:
            return scan
    return None


def create_scan_job(record: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
    job_id = str(record.get("job_id") or stable_id("job", record.get("scan_id"))).strip()
    scan_id = str(record.get("scan_id") or "").strip()
    repo = str(record.get("repo") or "").strip()
    if not job_id or not scan_id or not repo:
        raise ValueError("job_id, scan_id, and repo are required")
    timestamp = int(record.get("created_at") or time.time())
    provider_chain = provider_list_json(record.get("provider_chain"))
    worker_scope = WORKER_SCOPE_SHARED
    worker_owner_user_id = ""
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO scan_jobs (
                    job_id, scan_id, repo, branch, "commit", status, attempt,
                    claimed_by_worker_id, claimed_at, started_at, completed_at,
                    timeout_at, error, result_checksum, created_at, updated_at,
                    user_id, worker_scope, worker_owner_user_id,
                    repo_id, github_repo_id, installation_id, clone_url,
                    progress_phase, progress, progress_message, logs_summary,
                    review_output_language, provider_chain
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
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
                    worker_scope,
                    worker_owner_user_id or None,
                    record.get("repo_id"),
                    record.get("github_repo_id"),
                    record.get("installation_id"),
                    record.get("clone_url"),
                    record.get("review_output_language"),
                    provider_chain,
                ),
            )
            connection.execute("DELETE FROM scan_job_attempts WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM job_result_artifacts WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM job_results WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM review_decision_events WHERE job_id = ?", (job_id,))
            return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone()) or {}


def get_scan_job(job_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())


def get_scan_job_for_scan(scan_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone())


def scan_job_status_values_for_public_status(status: str) -> list[str]:
    normalized = str(status or "").strip().lower()
    if normalized == "running":
        return ["claimed", "running", "uploading_result"]
    if normalized == "queued":
        return ["queued"]
    if normalized == "failed":
        return ["failed", "lost"]
    if normalized == "done":
        return ["done", "partial_completed"]
    if normalized == "cancelled":
        return [normalized]
    return []


def list_user_scan_jobs_page(
    user_id: str,
    *,
    status: str = "",
    repo: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    safe_limit = max(1, min(100, int(limit or 50)))
    safe_offset = max(0, int(offset or 0))
    clauses = ["user_id = ?"]
    params: list[Any] = [target_user_id]
    status_values = scan_job_status_values_for_public_status(status)
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        clauses.append(f"status IN ({placeholders})")
        params.extend(status_values)
    normalized_repo = str(repo or "").strip().lower()
    if normalized_repo:
        clauses.append("lower(repo) = ?")
        params.append(normalized_repo)
    where = " AND ".join(clauses)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        total = int(connection.execute(f"SELECT COUNT(*) FROM scan_jobs WHERE {where}", tuple(params)).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT *
            FROM scan_jobs
            WHERE {where}
            ORDER BY created_at DESC, job_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, safe_limit, safe_offset),
        ).fetchall()
    return {
        "items": [row_to_dict(row) or {} for row in rows],
        "total": max(0, total),
        "limit": safe_limit,
        "offset": safe_offset,
    }


def list_user_scan_jobs_by_scan_ids(user_id: str, scan_ids: list[str]) -> list[dict[str, Any]]:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    ordered_ids = []
    seen = set()
    for value in scan_ids:
        scan_id = str(value or "").strip()
        if not scan_id or scan_id in seen:
            continue
        seen.add(scan_id)
        ordered_ids.append(scan_id)
    if not target_user_id or not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT *
            FROM scan_jobs
            WHERE user_id = ? AND scan_id IN ({placeholders})
            """,
            (target_user_id, *ordered_ids),
        ).fetchall()
    by_scan_id = {
        str(row["scan_id"] or "").strip(): row_to_dict(row) or {}
        for row in rows
        if str(row["scan_id"] or "").strip()
    }
    return [by_scan_id[scan_id] for scan_id in ordered_ids if scan_id in by_scan_id]


def count_user_scan_jobs_by_public_status(user_id: str) -> dict[str, int]:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return {}
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM scan_jobs
            WHERE user_id = ?
            GROUP BY status
            """,
            (target_user_id,),
        ).fetchall()
    counts: dict[str, int] = {}
    for status, count in rows:
        raw_status = str(status or "").strip().lower()
        if raw_status in {"claimed", "running", "uploading_result"}:
            public_status = "running"
        elif raw_status == "queued":
            public_status = "queued"
        elif raw_status in {"failed", "lost"}:
            public_status = "failed"
        elif raw_status in {"done", "partial_completed"}:
            public_status = "done"
        elif raw_status == "cancelled":
            public_status = raw_status
        else:
            public_status = raw_status or "unknown"
        counts[public_status] = counts.get(public_status, 0) + max(0, int(count or 0))
    return counts


def count_user_scan_jobs(user_id: str) -> int:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return 0
    with _LOCK, closing(connect()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM scan_jobs WHERE user_id = ?", (target_user_id,)).fetchone()
    return max(0, int(row[0] if row else 0))


def count_scan_jobs() -> int:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()
    return max(0, int(row[0] if row else 0))


def get_user_scan_job(user_id: str, scan_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_scan_id = str(scan_id or "").strip()
    if not target_user_id or not target_scan_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                "SELECT * FROM scan_jobs WHERE user_id = ? AND scan_id = ?",
                (target_user_id, target_scan_id),
            ).fetchone()
        )


def get_latest_user_repo_scan_job(user_id: str, repo_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_repo_id = str(repo_id or "").strip()
    if not target_user_id or not target_repo_id:
        return None
    clauses = ["user_id = ?", "repo_id = ?"]
    params: list[Any] = [target_user_id, target_repo_id]
    if active_only:
        clauses.append("status IN ('queued', 'claimed', 'running', 'uploading_result')")
    where = " AND ".join(clauses)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute(
                f"""
                SELECT *
                FROM scan_jobs
                WHERE {where}
                ORDER BY created_at DESC, job_id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        )


def scan_queue_stats(scan_id: str) -> dict[str, Any]:
    ensure_initialized()
    target_scan_id = str(scan_id or "").strip()
    if not target_scan_id:
        return {
            "running_global": 0,
            "position": 0,
            "ahead": 0,
        }
    queued_statuses = ("queued",)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        job = connection.execute(
            "SELECT scan_id, user_id, status, created_at FROM scan_jobs WHERE scan_id = ?",
            (target_scan_id,),
        ).fetchone()
        running_global = int(
            connection.execute(
                "SELECT COUNT(*) FROM scan_jobs WHERE status IN ('claimed', 'running', 'uploading_result')"
            ).fetchone()[0]
        )
        if not job or str(job["status"] or "") not in queued_statuses:
            return {
                "running_global": max(0, running_global),
                "position": 0,
                "ahead": 0,
            }
        created_at = int(job["created_at"] or 0)
        ahead = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM scan_jobs
                WHERE status = 'queued'
                  AND (created_at < ? OR (created_at = ? AND scan_id < ?))
                """,
                (created_at, created_at, target_scan_id),
            ).fetchone()[0]
        )
    return {
        "running_global": max(0, running_global),
        "position": ahead + 1,
        "ahead": max(0, ahead),
    }


def scan_queue_limit_counts() -> dict[str, int]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        queued_global = int(
            connection.execute(
                "SELECT COUNT(*) FROM scan_jobs WHERE status = 'queued'"
            ).fetchone()[0]
        )
    return {
        "queued_global": max(0, queued_global),
    }


def list_scan_jobs_for_scans(
    scan_ids: list[str] | set[str] | tuple[str, ...],
    job_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    ensure_initialized()
    unique_scan_ids = list(dict.fromkeys(str(value or "").strip() for value in scan_ids or [] if str(value or "").strip()))
    unique_job_ids = list(dict.fromkeys(str(value or "").strip() for value in job_ids or [] if str(value or "").strip()))
    if not unique_scan_ids and not unique_job_ids:
        return []

    rows: list[sqlite3.Row] = []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        for values, column in ((unique_scan_ids, "scan_id"), (unique_job_ids, "job_id")):
            for start in range(0, len(values), 400):
                chunk = values[start : start + 400]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"SELECT * FROM scan_jobs WHERE {column} IN ({placeholders})",
                        tuple(chunk),
                    ).fetchall()
                )

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = row_to_dict(row) or {}
        key = str(item.get("job_id") or item.get("scan_id") or "")
        if key:
            deduped[key] = item
    return list(deduped.values())



def list_scan_jobs_missing_from_state(scan_ids: list[str] | set[str]) -> list[dict[str, Any]]:
    ensure_initialized()
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
    ensure_initialized()
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
                q.reason,
                COUNT(*) AS ledger_rows
            FROM quota_ledger q
            LEFT JOIN scan_jobs sj ON sj.scan_id = q.scan_id
            WHERE q.reason IN ('scan_created', 'scan_consumed', 'scan_reserved')
              AND q.delta > 0
              AND q.scan_id IS NOT NULL
              AND q.scan_id != ''
              AND sj.scan_id IS NULL
              {existing_clause}
            GROUP BY q.scan_id, q.requested_by_user_id, q.request_id, q.reason
            ORDER BY MIN(q.created_at) ASC, q.scan_id ASC
            """,
            params,
        ).fetchall()
        return [row_to_dict(row) or {} for row in rows]


def update_scan_job_commit(job_id: str, commit: str) -> dict[str, Any] | None:
    ensure_initialized()
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
    ensure_initialized()
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
                SET status = CASE
                        WHEN status = 'claimed' THEN 'running'
                        ELSE status
                    END,
                    timeout_at = CASE
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


def worker_job_update_statuses(worker_id: str, job_ids: list[str]) -> dict[str, list[str]]:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    unique_job_ids = []
    seen = set()
    for value in job_ids or []:
        job_id = str(value or "").strip()
        if job_id and job_id not in seen:
            unique_job_ids.append(job_id)
            seen.add(job_id)
    if not worker_id or not unique_job_ids:
        return {"accepting": [], "no_longer_accepting": []}
    placeholders = ",".join("?" for _ in unique_job_ids)
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT job_id, status
            FROM scan_jobs
            WHERE claimed_by_worker_id = ?
              AND job_id IN ({placeholders})
            """,
            (worker_id, *unique_job_ids),
        ).fetchall()
    statuses = {str(row[0] or "").strip(): str(row[1] or "").strip() for row in rows if str(row[0] or "").strip()}
    accepting_statuses = {"claimed", "running", "uploading_result"}
    accepting = [job_id for job_id in unique_job_ids if statuses.get(job_id) in accepting_statuses]
    no_longer_accepting = [job_id for job_id in unique_job_ids if job_id in statuses and statuses.get(job_id) not in accepting_statuses]
    return {"accepting": accepting, "no_longer_accepting": no_longer_accepting}


def record_active_worker_heartbeat(
    record: dict[str, Any],
    active_job_ids: list[str],
    *,
    grace_seconds: int = 120,
    lease_seconds: int = 3600,
    progress_event: dict[str, Any] | None = None,
    scan_job_progress: dict[str, Any] | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    ensure_initialized()
    worker_id = str(record.get("worker_id") or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    unique_active_job_ids = []
    seen = set()
    for value in active_job_ids or []:
        job_id = str(value or "").strip()
        if job_id and job_id not in seen:
            unique_active_job_ids.append(job_id)
            seen.add(job_id)
    current_time = int(timestamp if timestamp is not None else record.get("timestamp") or time.time())
    requested_running_jobs = 1 if int(record.get("running_jobs") or 0) > 0 else 0
    accepting_statuses = {"claimed", "running", "uploading_result"}
    accepting: list[str] = []
    no_longer_accepting: list[str] = []
    recovered: list[dict[str, Any]] = []
    renewed_count = 0
    progress_job: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    with closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            if unique_active_job_ids:
                placeholders = ",".join("?" for _ in unique_active_job_ids)
                rows = connection.execute(
                    f"""
                    SELECT job_id, status
                    FROM scan_jobs
                    WHERE claimed_by_worker_id = ?
                      AND job_id IN ({placeholders})
                    """,
                    (worker_id, *unique_active_job_ids),
                ).fetchall()
                statuses = {
                    str(row["job_id"] or "").strip(): str(row["status"] or "").strip()
                    for row in rows
                    if str(row["job_id"] or "").strip()
                }
                accepting = [job_id for job_id in unique_active_job_ids if statuses.get(job_id) in accepting_statuses]
                no_longer_accepting = [
                    job_id
                    for job_id in unique_active_job_ids
                    if job_id in statuses and statuses.get(job_id) not in accepting_statuses
                ]
            heartbeat_record = {
                **record,
                "running_jobs": 1 if requested_running_jobs and accepting else 0,
                "timestamp": current_time,
            }
            worker = _upsert_worker_heartbeat_locked(connection, heartbeat_record)
            cutoff = current_time - max(30, int(grace_seconds or 120))
            active_clause = ""
            active_values: list[Any] = []
            if unique_active_job_ids:
                active_placeholders = ",".join("?" for _ in unique_active_job_ids)
                active_clause = f"AND job_id NOT IN ({active_placeholders})"
                active_values.extend(unique_active_job_ids)
            rows = connection.execute(
                f"""
                SELECT job_id, scan_id, attempt
                FROM scan_jobs
                WHERE claimed_by_worker_id = ?
                  AND status = 'claimed'
                  AND started_at IS NULL
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= ?
                  {active_clause}
                """,
                (worker_id, cutoff, *active_values),
            ).fetchall()
            for row in rows:
                _complete_scan_job_attempt_locked(
                    connection,
                    job_id=row["job_id"],
                    attempt=int(row["attempt"]),
                    worker_id=worker_id,
                    status="failed",
                    completed_at=current_time,
                    error="worker_job_startup_lost",
                )
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'failed',
                        completed_at = ?,
                        timeout_at = NULL,
                        error = 'worker_job_startup_lost',
                        projection_pending = 1,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'claimed' AND started_at IS NULL
                    """,
                    (current_time, current_time, row["job_id"]),
                )
                _finalize_recovered_review_run_locked(
                    connection,
                    job_id=str(row["job_id"]),
                    status="failed",
                    reason="worker_job_startup_lost",
                    completed_at=current_time,
                )
                recovered.append(
                    {
                        "job_id": row["job_id"],
                        "scan_id": row["scan_id"],
                        "status": "failed",
                        "reason": "worker_job_startup_lost",
                        "attempt": int(row["attempt"]),
                    }
                )
            if accepting:
                timeout_at = current_time + max(60, int(lease_seconds or 3600))
                placeholders = ",".join("?" for _ in accepting)
                cursor = connection.execute(
                    f"""
                    UPDATE scan_jobs
                    SET status = CASE
                            WHEN status = 'claimed' THEN 'running'
                            ELSE status
                        END,
                        timeout_at = CASE
                            WHEN timeout_at IS NULL OR timeout_at < ? THEN ?
                            ELSE timeout_at
                        END,
                        updated_at = ?
                    WHERE claimed_by_worker_id = ?
                      AND status IN ('claimed', 'running', 'uploading_result')
                      AND job_id IN ({placeholders})
                    """,
                    (timeout_at, timeout_at, current_time, worker_id, *accepting),
                )
                renewed_count = max(0, cursor.rowcount)
            if progress_event is not None and scan_job_progress is not None:
                progress_job_id = str(scan_job_progress.get("job_id") or progress_event.get("job_id") or "").strip()
                if progress_job_id and progress_job_id in accepting:
                    _upsert_review_run_progress_locked(connection, progress_event)
                    raw_timeout_at = scan_job_progress.get("timeout_at")
                    try:
                        progress_timeout_at = int(raw_timeout_at) if raw_timeout_at is not None else None
                    except (TypeError, ValueError):
                        progress_timeout_at = None
                    progress_cursor = connection.execute(
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
                            scan_job_progress.get("phase"),
                            max(0, min(100, int(scan_job_progress.get("progress") or 0))),
                            scan_job_progress.get("message"),
                            int(scan_job_progress.get("started_at") or current_time),
                            progress_timeout_at,
                            progress_timeout_at,
                            progress_timeout_at,
                            scan_job_progress.get("logs_summary"),
                            current_time,
                            progress_job_id,
                        ),
                    )
                    if progress_cursor.rowcount > 0:
                        progress_job = row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (progress_job_id,)).fetchone())
            command = _get_next_worker_command_locked(connection, worker_id)
            connection.commit()
            return {
                "worker": worker,
                "accepting": accepting,
                "no_longer_accepting": no_longer_accepting,
                "recovered_jobs": recovered,
                "renewed_count": renewed_count,
                "progress_job": progress_job,
                "command": command,
            }
        except Exception:
            connection.rollback()
            raise

def worker_job_ids_no_longer_accepting_updates(worker_id: str, job_ids: list[str]) -> list[str]:
    return worker_job_update_statuses(worker_id, job_ids)["no_longer_accepting"]


def worker_job_ids_accepting_updates(worker_id: str, job_ids: list[str]) -> list[str]:
    return worker_job_update_statuses(worker_id, job_ids)["accepting"]

def fail_worker_unstarted_scan_jobs_missing_from_heartbeat(
    worker_id: str,
    active_job_ids: list[str],
    *,
    grace_seconds: int = 120,
    timestamp: int | None = None,
) -> list[dict[str, Any]]:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return []
    unique_active_job_ids = []
    seen = set()
    for value in active_job_ids or []:
        job_id = str(value or "").strip()
        if job_id and job_id not in seen:
            unique_active_job_ids.append(job_id)
            seen.add(job_id)
    current_time = int(timestamp if timestamp is not None else time.time())
    cutoff = current_time - max(30, int(grace_seconds or 120))
    active_clause = ""
    active_values: list[Any] = []
    if unique_active_job_ids:
        placeholders = ",".join("?" for _ in unique_active_job_ids)
        active_clause = f"AND job_id NOT IN ({placeholders})"
        active_values.extend(unique_active_job_ids)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                f"""
                SELECT job_id, scan_id, attempt
                FROM scan_jobs
                WHERE claimed_by_worker_id = ?
                  AND status = 'claimed'
                  AND started_at IS NULL
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= ?
                  {active_clause}
                """,
                (worker_id, cutoff, *active_values),
            ).fetchall()
            recovered: list[dict[str, Any]] = []
            for row in rows:
                _complete_scan_job_attempt_locked(
                    connection,
                    job_id=row["job_id"],
                    attempt=int(row["attempt"]),
                    worker_id=worker_id,
                    status="failed",
                    completed_at=current_time,
                    error="worker_job_startup_lost",
                )
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'failed',
                        completed_at = ?,
                        timeout_at = NULL,
                        error = 'worker_job_startup_lost',
                        projection_pending = 1,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'claimed' AND started_at IS NULL
                    """,
                    (current_time, current_time, row["job_id"]),
                )
                _finalize_recovered_review_run_locked(
                    connection,
                    job_id=str(row["job_id"]),
                    status="failed",
                    reason="worker_job_startup_lost",
                    completed_at=current_time,
                )
                recovered.append(
                    {
                        "job_id": row["job_id"],
                        "scan_id": row["scan_id"],
                        "status": "failed",
                        "reason": "worker_job_startup_lost",
                        "attempt": int(row["attempt"]),
                    }
                )
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise

def list_worker_task_activity(worker_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    ensure_initialized()
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


def count_worker_running_scan_jobs(worker_id: str) -> int:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return 0
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_jobs
            WHERE claimed_by_worker_id = ?
              AND status IN ('running', 'uploading_result')
            """,
            (worker_id,),
        ).fetchone()
    return max(0, int(row[0] if row else 0))


def worker_running_scan_job_counts(worker_ids: list[str] | set[str] | tuple[str, ...] | None = None) -> dict[str, int]:
    ensure_initialized()
    unique_worker_ids = list(dict.fromkeys(str(value or "").strip() for value in worker_ids or [] if str(value or "").strip()))
    clauses = ["status IN ('claimed', 'running', 'uploading_result')", "claimed_by_worker_id IS NOT NULL", "claimed_by_worker_id != ''"]
    params: list[Any] = []
    if unique_worker_ids:
        placeholders = ",".join("?" for _ in unique_worker_ids)
        clauses.append(f"claimed_by_worker_id IN ({placeholders})")
        params.extend(unique_worker_ids)
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT claimed_by_worker_id, COUNT(*) AS running_count
            FROM scan_jobs
            WHERE {' AND '.join(clauses)}
            GROUP BY claimed_by_worker_id
            """,
            tuple(params),
        ).fetchall()
    return {
        str(row[0] or "").strip(): max(0, int(row[1] or 0))
        for row in rows
        if str(row[0] or "").strip()
    }


def _enabled_worker_count_locked(
    connection: sqlite3.Connection,
    *,
    worker_scope: str | None = WORKER_SCOPE_SHARED,
    owner_user_id: str | None = None,
) -> int:
    filters = ["enabled = 1", "deleted_at IS NULL"]
    params: list[Any] = []
    if worker_scope is not None:
        scope = normalize_worker_scope(worker_scope)
        filters.append("COALESCE(worker_scope, ?) = ?")
        params.extend([WORKER_SCOPE_SHARED, scope])
    owner = str(owner_user_id or "").strip()
    if owner:
        filters.append("owner_user_id = ?")
        params.append(owner)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS worker_count
        FROM workers
        WHERE {' AND '.join(filters)}
        """,
        tuple(params),
    ).fetchone()
    return max(0, int(row[0] if row else 0))


def count_enabled_workers(
    *,
    worker_scope: str | None = WORKER_SCOPE_SHARED,
    owner_user_id: str | None = None,
) -> int:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        return _enabled_worker_count_locked(
            connection,
            worker_scope=worker_scope,
            owner_user_id=owner_user_id,
        )


def _scan_job_attempt_id(job_id: str, attempt: int) -> str:
    return stable_id("sja", f"{job_id}:{attempt}")


def _record_scan_job_attempt_locked(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    attempt: int,
    worker_id: str,
    claimed_at: int,
) -> None:
    existing_worker_attempt = connection.execute(
        "SELECT attempt FROM scan_job_attempts WHERE job_id = ? AND worker_id = ?",
        (job_id, worker_id),
    ).fetchone()
    if existing_worker_attempt is not None:
        connection.execute(
            """
            UPDATE scan_job_attempts
            SET id = ?,
                attempt = ?,
                status = 'claimed',
                claimed_at = ?,
                completed_at = NULL,
                error = NULL,
                updated_at = ?
            WHERE job_id = ?
              AND worker_id = ?
            """,
            (_scan_job_attempt_id(job_id, attempt), attempt, claimed_at, claimed_at, job_id, worker_id),
        )
        return
    connection.execute(
        """
        INSERT INTO scan_job_attempts (
            id, job_id, attempt, worker_id, status, claimed_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?)
        ON CONFLICT(job_id, attempt) DO UPDATE SET
            worker_id = excluded.worker_id,
            status = 'claimed',
            claimed_at = excluded.claimed_at,
            completed_at = NULL,
            error = NULL,
            updated_at = excluded.updated_at
        """,
        (_scan_job_attempt_id(job_id, attempt), job_id, attempt, worker_id, claimed_at, claimed_at, claimed_at),
    )


def _complete_scan_job_attempt_locked(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    attempt: int,
    worker_id: str,
    status: str,
    completed_at: int,
    error: object = None,
) -> None:
    clean_status = str(status or "").strip().lower() or "failed"
    error_text = str(error or "").strip() or None
    updated = connection.execute(
        """
        UPDATE scan_job_attempts
        SET status = ?,
            completed_at = ?,
            error = ?,
            updated_at = ?
        WHERE job_id = ?
          AND attempt = ?
        """,
        (clean_status, completed_at, error_text, completed_at, job_id, attempt),
    ).rowcount
    if updated:
        return
    if worker_id:
        connection.execute(
            """
            INSERT OR IGNORE INTO scan_job_attempts (
                id, job_id, attempt, worker_id, status, claimed_at, completed_at, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _scan_job_attempt_id(job_id, attempt),
                job_id,
                attempt,
                worker_id,
                clean_status,
                completed_at,
                completed_at,
                error_text,
                completed_at,
                completed_at,
            ),
        )


def scan_job_status_counts(*, worker_scope: str | None = None) -> dict[str, int]:
    ensure_initialized()
    params: tuple[Any, ...] = ()
    where = ""
    if worker_scope is not None:
        scope = normalize_worker_scope(worker_scope)
        where = "WHERE COALESCE(worker_scope, ?) = ?"
        params = (WORKER_SCOPE_SHARED, scope)
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT status, COUNT(*) AS job_count
            FROM scan_jobs
            {where}
            GROUP BY status
            """,
            params,
        ).fetchall()
    counts = {str(row[0] or "").strip().lower(): max(0, int(row[1] or 0)) for row in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("claimed", 0) + counts.get("running", 0) + counts.get("uploading_result", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0) + counts.get("lost", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def scan_job_result_artifact_id(job_id: str, attempt_id: str) -> str:
    return stable_id("jra", f"{job_id}:{attempt_id}:payload")


def scan_job_result_summary_payload(payload: dict[str, Any], *, artifact_id: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "artifactId": artifact_id,
        "artifactKind": "worker_result_payload",
        "status": payload.get("status") if isinstance(payload, dict) else None,
    }
    if isinstance(payload, dict):
        for key in (
            "result_checksum",
            "resolved_commit",
            "resolvedCommit",
            "commit",
            "duration_ms",
            "error",
            "error_code",
            "errorCode",
            "summary",
            "preflight",
            "effectiveAgentConfig",
            "humanReport",
            "agentReport",
            "readingGuide",
        ):
            if key in payload:
                summary[key] = payload.get(key)
    return scan_payload_for_storage(summary)


def result_payload_from_row(item: dict[str, Any]) -> dict[str, Any]:
    artifact_text = item.pop("result_artifact_payload", None)
    payload_text = artifact_text or item.get("result_payload")
    try:
        payload = json.loads(str(payload_text or "{}"))
    except (TypeError, json.JSONDecodeError):
        payload = {}
    item.pop("result_artifact_payload", None)
    return payload if isinstance(payload, dict) else {}


def list_completed_scan_job_results(
    *,
    after_created_at: int = 0,
    after_job_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    ensure_initialized()
    cursor_time = max(0, int(after_created_at or 0))
    cursor_job_id = str(after_job_id or "").strip()
    cursor_clause = ""
    params: list[Any] = []
    if cursor_time > 0 or cursor_job_id:
        cursor_clause = "AND (jr.created_at > ? OR (jr.created_at = ? AND sj.job_id > ?))"
        params.extend([cursor_time, cursor_time, cursor_job_id])
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(max(1, min(1000, int(limit or 500))))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT
                sj.*,
                jr.attempt_id AS result_attempt_id,
                jr.result_checksum AS result_result_checksum,
                jr.status AS result_status,
                jr.payload AS result_payload,
                jr.payload_artifact_id AS result_payload_artifact_id,
                jra.payload AS result_artifact_payload,
                jr.created_at AS result_created_at
            FROM scan_jobs sj
            JOIN job_results jr ON jr.job_id = sj.job_id
            LEFT JOIN job_result_artifacts jra
              ON jra.id = jr.payload_artifact_id
            WHERE sj.status IN ('done', 'failed', 'cancelled', 'partial_completed')
              AND jr.attempt_id = sj.last_attempt_id
              {cursor_clause}
            ORDER BY jr.created_at ASC, sj.job_id ASC
            {limit_clause}
            """,
            tuple(params),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["result_payload"] = result_payload_from_row(item)
        results.append(item)
    return results


def get_completed_scan_job_result(job_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                sj.*,
                jr.attempt_id AS result_attempt_id,
                jr.result_checksum AS result_result_checksum,
                jr.status AS result_status,
                jr.payload AS result_payload,
                jr.payload_artifact_id AS result_payload_artifact_id,
                jra.payload AS result_artifact_payload,
                jr.created_at AS result_created_at
            FROM scan_jobs sj
            JOIN job_results jr ON jr.job_id = sj.job_id
            LEFT JOIN job_result_artifacts jra
              ON jra.id = jr.payload_artifact_id
            WHERE sj.job_id = ?
              AND sj.status IN ('done', 'failed', 'cancelled', 'partial_completed')
              AND jr.attempt_id = sj.last_attempt_id
            """,
            (job_id,),
        ).fetchone()
    item = row_to_dict(row) if row else None
    if not item:
        return None
    item["result_payload"] = result_payload_from_row(item)
    return item


def list_completed_scan_job_results_for_job_ids(job_ids: list[str] | set[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    ensure_initialized()
    unique_job_ids = list(dict.fromkeys(str(value or "").strip() for value in job_ids or [] if str(value or "").strip()))
    if not unique_job_ids:
        return []
    rows: list[sqlite3.Row] = []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        for start in range(0, len(unique_job_ids), 400):
            chunk = unique_job_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"""
                    SELECT
                        sj.*,
                        jr.attempt_id AS result_attempt_id,
                        jr.result_checksum AS result_result_checksum,
                        jr.status AS result_status,
                        jr.payload AS result_payload,
                        jr.payload_artifact_id AS result_payload_artifact_id,
                        jra.payload AS result_artifact_payload,
                        jr.created_at AS result_created_at
                    FROM scan_jobs sj
                    JOIN job_results jr ON jr.job_id = sj.job_id
                    LEFT JOIN job_result_artifacts jra
                      ON jra.id = jr.payload_artifact_id
                    WHERE sj.job_id IN ({placeholders})
                      AND sj.status IN ('done', 'failed', 'cancelled', 'partial_completed')
                      AND jr.attempt_id = sj.last_attempt_id
                    """,
                    tuple(chunk),
                ).fetchall()
            )
    results: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["result_payload"] = result_payload_from_row(item)
        results.append(item)
    return results


def issue_payload_for_storage(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): issue_payload_for_storage(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [issue_payload_for_storage(item) for item in value]
    return str(value)


def issue_storage_fields(record: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    payload = issue_payload_for_storage(record)
    if not isinstance(payload, dict):
        return None
    issue_id = str(payload.get("id") or payload.get("issueId") or payload.get("issue_id") or "").strip()
    user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
    scan_id = str(payload.get("scanId") or payload.get("scan_id") or "").strip()
    job_id = str(payload.get("jobId") or payload.get("job_id") or "").strip()
    title = str(payload.get("title") or payload.get("claim") or "Untitled finding").strip()
    file_path = str(payload.get("file") or payload.get("path") or "").strip()
    if not issue_id:
        issue_id = stable_id("iss", f"{user_id}:{scan_id}:{job_id}:{file_path}:{title}")
        payload["id"] = issue_id
    if not issue_id or not user_id:
        return None
    try:
        created_at = int(payload.get("createdAt") or payload.get("created_at") or current_time)
    except (TypeError, ValueError, OverflowError):
        created_at = current_time
    try:
        updated_at = int(payload.get("updatedAt") or payload.get("updated_at") or created_at)
    except (TypeError, ValueError, OverflowError):
        updated_at = created_at
    return {
        "issue_id": issue_id,
        "user_id": user_id,
        "scan_id": scan_id,
        "job_id": job_id,
        "repo": str(payload.get("repo") or "").strip(),
        "status": str(payload.get("status") or "open").strip().lower() or "open",
        "severity": str(payload.get("severity") or "").strip().lower(),
        "category": str(payload.get("category") or "").strip().lower(),
        "title": title,
        "file_path": file_path,
        "created_at": max(0, created_at),
        "updated_at": max(0, updated_at),
        "payload": payload,
    }


def issue_from_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload_text = row["payload"] if isinstance(row, sqlite3.Row) else row.get("payload")
    try:
        payload = json.loads(str(payload_text or "{}"))
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def assign_available_issue_id(fields: dict[str, Any], used_ids: set[str]) -> None:
    base_id = str(fields.get("issue_id") or "").strip()
    if not base_id:
        return
    issue_id = base_id
    suffix = 2
    while issue_id in used_ids:
        issue_id = f"{base_id}-{suffix}"
        suffix += 1
    used_ids.add(issue_id)
    if issue_id == base_id:
        return
    fields["issue_id"] = issue_id
    payload = dict(fields.get("payload") or {})
    payload["id"] = issue_id
    if "issueId" in payload:
        payload["issueId"] = issue_id
    fields["payload"] = payload


def upsert_issue(record: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any] | None:
    ensure_initialized()
    fields = issue_storage_fields(record, timestamp=timestamp)
    if not fields:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            existing = connection.execute(
                "SELECT user_id, scan_id, job_id FROM issues WHERE issue_id = ?",
                (fields["issue_id"],),
            ).fetchone()
            if existing and (
                str(existing["user_id"] or "") != str(fields["user_id"] or "")
                or str(existing["scan_id"] or "") != str(fields["scan_id"] or "")
                or str(existing["job_id"] or "") != str(fields["job_id"] or "")
            ):
                used_ids = {
                    str(row[0] or "").strip()
                    for row in connection.execute("SELECT issue_id FROM issues").fetchall()
                    if str(row[0] or "").strip()
                }
                assign_available_issue_id(fields, used_ids)
            connection.execute(
                """
                INSERT INTO issues (
                    issue_id, user_id, scan_id, job_id, repo, status, severity,
                    category, title, file_path, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    scan_id = excluded.scan_id,
                    job_id = excluded.job_id,
                    repo = excluded.repo,
                    status = excluded.status,
                    severity = excluded.severity,
                    category = excluded.category,
                    title = excluded.title,
                    file_path = excluded.file_path,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    fields["issue_id"],
                    fields["user_id"],
                    fields["scan_id"],
                    fields["job_id"],
                    fields["repo"],
                    fields["status"],
                    fields["severity"],
                    fields["category"],
                    fields["title"],
                    fields["file_path"],
                    fields["created_at"],
                    fields["updated_at"],
                    json.dumps(fields["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True),
                ),
            )
            row = connection.execute("SELECT * FROM issues WHERE issue_id = ?", (fields["issue_id"],)).fetchone()
    return issue_from_row(row)


def replace_scan_issues(
    scan_id: str,
    *,
    user_id: str = "",
    job_id: str = "",
    issues: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    timestamp: int | None = None,
    preserve_existing_status: bool = False,
) -> list[dict[str, Any]]:
    ensure_initialized()
    target_scan_id = str(scan_id or "").strip()
    target_user_id = str(user_id or "").strip()
    target_job_id = str(job_id or "").strip()
    if not target_scan_id:
        return []
    current_time = int(timestamp if timestamp is not None else time.time())
    stored: list[dict[str, Any]] = []
    fields_list = []
    for issue in issues or []:
        candidate = dict(issue)
        candidate.setdefault("scanId", target_scan_id)
        if target_user_id:
            candidate.setdefault("userId", target_user_id)
        if target_job_id:
            candidate.setdefault("jobId", target_job_id)
        fields = issue_storage_fields(candidate, timestamp=current_time)
        if fields:
            fields_list.append(fields)
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            clauses = ["scan_id = ?"]
            params: list[Any] = [target_scan_id]
            if target_user_id:
                clauses.append("user_id = ?")
                params.append(target_user_id)
            if target_job_id:
                clauses.append("job_id = ?")
                params.append(target_job_id)
            existing_statuses: dict[str, tuple[str, int, dict[str, Any]]] = {}
            if preserve_existing_status:
                rows = connection.execute(
                    f"SELECT issue_id, status, updated_at, payload FROM issues WHERE {' AND '.join(clauses)}",
                    tuple(params),
                ).fetchall()
                for row in rows:
                    issue_id = str(row["issue_id"] or "").strip()
                    if not issue_id:
                        continue
                    payload = issue_from_row(row) or {}
                    existing_statuses[issue_id] = (
                        str(row["status"] or payload.get("status") or "open").strip().lower()
                        or "open",
                        max(0, int(row["updated_at"] or 0)),
                        payload,
                    )
            connection.execute(f"DELETE FROM issues WHERE {' AND '.join(clauses)}", tuple(params))
            used_issue_ids = {
                str(row[0] or "").strip()
                for row in connection.execute("SELECT issue_id FROM issues").fetchall()
                if str(row[0] or "").strip()
            }
            for fields in fields_list:
                assign_available_issue_id(fields, used_issue_ids)
                existing_status = existing_statuses.get(fields["issue_id"])
                if existing_status:
                    status, updated_at, existing_payload = existing_status
                    payload = dict(fields["payload"])
                    payload["status"] = status
                    existing_payload_updated_at = existing_payload.get("updatedAt")
                    payload["updatedAt"] = (
                        existing_payload_updated_at
                        if existing_payload_updated_at is not None
                        else updated_at
                    )
                    fields["status"] = status
                    fields["updated_at"] = updated_at
                    fields["payload"] = payload
                connection.execute(
                    """
                    INSERT INTO issues (
                        issue_id, user_id, scan_id, job_id, repo, status, severity,
                        category, title, file_path, created_at, updated_at, payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fields["issue_id"],
                        fields["user_id"],
                        fields["scan_id"],
                        fields["job_id"],
                        fields["repo"],
                        fields["status"],
                        fields["severity"],
                        fields["category"],
                        fields["title"],
                        fields["file_path"],
                        fields["created_at"],
                        fields["updated_at"],
                        json.dumps(fields["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True),
                    ),
                )
                stored.append(dict(fields["payload"]))
    return stored


def delete_issues_for_scan(scan_id: str, *, user_id: str = "", job_id: str = "") -> int:
    ensure_initialized()
    target_scan_id = str(scan_id or "").strip()
    if not target_scan_id:
        return 0
    clauses = ["scan_id = ?"]
    params: list[Any] = [target_scan_id]
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id).strip())
    if job_id:
        clauses.append("job_id = ?")
        params.append(str(job_id).strip())
    with _LOCK, closing(connect()) as connection:
        with connection:
            return max(0, connection.execute(f"DELETE FROM issues WHERE {' AND '.join(clauses)}", tuple(params)).rowcount)


def list_issue_snapshots(*, limit: int = 5000) -> list[dict[str, Any]]:
    ensure_initialized()
    safe_limit = max(1, min(20000, int(limit or 5000)))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM issues
            ORDER BY updated_at DESC, created_at DESC, issue_id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [issue for row in rows if (issue := issue_from_row(row)) is not None]


def count_issue_snapshots() -> int:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM issues").fetchone()
    return max(0, int(row[0] if row else 0))


def count_user_issues(user_id: str) -> int:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return 0
    with _LOCK, closing(connect()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM issues WHERE user_id = ?", (target_user_id,)).fetchone()
    return max(0, int(row[0] if row else 0))


def list_issue_ids(
    *,
    exclude_user_id: str = "",
    exclude_scan_id: str = "",
    exclude_job_id: str = "",
) -> list[str]:
    ensure_initialized()
    clauses: list[str] = []
    params: list[Any] = []
    target_user_id = str(exclude_user_id or "").strip()
    target_scan_id = str(exclude_scan_id or "").strip()
    target_job_id = str(exclude_job_id or "").strip()
    if target_user_id and target_scan_id and target_job_id:
        clauses.append("NOT (user_id = ? AND scan_id = ? AND job_id = ?)")
        params.extend([target_user_id, target_scan_id, target_job_id])
    query = "SELECT issue_id FROM issues"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]


def list_user_issue_ids(
    user_id: str,
    *,
    exclude_scan_id: str = "",
    exclude_job_id: str = "",
) -> list[str]:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return []
    clauses = ["user_id = ?"]
    params: list[Any] = [target_user_id]
    target_scan_id = str(exclude_scan_id or "").strip()
    target_job_id = str(exclude_job_id or "").strip()
    if target_scan_id and target_job_id:
        clauses.append("NOT (scan_id = ? AND job_id = ?)")
        params.extend([target_scan_id, target_job_id])
    elif target_scan_id:
        clauses.append("scan_id != ?")
        params.append(target_scan_id)
    elif target_job_id:
        clauses.append("job_id != ?")
        params.append(target_job_id)
    with _LOCK, closing(connect()) as connection:
        rows = connection.execute(
            f"SELECT issue_id FROM issues WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
    return [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]


def list_user_issues_page(
    user_id: str,
    *,
    status: str = "",
    severity: str = "",
    scan_id: str = "",
    query: str = "",
    sort: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    safe_limit = max(1, min(100, int(limit or 50)))
    safe_offset = max(0, int(offset or 0))
    clauses = ["user_id = ?"]
    params: list[Any] = [target_user_id]
    if status:
        clauses.append("status = ?")
        params.append(str(status).strip().lower())
    if severity:
        clauses.append("severity = ?")
        params.append(str(severity).strip().lower())
    if scan_id:
        clauses.append("scan_id = ?")
        params.append(str(scan_id).strip())
    if query:
        like = f"%{str(query).strip().lower()}%"
        clauses.append(
            "(lower(issue_id) LIKE ? OR lower(title) LIKE ? OR lower(file_path) LIKE ? OR lower(repo) LIKE ? OR lower(category) LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    where = " AND ".join(clauses)
    sort_key = str(sort or "").strip().lower()
    if sort_key == "severity":
        order_by = """
            CASE severity
                WHEN 'critical' THEN 5
                WHEN 'high' THEN 4
                WHEN 'medium' THEN 3
                WHEN 'low' THEN 2
                WHEN 'info' THEN 1
                ELSE 0
            END DESC,
            created_at DESC,
            issue_id ASC
        """
    elif sort_key == "file":
        order_by = "lower(file_path) ASC, created_at DESC, issue_id ASC"
    else:
        order_by = "created_at DESC, issue_id ASC"
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        total = int(connection.execute(f"SELECT COUNT(*) FROM issues WHERE {where}", tuple(params)).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT *
            FROM issues
            WHERE {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*params, safe_limit, safe_offset),
        ).fetchall()
    return {
        "items": [issue for row in rows if (issue := issue_from_row(row)) is not None],
        "total": max(0, total),
        "limit": safe_limit,
        "offset": safe_offset,
    }


def get_user_issue(user_id: str, issue_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_issue_id = str(issue_id or "").strip()
    if not target_user_id or not target_issue_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM issues WHERE user_id = ? AND issue_id = ?",
            (target_user_id, target_issue_id),
        ).fetchone()
    return issue_from_row(row)


def update_user_issue(user_id: str, issue_id: str, payload: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any] | None:
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    target_issue_id = str(issue_id or "").strip()
    if not target_user_id or not target_issue_id or not isinstance(payload, dict):
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    record = dict(payload)
    record["id"] = target_issue_id
    record["userId"] = target_user_id
    record["updatedAt"] = current_time
    fields = issue_storage_fields(record, timestamp=current_time)
    if not fields:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            updated = connection.execute(
                """
                UPDATE issues
                SET scan_id = ?,
                    job_id = ?,
                    repo = ?,
                    status = ?,
                    severity = ?,
                    category = ?,
                    title = ?,
                    file_path = ?,
                    created_at = ?,
                    updated_at = ?,
                    payload = ?
                WHERE user_id = ? AND issue_id = ?
                """,
                (
                    fields["scan_id"],
                    fields["job_id"],
                    fields["repo"],
                    fields["status"],
                    fields["severity"],
                    fields["category"],
                    fields["title"],
                    fields["file_path"],
                    fields["created_at"],
                    fields["updated_at"],
                    json.dumps(fields["payload"], ensure_ascii=False, allow_nan=False, sort_keys=True),
                    target_user_id,
                    target_issue_id,
                ),
            ).rowcount
            if updated <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM issues WHERE user_id = ? AND issue_id = ?",
                (target_user_id, target_issue_id),
            ).fetchone()
    return issue_from_row(row)


def claim_next_scan_job(
    worker_id: str,
    *,
    lease_seconds: int = 3600,
    worker_heartbeat_timeout_seconds: int = 120,
    ready_providers: list[str] | None = None,
    timestamp: int | None = None,
    recover_before_claim: bool = True,
    create_review_run: bool = False,
    protocol_version: str = "review-worker-protocol/v1",
) -> dict[str, Any] | None:
    ensure_initialized()
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    current_time = int(timestamp if timestamp is not None else time.time())
    timeout_at = current_time + max(60, int(lease_seconds))
    ready_provider_set = set(normalize_provider_list(ready_providers)) if ready_providers is not None else None
    if ready_provider_set is not None and "codex" not in ready_provider_set:
        return None
    with closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            worker = connection.execute(
                "SELECT enabled, deleted_at FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker and (int(worker["enabled"] or 0) == 0 or worker["deleted_at"] is not None):
                connection.commit()
                return None
            offline_after = max(60, int(worker_heartbeat_timeout_seconds or 120))
            if recover_before_claim:
                _fail_expired_jobs_locked(connection, current_time)
                _fail_stale_worker_jobs_locked(connection, current_time, offline_after)
                _fail_exhausted_queued_jobs_locked(connection, current_time)
            row = connection.execute(
                """
                SELECT queued.job_id
                FROM scan_jobs queued
                WHERE queued.status = 'queued'
                  AND queued.attempt = 0

                  AND NOT EXISTS (
                      SELECT 1
                      FROM scan_jobs worker_active
                      WHERE worker_active.claimed_by_worker_id = ?
                        AND worker_active.status IN (
                            'claimed',
                            'running',
                            'uploading_result',
                            'cancel_requested',
                            'cancelling'
                        )
                      LIMIT 1
                  )
                  AND COALESCE(NULLIF(queued.worker_scope, ''), 'shared') = 'shared'
                ORDER BY queued.created_at ASC, queued.job_id ASC
                LIMIT 1
                """
                ,
                (worker_id,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
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
                  AND attempt = 0
                """,
                (worker_id, current_time, timeout_at, current_time, job_id),
            ).rowcount
            if updated != 1:
                connection.commit()
                return None
            claimed_job = row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())
            if claimed_job:
                _record_scan_job_attempt_locked(
                    connection,
                    job_id=job_id,
                    attempt=max(0, int(claimed_job.get("attempt") or 0)),
                    worker_id=worker_id,
                    claimed_at=current_time,
                )
                if create_review_run:
                    _upsert_review_run_claimed_locked(
                        connection,
                        claimed_job,
                        protocol_version=protocol_version,
                        timestamp=current_time,
                    )
            connection.commit()
            return claimed_job
        except Exception:
            connection.rollback()
            raise


def recover_expired_scan_jobs(
    timestamp: int | None = None,
    *,
    worker_heartbeat_timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    ensure_initialized()
    current_time = int(timestamp if timestamp is not None else time.time())
    offline_after = max(60, int(worker_heartbeat_timeout_seconds or 120))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE workers
                SET status = 'offline'
                WHERE status = 'online' AND last_heartbeat_at < ?
                """,
                (current_time - offline_after,),
            )
            recovered = _fail_exhausted_queued_jobs_locked(connection, current_time)
            recovered.extend(_fail_expired_jobs_locked(connection, current_time))
            recovered.extend(_fail_stale_worker_jobs_locked(connection, current_time, offline_after))
            recovered_job_ids = {str(item.get("job_id") or "") for item in recovered}
            for pending in _pending_recovered_scan_jobs_locked(connection):
                pending_job_id = str(pending.get("job_id") or "")
                if pending_job_id and pending_job_id not in recovered_job_ids:
                    recovered.append(pending)
                    recovered_job_ids.add(pending_job_id)
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise


def fail_interrupted_scan_job(scan_id: str, *, reason: str = "server_restart", timestamp: int | None = None) -> dict[str, Any] | None:
    ensure_initialized()
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM scan_jobs WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            prior_status = str(row["status"] or "").strip().lower()
            if prior_status not in {"claimed", "running", "uploading_result"}:
                connection.commit()
                return row_to_dict(row)
            updated = connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'failed',
                    completed_at = ?,
                    timeout_at = NULL,
                    error = ?,
                    projection_pending = 1,
                    updated_at = ?
                WHERE scan_id = ?
                  AND status = ?
                """,
                (current_time, reason, current_time, scan_id, prior_status),
            ).rowcount
            if updated > 0:
                _complete_scan_job_attempt_locked(
                    connection,
                    job_id=str(row["job_id"]),
                    attempt=int(row["attempt"] or 0),
                    worker_id=str(row["claimed_by_worker_id"] or ""),
                    status="failed",
                    completed_at=current_time,
                    error=reason,
                )
                _finalize_recovered_review_run_locked(
                    connection,
                    job_id=str(row["job_id"]),
                    status="failed",
                    reason=reason,
                    completed_at=current_time,
                )
            stored = connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (scan_id,)).fetchone()
            connection.commit()
            return row_to_dict(stored)
        except Exception:
            connection.rollback()
            raise

def _finalize_recovered_review_run_locked(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    status: str,
    reason: str,
    completed_at: int,
) -> None:
    terminal_status = str(status or "").strip().lower()
    if terminal_status not in {"failed", "cancelled"}:
        raise ValueError("recovered review run status must be failed or cancelled")
    rows = connection.execute(
        """
        SELECT run_id, progress_json
        FROM review_runs
        WHERE job_id = ?
          AND status NOT IN ('completed', 'failed', 'cancelled', 'partial_completed')
        """,
        (job_id,),
    ).fetchall()
    error_text = run_json_text(
        {
            "code": str(reason or "").strip() or "timed_out",
            "source": "server_lease_reaper",
        }
    )
    for row in rows:
        try:
            progress = json.loads(str(row["progress_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            progress = {}
        if not isinstance(progress, dict):
            progress = {}
        progress["status"] = terminal_status
        progress.pop("estimate", None)
        connection.execute(
            """
            UPDATE review_runs
            SET status = ?,
                result_status = ?,
                completed_at = ?,
                duration_ms = COALESCE(
                    duration_ms,
                    CASE
                        WHEN started_at IS NOT NULL
                        THEN MAX(0, (? - started_at) * 1000)
                        ELSE NULL
                    END
                ),
                progress_json = ?,
                error_json = ?,
                updated_at = ?
            WHERE run_id = ?
              AND status NOT IN ('completed', 'failed', 'cancelled', 'partial_completed')
            """,
            (
                terminal_status,
                terminal_status,
                completed_at,
                completed_at,
                run_json_text(progress),
                error_text,
                completed_at,
                row["run_id"],
            ),
        )


def _fail_exhausted_queued_jobs_locked(connection: sqlite3.Connection, current_time: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT job_id, scan_id, attempt, claimed_by_worker_id
        FROM scan_jobs
        WHERE status = 'queued'
          AND attempt > 0
        """
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        updated = connection.execute(
            """
            UPDATE scan_jobs
            SET status = 'failed',
                claimed_by_worker_id = NULL,
                claimed_at = NULL,
                started_at = NULL,
                completed_at = ?,
                timeout_at = NULL,
                error = 'scan_attempts_exhausted',
                projection_pending = 1,
                updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (current_time, current_time, row["job_id"]),
        ).rowcount
        if updated <= 0:
            continue
        _complete_scan_job_attempt_locked(
            connection,
            job_id=str(row["job_id"]),
            attempt=int(row["attempt"]),
            worker_id=str(row["claimed_by_worker_id"] or ""),
            status="failed",
            completed_at=current_time,
            error="scan_attempts_exhausted",
        )
        _finalize_recovered_review_run_locked(
            connection,
            job_id=str(row["job_id"]),
            status="failed",
            reason="scan_attempts_exhausted",
            completed_at=current_time,
        )
        recovered.append(
            {
                "job_id": row["job_id"],
                "scan_id": row["scan_id"],
                "status": "failed",
                "reason": "scan_attempts_exhausted",
                "attempt": int(row["attempt"]),
            }
        )
    return recovered

def _fail_expired_jobs_locked(connection: sqlite3.Connection, current_time: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT job_id, scan_id, attempt, claimed_by_worker_id, status
        FROM scan_jobs
        WHERE status IN ('claimed', 'running', 'uploading_result', 'cancel_requested', 'cancelling')
          AND timeout_at IS NOT NULL
          AND timeout_at <= ?
        """,
        (current_time,),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        prior_status = str(row["status"] or "").strip()
        cancel_recovery = prior_status in {"cancel_requested", "cancelling"}
        terminal_status = "cancelled" if cancel_recovery else "failed"
        reason = "cancel_timed_out" if cancel_recovery else "timed_out"
        updated = connection.execute(
            """
            UPDATE scan_jobs
            SET status = ?,
                completed_at = ?,
                timeout_at = NULL,
                error = ?,
                projection_pending = 1,
                updated_at = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                terminal_status,
                current_time,
                reason,
                current_time,
                row["job_id"],
                prior_status,
            ),
        ).rowcount
        if updated <= 0:
            continue
        _complete_scan_job_attempt_locked(
            connection,
            job_id=row["job_id"],
            attempt=int(row["attempt"]),
            worker_id=str(row["claimed_by_worker_id"] or ""),
            status=terminal_status,
            completed_at=current_time,
            error=reason,
        )
        _finalize_recovered_review_run_locked(
            connection,
            job_id=str(row["job_id"]),
            status=terminal_status,
            reason=reason,
            completed_at=current_time,
        )
        recovered.append(
            {
                "job_id": row["job_id"],
                "scan_id": row["scan_id"],
                "status": terminal_status,
                "reason": reason,
                "attempt": int(row["attempt"]),
            }
        )
    return recovered

def _fail_stale_worker_jobs_locked(
    connection: sqlite3.Connection,
    current_time: int,
    offline_after: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT sj.job_id, sj.scan_id, sj.attempt, sj.claimed_by_worker_id, sj.status
        FROM scan_jobs sj
        JOIN workers w ON w.worker_id = sj.claimed_by_worker_id
        WHERE sj.status IN ('claimed', 'running', 'uploading_result', 'cancel_requested', 'cancelling')
          AND w.last_heartbeat_at IS NOT NULL
          AND w.last_heartbeat_at < ?
        """,
        (current_time - max(60, int(offline_after)),),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        prior_status = str(row["status"] or "").strip()
        cancel_recovery = prior_status in {"cancel_requested", "cancelling"}
        terminal_status = "cancelled" if cancel_recovery else "failed"
        reason = "cancel_timed_out" if cancel_recovery else "worker_heartbeat_timed_out"
        updated = connection.execute(
            """
            UPDATE scan_jobs
            SET status = ?,
                completed_at = ?,
                timeout_at = NULL,
                error = ?,
                projection_pending = 1,
                updated_at = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                terminal_status,
                current_time,
                reason,
                current_time,
                row["job_id"],
                prior_status,
            ),
        ).rowcount
        if updated <= 0:
            continue
        _complete_scan_job_attempt_locked(
            connection,
            job_id=row["job_id"],
            attempt=int(row["attempt"]),
            worker_id=str(row["claimed_by_worker_id"] or ""),
            status=terminal_status,
            completed_at=current_time,
            error=reason,
        )
        _finalize_recovered_review_run_locked(
            connection,
            job_id=str(row["job_id"]),
            status=terminal_status,
            reason=reason,
            completed_at=current_time,
        )
        recovered.append(
            {
                "job_id": row["job_id"],
                "scan_id": row["scan_id"],
                "status": terminal_status,
                "reason": reason,
                "attempt": int(row["attempt"]),
            }
        )
    return recovered


def _pending_recovered_scan_jobs_locked(
    connection: sqlite3.Connection,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT sj.job_id,
               sj.scan_id,
               sj.status,
               sj.error AS reason,
               sj.attempt
        FROM scan_jobs sj
        JOIN scans s ON s.scan_id = sj.scan_id
        WHERE sj.projection_pending = 1
        ORDER BY sj.updated_at ASC, sj.job_id ASC
        LIMIT ?
        """,
        (max(1, min(5000, int(limit or 500))),),
    ).fetchall()
    return [
        {
            "job_id": row["job_id"],
            "scan_id": row["scan_id"],
            "status": row["status"],
            "reason": row["reason"],
            "attempt": int(row["attempt"] or 0),
        }
        for row in rows
    ]


def mark_scan_job_projection_applied(
    job_id: str,
    *,
    status: str,
    reason: str,
) -> bool:
    ensure_initialized()
    normalized_job_id = str(job_id or "").strip()
    normalized_status = str(status or "").strip().lower()
    normalized_reason = str(reason or "").strip()
    if not normalized_job_id or normalized_status not in {"failed", "cancelled"}:
        return False
    with _LOCK, closing(connect()) as connection:
        with connection:
            updated = connection.execute(
                """
                UPDATE scan_jobs
                SET projection_pending = 0
                WHERE job_id = ?
                  AND status = ?
                  AND error = ?
                  AND projection_pending = 1
                """,
                (normalized_job_id, normalized_status, normalized_reason),
            ).rowcount
    return updated > 0

def update_scan_job_progress(job_id: str, progress: dict[str, Any]) -> dict[str, Any] | None:
    ensure_initialized()
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


def request_scan_job_cancellation(
    scan_id: str,
    *,
    reason: str = "user_cancelled",
    timeout_seconds: int = 3600,
    timestamp: int | None = None,
) -> dict[str, Any] | None:
    ensure_initialized()
    target_scan_id = str(scan_id or "").strip()
    if not target_scan_id:
        return None
    current_time = int(timestamp if timestamp is not None else time.time())
    cancel_reason = str(reason or "").strip() or "user_cancelled"
    cancellation_timeout_at = current_time + max(60, int(timeout_seconds or 3600))
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM scan_jobs WHERE scan_id = ?",
                (target_scan_id,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            status = str(row["status"] or "").strip().lower()
            if status == "queued":
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'cancelled',
                        completed_at = COALESCE(completed_at, ?),
                        timeout_at = NULL,
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_reason = ?,
                        updated_at = ?
                    WHERE scan_id = ? AND status = 'queued'
                    """,
                    (current_time, current_time, cancel_reason, current_time, target_scan_id),
                )
            elif status in {"claimed", "running", "uploading_result"}:
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'cancel_requested',
                        completed_at = NULL,
                        timeout_at = ?,
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_reason = ?,
                        updated_at = ?
                    WHERE scan_id = ?
                      AND status IN ('claimed', 'running', 'uploading_result')
                    """,
                    (cancellation_timeout_at, current_time, cancel_reason, current_time, target_scan_id),
                )
            connection.commit()
            return row_to_dict(
                connection.execute("SELECT * FROM scan_jobs WHERE scan_id = ?", (target_scan_id,)).fetchone()
            )
        except Exception:
            connection.rollback()
            raise


def cancel_scan_job_for_scan(scan_id: str) -> None:
    ensure_initialized()
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


def _store_scan_job_result_artifact(
    *,
    artifact_id: str,
    job_id: str,
    attempt_id: str,
    payload_text: str,
    timestamp: int,
) -> None:
    with closing(connect()) as connection:
        with connection:
            _store_scan_job_result_artifact_locked(
                connection,
                artifact_id=artifact_id,
                job_id=job_id,
                attempt_id=attempt_id,
                payload_text=payload_text,
                timestamp=timestamp,
            )


def _store_scan_job_result_artifact_locked(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    job_id: str,
    attempt_id: str,
    payload_text: str,
    timestamp: int,
) -> None:
    connection.execute(
        """
        INSERT INTO job_result_artifacts (id, job_id, attempt_id, kind, payload, created_at)
        VALUES (?, ?, ?, 'worker_result_payload', ?, ?)
        ON CONFLICT(job_id, attempt_id, kind) DO UPDATE SET
            id = excluded.id,
            payload = excluded.payload,
            created_at = excluded.created_at
        """,
        (artifact_id, job_id, attempt_id, payload_text, timestamp),
    )


def review_artifact_storage_root() -> str:
    configured = os.environ.get(REVIEW_ARTIFACT_STORAGE_DIR_ENV) or os.environ.get(LEGACY_ARTIFACT_STORAGE_DIR_ENV)
    if configured:
        return os.path.abspath(configured)
    database_parent = os.path.dirname(os.path.abspath(database_path()))
    return os.path.join(database_parent, "review-artifacts")


def _review_artifact_storage_segment(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value or ""))
    cleaned = cleaned.strip("._ ")[:128]
    return cleaned or stable_id("artifact_path", fallback)


def review_artifact_content_from_payload(payload: dict[str, Any]) -> bytes | None:
    content_bytes = payload.get("_content_bytes")
    if isinstance(content_bytes, bytes):
        return content_bytes
    content_b64_value = payload.get("content_base64") if payload.get("content_base64") is not None else payload.get("contentBase64")
    if content_b64_value is None:
        return None
    try:
        return base64.b64decode(str(content_b64_value).encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        return None


def review_artifact_payload_without_content(payload: dict[str, Any]) -> dict[str, Any]:
    raw = dict(payload)
    raw.pop("content_base64", None)
    raw.pop("contentBase64", None)
    raw.pop("_content_bytes", None)
    sanitized = to_jsonable(raw)
    return sanitized if isinstance(sanitized, dict) else {}


def review_artifact_inline_json(content: bytes | None, media_type: str, size_bytes: int) -> str | None:
    if content is None or size_bytes > 256 * 1024 or "json" not in str(media_type or "").lower():
        return None
    try:
        decoded = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return json.dumps(to_jsonable(decoded), ensure_ascii=False, sort_keys=True)


def stage_review_artifact_content(run_id: str, artifact_id: str, content: bytes, sha256: str) -> tuple[str, str, str]:
    run_segment = _review_artifact_storage_segment(run_id, f"{run_id}:run")
    artifact_segment = _review_artifact_storage_segment(artifact_id, f"{run_id}:{artifact_id}")
    sha_segment = _review_artifact_storage_segment(sha256, f"{run_id}:{artifact_id}:sha")[:64]
    relative_path = os.path.join(run_segment, f"{artifact_segment}-{sha_segment}.bin")
    root = review_artifact_storage_root()
    final_path = os.path.abspath(os.path.join(root, relative_path))
    root_path = os.path.abspath(root)
    if os.path.commonpath([root_path, final_path]) != root_path:
        raise ValueError("artifact content path escapes storage root")
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    temp_path = f"{final_path}.{threading.get_ident()}.{time.time_ns()}.tmp"
    with open(temp_path, "wb") as handle:
        handle.write(content)
    return relative_path.replace(os.sep, "/"), temp_path, final_path


def discard_staged_review_artifact_content(staged: tuple[str, str, str] | None) -> None:
    if not staged:
        return
    temp_path = staged[1]
    try:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    except OSError:
        pass


def commit_staged_review_artifact_content(staged: tuple[str, str, str] | None) -> str:
    if not staged:
        return ""
    relative_path, temp_path, final_path = staged
    os.replace(temp_path, final_path)
    return relative_path


def review_artifact_content_file_path(row: dict[str, Any]) -> str | None:
    content_path = str(row.get("content_path") or "").strip()
    if not content_path:
        return None
    normalized = os.path.normpath(content_path.replace("\\", os.sep).replace("/", os.sep))
    if os.path.isabs(normalized) or normalized == ".." or normalized.startswith(f"..{os.sep}"):
        return None
    root = os.path.abspath(review_artifact_storage_root())
    full_path = os.path.abspath(os.path.join(root, normalized))
    if os.path.commonpath([root, full_path]) != root:
        return None
    return full_path


def review_artifact_content_bytes(row: dict[str, Any]) -> bytes | None:
    full_path = review_artifact_content_file_path(row)
    if not full_path:
        return None
    try:
        with open(full_path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def delete_review_artifact_content_file(content_path: object) -> bool:
    full_path = review_artifact_content_file_path({"content_path": content_path})
    if not full_path:
        return False
    try:
        os.unlink(full_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        os.rmdir(os.path.dirname(full_path))
    except OSError:
        pass
    return True


REPLACEABLE_REVIEW_LOG_ARTIFACT_KINDS = {"codex_event_log", "worker_log", "progress_log", "debug_bundle"}


def store_review_run_artifact(
    *,
    job_id: str,
    attempt_id: str,
    artifact_id: str,
    payload: dict[str, Any],
    timestamp: int | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    ensure_initialized()
    job_id = str(job_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    artifact_id = str(artifact_id or "").strip()
    if not job_id or not attempt_id or not artifact_id:
        raise ValueError("job_id, attempt_id, and artifact_id are required")
    content = review_artifact_content_from_payload(payload)
    payload_text = json.dumps(review_artifact_payload_without_content(payload), ensure_ascii=False, sort_keys=True)
    current_time = int(timestamp if timestamp is not None else time.time())
    run_id = str(payload.get("run_id") or f"run_{job_id}").strip()
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    kind = str(artifact.get("kind") or payload.get("kind") or artifact_id).strip()
    if not kind:
        raise ValueError("artifact.kind is required")
    name = str(artifact.get("name") or payload.get("name") or "").strip()
    media_type = str(artifact.get("media_type") or artifact.get("mediaType") or payload.get("media_type") or payload.get("mediaType") or "").strip()
    schema_id = str(artifact.get("schema_id") or artifact.get("schemaId") or payload.get("schema_id") or payload.get("schemaId") or "").strip()
    schema_version = str(artifact.get("schema_version") or artifact.get("schemaVersion") or payload.get("schema_version") or payload.get("schemaVersion") or "").strip()
    sha256 = str(payload.get("sha256") or artifact.get("sha256") or "").strip().lower()
    if content is not None and not sha256:
        sha256 = hashlib.sha256(content).hexdigest()
    try:
        size_bytes = int(
            payload.get("size_bytes")
            if payload.get("size_bytes") is not None
            else artifact.get("size_bytes")
            if artifact.get("size_bytes") is not None
            else payload.get("sizeBytes")
            if payload.get("sizeBytes") is not None
            else artifact.get("sizeBytes")
        )
    except (TypeError, ValueError):
        size_bytes = len(content) if content is not None else 0
    required = 1 if artifact.get("required") is True or payload.get("required") is True else 0
    replaceable_log_artifact = bool(replace_existing and kind in REPLACEABLE_REVIEW_LOG_ARTIFACT_KINDS)
    storage_url = f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"
    storage_json = json.dumps(
        {
            "type": "server_artifact",
            "url": storage_url,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    inline_json = review_artifact_inline_json(content, media_type, size_bytes)
    staged_content = stage_review_artifact_content(run_id, artifact_id, content, sha256) if content is not None else None
    staged_content_committed = False
    try:
        with closing(connect()) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                job = connection.execute(
                    "SELECT status FROM scan_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if not job:
                    return {"accepted": False, "conflict": True, "reason": "job_not_found"}
                if (
                    str(job["status"] or "")
                    not in {"claimed", "running", "uploading_result", "cancel_requested", "cancelling"}
                    and not replaceable_log_artifact
                ):
                    return {"accepted": False, "conflict": True, "reason": "job_not_accepting_artifacts"}
                if not replaceable_log_artifact:
                    content_path = staged_content[0] if staged_content is not None else ""
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO review_artifacts (
                            id, artifact_id, run_id, job_id, attempt_id, kind, name,
                            media_type, schema_id, schema_version, required, sha256,
                            size_bytes, storage_url, storage_json, inline_json,
                            content_path, payload_json, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stable_id("art", f"{run_id}:{artifact_id}"),
                            artifact_id,
                            run_id,
                            job_id,
                            attempt_id,
                            kind,
                            name,
                            media_type,
                            schema_id,
                            schema_version,
                            required,
                            sha256,
                            size_bytes,
                            storage_url,
                            storage_json,
                            inline_json,
                            content_path,
                            payload_text,
                            current_time,
                            current_time,
                        ),
                    )
                    if cursor.rowcount:
                        commit_staged_review_artifact_content(staged_content)
                        staged_content_committed = True
                        return {
                            "accepted": True,
                            "duplicate": False,
                            "artifactId": artifact_id,
                            "runId": run_id,
                            "storage": json.loads(storage_json),
                        }
                    existing = connection.execute(
                        "SELECT payload_json FROM review_artifacts WHERE run_id = ? AND artifact_id = ?",
                        (run_id, artifact_id),
                    ).fetchone()
                    if existing and str(existing["payload_json"] or "") == payload_text:
                        return {
                            "accepted": True,
                            "duplicate": True,
                            "artifactId": artifact_id,
                            "runId": run_id,
                            "storage": json.loads(storage_json),
                        }
                    return {"accepted": False, "conflict": True, "reason": "artifact_payload_conflict"}
                existing = connection.execute(
                    "SELECT payload_json, job_id, attempt_id, kind, content_path FROM review_artifacts WHERE run_id = ? AND artifact_id = ?",
                    (run_id, artifact_id),
                ).fetchone()
                if existing:
                    if str(existing["payload_json"] or "") == payload_text:
                        return {
                            "accepted": True,
                            "duplicate": True,
                            "artifactId": artifact_id,
                            "runId": run_id,
                            "storage": json.loads(storage_json),
                        }
                    if (
                        replaceable_log_artifact
                        and str(existing["kind"] or "") in REPLACEABLE_REVIEW_LOG_ARTIFACT_KINDS
                        and str(existing["job_id"] or "") == job_id
                        and str(existing["attempt_id"] or "") == attempt_id
                    ):
                        content_path = commit_staged_review_artifact_content(staged_content)
                        staged_content_committed = True
                        connection.execute(
                            """
                            UPDATE review_artifacts
                            SET kind = ?, name = ?, media_type = ?, schema_id = ?, schema_version = ?,
                                required = ?, sha256 = ?, size_bytes = ?, storage_url = ?, storage_json = ?,
                                inline_json = ?, content_path = ?, payload_json = ?, updated_at = ?
                            WHERE run_id = ? AND artifact_id = ?
                            """,
                            (
                                kind,
                                name,
                                media_type,
                                schema_id,
                                schema_version,
                                required,
                                sha256,
                                size_bytes,
                                storage_url,
                                storage_json,
                                inline_json,
                                content_path,
                                payload_text,
                                current_time,
                                run_id,
                                artifact_id,
                            ),
                        )
                        connection.commit()
                        previous_content_path = str(existing["content_path"] or "")
                        if previous_content_path and previous_content_path != content_path:
                            delete_review_artifact_content_file(previous_content_path)
                        return {
                            "accepted": True,
                            "duplicate": False,
                            "replaced": True,
                            "artifactId": artifact_id,
                            "runId": run_id,
                            "storage": json.loads(storage_json),
                        }
                    return {"accepted": False, "conflict": True, "reason": "artifact_payload_conflict"}
                content_path = commit_staged_review_artifact_content(staged_content)
                staged_content_committed = True
                connection.execute(
                    """
                    INSERT INTO review_artifacts (
                        id, artifact_id, run_id, job_id, attempt_id, kind, name,
                        media_type, schema_id, schema_version, required, sha256,
                        size_bytes, storage_url, storage_json, inline_json,
                        content_path, payload_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id("art", f"{run_id}:{artifact_id}"),
                        artifact_id,
                        run_id,
                        job_id,
                        attempt_id,
                        kind,
                        name,
                        media_type,
                        schema_id,
                        schema_version,
                        required,
                        sha256,
                        size_bytes,
                        storage_url,
                        storage_json,
                        inline_json,
                        content_path,
                        payload_text,
                        current_time,
                        current_time,
                    ),
                )
        return {
            "accepted": True,
            "duplicate": False,
            "artifactId": artifact_id,
            "runId": run_id,
            "storage": json.loads(storage_json),
        }
    finally:
        if not staged_content_committed:
            discard_staged_review_artifact_content(staged_content)

def list_scan_job_attempts(job_id: str) -> list[dict[str, Any]]:
    ensure_initialized()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM scan_job_attempts
            WHERE job_id = ?
            ORDER BY attempt ASC
            """,
            (normalized_job_id,),
        ).fetchall()
    return [dict(row) for row in rows]

def list_review_run_artifacts(job_id: str, attempt_id: str) -> list[dict[str, Any]]:
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM review_artifacts
            WHERE job_id = ? AND attempt_id = ?
            ORDER BY artifact_id
            """,
            (str(job_id or "").strip(), str(attempt_id or "").strip()),
        ).fetchall()
    artifacts = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            artifacts.append(payload)
    return artifacts


def list_review_run_artifact_records(run_id: str) -> list[dict[str, Any]]:
    ensure_initialized()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                artifact_id, run_id, job_id, attempt_id, kind, name, media_type,
                schema_id, schema_version, required, sha256, size_bytes,
                storage_url, storage_json, inline_json, created_at, updated_at
            FROM review_artifacts
            WHERE run_id = ?
            ORDER BY required DESC, kind, artifact_id
            """,
            (normalized_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_review_run_artifact(run_id: str, artifact_id: str) -> dict[str, Any] | None:
    ensure_initialized()
    normalized_run_id = str(run_id or "").strip()
    normalized_artifact_id = str(artifact_id or "").strip()
    if not normalized_run_id or not normalized_artifact_id:
        return None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM review_artifacts
            WHERE run_id = ? AND artifact_id = ?
            """,
            (normalized_run_id, normalized_artifact_id),
        ).fetchone()
        return row_to_dict(row)

def record_scan_job_result(
    job_id: str,
    *,
    attempt_id: str,
    status: str,
    result_checksum: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ensure_initialized()
    job_id = str(job_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    status = str(status or "").strip().lower()
    result_checksum = str(result_checksum or "").strip()
    if not job_id or not attempt_id or not result_checksum:
        raise ValueError("job_id, attempt_id, and result_checksum are required")
    if status not in {"done", "failed", "cancelled", "partial_completed"}:
        raise ValueError("status must be done, failed, cancelled, or partial_completed")
    current_time = int(time.time())
    artifact_id = scan_job_result_artifact_id(job_id, attempt_id)
    artifact_payload_text = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    summary_payload = scan_job_result_summary_payload(payload, artifact_id=artifact_id)
    summary_payload_text = json.dumps(to_jsonable(summary_payload), ensure_ascii=False, sort_keys=True)
    result: dict[str, Any] | None = None
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            job = connection.execute(
                """
                SELECT status, last_attempt_id, claimed_by_worker_id, attempt,
                       completed_at, error, cancel_reason
                FROM scan_jobs
                WHERE job_id = ?
                """,
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
            job_status = str(job["status"] or "").strip().lower()
            accepts_result = job_status in {"claimed", "running", "uploading_result"} or (
                status == "cancelled"
                and job_status in {"cancel_requested", "cancelling", "cancelled"}
            )
            if not accepts_result:
                connection.commit()
                return {"accepted": False, "duplicate": False, "conflict": True}
            late_cancelled_receipt = (
                job_status == "cancelled" and status == "cancelled"
            )
            claimed_worker_id = str(job["claimed_by_worker_id"] or "")
            try:
                attempt = int(job["attempt"] or 0)
            except (TypeError, ValueError):
                attempt = 0
            expected_attempt_id = f"{claimed_worker_id}-{attempt}" if claimed_worker_id and attempt else ""
            if not expected_attempt_id or attempt_id != expected_attempt_id:
                connection.commit()
                return {"accepted": False, "duplicate": False, "conflict": True}
            if not late_cancelled_receipt:
                _complete_scan_job_attempt_locked(
                    connection,
                    job_id=job_id,
                    attempt=attempt,
                    worker_id=claimed_worker_id,
                    status=status,
                    completed_at=current_time,
                    error=payload.get("error"),
                )
            connection.execute(
                """
                INSERT INTO job_results (id, job_id, attempt_id, result_checksum, status, payload, payload_artifact_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("jr", f"{job_id}:{attempt_id}"),
                    job_id,
                    attempt_id,
                    result_checksum,
                    status,
                    summary_payload_text,
                    artifact_id,
                ),
            )
            _store_scan_job_result_artifact_locked(
                connection,
                artifact_id=artifact_id,
                job_id=job_id,
                attempt_id=attempt_id,
                payload_text=artifact_payload_text,
                timestamp=current_time,
            )
            if late_cancelled_receipt:
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET result_checksum = ?,
                        last_attempt_id = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        result_checksum,
                        attempt_id,
                        current_time,
                        job_id,
                    ),
                )
            else:
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
            next_job = row_to_dict(connection.execute("SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)).fetchone())
            result = {
                "accepted": True,
                "duplicate": False,
                "conflict": False,
                "job_status": status,
                "attempt": attempt,
                "job": next_job or {},
            }
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return result or {"accepted": False, "duplicate": False, "conflict": True}


def inspect_scan_job_result_submission(
    job_id: str,
    *,
    attempt_id: str,
    result_checksum: str,
    status: str = "",
) -> dict[str, bool]:
    ensure_initialized()
    normalized_job_id = str(job_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    normalized_checksum = str(result_checksum or "").strip()
    normalized_status = str(status or "").strip().lower()
    if not normalized_job_id or not normalized_attempt_id or not normalized_checksum:
        raise ValueError("job_id, attempt_id, and result_checksum are required")
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        job = connection.execute(
            "SELECT status, claimed_by_worker_id, attempt FROM scan_jobs WHERE job_id = ?",
            (normalized_job_id,),
        ).fetchone()
        if not job:
            return {"accepted": False, "duplicate": False, "conflict": True}
        existing = connection.execute(
            "SELECT result_checksum FROM job_results WHERE job_id = ? AND attempt_id = ?",
            (normalized_job_id, normalized_attempt_id),
        ).fetchone()
        if existing:
            duplicate = str(existing["result_checksum"] or "") == normalized_checksum
            return {"accepted": duplicate, "duplicate": True, "conflict": not duplicate}
        job_status = str(job["status"] or "").strip().lower()
        accepts_result = job_status in {"claimed", "running", "uploading_result"} or (
            normalized_status == "cancelled"
            and job_status in {"cancel_requested", "cancelling", "cancelled"}
        )
        if not accepts_result:
            return {"accepted": False, "duplicate": False, "conflict": True}
        claimed_worker_id = str(job["claimed_by_worker_id"] or "")
        try:
            attempt = int(job["attempt"] or 0)
        except (TypeError, ValueError):
            attempt = 0
        expected_attempt_id = f"{claimed_worker_id}-{attempt}" if claimed_worker_id and attempt else ""
        if not expected_attempt_id or normalized_attempt_id != expected_attempt_id:
            return {"accepted": False, "duplicate": False, "conflict": True}
    return {"accepted": False, "duplicate": False, "conflict": False}


def count_scan_job_results(job_id: str) -> int:
    ensure_initialized()
    target_job_id = str(job_id or "").strip()
    if not target_job_id:
        return 0
    with _LOCK, closing(connect()) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM job_results WHERE job_id = ?",
            (target_job_id,),
        ).fetchone()
    return max(0, int(row[0] if row else 0))


def record_review_decision_events(events: list[dict[str, Any]]) -> dict[str, int]:
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
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
                    label_source, outcome_weight, label_reason, created_at, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    candidate_observation_key = excluded.candidate_observation_key,
                    outcome_label = excluded.outcome_label,
                    label_source = excluded.label_source,
                    outcome_weight = excluded.outcome_weight,
                    label_reason = excluded.label_reason,
                    created_at = excluded.created_at,
                    created_by = excluded.created_by
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
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_outcome_labels WHERE label_id = ?",
                (label_id,),
            ).fetchone()
    return row_to_dict(row) or {}


def list_review_outcome_labels(candidate_observation_key: str) -> list[dict[str, Any]]:
    ensure_initialized()
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


def repository_id_for_github_repo(github_repo_id: object) -> str:
    return stable_id("repo", github_repo_id)


def upsert_repository(repository: dict[str, Any]) -> dict[str, Any]:
    ensure_initialized()
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
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(connection.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,)).fetchone())


def get_repository_by_github_repo_id(github_repo_id: object) -> dict[str, Any] | None:
    github_repo_id_text = str(github_repo_id or "").strip()
    if not github_repo_id_text:
        return None
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        return row_to_dict(
            connection.execute("SELECT * FROM repositories WHERE github_repo_id = ?", (github_repo_id_text,)).fetchone()
        )


def upsert_repo_fingerprint(repository_id: str, fingerprint: dict[str, Any]) -> dict[str, Any] | None:
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
    api_key_id = str(record.get("id") or "").strip()
    user_id = str(record.get("user_id") or "").strip()
    name = str(record.get("name") or "API key").strip() or "API key"
    key_prefix = str(record.get("key_prefix") or "").strip()
    key_hash = str(record.get("key_hash") or "").strip()
    scopes = record.get("scopes")
    expires_at = int(record.get("expires_at") or 0) or None
    restrictions = record.get("restrictions")
    if not api_key_id or not user_id or not key_prefix or not key_hash:
        raise ValueError("api key id, user_id, prefix, and hash are required")
    scopes_text = scopes if isinstance(scopes, str) else json.dumps(scopes or [], sort_keys=True)
    restrictions_text = (
        restrictions
        if isinstance(restrictions, str)
        else json.dumps(restrictions or {}, sort_keys=True)
    )
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                INSERT INTO api_keys (
                    id, user_id, name, key_prefix, key_hash, scopes,
                    expires_at, restrictions, created_at, last_used_at, revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), NULL, NULL)
                """,
                (
                    api_key_id,
                    user_id,
                    name,
                    key_prefix,
                    key_hash,
                    scopes_text,
                    expires_at,
                    restrictions_text,
                ),
            )
            return row_to_dict(connection.execute("SELECT * FROM api_keys WHERE id = ?", (api_key_id,)).fetchone()) or {}


def list_api_keys_for_user(user_id: str) -> list[dict[str, Any]]:
    ensure_initialized()
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
    ensure_initialized()
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
    ensure_initialized()
    with _LOCK, closing(connect()) as connection:
        with connection:
            connection.execute(
                "UPDATE api_keys SET last_used_at = strftime('%s', 'now') WHERE id = ? AND revoked_at IS NULL",
                (api_key_id,),
            )


def revoke_api_key(api_key_id: str, user_id: str) -> bool:
    ensure_initialized()
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
    ensure_initialized()
    target_user_id = str(user_id or "").strip()
    if not target_user_id:
        return {}
    target_scan_ids = [str(scan_id) for scan_id in (scan_ids or []) if str(scan_id or "").strip()]
    counts: dict[str, int] = {}
    artifact_content_paths: list[str] = []
    with _LOCK, closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            durable_scan_rows = connection.execute(
                "SELECT scan_id FROM scans WHERE user_id = ?",
                (target_user_id,),
            ).fetchall()
            target_scan_ids = list(
                dict.fromkeys(
                    [*target_scan_ids, *(str(row["scan_id"] or "") for row in durable_scan_rows)]
                )
            )
            target_scan_ids = [scan_id for scan_id in target_scan_ids if scan_id]
            job_predicates = ["user_id = ?"]
            job_parameters: list[str] = [target_user_id]
            if target_scan_ids:
                placeholders = ",".join("?" for _ in target_scan_ids)
                job_predicates.append(f"scan_id IN ({placeholders})")
                job_parameters.extend(target_scan_ids)
            job_rows = connection.execute(
                f"SELECT job_id FROM scan_jobs WHERE {' OR '.join(job_predicates)}",
                job_parameters,
            ).fetchall()
            target_job_ids = [str(row["job_id"] or "") for row in job_rows if str(row["job_id"] or "")]
            if target_job_ids:
                job_placeholders = ",".join("?" for _ in target_job_ids)
                artifact_rows = connection.execute(
                    f"SELECT content_path FROM review_artifacts WHERE job_id IN ({job_placeholders})",
                    target_job_ids,
                ).fetchall()
                artifact_content_paths = [str(row["content_path"] or "") for row in artifact_rows if str(row["content_path"] or "")]
                counts["reviewArtifacts"] = connection.execute(
                    f"DELETE FROM review_artifacts WHERE job_id IN ({job_placeholders})",
                    target_job_ids,
                ).rowcount
                counts["reviewRunEvents"] = connection.execute(
                    f"DELETE FROM review_run_events WHERE job_id IN ({job_placeholders})",
                    target_job_ids,
                ).rowcount
                counts["reviewRuns"] = connection.execute(
                    f"DELETE FROM review_runs WHERE job_id IN ({job_placeholders})",
                    target_job_ids,
                ).rowcount
            else:
                counts["reviewArtifacts"] = 0
                counts["reviewRunEvents"] = 0
                counts["reviewRuns"] = 0
            counts["apiKeys"] = connection.execute(
                "DELETE FROM api_keys WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["quotaLedger"] = connection.execute(
                "DELETE FROM quota_ledger WHERE requested_by_user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["quotaLedger"] += connection.execute(
                "DELETE FROM quota_ledger WHERE bucket_id IN (SELECT id FROM quota_buckets WHERE scope_type = 'user' AND scope_id = ?)",
                (target_user_id,),
            ).rowcount
            counts["quotaBuckets"] = connection.execute(
                "DELETE FROM quota_buckets WHERE scope_type = 'user' AND scope_id = ?",
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
            counts["issues"] = connection.execute(
                "DELETE FROM issues WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["scans"] = connection.execute(
                "DELETE FROM scans WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            counts["scanJobs"] = connection.execute(
                "DELETE FROM scan_jobs WHERE user_id = ?",
                (target_user_id,),
            ).rowcount
            if target_scan_ids:
                placeholders = ",".join("?" for _ in target_scan_ids)
                counts["issues"] += connection.execute(
                    f"DELETE FROM issues WHERE scan_id IN ({placeholders})",
                    target_scan_ids,
                ).rowcount
                counts["scans"] += connection.execute(
                    f"DELETE FROM scans WHERE scan_id IN ({placeholders})",
                    target_scan_ids,
                ).rowcount
                counts["scanJobs"] += connection.execute(
                    f"DELETE FROM scan_jobs WHERE scan_id IN ({placeholders})",
                    target_scan_ids,
                ).rowcount
    counts["reviewArtifactFiles"] = sum(
        1 for content_path in artifact_content_paths if delete_review_artifact_content_file(content_path)
    )
    return counts


def quota_bucket_id(scope_type: str, scope_id: str, period: str, plan: str) -> str:
    return stable_id("qb", f"{scope_type}:{scope_id}:{period}:{plan}")


def quota_ledger_id(*parts: object) -> str:
    return stable_id("ql", ":".join(str(part or "") for part in parts))
