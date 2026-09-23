from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .product_domain import context_hash, validate_watch_interests, watch_scope_key
from .product_dto_rules import (
    iso_timestamp as _iso_timestamp,
    source_context_dto, source_record_dto, watch_dto,
    handling_event_dto, item_read_dto, item_dependencies_current,
)
from .update_filter import project_saved_updates
from .product_usage_events import parse_usage_events_query, usage_events_page


_UNIT_TYPES = frozenset(
    {
        "pr_thread",
        "pr_comment",
        "pr_review_body",
        "pr_review_request",
        "ci_job",
        "update_release",
    }
)
_UNSET = object()
_MODULE_SOURCE_TYPES = {
    "pr": ("pr_state", "pr_comment", "pr_review_body", "pr_review_comment"),
    "ci": ("ci_failure",) * 4,
    "updates": ("release",) * 4,
}


def _now() -> int:
    return int(time.time())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


class ProductStore:
    def __init__(self, database_path: str | Path, *, _connection: sqlite3.Connection | None = None) -> None:
        self.database_path = str(database_path)
        self._connection = _connection

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
        else:
            with closing(self.connect()) as connection:
                yield connection

    @contextmanager
    def atomic(self) -> Iterator[ProductStore]:
        """Compose store operations in one short transaction; never perform network I/O here."""
        with self._immediate() as connection:
            yield ProductStore(self.database_path, _connection=connection)

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _release_processing_reservation(
        connection: sqlite3.Connection,
        reservation: sqlite3.Row,
        *,
        now: int,
    ) -> None:
        if reservation["state"] != "reserved":
            return
        released_bucket = connection.execute(
            """
            UPDATE processing_usage_buckets
            SET reserved = reserved - 1, updated_at = ?
            WHERE billing_owner_id = ? AND period = ?
              AND metric = 'intelligent_processing' AND reserved >= 1
            """,
            (now, reservation["billing_owner_id"], reservation["period"]),
        )
        if released_bucket.rowcount != 1:
            raise RuntimeError("processing reservation bucket is inconsistent")
        released_ledger = connection.execute(
            """
            UPDATE processing_usage_ledger
            SET state = 'released', finished_at = ?
            WHERE reservation_id = ? AND state = 'reserved'
            """,
            (now, reservation["reservation_id"]),
        )
        if released_ledger.rowcount != 1:
            raise RuntimeError("processing reservation ledger is inconsistent")

    def initialize(self) -> None:
        with closing(self.connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS discovery_targets (
                    control_key TEXT PRIMARY KEY,
                    resource_kind TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    github_repository_id TEXT NOT NULL,
                    installation_id TEXT,
                    app_id TEXT,
                    billing_owner_id TEXT NOT NULL,
                    authorization_revision INTEGER NOT NULL,
                    accessible INTEGER NOT NULL,
                    valid_until INTEGER NOT NULL,
                    analysis_authorized_at INTEGER,
                    configuration_stamp TEXT,
                    configuration_epoch INTEGER NOT NULL DEFAULT 0,
                    next_scheduled_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS fact_sync_generations (
                    parent_key TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_read_backoffs (
                    control_key TEXT PRIMARY KEY REFERENCES discovery_targets(control_key),
                    retry_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_authorization_refreshes (
                    control_key TEXT PRIMARY KEY REFERENCES discovery_targets(control_key),
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claimed_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS source_observations (
                    source_id TEXT PRIMARY KEY REFERENCES source_records(source_id),
                    authoritative_changed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS github_run_states (
                    repository_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    run_attempt INTEGER NOT NULL CHECK (run_attempt >= 1),
                    authoritative_updated_at INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    PRIMARY KEY(repository_id, run_id, run_attempt)
                );
                CREATE TABLE IF NOT EXISTS github_deliveries (
                    app_id TEXT NOT NULL,
                    delivery_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    PRIMARY KEY(app_id, delivery_id)
                );
                CREATE TABLE IF NOT EXISTS github_delivery_targets (
                    app_id TEXT NOT NULL,
                    delivery_id TEXT NOT NULL,
                    control_key TEXT NOT NULL REFERENCES discovery_targets(control_key),
                    repository_id TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    pull_number INTEGER,
                    ready_at INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY(app_id, delivery_id, control_key)
                );

                CREATE TABLE IF NOT EXISTS watch_controls (
                    watch_scope_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    target_repository_id TEXT,
                    upstream_repository_id TEXT NOT NULL,
                    context_version INTEGER NOT NULL CHECK (context_version >= 1),
                    context_hash TEXT NOT NULL,
                    interests_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repository_services (
                    repository_id TEXT PRIMARY KEY,
                    installation_id TEXT NOT NULL,
                    billing_owner_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    modules_json TEXT NOT NULL,
                    analysis_enabled_json TEXT NOT NULL,
                    allow_member_sync INTEGER NOT NULL CHECK (allow_member_sync IN (0, 1)),
                    default_assignee_id TEXT,
                    priority_order INTEGER NOT NULL CHECK (priority_order >= 0),
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS update_watches (
                    id TEXT PRIMARY KEY,
                    watch_scope_key TEXT NOT NULL REFERENCES watch_controls(watch_scope_key),
                    owner_id TEXT NOT NULL,
                    target_repository_id TEXT,
                    upstream_repository_id TEXT NOT NULL,
                    billing_owner_id TEXT NOT NULL,
                    context_version INTEGER NOT NULL CHECK (context_version >= 1),
                    context_hash TEXT NOT NULL,
                    interests_json TEXT NOT NULL,
                    include_prerelease INTEGER NOT NULL DEFAULT 0 CHECK (include_prerelease IN (0, 1)),
                    priority_order INTEGER NOT NULL DEFAULT 0 CHECK (priority_order >= 0),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    analysis_enabled INTEGER NOT NULL CHECK (analysis_enabled IN (0, 1)),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS update_watches_one_active_scope
                    ON update_watches(watch_scope_key) WHERE archived_at IS NULL;

                CREATE TABLE IF NOT EXISTS processing_controls (
                    control_key TEXT PRIMARY KEY,
                    eligible_since INTEGER,
                    discovery_cursor TEXT,
                    high_watermark TEXT,
                    initial_backfill_state TEXT NOT NULL,
                    initial_backfill_sources_json TEXT NOT NULL DEFAULT '[]',
                    initial_backfill_completed_json TEXT NOT NULL DEFAULT '[]',
                    config_stable_at INTEGER,
                    last_context_refresh_at INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_records (
                    source_id TEXT PRIMARY KEY,
                    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
                    source_type TEXT,
                    external_key TEXT,
                    repository_id TEXT,
                    latest_version TEXT,
                    state_hash TEXT,
                    lifecycle TEXT,
                    source_facts_json TEXT,
                    source_url TEXT,
                    processing_mode TEXT,
                    completeness TEXT,
                    last_synced_at INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS source_records_external_identity
                    ON source_records(source_type, external_key)
                    WHERE source_type IS NOT NULL AND external_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS source_versions (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES source_records(source_id),
                    content_hash TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(source_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS source_contexts (
                    source_id TEXT NOT NULL REFERENCES source_records(source_id),
                    context_id TEXT NOT NULL,
                    billing_owner_id TEXT NOT NULL DEFAULT '',
                    watch_id TEXT,
                    item_id TEXT,
                    context_version INTEGER NOT NULL CHECK (context_version >= 1),
                    configuration_revision INTEGER NOT NULL CHECK (configuration_revision >= 1),
                    authorization_revision INTEGER NOT NULL CHECK (authorization_revision >= 1),
                    authorization_valid_until INTEGER NOT NULL CHECK (authorization_valid_until >= 0),
                    accessible INTEGER NOT NULL CHECK (accessible IN (0, 1)),
                    processing_status TEXT NOT NULL DEFAULT 'pending',
                    analysis_enabled INTEGER NOT NULL DEFAULT 0 CHECK (analysis_enabled IN (0, 1)),
                    context_stale INTEGER NOT NULL DEFAULT 0 CHECK (context_stale IN (0, 1)),
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(source_id, context_id)
                );

                CREATE TABLE IF NOT EXISTS assessments (
                    id TEXT PRIMARY KEY,
                    billing_owner_id TEXT NOT NULL,
                    source_version_id TEXT NOT NULL,
                    context_hash TEXT NOT NULL,
                    evaluated_context_version INTEGER NOT NULL CHECK (evaluated_context_version >= 1),
                    question_version TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('succeeded', 'discarded', 'failed')),
                    created_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS assessments_success_cache_key
                    ON assessments(
                        billing_owner_id, source_version_id, context_hash,
                        question_version, extractor_version, model, input_hash
                    ) WHERE status = 'succeeded';

                CREATE TABLE IF NOT EXISTS source_assessment_publications (
                    source_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    source_version_id TEXT NOT NULL,
                    context_version INTEGER NOT NULL,
                    authorization_revision INTEGER NOT NULL,
                    billing_owner_id TEXT NOT NULL,
                    assessment_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    fences_json TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    PRIMARY KEY(source_id, context_id)
                );

                CREATE TABLE IF NOT EXISTS pr_parent_checks (
                    source_id TEXT PRIMARY KEY REFERENCES source_records(source_id),
                    checked_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    unit_type TEXT NOT NULL,
                    unit_key TEXT NOT NULL,
                    current_item_version INTEGER NOT NULL DEFAULT 0,
                    current_snapshot_hash TEXT,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(context_id, unit_type, unit_key)
                );

                CREATE TABLE IF NOT EXISTS item_versions (
                    item_id TEXT NOT NULL REFERENCES items(id),
                    item_version INTEGER NOT NULL CHECK (item_version >= 1),
                    snapshot_hash TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    context_fences_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    PRIMARY KEY(item_id, item_version)
                );

                CREATE TABLE IF NOT EXISTS item_handling_events (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES items(id),
                    item_version INTEGER NOT NULL CHECK (item_version >= 1),
                    actor_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK (disposition IN ('open', 'done', 'dismissed')),
                    assignee_id TEXT,
                    note TEXT,
                    feedback TEXT CHECK (feedback IS NULL OR feedback = 'classification_inaccurate'),
                    event_kind TEXT NOT NULL,
                    carried_from_item_version INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS item_handling_events_item_order
                    ON item_handling_events(item_id, created_at, id);

                CREATE TABLE IF NOT EXISTS provider_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    billing_owner_id TEXT NOT NULL,
                    input_key TEXT NOT NULL,
                    period_utc TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS provider_attempts_owner_period
                    ON provider_attempts(billing_owner_id, period_utc, occurred_at);
                CREATE INDEX IF NOT EXISTS provider_attempts_global_period
                    ON provider_attempts(period_utc, occurred_at);

                CREATE TABLE IF NOT EXISTS processing_usage_buckets (
                    billing_owner_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
                    limit_value INTEGER NOT NULL CHECK (limit_value >= 0),
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(billing_owner_id, period, metric)
                );

                CREATE TABLE IF NOT EXISTS processing_usage_ledger (
                    charge_key TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL UNIQUE,
                    billing_owner_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    module TEXT NOT NULL CHECK (module IN ('pr', 'ci', 'updates')),
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'consumed', 'released')),
                    reserved_at INTEGER NOT NULL,
                    finished_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS processing_usage_ledger_owner_period
                    ON processing_usage_ledger(billing_owner_id, period, state, module);

                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    logical_key TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    trusted_trigger TEXT NOT NULL,
                    requester_id TEXT,
                    billing_owner_id TEXT,
                    source_id TEXT,
                    context_id TEXT,
                    reservation_id TEXT,
                    source_revision INTEGER,
                    source_version_id TEXT,
                    context_version INTEGER,
                    configuration_revision INTEGER,
                    authorization_revision INTEGER,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    claim_token TEXT,
                    claimed_until INTEGER,
                    next_attempt_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS background_jobs_one_active_logical_key
                    ON background_jobs(logical_key)
                    WHERE state IN ('queued', 'running', 'retry_wait');

                CREATE TABLE IF NOT EXISTS analysis_claim_owners (
                    billing_owner_id TEXT PRIMARY KEY,
                    last_claim_order INTEGER NOT NULL CHECK (last_claim_order >= 1)
                );

                CREATE TABLE IF NOT EXISTS request_idempotency (
                    subject_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
                    status_code INTEGER,
                    response_json TEXT,
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(subject_id, method, path, idempotency_key)
                );
                """
            )
            background_job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(background_jobs)").fetchall()
            }
            delivery_columns = {row["name"] for row in connection.execute("PRAGMA table_info(github_delivery_targets)")}
            if "pull_number" not in delivery_columns:
                connection.execute("ALTER TABLE github_delivery_targets ADD COLUMN pull_number INTEGER")
            for column, definition in {
                "billing_owner_id": "TEXT",
                "source_id": "TEXT",
                "context_id": "TEXT",
                "reservation_id": "TEXT",
                "source_revision": "INTEGER",
                "source_version_id": "TEXT",
                "context_version": "INTEGER",
                "configuration_revision": "INTEGER",
                "authorization_revision": "INTEGER",
                "claim_token": "TEXT",
                "claimed_until": "INTEGER",
            }.items():
                if column not in background_job_columns:
                    connection.execute(
                        f"ALTER TABLE background_jobs ADD COLUMN {column} {definition}"
                    )

    def set_discovery_authorization(
        self, *, resource_kind: str, resource_id: str, module: str,
        github_repository_id: str, installation_id: str | None, app_id: str | None,
        authorization_revision: int, accessible: bool, valid_until: int, observed_at: int,
    ) -> str:
        """Persist a server-verified, short-lived proof. Never expose this as a user write."""
        if type(authorization_revision) is not int or authorization_revision < 1:
            raise ValueError("invalid authorization revision")
        if type(accessible) is not bool or not observed_at <= valid_until <= observed_at + 300:
            raise ValueError("authorization proof must expire within five minutes")
        github_repository_id = _identifier(github_repository_id, "github_repository_id")
        with self._immediate() as connection:
            if resource_kind == "watch" and module == "updates":
                resource = connection.execute(
                    "SELECT * FROM update_watches WHERE id=? AND archived_at IS NULL", (resource_id,)
                ).fetchone()
                if resource is None or resource["upstream_repository_id"] != f"github:{github_repository_id}":
                    raise ValueError("DISCOVERY_TARGET_NOT_FOUND")
                context_id = resource["watch_scope_key"]
                repository_id = resource["upstream_repository_id"]
            elif resource_kind == "repository" and module in {"pr", "ci"}:
                resource = connection.execute(
                    "SELECT * FROM repository_services WHERE repository_id=?", (resource_id,)
                ).fetchone()
                if resource is None or resource["installation_id"] != installation_id:
                    raise ValueError("DISCOVERY_TARGET_NOT_FOUND")
                context_id = f"repository:{resource_id}"
                repository_id = resource_id
            else:
                raise ValueError("invalid discovery target")
            owner = resource["billing_owner_id"]
            analysis_authorized = accessible and bool(resource["enabled"]) and (
                bool(resource["analysis_enabled"]) if resource_kind == "watch"
                else json.loads(resource["analysis_enabled_json"])[module]
            )
            control_key = context_id if resource_kind == "watch" else "discovery:" + hashlib.sha256(
                _json([owner, context_id, module]).encode("utf-8")
            ).hexdigest()
            old = connection.execute("SELECT * FROM discovery_targets WHERE control_key=?", (control_key,)).fetchone()
            if old is not None:
                if authorization_revision < old["authorization_revision"]:
                    raise ValueError("STALE_AUTHORIZATION")
                if authorization_revision == old["authorization_revision"] and (
                    bool(old["accessible"]) != accessible or old["installation_id"] != installation_id
                    or old["app_id"] != app_id or old["github_repository_id"] != github_repository_id
                ):
                    raise ValueError("AUTHORIZATION_REVISION_CONFLICT")
            connection.execute(
                """INSERT INTO discovery_targets(control_key, resource_kind, resource_id, context_id,
                       module, repository_id, github_repository_id, installation_id, app_id,
                       billing_owner_id, authorization_revision, accessible, valid_until, analysis_authorized_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(control_key) DO UPDATE SET resource_id=excluded.resource_id,
                       installation_id=excluded.installation_id, app_id=excluded.app_id,
                       authorization_revision=excluded.authorization_revision,
                       accessible=excluded.accessible, valid_until=excluded.valid_until,
                       analysis_authorized_at=COALESCE(discovery_targets.analysis_authorized_at, excluded.analysis_authorized_at)""",
                (control_key, resource_kind, resource_id, context_id, module, repository_id,
                 github_repository_id, installation_id, app_id, owner, authorization_revision,
                 int(accessible), valid_until, observed_at if analysis_authorized else None),
            )
            connection.execute(
                """INSERT OR IGNORE INTO processing_controls(control_key, initial_backfill_state, updated_at)
                   VALUES (?, 'not_started', ?)""", (control_key, observed_at),
            )
            connection.execute(
                """UPDATE source_contexts SET authorization_revision=?, authorization_valid_until=?,
                       accessible=? WHERE context_id=? AND billing_owner_id=? AND source_id IN (
                           SELECT source_id FROM source_records WHERE source_type IN (?, ?, ?, ?))""",
                (authorization_revision, valid_until, int(accessible), context_id, owner, *_MODULE_SOURCE_TYPES[module]),
            )
            if not accessible:
                jobs = connection.execute(
                    """SELECT * FROM background_jobs WHERE context_id=? AND billing_owner_id=?
                       AND state IN ('queued','running','retry_wait') AND source_id IN (
                           SELECT source_id FROM source_records WHERE source_type IN (?, ?, ?, ?))""",
                    (context_id, owner, *_MODULE_SOURCE_TYPES[module]),
                ).fetchall()
                for job in jobs:
                    reservation = connection.execute(
                        "SELECT * FROM processing_usage_ledger WHERE reservation_id=?", (job["reservation_id"],)
                    ).fetchone()
                    if reservation is not None:
                        self._release_processing_reservation(connection, reservation, now=observed_at)
                    connection.execute(
                        "UPDATE background_jobs SET state='cancelled', claim_token=NULL WHERE id=?",
                        (job["id"],),
                    )
        return control_key

    def revoke_discovery_installation(self, *, app_id: str, installation_id: str,
                                     repository_ids: Sequence[str] | None, observed_at: int) -> int:
        with self._immediate() as connection:
            targets = connection.execute(
                """SELECT d.*, r.installation_id AS target_installation_id,
                          w.target_repository_id
                   FROM discovery_targets d
                   LEFT JOIN update_watches w ON d.resource_kind='watch' AND w.id=d.resource_id
                   LEFT JOIN repository_services r ON r.repository_id=w.target_repository_id
                   WHERE d.app_id=? AND (d.installation_id=? OR r.installation_id=?)""",
                (app_id, installation_id, installation_id),
            ).fetchall()
            count = 0
            for target in targets:
                upstream_matches = target["installation_id"] == installation_id and (
                    repository_ids is None or target["github_repository_id"] in repository_ids)
                parent_matches = target["target_installation_id"] == installation_id and (
                    repository_ids is None or target["target_repository_id"] in {f"github:{repo}" for repo in repository_ids})
                if not upstream_matches and not parent_matches:
                    continue
                connection.execute(
                    "UPDATE discovery_targets SET accessible=0, authorization_revision=authorization_revision+1, valid_until=? WHERE control_key=?",
                    (observed_at, target["control_key"]),
                )
                connection.execute(
                    """UPDATE source_contexts SET accessible=0, authorization_revision=authorization_revision+1, authorization_valid_until=?
                       WHERE context_id=? AND billing_owner_id=? AND source_id IN (
                           SELECT source_id FROM source_records WHERE source_type IN (?, ?, ?, ?))""",
                    (observed_at, target["context_id"], target["billing_owner_id"], *_MODULE_SOURCE_TYPES[target["module"]]),
                )
                jobs = connection.execute(
                    """SELECT * FROM background_jobs WHERE context_id=? AND billing_owner_id=? AND state IN ('queued','running','retry_wait')
                       AND source_id IN (SELECT source_id FROM source_records WHERE source_type IN (?, ?, ?, ?))""",
                    (target["context_id"], target["billing_owner_id"], *_MODULE_SOURCE_TYPES[target["module"]]),
                ).fetchall()
                for job in jobs:
                    reservation = connection.execute("SELECT * FROM processing_usage_ledger WHERE reservation_id=?", (job["reservation_id"],)).fetchone()
                    if reservation is not None:
                        self._release_processing_reservation(connection, reservation, now=observed_at)
                    connection.execute("UPDATE background_jobs SET state='cancelled', claim_token=NULL WHERE id=?", (job["id"],))
                count += 1
            return count

    def discovery_target(self, control_key: str) -> dict | None:
        with self._read() as connection:
            row = connection.execute("SELECT * FROM discovery_targets WHERE control_key=?", (control_key,)).fetchone()
            if row is None:
                return None
            target = dict(row)
            if row["resource_kind"] == "watch":
                config = connection.execute(
                    "SELECT * FROM update_watches WHERE id=? AND archived_at IS NULL", (row["resource_id"],)
                ).fetchone()
                if config is None:
                    return None
                target.update(context_version=config["context_version"], context_hash=config["context_hash"],
                              owner_id=config["owner_id"], target_repository_id=config["target_repository_id"],
                              configuration_revision=config["revision"], enabled=bool(config["enabled"]),
                              analysis_enabled=bool(config["analysis_enabled"]),
                              include_prerelease=bool(config["include_prerelease"]))
                if config["target_repository_id"] is not None:
                    parent = connection.execute("SELECT * FROM repository_services WHERE repository_id=?",
                                                (config["target_repository_id"],)).fetchone()
                    target.update(target_installation_id=parent["installation_id"] if parent else None,
                                  target_repository_revision=parent["revision"] if parent else None,
                                  target_billing_owner_id=parent["billing_owner_id"] if parent else None)
                    target["enabled"] = target["enabled"] and parent is not None and bool(parent["enabled"]) and parent["billing_owner_id"] == row["billing_owner_id"]
            else:
                config = connection.execute("SELECT * FROM repository_services WHERE repository_id=?", (row["resource_id"],)).fetchone()
                if config is None or config["installation_id"] != row["installation_id"]:
                    return None
                target.update(context_version=1, context_hash="", configuration_revision=config["revision"],
                              default_assignee_id=config["default_assignee_id"],
                              enabled=bool(config["enabled"]) and json.loads(config["modules_json"])[row["module"]],
                              analysis_enabled=json.loads(config["analysis_enabled_json"])[row["module"]],
                              include_prerelease=False)
            if config["billing_owner_id"] != row["billing_owner_id"]:
                return None
            stamp_fields = [row["resource_id"], target["configuration_revision"]]
            if row["resource_kind"] == "watch" and target["target_repository_id"] is not None:
                stamp_fields.extend([target["target_repository_id"], target["target_installation_id"],
                                     target["target_repository_revision"], target["target_billing_owner_id"]])
            stamp = _json(stamp_fields)
            target["configuration_revision"] = (
                max(int(row["configuration_epoch"]) + 1, target["configuration_revision"])
                if stamp != row["configuration_stamp"] else int(row["configuration_epoch"])
            )
            target["configuration_stamp"] = stamp
            return target

    def _invalidate_discovery_configuration(self, connection: sqlite3.Connection, *, resource_kind: str, resource_id: str) -> None:
        bound = ProductStore(self.database_path, _connection=connection)
        targets = connection.execute(
            "SELECT * FROM discovery_targets WHERE resource_kind=? AND resource_id=?",
            (resource_kind, resource_id),
        ).fetchall()
        for previous in targets:
            target = bound.discovery_target(previous["control_key"])
            epoch = target["configuration_revision"] if target else previous["configuration_epoch"] + 1
            connection.execute("UPDATE discovery_targets SET configuration_epoch=?, configuration_stamp=? WHERE control_key=?",
                               (epoch, target["configuration_stamp"] if target else None, previous["control_key"]))
            enabled = bool(target and target["enabled"] and target["analysis_enabled"])
            version = target["context_version"] if target else None
            connection.execute(
                """UPDATE source_contexts SET configuration_revision=?, analysis_enabled=?,
                       context_stale=CASE WHEN context_version!=COALESCE(?,context_version) THEN 1 ELSE context_stale END,
                       context_version=COALESCE(?,context_version),
                       processing_status=CASE WHEN ?=0 THEN 'analysis_disabled' ELSE processing_status END
                   WHERE context_id=? AND billing_owner_id=?
                     AND source_id IN (SELECT source_id FROM source_records WHERE source_type IN (?, ?, ?, ?))""",
                (epoch, int(enabled), version, version, int(enabled), previous["context_id"], previous["billing_owner_id"],
                 *_MODULE_SOURCE_TYPES[previous["module"]]),
            )
            # Running work keeps its lease for successor fencing, but publication
            # already fails against the changed source-context configuration.
            jobs = connection.execute(
                """SELECT * FROM background_jobs WHERE context_id=? AND billing_owner_id=?
                   AND state IN ('queued','retry_wait') AND source_id IN (
                       SELECT source_id FROM source_contexts WHERE context_id=? AND configuration_revision=?)""",
                (previous["context_id"], previous["billing_owner_id"], previous["context_id"], epoch),
            ).fetchall()
            for job in jobs:
                reservation = connection.execute("SELECT * FROM processing_usage_ledger WHERE reservation_id=?", (job["reservation_id"],)).fetchone()
                if reservation is not None:
                    self._release_processing_reservation(connection, reservation, now=_now())
                connection.execute("UPDATE background_jobs SET state='cancelled', claim_token=NULL, claimed_until=NULL WHERE id=?", (job["id"],))

    def create_watch(
        self,
        *,
        owner_id: str,
        target_repository_id: str | None,
        upstream_repository_id: str,
        billing_owner_id: str,
        interests: Sequence[str],
        enabled: bool,
        analysis_enabled: bool,
        include_prerelease: bool = False,
        priority_order: int = 0,
        active_limit: int | None = None,
    ) -> dict:
        if isinstance(priority_order, bool) or not isinstance(priority_order, int) or priority_order < 0:
            raise ValueError("priority_order must be a non-negative integer")
        if active_limit is not None and (
            isinstance(active_limit, bool) or not isinstance(active_limit, int) or active_limit < 0
        ):
            raise ValueError("active_limit must be a non-negative integer")
        scope_key = watch_scope_key(
            owner_id=owner_id,
            target_repository_id=target_repository_id,
            upstream_repository_id=upstream_repository_id,
        )
        normalized = validate_watch_interests(interests)
        semantic_hash = context_hash(normalized)
        now = _now()
        with self._immediate() as connection:
            active = connection.execute(
                "SELECT 1 FROM update_watches WHERE watch_scope_key = ? AND archived_at IS NULL",
                (scope_key,),
            ).fetchone()
            if active is not None:
                raise ValueError("WATCH_ALREADY_EXISTS")
            if enabled and active_limit is not None:
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM update_watches
                        WHERE billing_owner_id = ? AND enabled = 1 AND archived_at IS NULL
                        """,
                        (billing_owner_id,),
                    ).fetchone()[0]
                )
                if active_count >= active_limit:
                    raise ValueError("WATCH_LIMIT_REACHED")
            control = connection.execute(
                "SELECT context_version, context_hash FROM watch_controls WHERE watch_scope_key = ?",
                (scope_key,),
            ).fetchone()
            if control is None:
                context_version = 1
                connection.execute(
                    """
                    INSERT INTO watch_controls (
                        watch_scope_key, owner_id, target_repository_id, upstream_repository_id,
                        context_version, context_hash, interests_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope_key,
                        _identifier(owner_id, "owner_id"),
                        target_repository_id,
                        _identifier(upstream_repository_id, "upstream_repository_id"),
                        context_version,
                        semantic_hash,
                        _json(normalized),
                        now,
                        now,
                    ),
                )
            else:
                context_version = int(control["context_version"])
                if control["context_hash"] != semantic_hash:
                    context_version += 1
                    connection.execute(
                        """
                        UPDATE watch_controls
                        SET context_version = ?, context_hash = ?, interests_json = ?, updated_at = ?
                        WHERE watch_scope_key = ?
                        """,
                        (context_version, semantic_hash, _json(normalized), now, scope_key),
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO processing_controls (
                    control_key, initial_backfill_state, updated_at
                ) VALUES (?, 'not_started', ?)
                """,
                (scope_key, now),
            )
            watch_id = f"watch_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO update_watches (
                    id, watch_scope_key, owner_id, target_repository_id,
                    upstream_repository_id, billing_owner_id, context_version, context_hash,
                    interests_json, include_prerelease, priority_order, enabled,
                    analysis_enabled, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    watch_id,
                    scope_key,
                    _identifier(owner_id, "owner_id"),
                    target_repository_id,
                    _identifier(upstream_repository_id, "upstream_repository_id"),
                    _identifier(billing_owner_id, "billing_owner_id"),
                    context_version,
                    semantic_hash,
                    _json(normalized),
                    int(bool(include_prerelease)),
                    priority_order,
                    int(bool(enabled)),
                    int(bool(analysis_enabled)),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM update_watches WHERE id = ?", (watch_id,)).fetchone()
        return self._watch_dto(row)

    def put_repository_service(
        self,
        *,
        repository_id: str,
        installation_id: str,
        billing_owner_id: str,
        expected_revision: int,
        enabled: bool,
        modules: Mapping[str, object],
        analysis_enabled: Mapping[str, object],
        allow_member_sync: bool,
        default_assignee_id: str | None,
        priority_order: int,
        active_limit: int | None = None,
    ) -> dict:
        repository_id = _identifier(repository_id, "repository_id")
        installation_id = _identifier(installation_id, "installation_id")
        billing_owner_id = _identifier(billing_owner_id, "billing_owner_id")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(enabled, bool) or not isinstance(allow_member_sync, bool):
            raise ValueError("enabled and allow_member_sync must be boolean")
        if set(modules) != {"pr", "ci"} or set(analysis_enabled) != {"pr", "ci"}:
            raise ValueError("modules and analysis_enabled must contain pr and ci")
        if any(not isinstance(value, bool) for value in (*modules.values(), *analysis_enabled.values())):
            raise ValueError("module switches must be boolean")
        if any(bool(analysis_enabled[name]) and not bool(modules[name]) for name in ("pr", "ci")):
            raise ValueError("analysis cannot be enabled for a disabled module")
        if default_assignee_id is not None:
            default_assignee_id = _identifier(default_assignee_id, "default_assignee_id")
        if isinstance(priority_order, bool) or not isinstance(priority_order, int) or priority_order < 0:
            raise ValueError("priority_order must be a non-negative integer")
        if active_limit is not None and (
            isinstance(active_limit, bool) or not isinstance(active_limit, int) or active_limit < 0
        ):
            raise ValueError("active_limit must be a non-negative integer")
        now = _now()
        modules_json = _json({"pr": modules["pr"], "ci": modules["ci"]})
        analysis_json = _json({"pr": analysis_enabled["pr"], "ci": analysis_enabled["ci"]})
        with self._immediate() as connection:
            current = connection.execute(
                "SELECT * FROM repository_services WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if enabled and active_limit is not None and (current is None or not bool(current["enabled"])):
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM repository_services
                        WHERE billing_owner_id = ? AND enabled = 1 AND repository_id != ?
                        """,
                        (billing_owner_id, repository_id),
                    ).fetchone()[0]
                )
                if active_count >= active_limit:
                    raise ValueError("REPOSITORY_LIMIT_REACHED")
            if current is None:
                if expected_revision != 0:
                    raise ValueError("REVISION_MISMATCH")
                connection.execute(
                    """
                    INSERT INTO repository_services(
                        repository_id, installation_id, billing_owner_id, enabled,
                        modules_json, analysis_enabled_json, allow_member_sync,
                        default_assignee_id, priority_order, status, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        repository_id,
                        installation_id,
                        billing_owner_id,
                        int(enabled),
                        modules_json,
                        analysis_json,
                        int(allow_member_sync),
                        default_assignee_id,
                        priority_order,
                        "active" if enabled else "paused",
                        now,
                        now,
                    ),
                )
            else:
                if int(current["revision"]) != expected_revision:
                    raise ValueError("REVISION_MISMATCH")
                if current["billing_owner_id"] != billing_owner_id:
                    raise ValueError("REPOSITORY_ALREADY_MANAGED")
                cursor = connection.execute(
                    """
                    UPDATE repository_services
                    SET installation_id = ?, enabled = ?, modules_json = ?,
                        analysis_enabled_json = ?, allow_member_sync = ?,
                        default_assignee_id = ?, priority_order = ?, status = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE repository_id = ? AND revision = ?
                    """,
                    (
                        installation_id,
                        int(enabled),
                        modules_json,
                        analysis_json,
                        int(allow_member_sync),
                        default_assignee_id,
                        priority_order,
                        "active" if enabled else "paused",
                        now,
                        repository_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("REVISION_MISMATCH")
            row = connection.execute(
                "SELECT * FROM repository_services WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            self._invalidate_discovery_configuration(connection, resource_kind="repository", resource_id=repository_id)
            shared_watches = connection.execute(
                "SELECT id FROM update_watches WHERE target_repository_id=? AND archived_at IS NULL",
                (repository_id,),
            ).fetchall()
            for watch in shared_watches:
                self._invalidate_discovery_configuration(connection, resource_kind="watch", resource_id=watch["id"])
                if current is None or current["installation_id"] != installation_id or current["billing_owner_id"] != billing_owner_id:
                    proofs = connection.execute(
                        "SELECT * FROM discovery_targets WHERE resource_kind='watch' AND resource_id=?",
                        (watch["id"],),
                    ).fetchall()
                    bound = ProductStore(self.database_path, _connection=connection)
                    for proof in proofs:
                        bound.set_discovery_authorization(
                            resource_kind="watch", resource_id=watch["id"], module="updates",
                            github_repository_id=proof["github_repository_id"], installation_id=proof["installation_id"],
                            app_id=proof["app_id"], authorization_revision=proof["authorization_revision"] + 1,
                            accessible=False, valid_until=now, observed_at=now)
        return self._repository_service_dto(row)

    def get_repository_service(self, repository_id: str) -> dict | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM repository_services WHERE repository_id = ?",
                (_identifier(repository_id, "repository_id"),),
            ).fetchone()
        return self._repository_service_dto(row) if row is not None else None

    def list_repository_services_for_billing_owner(self, billing_owner_id: str) -> list[dict]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM repository_services
                WHERE billing_owner_id = ? ORDER BY priority_order, created_at, repository_id
                """,
                (_identifier(billing_owner_id, "billing_owner_id"),),
            ).fetchall()
        return [self._repository_service_dto(row) for row in rows]

    def count_active_repository_services(self, billing_owner_id: str) -> int:
        with self._read() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM repository_services
                    WHERE billing_owner_id = ? AND enabled = 1
                    """,
                    (_identifier(billing_owner_id, "billing_owner_id"),),
                ).fetchone()[0]
            )

    def update_watch(
        self,
        watch_id: str,
        *,
        expected_revision: int,
        interests: Sequence[str] | None = None,
        enabled: bool | None = None,
        analysis_enabled: bool | None = None,
        include_prerelease: bool | None = None,
        priority_order: int | None = None,
        active_limit: int | None = None,
    ) -> dict:
        now = _now()
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM update_watches WHERE id = ? AND archived_at IS NULL",
                (_identifier(watch_id, "watch_id"),),
            ).fetchone()
            if row is None:
                raise ValueError("NOT_FOUND")
            if int(row["revision"]) != expected_revision:
                raise ValueError("REVISION_MISMATCH")
            normalized = tuple(json.loads(row["interests_json"]))
            semantic_hash = str(row["context_hash"])
            context_version = int(row["context_version"])
            if interests is not None:
                normalized = validate_watch_interests(interests)
                candidate_hash = context_hash(normalized)
                if candidate_hash != semantic_hash:
                    control = connection.execute(
                        "SELECT context_version FROM watch_controls WHERE watch_scope_key = ?",
                        (row["watch_scope_key"],),
                    ).fetchone()
                    context_version = int(control["context_version"]) + 1
                    semantic_hash = candidate_hash
                    connection.execute(
                        """
                        UPDATE watch_controls
                        SET context_version = ?, context_hash = ?, interests_json = ?, updated_at = ?
                        WHERE watch_scope_key = ?
                        """,
                        (context_version, semantic_hash, _json(normalized), now, row["watch_scope_key"]),
                    )
                    connection.execute(
                        """
                        UPDATE processing_controls
                        SET config_stable_at = ?, updated_at = ?
                        WHERE control_key = ?
                        """,
                        (now, now, row["watch_scope_key"]),
                    )
            next_enabled = int(row["enabled"] if enabled is None else bool(enabled))
            next_analysis = int(row["analysis_enabled"] if analysis_enabled is None else bool(analysis_enabled))
            next_prerelease = int(
                row["include_prerelease"] if include_prerelease is None else bool(include_prerelease)
            )
            next_priority = int(row["priority_order"] if priority_order is None else priority_order)
            if next_priority < 0:
                raise ValueError("priority_order must be a non-negative integer")
            if active_limit is not None and (
                isinstance(active_limit, bool) or not isinstance(active_limit, int) or active_limit < 0
            ):
                raise ValueError("active_limit must be a non-negative integer")
            if next_enabled and not bool(row["enabled"]) and active_limit is not None:
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM update_watches
                        WHERE billing_owner_id = ? AND enabled = 1
                          AND archived_at IS NULL AND id != ?
                        """,
                        (row["billing_owner_id"], row["id"]),
                    ).fetchone()[0]
                )
                if active_count >= active_limit:
                    raise ValueError("WATCH_LIMIT_REACHED")
            cursor = connection.execute(
                """
                UPDATE update_watches
                SET context_version = ?, context_hash = ?, interests_json = ?, enabled = ?,
                    analysis_enabled = ?, include_prerelease = ?, priority_order = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ? AND archived_at IS NULL
                """,
                (
                    context_version,
                    semantic_hash,
                    _json(normalized),
                    next_enabled,
                    next_analysis,
                    next_prerelease,
                    next_priority,
                    now,
                    row["id"],
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("REVISION_MISMATCH")
            updated = connection.execute("SELECT * FROM update_watches WHERE id = ?", (row["id"],)).fetchone()
            self._invalidate_discovery_configuration(connection, resource_kind="watch", resource_id=row["id"])
            waiting = connection.execute(
                """SELECT j.* FROM background_jobs j
                   WHERE j.job_type='analyze_source'
                     AND j.state IN ('queued','retry_wait')
                     AND EXISTS (SELECT 1 FROM source_contexts sc
                         WHERE sc.watch_id=? AND sc.source_id=j.source_id
                           AND sc.context_id=j.context_id)""",
                (row["id"],),
            ).fetchall()
            for job in waiting:
                if job["reservation_id"]:
                    reservation = connection.execute(
                        "SELECT * FROM processing_usage_ledger WHERE reservation_id=?",
                        (job["reservation_id"],),
                    ).fetchone()
                    if reservation is not None:
                        self._release_processing_reservation(connection, reservation, now=now)
                connection.execute(
                    """UPDATE background_jobs SET state='cancelled',claim_token=NULL,
                           claimed_until=NULL,updated_at=? WHERE id=?""",
                    (now, job["id"]),
                )
            connection.execute(
                """UPDATE source_contexts SET
                       configuration_revision=COALESCE((
                           SELECT configuration_epoch FROM discovery_targets
                           WHERE resource_kind='watch' AND resource_id=?),?),
                       analysis_enabled=?,
                       context_stale=CASE WHEN context_version!=? THEN 1 ELSE context_stale END,
                       context_version=?,
                       processing_status=CASE WHEN ?=0 THEN 'analysis_disabled'
                                              ELSE processing_status END,
                       updated_at=? WHERE watch_id=? AND billing_owner_id=?""",
                (row["id"], updated["revision"], int(bool(next_enabled and next_analysis)),
                 context_version, context_version, int(bool(next_enabled and next_analysis)),
                 now, row["id"], row["billing_owner_id"]),
            )
        return self._watch_dto(updated)

    def list_watches_for_billing_owner(self, billing_owner_id: str) -> list[dict]:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM update_watches
                WHERE billing_owner_id = ? AND archived_at IS NULL
                ORDER BY created_at, id
                """,
                (owner_id,),
            ).fetchall()
        return [self._watch_dto(row) for row in rows]

    def get_watch(self, watch_id: str) -> dict | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM update_watches WHERE id = ? AND archived_at IS NULL",
                (_identifier(watch_id, "watch_id"),),
            ).fetchone()
        return self._watch_dto(row) if row is not None else None

    def archive_watch(self, watch_id: str, *, expected_revision: int) -> None:
        now = _now()
        with self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE update_watches
                SET archived_at = ?, enabled = 0, analysis_enabled = 0,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ? AND archived_at IS NULL
                """,
                (now, now, _identifier(watch_id, "watch_id"), expected_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("REVISION_MISMATCH")
            self._invalidate_discovery_configuration(connection, resource_kind="watch", resource_id=watch_id)
            active_jobs = connection.execute(
                """SELECT j.* FROM background_jobs j
                   WHERE j.state IN ('queued','running','retry_wait')
                     AND ((j.job_type='sync_watch' AND j.logical_key=?)
                       OR (j.job_type='analyze_source' AND EXISTS (
                           SELECT 1 FROM source_contexts sc
                           WHERE sc.watch_id=? AND sc.source_id=j.source_id
                             AND sc.context_id=j.context_id)))""",
                (f"sync_watch:{watch_id}", watch_id),
            ).fetchall()
            for job in active_jobs:
                if job["reservation_id"]:
                    reservation = connection.execute(
                        "SELECT * FROM processing_usage_ledger WHERE reservation_id=?",
                        (job["reservation_id"],),
                    ).fetchone()
                    if reservation is not None:
                        self._release_processing_reservation(connection, reservation, now=now)
                connection.execute(
                    """UPDATE background_jobs
                       SET state='cancelled',claim_token=NULL,claimed_until=NULL,updated_at=?
                       WHERE id=? AND state IN ('queued','running','retry_wait')""",
                    (now, job["id"]),
                )
            connection.execute(
                """UPDATE source_contexts SET accessible=0,
                       authorization_revision=authorization_revision+1,
                       authorization_valid_until=?,analysis_enabled=0,
                       context_stale=1,processing_status='analysis_disabled',updated_at=?
                   WHERE watch_id=?""",
                (now, now, watch_id),
            )
            connection.execute(
                """UPDATE discovery_targets SET accessible=0,
                       authorization_revision=authorization_revision+1,
                       valid_until=?
                   WHERE resource_kind='watch' AND resource_id=?""",
                (now, watch_id),
            )

    def note_context_configuration(self, control_key: str, *, changed_at: int) -> None:
        control_key = _identifier(control_key, "control_key")
        if isinstance(changed_at, bool) or not isinstance(changed_at, int) or changed_at < 0:
            raise ValueError("changed_at must be a non-negative integer")
        with self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_controls
                SET config_stable_at = ?, updated_at = ?
                WHERE control_key = ?
                """,
                (changed_at, changed_at, control_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")

    def establish_processing_eligibility(
        self,
        control_key: str,
        *,
        eligible_since: int,
    ) -> dict:
        control_key = _identifier(control_key, "control_key")
        if (
            isinstance(eligible_since, bool)
            or not isinstance(eligible_since, int)
            or eligible_since < 0
        ):
            raise ValueError("eligible_since must be a non-negative integer")
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT eligible_since FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            if row is None:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
            created = row["eligible_since"] is None
            if created:
                connection.execute(
                    """
                    UPDATE processing_controls
                    SET eligible_since = ?, updated_at = ?
                    WHERE control_key = ? AND eligible_since IS NULL
                    """,
                    (eligible_since, eligible_since, control_key),
                )
                persisted = eligible_since
            else:
                persisted = int(row["eligible_since"])
        return {"eligibleSince": persisted, "created": created}

    def discovery_source_eligible(
        self,
        control_key: str,
        *,
        source_key: str,
        authoritative_changed_at: int,
    ) -> dict:
        control_key = _identifier(control_key, "control_key")
        source_key = _identifier(source_key, "source_key")
        if (
            isinstance(authoritative_changed_at, bool)
            or not isinstance(authoritative_changed_at, int)
            or authoritative_changed_at < 0
        ):
            raise ValueError("authoritative_changed_at must be a non-negative integer")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
        if row is None:
            raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
        frozen = json.loads(row["initial_backfill_sources_json"] or "[]")
        if source_key in frozen:
            return {"eligible": True, "reason": "initial_backfill"}
        if row["eligible_since"] is None:
            return {"eligible": False, "reason": "analysis_not_authorized"}
        if authoritative_changed_at >= int(row["eligible_since"]):
            return {"eligible": True, "reason": "new_or_changed"}
        return {"eligible": False, "reason": "before_eligible_since"}

    def advance_discovery_checkpoint(
        self,
        control_key: str,
        *,
        expected_cursor: str | None,
        expected_high_watermark: str | None,
        next_cursor: str | None,
        next_high_watermark: str,
        observed_at: int,
    ) -> dict:
        control_key = _identifier(control_key, "control_key")
        if expected_cursor is not None:
            expected_cursor = _identifier(expected_cursor, "expected_cursor")
        if expected_high_watermark is not None:
            expected_high_watermark = _identifier(
                expected_high_watermark,
                "expected_high_watermark",
            )
        if next_cursor is not None:
            next_cursor = _identifier(next_cursor, "next_cursor")
        next_high_watermark = _identifier(next_high_watermark, "next_high_watermark")
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            if row is None:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
            if (
                row["discovery_cursor"] != expected_cursor
                or row["high_watermark"] != expected_high_watermark
            ):
                raise ValueError("DISCOVERY_CHECKPOINT_MISMATCH")
            connection.execute(
                """
                UPDATE processing_controls
                SET discovery_cursor = ?, high_watermark = ?, updated_at = ?
                WHERE control_key = ?
                """,
                (next_cursor, next_high_watermark, observed_at, control_key),
            )
        return {
            "cursor": next_cursor,
            "highWatermark": next_high_watermark,
            "observedAt": observed_at,
        }

    def admit_context_refresh(
        self,
        control_key: str,
        *,
        timestamp: int,
        stability_seconds: int = 10 * 60,
        cooldown_seconds: int = 60 * 60,
    ) -> dict:
        control_key = _identifier(control_key, "control_key")
        for field, value in {
            "timestamp": timestamp,
            "stability_seconds": stability_seconds,
            "cooldown_seconds": cooldown_seconds,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            if row is None:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
            stable_at = int(row["config_stable_at"] or 0) + stability_seconds
            cooldown_at = int(row["last_context_refresh_at"] or 0) + cooldown_seconds
            next_eligible_at = max(stable_at, cooldown_at)
            if timestamp < next_eligible_at:
                return {"allowed": False, "nextEligibleAt": next_eligible_at}
            connection.execute(
                """
                UPDATE processing_controls
                SET last_context_refresh_at = ?, updated_at = ?
                WHERE control_key = ?
                """,
                (timestamp, timestamp, control_key),
            )
        return {"allowed": True, "nextEligibleAt": timestamp + cooldown_seconds}

    def freeze_initial_backfill(
        self,
        control_key: str,
        *,
        source_keys: Sequence[str],
        limit: int,
    ) -> dict:
        control_key = _identifier(control_key, "control_key")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        normalized = []
        for source_key in source_keys:
            source_key = _identifier(source_key, "source_key")
            if source_key not in normalized:
                normalized.append(source_key)
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            if row is None:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
            state = row["initial_backfill_state"]
            frozen = json.loads(row["initial_backfill_sources_json"] or "[]")
            completed = json.loads(row["initial_backfill_completed_json"] or "[]")
            if state == "not_started":
                frozen = normalized[:limit]
                completed = []
                state = "in_progress" if frozen else "completed"
                connection.execute(
                    """
                    UPDATE processing_controls
                    SET initial_backfill_state = ?, initial_backfill_sources_json = ?,
                        initial_backfill_completed_json = ?, updated_at = ?
                    WHERE control_key = ?
                    """,
                    (state, _json(frozen), _json(completed), _now(), control_key),
                )
        return {"state": state, "sourceKeys": frozen, "completedSourceKeys": completed}

    def complete_initial_backfill_source(self, control_key: str, *, source_key: str) -> dict:
        control_key = _identifier(control_key, "control_key")
        source_key = _identifier(source_key, "source_key")
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM processing_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            if row is None:
                raise ValueError("PROCESSING_CONTROL_NOT_FOUND")
            frozen = json.loads(row["initial_backfill_sources_json"] or "[]")
            completed = json.loads(row["initial_backfill_completed_json"] or "[]")
            if source_key not in frozen:
                raise ValueError("SOURCE_NOT_IN_INITIAL_BACKFILL")
            if source_key not in completed:
                completed.append(source_key)
            state = "completed" if set(completed) == set(frozen) else "in_progress"
            connection.execute(
                """
                UPDATE processing_controls
                SET initial_backfill_state = ?, initial_backfill_completed_json = ?, updated_at = ?
                WHERE control_key = ?
                """,
                (state, _json(completed), _now(), control_key),
            )
        return {"state": state, "sourceKeys": frozen, "completedSourceKeys": completed}

    def set_source_revision(self, source_id: str, source_revision: int) -> None:
        if (
            isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision < 1
        ):
            raise ValueError("source_revision must be a positive integer")
        with self._immediate() as connection:
            current = connection.execute(
                "SELECT source_revision FROM source_records WHERE source_id = ?",
                (_identifier(source_id, "source_id"),),
            ).fetchone()
            if current is not None and source_revision < int(current["source_revision"]):
                raise ValueError("SOURCE_REVISION_NOT_MONOTONIC")
            if current is not None and source_revision == int(current["source_revision"]):
                return
            connection.execute(
                """
                INSERT INTO source_records(source_id, source_revision, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    updated_at = excluded.updated_at
                """,
                (source_id, source_revision, _now()),
            )

    def create_item(self, *, context_id: str, unit_type: str, unit_key: str) -> dict:
        if unit_type not in _UNIT_TYPES:
            raise ValueError("invalid unit_type")
        item_id = f"item_{uuid.uuid4().hex}"
        now = _now()
        with self._immediate() as connection:
            connection.execute(
                """
                INSERT INTO items(id, context_id, unit_type, unit_key, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    item_id,
                    _identifier(context_id, "context_id"),
                    unit_type,
                    _identifier(unit_key, "unit_key"),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item_dto(row)

    def set_source_context(
        self,
        *,
        source_id: str,
        context_id: str,
        context_version: int,
        configuration_revision: int,
        authorization_revision: int,
        authorization_valid_until: int,
        accessible: bool,
        billing_owner_id: str = "",
        watch_id: str | None = None,
        item_id: str | None = None,
        processing_status: str = "pending",
        analysis_enabled: bool = False,
        context_stale: bool = False,
        coverage: Mapping[str, object] | None = None,
    ) -> None:
        source_id = _identifier(source_id, "source_id")
        context_id = _identifier(context_id, "context_id")
        revisions = {
            "context_version": context_version,
            "configuration_revision": configuration_revision,
            "authorization_revision": authorization_revision,
        }
        for field, revision in revisions.items():
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError(f"{field} must be a positive integer")
        if (
            isinstance(authorization_valid_until, bool)
            or not isinstance(authorization_valid_until, int)
            or authorization_valid_until < 0
        ):
            raise ValueError("authorization_valid_until must be a non-negative integer")
        if billing_owner_id:
            billing_owner_id = _identifier(billing_owner_id, "billing_owner_id")
        if watch_id is not None:
            watch_id = _identifier(watch_id, "watch_id")
        if item_id is not None:
            item_id = _identifier(item_id, "item_id")
        if processing_status not in {
            "rules_only",
            "pending",
            "processing",
            "assessed",
            "analysis_disabled",
            "paused_quota",
            "provider_unavailable",
            "needs_manual",
            "failed",
            "throttled",
            "not_scheduled",
        }:
            raise ValueError("invalid processing_status")
        coverage_json = _json(coverage or {})
        with self._immediate() as connection:
            source = connection.execute(
                "SELECT 1 FROM source_records WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if source is None:
                raise ValueError("SOURCE_NOT_FOUND")
            current = connection.execute(
                "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                (source_id, context_id),
            ).fetchone()
            if current is not None:
                for field, revision in revisions.items():
                    if revision < int(current[field]):
                        raise ValueError("SOURCE_CONTEXT_REVISION_NOT_MONOTONIC")
            connection.execute(
                """
                INSERT INTO source_contexts(
                    source_id, context_id, billing_owner_id, watch_id, item_id,
                    context_version, configuration_revision, authorization_revision,
                    authorization_valid_until, accessible, processing_status,
                    analysis_enabled, context_stale, coverage_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, context_id) DO UPDATE SET
                    billing_owner_id = CASE
                        WHEN excluded.billing_owner_id = '' THEN source_contexts.billing_owner_id
                        ELSE excluded.billing_owner_id
                    END,
                    watch_id = excluded.watch_id,
                    item_id = excluded.item_id,
                    context_version = excluded.context_version,
                    configuration_revision = excluded.configuration_revision,
                    authorization_revision = excluded.authorization_revision,
                    authorization_valid_until = excluded.authorization_valid_until,
                    accessible = excluded.accessible,
                    processing_status = excluded.processing_status,
                    analysis_enabled = excluded.analysis_enabled,
                    context_stale = excluded.context_stale,
                    coverage_json = excluded.coverage_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    context_id,
                    billing_owner_id,
                    watch_id,
                    item_id,
                    context_version,
                    configuration_revision,
                    authorization_revision,
                    authorization_valid_until,
                    int(bool(accessible)),
                    processing_status,
                    int(bool(analysis_enabled)),
                    int(bool(context_stale)),
                    coverage_json,
                    _now(),
                ),
            )

    def upsert_source_snapshot(
        self,
        *,
        source_id: str,
        source_type: str,
        external_key: str,
        repository_id: str | None,
        content: Mapping[str, object],
        source_facts: Mapping[str, object],
        source_url: str,
        processing_mode: str,
        completeness: str,
        lifecycle: str,
        observed_at: int,
    ) -> dict:
        source_id = _identifier(source_id, "source_id")
        if source_type not in {
            "pr_state",
            "pr_review_body",
            "pr_review_comment",
            "pr_comment",
            "ci_failure",
            "release",
        }:
            raise ValueError("invalid source_type")
        external_key = _identifier(external_key, "external_key")
        if repository_id is not None:
            repository_id = _identifier(repository_id, "repository_id")
        source_url = _identifier(source_url, "source_url")
        if processing_mode not in {"rules_only", "model"}:
            raise ValueError("invalid processing_mode")
        if completeness not in {"complete", "partial", "unavailable"}:
            raise ValueError("invalid completeness")
        if lifecycle not in {"active", "source_closed", "source_deleted", "superseded"}:
            raise ValueError("invalid lifecycle")
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        content_json = _json(content)
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        version_id = f"sv_{hashlib.sha256(f'{source_id}:{content_hash}'.encode('utf-8')).hexdigest()}"
        facts_json = _json(source_facts)
        state_hash = hashlib.sha256(
            _json(
                {
                    "sourceType": source_type,
                    "externalKey": external_key,
                    "repositoryId": repository_id,
                    "contentHash": content_hash,
                    "sourceFacts": source_facts,
                    "sourceUrl": source_url,
                    "processingMode": processing_mode,
                    "completeness": completeness,
                    "lifecycle": lifecycle,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self._immediate() as connection:
            current = connection.execute(
                "SELECT * FROM source_records WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if current is not None and (
                current["source_type"] != source_type or current["external_key"] != external_key
            ):
                raise ValueError("SOURCE_ID_CONFLICT")
            source_revision = 1 if current is None else int(current["source_revision"])
            if current is None or current["state_hash"] != state_hash:
                if current is not None:
                    source_revision += 1
            connection.execute(
                """
                INSERT INTO source_records(
                    source_id, source_revision, source_type, external_key, repository_id,
                    latest_version, state_hash, lifecycle, source_facts_json, source_url,
                    processing_mode, completeness, last_synced_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    repository_id = excluded.repository_id,
                    latest_version = excluded.latest_version,
                    state_hash = excluded.state_hash,
                    lifecycle = excluded.lifecycle,
                    source_facts_json = excluded.source_facts_json,
                    source_url = excluded.source_url,
                    processing_mode = excluded.processing_mode,
                    completeness = excluded.completeness,
                    last_synced_at = excluded.last_synced_at,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    source_revision,
                    source_type,
                    external_key,
                    repository_id,
                    version_id,
                    state_hash,
                    lifecycle,
                    facts_json,
                    source_url,
                    processing_mode,
                    completeness,
                    observed_at,
                    observed_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_versions(
                    id, source_id, content_hash, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (version_id, source_id, content_hash, content_json, observed_at),
            )
            row = connection.execute("SELECT * FROM source_records WHERE source_id = ?", (source_id,)).fetchone()
        return self._source_record_dto(row, contexts=[])

    def next_open_pr_for_reconciliation(self, target: Mapping[str, object]) -> dict | None:
        """Rotate through known open parents; absence from GitHub lists proves nothing."""
        if target.get("module") != "pr":
            raise ValueError("GITHUB_PR_BINDING_INVALID")
        repository_id = _identifier(target.get("repository_id"), "repository_id")
        context_id = _identifier(target.get("context_id"), "context_id")
        with self._read() as connection:
            row = connection.execute(
                """SELECT sr.source_id, json_extract(sr.source_facts_json,'$.pullNumber') AS number
                   FROM source_records sr
                   JOIN source_contexts sc ON sc.source_id=sr.source_id
                   LEFT JOIN pr_parent_checks pc ON pc.source_id=sr.source_id
                   WHERE sr.repository_id=? AND sr.source_type='pr_state'
                     AND sr.lifecycle='active' AND sc.context_id=? AND sc.accessible=1
                     AND sc.authorization_valid_until>=?
                     AND json_type(sr.source_facts_json,'$.pullNumber')='integer'
                     AND json_extract(sr.source_facts_json,'$.pullNumber')>0
                   ORDER BY pc.checked_at IS NOT NULL, pc.checked_at, sr.source_id LIMIT 1""",
                (repository_id, context_id, _now()),
            ).fetchone()
        return {"sourceId": row["source_id"], "pullNumber": int(row["number"])} if row else None

    def mark_pr_reconciled(self, source_id: str, *, observed_at: int) -> None:
        source_id = _identifier(source_id, "source_id")
        if type(observed_at) is not int or observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        with self._immediate() as connection:
            row = connection.execute("SELECT source_type FROM source_records WHERE source_id=?", (source_id,)).fetchone()
            if row is None or row["source_type"] != "pr_state":
                raise ValueError("PR_RECONCILIATION_BINDING_MISMATCH")
            connection.execute(
                """INSERT INTO pr_parent_checks(source_id, checked_at) VALUES (?,?)
                   ON CONFLICT(source_id) DO UPDATE SET checked_at=excluded.checked_at""",
                (source_id, observed_at),
            )

    def list_sources_for_billing_owner(self, billing_owner_id: str, *, include_content: bool = False, source_id: str | None = None) -> list[dict]:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        timestamp = _now()
        with self._read() as connection:
            if self._connection is None:
                connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT sr.*, sc.context_id, sc.watch_id, sc.item_id,
                       uw.target_repository_id,
                       sc.context_version, sc.processing_status, sc.analysis_enabled,
                       sc.context_stale, sc.coverage_json, CASE WHEN ? THEN sv.content_json END AS content_json
                FROM source_records sr
                JOIN source_versions sv ON sv.id = sr.latest_version
                JOIN source_contexts sc ON sc.source_id = sr.source_id
                LEFT JOIN update_watches uw ON uw.id = sc.watch_id
                WHERE sc.billing_owner_id = ? AND sc.accessible = 1
                  AND (sc.watch_id IS NULL OR (uw.id IS NOT NULL AND uw.archived_at IS NULL))
                  AND (? IS NULL OR sr.source_id = ?)
                  AND sc.authorization_valid_until >= ?
                ORDER BY sr.updated_at DESC, sr.source_id, sc.context_id
                """,
                (include_content, owner_id, source_id, source_id, timestamp),
            ).fetchall()
            grouped: dict[str, tuple[sqlite3.Row, list[dict]]] = {}
            for row in rows:
                entry = grouped.setdefault(row["source_id"], (row, []))
                context = self._source_context_dto(row)
                publication = connection.execute(
                    "SELECT * FROM source_assessment_publications WHERE source_id=? AND context_id=?",
                    (row["source_id"], row["context_id"]),
                ).fetchone()
                current = publication is not None and not context["contextStale"]
                if current:
                    dependencies = json.loads(publication["sources_json"])
                    fences = json.loads(publication["fences_json"])
                    dependency_ids = [source.get("sourceId") for source in dependencies]
                    fence_ids = [fence.get("sourceId") for fence in fences]
                    if (not dependencies or len(dependencies) != len(fences)
                            or len(set(dependency_ids)) != len(dependencies)
                            or set(dependency_ids) != set(fence_ids)
                            or row["source_id"] not in dependency_ids
                            or not any(fence.get("sourceId") == row["source_id"]
                                       and fence.get("contextId") == row["context_id"]
                                       for fence in fences)):
                        current = False
                    for source in dependencies:
                        stored = connection.execute(
                            "SELECT latest_version, source_revision FROM source_records WHERE source_id=?",
                            (source["sourceId"],),
                        ).fetchone()
                        if (stored is None or stored["latest_version"] != source["sourceVersion"]
                                or stored["source_revision"] != source["sourceRevision"]):
                            current = False
                    for fence in fences:
                        stored = connection.execute(
                            """SELECT sc.*,CASE WHEN sc.watch_id IS NULL THEN 1
                                 WHEN w.id IS NOT NULL AND w.archived_at IS NULL THEN 1
                                 ELSE 0 END AS watch_active
                               FROM source_contexts sc
                               LEFT JOIN update_watches w ON w.id=sc.watch_id
                               WHERE sc.source_id=? AND sc.context_id=?""",
                            (fence["sourceId"], fence["contextId"]),
                        ).fetchone()
                        if (stored is None or not stored["accessible"] or stored["context_stale"]
                                or not stored["watch_active"]
                                or stored["authorization_valid_until"] < timestamp
                                or stored["billing_owner_id"] != owner_id
                                or stored["context_version"] != fence["contextVersion"]
                                or stored["authorization_revision"] != fence["authorizationRevision"]):
                            current = False
                public_assessment = json.loads(publication["assessment_json"]) if current else None
                if current:
                    context["coverage"] = json.loads(publication["coverage_json"])
                if row["source_type"] == "release":
                    projection = project_saved_updates(public_assessment, context["coverage"]) if public_assessment else None
                    context["relevance"] = projection["relevance"] if projection else None
                    context["updateSignals"] = projection["updateSignals"] if projection else {}
                if include_content:
                    context["assessments"] = [json.loads(publication["assessment_json"])] if current else []
                    context["evidence"] = json.loads(publication["evidence_json"]) if current else []
                entry[1].append(context)
        result = []
        for row, contexts in grouped.values():
            source = self._source_record_dto(row, contexts)
            if include_content:
                source["content"] = json.loads(row["content_json"])
            result.append(source)
        return result

    def list_items_for_billing_owner(self, billing_owner_id: str, *, include_history: bool = False, item_id: str | None = None) -> list[dict]:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        timestamp = _now()
        with self._read() as connection:
            if self._connection is None:
                connection.execute("BEGIN")  # Item, authority and history share one read snapshot.
            rows = connection.execute(
                """
                SELECT i.*, iv.sources_json, iv.context_fences_json,
                       iv.snapshot_json, iv.observed_at
                FROM items i
                JOIN item_versions iv
                  ON iv.item_id = i.id AND iv.item_version = i.current_item_version
                WHERE EXISTS (
                    SELECT 1 FROM source_contexts sc
                    WHERE sc.context_id = i.context_id AND sc.billing_owner_id = ?
                      AND sc.accessible = 1 AND sc.authorization_valid_until >= ?
                      AND (sc.watch_id IS NULL OR EXISTS (
                          SELECT 1 FROM update_watches w
                          WHERE w.id=sc.watch_id AND w.archived_at IS NULL))
                )
                AND (? IS NULL OR i.id = ?)
                ORDER BY i.updated_at DESC, i.id
                """,
                (owner_id, timestamp, item_id, item_id),
            ).fetchall()
            readable_sources = connection.execute(
                    """
                    SELECT sc.source_id, sc.context_id, sc.billing_owner_id,
                           sc.accessible, sc.authorization_valid_until,
                           sc.context_stale, sc.context_version,
                           sc.configuration_revision, sc.authorization_revision,
                           sr.latest_version, sr.source_revision, sr.last_synced_at
                    FROM source_contexts sc JOIN source_records sr ON sr.source_id=sc.source_id
                    LEFT JOIN update_watches w ON w.id=sc.watch_id
                    WHERE sc.billing_owner_id = ? AND sc.accessible = 1
                      AND sc.authorization_valid_until >= ?
                      AND (sc.watch_id IS NULL OR (w.id IS NOT NULL AND w.archived_at IS NULL))
                    """,
                    (owner_id, timestamp),
                ).fetchall()
            context_rows = {(row["source_id"], row["context_id"]): row for row in readable_sources}
            sync_times = {key: row["last_synced_at"] for key, row in context_rows.items()}
            allowed_pairs = set(sync_times)
            handling_rows = connection.execute(
                """
                SELECT h.* FROM item_handling_events h
                JOIN (
                    SELECT item_id, MAX(rowid) AS ordering
                    FROM item_handling_events GROUP BY item_id
                ) latest ON latest.item_id = h.item_id
                  AND h.rowid = latest.ordering
                """ if not include_history else
                "SELECT * FROM item_handling_events WHERE (? IS NULL OR item_id=?) ORDER BY rowid",
                (item_id, item_id) if include_history else (),
            ).fetchall()
        handling = {row["item_id"]: row for row in handling_rows}
        history: dict[str, list[dict]] = {}
        if include_history:
            for event in handling_rows:
                history.setdefault(event["item_id"], []).append(self._handling_event_dto(event))
        result: list[dict] = []
        for row in rows:
            sources = json.loads(row["sources_json"])
            if any((source["sourceId"], row["context_id"]) not in allowed_pairs for source in sources):
                continue
            if not item_dependencies_current(sources, json.loads(row["context_fences_json"]),
                                             context_rows, context_id=row["context_id"],
                                             owner_id=owner_id, now=timestamp):
                continue
            snapshot = json.loads(row["snapshot_json"])
            event = handling.get(row["id"])
            item = self._item_read_dto(row, sources, snapshot, event)
            if include_history:
                item["handlingHistory"] = history.get(row["id"], [])
            synced = [sync_times[(source["sourceId"], row["context_id"])] for source in sources]
            if synced and all(value is not None for value in synced):
                item["lastSyncedAt"] = _iso_timestamp(min(synced))
            result.append(item)
        return result

    def publish_item_snapshot(
        self,
        *,
        item_id: str,
        expected_item_revision: int,
        sources: Sequence[Mapping[str, object]],
        snapshot: Mapping[str, object],
        context_fences: Sequence[Mapping[str, object]] = (),
        observed_at: int | None = None,
    ) -> dict:
        canonical_sources = sorted(
            (
                {
                    "sourceId": _identifier(source.get("sourceId"), "sourceId"),
                    "sourceVersion": _identifier(source.get("sourceVersion"), "sourceVersion"),
                    "sourceRevision": source.get("sourceRevision"),
                }
                for source in sources
            ),
            key=lambda source: source["sourceId"],
        )
        if not canonical_sources:
            raise ValueError("sources must not be empty")
        for source in canonical_sources:
            revision = source["sourceRevision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError("sourceRevision must be a positive integer")
        canonical_fences = sorted(
            (
                {
                    "sourceId": _identifier(fence.get("sourceId"), "sourceId"),
                    "contextId": _identifier(fence.get("contextId"), "contextId"),
                    "contextVersion": fence.get("contextVersion"),
                    "configurationRevision": fence.get("configurationRevision"),
                    "authorizationRevision": fence.get("authorizationRevision"),
                }
                for fence in context_fences
            ),
            key=lambda fence: (fence["sourceId"], fence["contextId"]),
        )
        source_ids = {source["sourceId"] for source in canonical_sources}
        for fence in canonical_fences:
            if fence["sourceId"] not in source_ids:
                raise ValueError("context fence must reference a snapshot source")
            for field in ("contextVersion", "configurationRevision", "authorizationRevision"):
                value = fence[field]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"{field} must be a positive integer")
        snapshot_json = _json(snapshot)
        sources_json = _json(canonical_sources)
        fences_json = _json(canonical_fences)
        snapshot_hash = hashlib.sha256(
            f"{sources_json}\n{fences_json}\n{snapshot_json}".encode("utf-8")
        ).hexdigest()
        now = _now() if observed_at is None else observed_at
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("observed_at must be a non-negative integer")
        with self._immediate() as connection:
            item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if item is None:
                raise ValueError("NOT_FOUND")
            if int(item["revision"]) != expected_item_revision:
                raise ValueError("REVISION_MISMATCH")
            for source in canonical_sources:
                current = connection.execute(
                    "SELECT source_revision FROM source_records WHERE source_id = ?",
                    (source["sourceId"],),
                ).fetchone()
                if current is None or int(current["source_revision"]) != source["sourceRevision"]:
                    raise ValueError("STALE_SOURCE")
            for fence in canonical_fences:
                current = connection.execute(
                    "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                    (fence["sourceId"], fence["contextId"]),
                ).fetchone()
                if current is None:
                    raise ValueError("STALE_AUTHORIZATION")
                if (
                    int(current["authorization_revision"]) != fence["authorizationRevision"]
                    or not bool(current["accessible"])
                    or int(current["authorization_valid_until"]) < now
                ):
                    raise ValueError("STALE_AUTHORIZATION")
                if (
                    int(current["context_version"]) != fence["contextVersion"]
                    or int(current["configuration_revision"]) != fence["configurationRevision"]
                ):
                    raise ValueError("STALE_CONTEXT")
            if item["current_snapshot_hash"] == snapshot_hash:
                return self._item_dto(item)
            item_version = int(item["current_item_version"]) + 1
            connection.execute(
                """
                INSERT INTO item_versions(
                    item_id, item_version, snapshot_hash, sources_json,
                    context_fences_json, snapshot_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, item_version, snapshot_hash, sources_json, fences_json, snapshot_json, now),
            )
            cursor = connection.execute(
                """
                UPDATE items
                SET current_item_version = ?, current_snapshot_hash = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (item_version, snapshot_hash, now, item_id, expected_item_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("REVISION_MISMATCH")
            updated = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item_dto(updated)

    def store_assessment(
        self,
        *,
        billing_owner_id: str,
        source_version_id: str,
        context_hash: str,
        evaluated_context_version: int,
        question_version: str,
        extractor_version: str,
        model: str,
        input_hash: str,
        dependencies: Sequence[Mapping[str, object]],
        answers: Mapping[str, object],
        usage: Mapping[str, object],
        status: str,
    ) -> dict:
        values = {
            "billing_owner_id": _identifier(billing_owner_id, "billing_owner_id"),
            "source_version_id": _identifier(source_version_id, "source_version_id"),
            "context_hash": _identifier(context_hash, "context_hash"),
            "question_version": _identifier(question_version, "question_version"),
            "extractor_version": _identifier(extractor_version, "extractor_version"),
            "model": _identifier(model, "model"),
            "input_hash": _identifier(input_hash, "input_hash"),
        }
        if (
            isinstance(evaluated_context_version, bool)
            or not isinstance(evaluated_context_version, int)
            or evaluated_context_version < 1
        ):
            raise ValueError("evaluated_context_version must be a positive integer")
        if status not in {"succeeded", "discarded", "failed"}:
            raise ValueError("invalid assessment status")
        dependencies_json = _json(list(dependencies))
        answers_json = _json(answers)
        usage_json = _json(usage)
        with self._immediate() as connection:
            if status == "succeeded":
                existing = connection.execute(
                    """
                    SELECT * FROM assessments
                    WHERE billing_owner_id = ? AND source_version_id = ? AND context_hash = ?
                      AND question_version = ? AND extractor_version = ? AND model = ?
                      AND input_hash = ? AND status = 'succeeded'
                    """,
                    (
                        values["billing_owner_id"],
                        values["source_version_id"],
                        values["context_hash"],
                        values["question_version"],
                        values["extractor_version"],
                        values["model"],
                        values["input_hash"],
                    ),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["dependencies_json"] != dependencies_json
                        or existing["answers_json"] != answers_json
                        or existing["usage_json"] != usage_json
                    ):
                        raise ValueError("ASSESSMENT_CACHE_CONFLICT")
                    return self._assessment_dto(existing)
            assessment_id = f"assessment_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO assessments(
                    id, billing_owner_id, source_version_id, context_hash,
                    evaluated_context_version, question_version, extractor_version,
                    model, input_hash, dependencies_json, answers_json, usage_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    values["billing_owner_id"],
                    values["source_version_id"],
                    values["context_hash"],
                    evaluated_context_version,
                    values["question_version"],
                    values["extractor_version"],
                    values["model"],
                    values["input_hash"],
                    dependencies_json,
                    answers_json,
                    usage_json,
                    status,
                    _now(),
                ),
            )
            row = connection.execute("SELECT * FROM assessments WHERE id = ?", (assessment_id,)).fetchone()
        return self._assessment_dto(row)

    def find_reusable_assessment(
        self,
        *,
        billing_owner_id: str,
        source_version_id: str,
        context_hash: str,
        question_version: str,
        extractor_version: str,
        model: str,
        input_hash: str,
        publish_context_version: int,
    ) -> dict | None:
        if (
            isinstance(publish_context_version, bool)
            or not isinstance(publish_context_version, int)
            or publish_context_version < 1
        ):
            raise ValueError("publish_context_version must be a positive integer")
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM assessments
                WHERE billing_owner_id = ? AND source_version_id = ? AND context_hash = ?
                  AND question_version = ? AND extractor_version = ? AND model = ?
                  AND input_hash = ? AND status = 'succeeded'
                """,
                (
                    _identifier(billing_owner_id, "billing_owner_id"),
                    _identifier(source_version_id, "source_version_id"),
                    _identifier(context_hash, "context_hash"),
                    _identifier(question_version, "question_version"),
                    _identifier(extractor_version, "extractor_version"),
                    _identifier(model, "model"),
                    _identifier(input_hash, "input_hash"),
                ),
            ).fetchone()
        if row is None:
            return None
        result = self._assessment_dto(row)
        result["publishContextVersion"] = publish_context_version
        return result

    def publish_assessment_result(
        self,
        *,
        reservation_id: str,
        assessment: Mapping[str, object],
        item_id: str | None,
        expected_item_revision: int | None,
        sources: Sequence[Mapping[str, object]],
        context_fences: Sequence[Mapping[str, object]],
        snapshot: Mapping[str, object],
        observed_at: int,
        job_id: str | None = None,
        claim_token: str | None = None,
    ) -> dict:
        reservation_id = _identifier(reservation_id, "reservation_id")
        if item_id is not None:
            item_id = _identifier(item_id, "item_id")
        elif expected_item_revision is not None or snapshot.get("actionTypes"):
            raise ValueError("SOURCE_ONLY_PUBLICATION_HAS_ITEM_ACTION")
        if item_id is not None and (
            isinstance(expected_item_revision, bool)
            or not isinstance(expected_item_revision, int)
            or expected_item_revision < 1
        ):
            raise ValueError("expected_item_revision must be a positive integer")
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        assessment_fields = {
            "billingOwnerId": _identifier(assessment.get("billingOwnerId"), "billingOwnerId"),
            "sourceVersionId": _identifier(assessment.get("sourceVersionId"), "sourceVersionId"),
            "contextHash": _identifier(assessment.get("contextHash"), "contextHash"),
            "questionVersion": _identifier(assessment.get("questionVersion"), "questionVersion"),
            "extractorVersion": _identifier(assessment.get("extractorVersion"), "extractorVersion"),
            "model": _identifier(assessment.get("model"), "model"),
            "inputHash": _identifier(assessment.get("inputHash"), "inputHash"),
        }
        if assessment_fields["model"] != "jev-1.13.0":
            raise ValueError("ASSESSMENT_MODEL_MISMATCH")
        evaluated_context_version = assessment.get("evaluatedContextVersion")
        if (
            isinstance(evaluated_context_version, bool)
            or not isinstance(evaluated_context_version, int)
            or evaluated_context_version < 1
        ):
            raise ValueError("evaluatedContextVersion must be a positive integer")
        dependencies = assessment.get("dependencies")
        bindings = assessment.get("bindings")
        answers = assessment.get("answers")
        usage = assessment.get("usage")
        if (
            not isinstance(dependencies, list)
            or not isinstance(bindings, Mapping)
            or not isinstance(answers, Mapping)
            or not isinstance(usage, Mapping)
        ):
            raise ValueError("assessment dependencies, bindings, answers and usage are invalid")
        answer_keys = {_identifier(key, "answer question id") for key in answers}
        if len(answer_keys) != len(answers):
            raise ValueError("assessment answer question ids must be unique")
        canonical_bindings: dict[str, dict] = {}
        for raw_question_id, raw_binding in bindings.items():
            question_id = _identifier(raw_question_id, "binding question id")
            if question_id in canonical_bindings or not isinstance(raw_binding, Mapping):
                raise ValueError("assessment question binding is invalid")
            evidence_ids = raw_binding.get("evidenceIds")
            if not isinstance(evidence_ids, list):
                raise ValueError("assessment binding evidenceIds must be an array")
            normalized_evidence_ids = [
                _identifier(evidence_id, "binding evidence id") for evidence_id in evidence_ids
            ]
            if len(normalized_evidence_ids) != len(set(normalized_evidence_ids)):
                raise ValueError("assessment binding evidenceIds must be unique")
            binding = dict(raw_binding)
            binding["evidenceIds"] = normalized_evidence_ids
            for field in ("sourceId", "sourceVersion", "segmentAnchor", "windowId", "changeUnitId"):
                if field in binding:
                    binding[field] = _identifier(binding[field], f"binding {field}")
            if "contextVersion" in binding:
                value = binding["contextVersion"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("binding contextVersion must be a positive integer")
            canonical_bindings[question_id] = binding
        if set(canonical_bindings) != answer_keys:
            raise ValueError("assessment bindings must match answer question ids")
        canonical_sources = sorted(
            (
                {
                    "sourceId": _identifier(source.get("sourceId"), "sourceId"),
                    "sourceVersion": _identifier(source.get("sourceVersion"), "sourceVersion"),
                    "sourceRevision": source.get("sourceRevision"),
                }
                for source in sources
            ),
            key=lambda source: source["sourceId"],
        )
        if not canonical_sources:
            raise ValueError("sources must not be empty")
        for source in canonical_sources:
            revision = source["sourceRevision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError("sourceRevision must be a positive integer")
        source_ids = {source["sourceId"] for source in canonical_sources}
        canonical_fences = sorted(
            (
                {
                    "sourceId": _identifier(fence.get("sourceId"), "sourceId"),
                    "contextId": _identifier(fence.get("contextId"), "contextId"),
                    "contextVersion": fence.get("contextVersion"),
                    "configurationRevision": fence.get("configurationRevision"),
                    "authorizationRevision": fence.get("authorizationRevision"),
                }
                for fence in context_fences
            ),
            key=lambda fence: (fence["sourceId"], fence["contextId"]),
        )
        for fence in canonical_fences:
            if fence["sourceId"] not in source_ids:
                raise ValueError("context fence must reference a snapshot source")
            for field in ("contextVersion", "configurationRevision", "authorizationRevision"):
                value = fence[field]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"{field} must be a positive integer")
        snapshot_evidence = snapshot.get("evidence") or []
        if not isinstance(snapshot_evidence, list):
            raise ValueError("snapshot evidence must be an array")
        snapshot_evidence_ids = {
            _identifier(evidence.get("id"), "evidence id")
            for evidence in snapshot_evidence
            if isinstance(evidence, Mapping)
        }
        bound_evidence_ids = sorted(
            {
                evidence_id
                for binding in canonical_bindings.values()
                for evidence_id in binding["evidenceIds"]
            }
        )
        if not set(bound_evidence_ids).issubset(snapshot_evidence_ids):
            raise ValueError("ASSESSMENT_EVIDENCE_BINDING_MISMATCH")
        dependencies_json = _json(dependencies)
        answers_json = _json(answers)
        usage_json = _json(usage)
        if (job_id is None) != (claim_token is None):
            raise ValueError("job_id and claim_token must be provided together")
        if job_id is not None:
            job_id = _identifier(job_id, "job_id")
            claim_token = _identifier(claim_token, "claim_token")
        with self._immediate() as connection:
            claimed_job = None
            semantic_thread = False
            if assessment_fields["questionVersion"] == "pr-followup/v3":
                primary = connection.execute(
                    "SELECT source_type FROM source_records WHERE latest_version=? AND source_id IN (%s)"
                    % ",".join("?" for _ in canonical_sources),
                    (assessment_fields["sourceVersionId"], *(s["sourceId"] for s in canonical_sources)),
                ).fetchone()
                semantic_thread = primary is not None and primary["source_type"] == "pr_review_comment"
                if semantic_thread and any(dependency not in canonical_sources for dependency in dependencies):
                    raise ValueError("ASSESSMENT_DEPENDENCY_BINDING_MISMATCH")
            if job_id is not None:
                claimed_job = connection.execute(
                    "SELECT * FROM background_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if (
                    claimed_job is None
                    or claimed_job["job_type"] != "analyze_source"
                    or claimed_job["state"] != "running"
                    or claimed_job["claim_token"] != claim_token
                ):
                    raise ValueError("JOB_CLAIM_LOST")
                if (
                    claimed_job["claimed_until"] is None
                    or int(claimed_job["claimed_until"]) <= observed_at
                ):
                    raise ValueError("JOB_CLAIM_EXPIRED")
                if claimed_job["reservation_id"] != reservation_id:
                    raise ValueError("JOB_RESERVATION_MISMATCH")
                current_source = connection.execute(
                    "SELECT * FROM source_records WHERE source_id = ?",
                    (claimed_job["source_id"],),
                ).fetchone()
                current_context = connection.execute(
                    "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                    (claimed_job["source_id"], claimed_job["context_id"]),
                ).fetchone()
                if (
                    current_source is None
                    or int(current_source["source_revision"]) != int(claimed_job["source_revision"])
                    or current_source["latest_version"] != claimed_job["source_version_id"]
                ):
                    raise ValueError("STALE_SOURCE")
                if current_context is None:
                    raise ValueError("STALE_AUTHORIZATION")
                if (
                    not bool(current_context["accessible"])
                    or int(current_context["authorization_valid_until"]) < observed_at
                    or int(current_context["authorization_revision"])
                    != int(claimed_job["authorization_revision"])
                ):
                    raise ValueError("STALE_AUTHORIZATION")
                if not bool(current_context["analysis_enabled"]):
                    raise ValueError("ANALYSIS_DISABLED")
                if (
                    int(current_context["context_version"]) != int(claimed_job["context_version"])
                    or int(current_context["configuration_revision"])
                    != int(claimed_job["configuration_revision"])
                ):
                    raise ValueError("STALE_CONTEXT")
                if current_context["item_id"] != item_id and not semantic_thread:
                    raise ValueError("STALE_CONTEXT")
                primary_source = next(
                    (source for source in canonical_sources if source["sourceId"] == claimed_job["source_id"]),
                    None,
                )
                if (
                    primary_source is None
                    or primary_source["sourceVersion"] != claimed_job["source_version_id"]
                    or int(primary_source["sourceRevision"]) != int(claimed_job["source_revision"])
                    or assessment_fields["sourceVersionId"] != claimed_job["source_version_id"]
                ):
                    raise ValueError("JOB_SOURCE_BINDING_MISMATCH")
                primary_fence = next(
                    (
                        fence
                        for fence in canonical_fences
                        if fence["sourceId"] == claimed_job["source_id"]
                        and fence["contextId"] == claimed_job["context_id"]
                    ),
                    None,
                )
                if (
                    primary_fence is None
                    or int(primary_fence["contextVersion"]) != int(claimed_job["context_version"])
                    or int(primary_fence["configurationRevision"])
                    != int(claimed_job["configuration_revision"])
                    or int(primary_fence["authorizationRevision"])
                    != int(claimed_job["authorization_revision"])
                ):
                    raise ValueError("JOB_CONTEXT_BINDING_MISMATCH")
            ledger = connection.execute(
                "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if ledger is None or ledger["state"] != "reserved":
                raise ValueError("RESERVATION_NOT_ACTIVE")
            if ledger["billing_owner_id"] != assessment_fields["billingOwnerId"]:
                raise ValueError("RESERVATION_OWNER_MISMATCH")
            if snapshot.get("module") != ledger["module"]:
                raise ValueError("RESERVATION_MODULE_MISMATCH")
            item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if item_id is not None and item is None:
                raise ValueError("NOT_FOUND")
            if item is not None and int(item["revision"]) != expected_item_revision:
                raise ValueError("REVISION_MISMATCH")
            if ({fence["sourceId"] for fence in canonical_fences} != source_ids
                    or len({fence["contextId"] for fence in canonical_fences}) != 1):
                raise ValueError("ASSESSMENT_CONTEXT_BINDING_MISMATCH")
            for source in canonical_sources:
                current = connection.execute(
                    "SELECT * FROM source_records WHERE source_id = ?",
                    (source["sourceId"],),
                ).fetchone()
                if (
                    current is None
                    or int(current["source_revision"]) != source["sourceRevision"]
                    or current["latest_version"] != source["sourceVersion"]
                ):
                    raise ValueError("STALE_SOURCE")
            for fence in canonical_fences:
                current = connection.execute(
                    "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                    (fence["sourceId"], fence["contextId"]),
                ).fetchone()
                if current is None:
                    raise ValueError("STALE_AUTHORIZATION")
                if current["billing_owner_id"] != ledger["billing_owner_id"]:
                    raise ValueError("RESERVATION_OWNER_MISMATCH")
                if not bool(current["analysis_enabled"]):
                    raise ValueError("ANALYSIS_DISABLED")
                if item_id is None and current["item_id"] is not None and not semantic_thread:
                    raise ValueError("STALE_CONTEXT")
                if (
                    int(current["authorization_revision"]) != fence["authorizationRevision"]
                    or not bool(current["accessible"])
                    or int(current["authorization_valid_until"]) < observed_at
                ):
                    raise ValueError("STALE_AUTHORIZATION")
                if (
                    int(current["context_version"]) != fence["contextVersion"]
                    or int(current["configuration_revision"]) != fence["configurationRevision"]
                ):
                    raise ValueError("STALE_CONTEXT")
            existing_assessment = connection.execute(
                """
                SELECT * FROM assessments
                WHERE billing_owner_id = ? AND source_version_id = ? AND context_hash = ?
                  AND question_version = ? AND extractor_version = ? AND model = ?
                  AND input_hash = ? AND status = 'succeeded'
                """,
                (
                    assessment_fields["billingOwnerId"],
                    assessment_fields["sourceVersionId"],
                    assessment_fields["contextHash"],
                    assessment_fields["questionVersion"],
                    assessment_fields["extractorVersion"],
                    assessment_fields["model"],
                    assessment_fields["inputHash"],
                ),
            ).fetchone()
            if existing_assessment is None:
                assessment_id = f"assessment_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO assessments(
                        id, billing_owner_id, source_version_id, context_hash,
                        evaluated_context_version, question_version, extractor_version,
                        model, input_hash, dependencies_json, answers_json, usage_json,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?)
                    """,
                    (
                        assessment_id,
                        assessment_fields["billingOwnerId"],
                        assessment_fields["sourceVersionId"],
                        assessment_fields["contextHash"],
                        evaluated_context_version,
                        assessment_fields["questionVersion"],
                        assessment_fields["extractorVersion"],
                        assessment_fields["model"],
                        assessment_fields["inputHash"],
                        dependencies_json,
                        answers_json,
                        usage_json,
                        observed_at,
                    ),
                )
                assessment_row = connection.execute(
                    "SELECT * FROM assessments WHERE id = ?",
                    (assessment_id,),
                ).fetchone()
            else:
                if (
                    existing_assessment["dependencies_json"] != dependencies_json
                    or existing_assessment["answers_json"] != answers_json
                    or existing_assessment["usage_json"] != usage_json
                ):
                    raise ValueError("ASSESSMENT_CACHE_CONFLICT")
                assessment_row = existing_assessment
            assessment_dto = self._assessment_dto(assessment_row)
            primary_source = next(
                (
                    source
                    for source in canonical_sources
                    if source["sourceVersion"] == assessment_dto["sourceVersionId"]
                ),
                None,
            )
            if primary_source is None:
                raise ValueError("ASSESSMENT_SOURCE_BINDING_MISMATCH")
            primary_fence = next(
                (
                    fence
                    for fence in canonical_fences
                    if fence["sourceId"] == primary_source["sourceId"]
                ),
                None,
            )
            if primary_fence is None:
                raise ValueError("ASSESSMENT_CONTEXT_BINDING_MISMATCH")
            public_assessment = {
                "id": assessment_dto["id"],
                "sourceId": primary_source["sourceId"],
                "sourceVersion": assessment_dto["sourceVersionId"],
                "evidenceIds": bound_evidence_ids,
                "questionVersion": assessment_dto["questionVersion"],
                "extractorVersion": assessment_dto["extractorVersion"],
                "model": assessment_dto["model"],
                "contextHash": assessment_dto["contextHash"],
                "evaluatedContextVersion": assessment_dto["evaluatedContextVersion"],
                "publishContextVersion": primary_fence["contextVersion"],
                "dependencies": assessment_dto["dependencies"],
                "bindings": canonical_bindings,
                "answers": assessment_dto["answers"],
                "usage": assessment_dto["usage"],
                "status": assessment_dto["status"],
            }
            connection.execute(
                """INSERT INTO source_assessment_publications(
                    source_id, context_id, source_version_id, context_version,
                    authorization_revision, billing_owner_id, assessment_json, evidence_json,
                    sources_json, fences_json, coverage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, context_id) DO UPDATE SET
                    source_version_id=excluded.source_version_id,
                    context_version=excluded.context_version,
                    authorization_revision=excluded.authorization_revision,
                    billing_owner_id=excluded.billing_owner_id,
                    assessment_json=excluded.assessment_json, evidence_json=excluded.evidence_json,
                    sources_json=excluded.sources_json, fences_json=excluded.fences_json,
                    coverage_json=excluded.coverage_json""",
                (primary_source["sourceId"], primary_fence["contextId"],
                 primary_source["sourceVersion"], primary_fence["contextVersion"],
                 primary_fence["authorizationRevision"], ledger["billing_owner_id"],
                 _json(public_assessment), _json(snapshot_evidence),
                 _json(canonical_sources), _json(canonical_fences),
                 connection.execute("SELECT coverage_json FROM source_contexts WHERE source_id=? AND context_id=?",
                                    (primary_source["sourceId"], primary_fence["contextId"])).fetchone()[0]),
            )
            connection.execute(
                """UPDATE source_contexts SET processing_status='assessed', updated_at=?
                   WHERE source_id=? AND context_id=?""",
                (observed_at, primary_source["sourceId"], primary_fence["contextId"]),
            )
            if item is not None and not semantic_thread:
                next_snapshot = dict(snapshot)
                next_snapshot["assessments"] = [public_assessment]
                sources_json = _json(canonical_sources)
                fences_json = _json(canonical_fences)
                snapshot_json = _json(next_snapshot)
                snapshot_hash = hashlib.sha256(
                    f"{sources_json}\n{fences_json}\n{snapshot_json}".encode("utf-8")
                ).hexdigest()
                item_version = int(item["current_item_version"]) + 1
                connection.execute(
                    """
                    INSERT INTO item_versions(
                        item_id, item_version, snapshot_hash, sources_json,
                        context_fences_json, snapshot_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (item_id, item_version, snapshot_hash, sources_json, fences_json, snapshot_json, observed_at),
                )
                cursor = connection.execute(
                    """
                    UPDATE items SET current_item_version = ?, current_snapshot_hash = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (item_version, snapshot_hash, observed_at, item_id, expected_item_revision),
                )
                if cursor.rowcount != 1:
                    raise ValueError("REVISION_MISMATCH")
                if existing_assessment is not None:
                    previous_handling = connection.execute(
                        """
                        SELECT * FROM item_handling_events
                        WHERE item_id = ? ORDER BY rowid DESC LIMIT 1
                        """,
                        (item_id,),
                    ).fetchone()
                    if previous_handling is not None and (
                        previous_handling["disposition"] != "open"
                        or previous_handling["assignee_id"] is not None
                        or previous_handling["note"] is not None
                        or previous_handling["feedback"] is not None
                    ):
                        connection.execute(
                            """
                            INSERT INTO item_handling_events(
                                id, item_id, item_version, actor_id, disposition,
                                assignee_id, note, feedback, event_kind,
                                carried_from_item_version, created_at
                            ) VALUES (?, ?, ?, 'system', 'open', NULL, NULL, NULL,
                                      'assessment_rebound', NULL, ?)
                            """,
                            (f"handling_{uuid.uuid4().hex}", item_id, item_version, observed_at),
                        )
            if semantic_thread:
                from .pr_followup import reconcile_thread
                projected = reconcile_thread(ProductStore(self.database_path, _connection=connection),
                    source_id=primary_source["sourceId"], context_id=primary_fence["contextId"], now=observed_at)
                if projected is not None:
                    item_id = projected["id"]
            bucket = connection.execute(
                """
                UPDATE processing_usage_buckets
                SET reserved = reserved - 1, used = used + 1, updated_at = ?
                WHERE billing_owner_id = ? AND period = ?
                  AND metric = 'intelligent_processing' AND reserved >= 1
                """,
                (observed_at, ledger["billing_owner_id"], ledger["period"]),
            )
            if bucket.rowcount != 1:
                raise RuntimeError("processing reservation bucket is inconsistent")
            consumed = connection.execute(
                """
                UPDATE processing_usage_ledger
                SET state = 'consumed', finished_at = ?
                WHERE reservation_id = ? AND state = 'reserved'
                """,
                (observed_at, reservation_id),
            )
            if consumed.rowcount != 1:
                raise RuntimeError("processing reservation ledger is inconsistent")
            if claimed_job is not None:
                completed = connection.execute(
                    """
                    UPDATE background_jobs
                    SET state = 'succeeded', claim_token = NULL, claimed_until = NULL,
                        updated_at = ?
                    WHERE id = ? AND state = 'running' AND claim_token = ?
                    """,
                    (observed_at, job_id, claim_token),
                )
                if completed.rowcount != 1:
                    raise ValueError("JOB_CLAIM_LOST")
                connection.execute(
                    """
                    UPDATE source_contexts SET processing_status = 'assessed', updated_at = ?
                    WHERE source_id = ? AND context_id = ?
                    """,
                    (observed_at, claimed_job["source_id"], claimed_job["context_id"]),
                )
            updated_item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return {"assessment": public_assessment, "item": self._item_dto(updated_item) if updated_item is not None else None}

    def patch_item_handling(
        self,
        *,
        item_id: str,
        item_version: int,
        expected_revision: int,
        actor_id: str,
        disposition: object = _UNSET,
        assignee_id: object = _UNSET,
        note: object = _UNSET,
        feedback: object = _UNSET,
    ) -> dict:
        item_id = _identifier(item_id, "item_id")
        actor_id = _identifier(actor_id, "actor_id")
        if isinstance(item_version, bool) or not isinstance(item_version, int) or item_version < 1:
            raise ValueError("item_version must be a positive integer")
        now = _now()
        with self._immediate() as connection:
            item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if item is None:
                raise ValueError("NOT_FOUND")
            if int(item["current_item_version"]) != item_version:
                raise ValueError("STALE_ITEM")
            if int(item["revision"]) != expected_revision:
                raise ValueError("REVISION_MISMATCH")
            previous = connection.execute(
                """
                SELECT * FROM item_handling_events
                WHERE item_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            next_disposition = previous["disposition"] if previous is not None else "open"
            next_assignee = previous["assignee_id"] if previous is not None else None
            next_note = previous["note"] if previous is not None else None
            next_feedback = previous["feedback"] if previous is not None else None
            if disposition is not _UNSET:
                if disposition not in {"open", "done", "dismissed"}:
                    raise ValueError("invalid disposition")
                next_disposition = disposition
            if assignee_id is not _UNSET:
                if assignee_id is not None:
                    assignee_id = _identifier(assignee_id, "assignee_id")
                next_assignee = assignee_id
            if note is not _UNSET:
                if note is not None and (not isinstance(note, str) or len(note) > 2048):
                    raise ValueError("note must be null or at most 2048 characters")
                next_note = note
            if feedback is not _UNSET:
                if feedback not in {None, "classification_inaccurate"}:
                    raise ValueError("invalid feedback")
                next_feedback = feedback
            event_id = f"handling_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO item_handling_events(
                    id, item_id, item_version, actor_id, disposition, assignee_id,
                    note, feedback, event_kind, carried_from_item_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user_update', NULL, ?)
                """,
                (
                    event_id,
                    item_id,
                    item_version,
                    actor_id,
                    next_disposition,
                    next_assignee,
                    next_note,
                    next_feedback,
                    now,
                ),
            )
            cursor = connection.execute(
                "UPDATE items SET revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?",
                (now, item_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("REVISION_MISMATCH")
            updated = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        payload = self._item_dto(updated)
        payload["handling"] = {
            "disposition": next_disposition,
            "assigneeId": next_assignee,
            "note": next_note,
            "feedback": next_feedback,
            "carriedFromItemVersion": None,
        }
        return payload

    def list_handling_events(self, item_id: str) -> list[dict]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM item_handling_events
                WHERE item_id = ? ORDER BY rowid
                """,
                (_identifier(item_id, "item_id"),),
            ).fetchall()
        return [self._handling_event_dto(row) for row in rows]

    @staticmethod
    def _handling_event_dto(row: sqlite3.Row) -> dict:
        return handling_event_dto(row)

    def admit_provider_attempt(
        self,
        *,
        attempt_id: str,
        billing_owner_id: str,
        input_key: str,
        occurred_at: int,
        owner_monthly_limit: int,
        global_monthly_limit: int,
        owner_rolling_limit: int,
        global_rolling_limit: int,
    ) -> dict:
        attempt_id = _identifier(attempt_id, "attempt_id")
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        input_key = _identifier(input_key, "input_key")
        if isinstance(occurred_at, bool) or not isinstance(occurred_at, int) or occurred_at < 0:
            raise ValueError("occurred_at must be a non-negative integer")
        limits = {
            "owner_monthly_limit": owner_monthly_limit,
            "global_monthly_limit": global_monthly_limit,
            "owner_rolling_limit": owner_rolling_limit,
            "global_rolling_limit": global_rolling_limit,
        }
        for field, limit in limits.items():
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                if field == "global_monthly_limit":
                    raise ValueError("PRODUCTION_JEV_DISABLED")
                raise ValueError(f"{field} must be a positive integer")
        period_utc = time.strftime("%Y-%m", time.gmtime(occurred_at))
        rolling_start = max(0, occurred_at - 59)
        with self._immediate() as connection:
            existing = connection.execute(
                "SELECT * FROM provider_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["billing_owner_id"] != owner_id or existing["input_key"] != input_key:
                    raise ValueError("ATTEMPT_ID_CONFLICT")
                return {
                    "attemptId": attempt_id,
                    "billingOwnerId": owner_id,
                    "occurredAt": int(existing["occurred_at"]),
                    "reused": True,
                }
            global_monthly = connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE period_utc = ?",
                (period_utc,),
            ).fetchone()[0]
            if global_monthly >= global_monthly_limit:
                raise ValueError("GLOBAL_MONTHLY_PROVIDER_BUDGET")
            owner_monthly = connection.execute(
                """
                SELECT COUNT(*) FROM provider_attempts
                WHERE billing_owner_id = ? AND period_utc = ?
                """,
                (owner_id, period_utc),
            ).fetchone()[0]
            if owner_monthly >= owner_monthly_limit:
                raise ValueError("OWNER_MONTHLY_PROVIDER_BUDGET")
            global_rolling = connection.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE occurred_at BETWEEN ? AND ?",
                (rolling_start, occurred_at),
            ).fetchone()[0]
            if global_rolling >= global_rolling_limit:
                raise ValueError("GLOBAL_ROLLING_PROVIDER_LIMIT")
            owner_rolling = connection.execute(
                """
                SELECT COUNT(*) FROM provider_attempts
                WHERE billing_owner_id = ? AND occurred_at BETWEEN ? AND ?
                """,
                (owner_id, rolling_start, occurred_at),
            ).fetchone()[0]
            if owner_rolling >= owner_rolling_limit:
                raise ValueError("OWNER_ROLLING_PROVIDER_LIMIT")
            connection.execute(
                """
                INSERT INTO provider_attempts(
                    attempt_id, billing_owner_id, input_key, period_utc, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, owner_id, input_key, period_utc, occurred_at, _now()),
            )
        return {
            "attemptId": attempt_id,
            "billingOwnerId": owner_id,
            "occurredAt": occurred_at,
            "reused": False,
        }

    def count_provider_attempts(
        self,
        *,
        billing_owner_id: str,
        started_at: int,
        ended_at: int,
    ) -> int:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        if (
            isinstance(started_at, bool)
            or isinstance(ended_at, bool)
            or not isinstance(started_at, int)
            or not isinstance(ended_at, int)
            or started_at < 0
            or ended_at <= started_at
        ):
            raise ValueError("provider attempt interval is invalid")
        with self._read() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM provider_attempts
                    WHERE billing_owner_id = ? AND occurred_at >= ? AND occurred_at < ?
                    """,
                    (owner_id, started_at, ended_at),
                ).fetchone()[0]
            )

    def reserve_processing_unit(
        self,
        *,
        charge_key: str,
        billing_owner_id: str,
        period: str,
        module: str,
        limit: int,
    ) -> dict:
        charge_key = _identifier(charge_key, "charge_key")
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        period = _identifier(period, "period")
        if module not in {"pr", "ci", "updates"}:
            raise ValueError("invalid module")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        now = _now()
        with self._immediate() as connection:
            existing = connection.execute(
                "SELECT * FROM processing_usage_ledger WHERE charge_key = ?",
                (charge_key,),
            ).fetchone()
            if existing is not None and existing["state"] in {"reserved", "consumed"}:
                if (
                    existing["billing_owner_id"] != owner_id
                    or existing["period"] != period
                    or existing["module"] != module
                ):
                    raise ValueError("CHARGE_KEY_CONFLICT")
                return {
                    "chargeKey": charge_key,
                    "reservationId": existing["reservation_id"],
                    "state": existing["state"],
                    "reused": True,
                }
            connection.execute(
                """
                INSERT INTO processing_usage_buckets(
                    billing_owner_id, period, metric, used, reserved, limit_value, updated_at
                ) VALUES (?, ?, 'intelligent_processing', 0, 0, ?, ?)
                ON CONFLICT(billing_owner_id, period, metric) DO UPDATE SET
                    limit_value = excluded.limit_value,
                    updated_at = excluded.updated_at
                """,
                (owner_id, period, limit, now),
            )
            bucket = connection.execute(
                """
                SELECT used, reserved, limit_value FROM processing_usage_buckets
                WHERE billing_owner_id = ? AND period = ? AND metric = 'intelligent_processing'
                """,
                (owner_id, period),
            ).fetchone()
            if int(bucket["used"]) + int(bucket["reserved"]) >= int(bucket["limit_value"]):
                raise ValueError("PROCESSING_QUOTA")
            reservation_id = f"reservation_{uuid.uuid4().hex}"
            connection.execute(
                """
                UPDATE processing_usage_buckets
                SET reserved = reserved + 1, updated_at = ?
                WHERE billing_owner_id = ? AND period = ? AND metric = 'intelligent_processing'
                """,
                (now, owner_id, period),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO processing_usage_ledger(
                        charge_key, reservation_id, billing_owner_id, period, module,
                        state, reserved_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, NULL)
                    """,
                    (charge_key, reservation_id, owner_id, period, module, now),
                )
            else:
                if (
                    existing["billing_owner_id"] != owner_id
                    or existing["module"] != module
                ):
                    raise ValueError("CHARGE_KEY_CONFLICT")
                connection.execute(
                    """
                    UPDATE processing_usage_ledger
                    SET reservation_id = ?, period = ?, state = 'reserved', reserved_at = ?, finished_at = NULL
                    WHERE charge_key = ? AND state = 'released'
                    """,
                    (reservation_id, period, now, charge_key),
                )
        return {
            "chargeKey": charge_key,
            "reservationId": reservation_id,
            "state": "reserved",
            "reused": False,
        }

    def finish_processing_unit(self, reservation_id: str, *, succeeded: bool) -> dict:
        reservation_id = _identifier(reservation_id, "reservation_id")
        now = _now()
        with self._immediate() as connection:
            ledger = connection.execute(
                "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if ledger is None:
                raise ValueError("RESERVATION_NOT_FOUND")
            if ledger["state"] != "reserved":
                return {
                    "chargeKey": ledger["charge_key"],
                    "reservationId": reservation_id,
                    "state": ledger["state"],
                    "reused": True,
                }
            state = "consumed" if succeeded else "released"
            used_increment = 1 if succeeded else 0
            cursor = connection.execute(
                """
                UPDATE processing_usage_buckets
                SET reserved = reserved - 1, used = used + ?, updated_at = ?
                WHERE billing_owner_id = ? AND period = ?
                  AND metric = 'intelligent_processing' AND reserved >= 1
                """,
                (
                    used_increment,
                    now,
                    ledger["billing_owner_id"],
                    ledger["period"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("processing reservation bucket is inconsistent")
            connection.execute(
                """
                UPDATE processing_usage_ledger
                SET state = ?, finished_at = ?
                WHERE reservation_id = ? AND state = 'reserved'
                """,
                (state, now, reservation_id),
            )
        return {
            "chargeKey": ledger["charge_key"],
            "reservationId": reservation_id,
            "state": state,
            "reused": False,
        }

    def list_processing_usage_events(self, billing_owner_id: str, *,
                                     module: str | None = None,
                                     cursor: str | None = None,
                                     limit: int = 20) -> dict:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        selected_module, position, page_limit = parse_usage_events_query({
            "module": module, "cursor": cursor, "limit": str(limit)}, owner_id=owner_id)
        at, reservation = position if position else (None, None)
        with self._read() as connection:
            rows = connection.execute("""SELECT reservation_id,module,period,finished_at
                FROM processing_usage_ledger
                WHERE billing_owner_id=? AND state='consumed'
                  AND finished_at IS NOT NULL
                  AND (? IS NULL OR module=?)
                  AND (? IS NULL OR finished_at<?
                       OR (finished_at=? AND reservation_id<?))
                ORDER BY finished_at DESC,reservation_id DESC LIMIT ?""",
                (owner_id, selected_module, selected_module, at, at, at,
                 reservation, page_limit + 1)).fetchall()
        return usage_events_page(rows, page_limit,
            owner_id=owner_id, module=selected_module)

    def processing_usage(self, *, billing_owner_id: str, period: str) -> dict:
        owner_id = _identifier(billing_owner_id, "billing_owner_id")
        period = _identifier(period, "period")
        with self._read() as connection:
            bucket = connection.execute(
                """
                SELECT used, reserved, limit_value FROM processing_usage_buckets
                WHERE billing_owner_id = ? AND period = ? AND metric = 'intelligent_processing'
                """,
                (owner_id, period),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT module, COUNT(*) AS count FROM processing_usage_ledger
                WHERE billing_owner_id = ? AND period = ? AND state = 'consumed'
                GROUP BY module
                """,
                (owner_id, period),
            ).fetchall()
        counts = {"pr": 0, "ci": 0, "updates": 0}
        counts.update({row["module"]: int(row["count"]) for row in rows})
        used = int(bucket["used"]) if bucket is not None else 0
        reserved = int(bucket["reserved"]) if bucket is not None else 0
        limit = int(bucket["limit_value"]) if bucket is not None else 0
        return {
            "metric": "intelligent_processing",
            "period": period,
            "used": used,
            "reserved": reserved,
            "limit": limit,
            "remaining": max(0, limit - used - reserved),
            "byModule": counts,
        }

    def enqueue_background_job(
        self,
        *,
        job_type: str,
        logical_key: str,
        trusted_trigger: str,
        requester_id: str | None,
        source_id: str | None = None,
        context_id: str | None = None,
        reservation_id: str | None = None,
        global_active_limit: int = 1000,
        owner_active_limit: int = 100,
    ) -> dict:
        if job_type not in {
            "sync_repository",
            "sync_watch",
            "analyze_source",
            "reconcile_sources",
            "retention_cleanup",
        }:
            raise ValueError("invalid job_type")
        logical_key = _identifier(logical_key, "logical_key")
        trusted_trigger = _identifier(trusted_trigger, "trusted_trigger")
        if requester_id is not None:
            requester_id = _identifier(requester_id, "requester_id")
        if job_type == "analyze_source":
            source_id = _identifier(source_id, "source_id")
            context_id = _identifier(context_id, "context_id")
            reservation_id = _identifier(reservation_id, "reservation_id")
            for field, value in {
                "global_active_limit": global_active_limit,
                "owner_active_limit": owner_active_limit,
            }.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"{field} must be a positive integer")
        elif any(value is not None for value in (source_id, context_id, reservation_id)):
            raise ValueError("analysis bindings are only valid for analyze_source jobs")
        now = _now()
        with self._immediate() as connection:
            analysis_binding = None
            if job_type == "analyze_source":
                source = connection.execute(
                    "SELECT * FROM source_records WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                context = connection.execute(
                    "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                    (source_id, context_id),
                ).fetchone()
                reservation = connection.execute(
                    "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if (
                    source is None
                    or source["processing_mode"] != "model"
                    or source["lifecycle"] != "active"
                    or source["latest_version"] is None
                ):
                    raise ValueError("SOURCE_NOT_ELIGIBLE")
                if context is None or not bool(context["accessible"]):
                    raise ValueError("STALE_AUTHORIZATION")
                if int(context["authorization_valid_until"]) < now:
                    raise ValueError("STALE_AUTHORIZATION")
                if not bool(context["analysis_enabled"]):
                    raise ValueError("ANALYSIS_DISABLED")
                if reservation is None or reservation["state"] != "reserved":
                    raise ValueError("RESERVATION_NOT_ACTIVE")
                if reservation["billing_owner_id"] != context["billing_owner_id"]:
                    raise ValueError("RESERVATION_OWNER_MISMATCH")
                analysis_binding = {
                    "billingOwnerId": context["billing_owner_id"],
                    "sourceRevision": int(source["source_revision"]),
                    "sourceVersionId": source["latest_version"],
                    "contextVersion": int(context["context_version"]),
                    "configurationRevision": int(context["configuration_revision"]),
                    "authorizationRevision": int(context["authorization_revision"]),
                }
            existing = connection.execute(
                """
                SELECT * FROM background_jobs
                WHERE logical_key = ? AND state IN ('queued', 'running', 'retry_wait')
                """,
                (logical_key,),
            ).fetchone()
            if existing is not None:
                same_binding = (
                    job_type != "analyze_source"
                    or (
                        existing["billing_owner_id"] == analysis_binding["billingOwnerId"]
                        and existing["source_id"] == source_id
                        and existing["context_id"] == context_id
                        and int(existing["source_revision"]) == analysis_binding["sourceRevision"]
                        and existing["source_version_id"] == analysis_binding["sourceVersionId"]
                        and int(existing["context_version"]) == analysis_binding["contextVersion"]
                        and int(existing["configuration_revision"])
                        == analysis_binding["configurationRevision"]
                        and int(existing["authorization_revision"])
                        == analysis_binding["authorizationRevision"]
                    )
                )
                if same_binding:
                    if job_type == "analyze_source" and existing["reservation_id"] != reservation_id:
                        self._release_processing_reservation(connection, reservation, now=now)
                    return self._job_dto(existing, reused=True)
                if existing["reservation_id"] == reservation_id:
                    raise ValueError("ANALYSIS_RESERVATION_REUSED_FOR_NEW_INPUT")
                existing_reservation = connection.execute(
                    "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                    (existing["reservation_id"],),
                ).fetchone()
                if existing_reservation is not None:
                    self._release_processing_reservation(
                        connection,
                        existing_reservation,
                        now=now,
                    )
                successor_not_before = max(
                    int(existing["claimed_until"] or 0),
                    int(existing["next_attempt_at"] or 0),
                ) or None
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET state = 'superseded', claim_token = NULL, claimed_until = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, existing["id"]),
                )
            else:
                successor_not_before = None
                latest = connection.execute(
                    """
                    SELECT * FROM background_jobs
                    WHERE logical_key = ? ORDER BY generation DESC LIMIT 1
                    """,
                    (logical_key,),
                ).fetchone()
                if latest is not None:
                    successor_not_before = max(
                        int(latest["claimed_until"] or 0), int(latest["next_attempt_at"] or 0)
                    ) or None
                same_exhausted_input = (
                    job_type == "analyze_source"
                    and latest is not None
                    and latest["state"] == "failed"
                    and int(latest["attempt"]) >= 3
                    and latest["billing_owner_id"] == analysis_binding["billingOwnerId"]
                    and latest["source_id"] == source_id
                    and latest["context_id"] == context_id
                    and int(latest["source_revision"]) == analysis_binding["sourceRevision"]
                    and latest["source_version_id"] == analysis_binding["sourceVersionId"]
                    and int(latest["context_version"]) == analysis_binding["contextVersion"]
                    and int(latest["configuration_revision"])
                    == analysis_binding["configurationRevision"]
                    and int(latest["authorization_revision"])
                    == analysis_binding["authorizationRevision"]
                )
                if same_exhausted_input:
                    self._release_processing_reservation(connection, reservation, now=now)
                    return self._job_dto(latest, reused=True)
            if job_type == "analyze_source":
                global_active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM background_jobs
                        WHERE job_type = 'analyze_source'
                          AND state IN ('queued', 'running', 'retry_wait')
                        """
                    ).fetchone()[0]
                )
                owner_active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM background_jobs
                        WHERE job_type = 'analyze_source' AND billing_owner_id = ?
                          AND state IN ('queued', 'running', 'retry_wait')
                        """,
                        (analysis_binding["billingOwnerId"],),
                    ).fetchone()[0]
                )
                rejection_code = (
                    "ANALYSIS_QUEUE_GLOBAL_LIMIT"
                    if global_active >= global_active_limit
                    else "ANALYSIS_QUEUE_OWNER_LIMIT"
                    if owner_active >= owner_active_limit
                    else None
                )
                if rejection_code is not None:
                    self._release_processing_reservation(connection, reservation, now=now)
                    connection.execute(
                        """
                        UPDATE source_contexts SET processing_status = 'throttled', updated_at = ?
                        WHERE source_id = ? AND context_id = ?
                        """,
                        (now, source_id, context_id),
                    )
                    return {"rejected": True, "code": rejection_code}
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM background_jobs WHERE logical_key = ?",
                    (logical_key,),
                ).fetchone()[0]
            )
            job_id = f"job_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO background_jobs(
                    id, job_type, logical_key, generation, trusted_trigger,
                    requester_id, billing_owner_id, source_id, context_id, reservation_id,
                    source_revision, source_version_id, context_version,
                    configuration_revision, authorization_revision,
                    state, attempt, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    logical_key,
                    generation,
                    trusted_trigger,
                    requester_id,
                    analysis_binding["billingOwnerId"] if analysis_binding else None,
                    source_id,
                    context_id,
                    reservation_id,
                    analysis_binding["sourceRevision"] if analysis_binding else None,
                    analysis_binding["sourceVersionId"] if analysis_binding else None,
                    analysis_binding["contextVersion"] if analysis_binding else None,
                    analysis_binding["configurationRevision"] if analysis_binding else None,
                    analysis_binding["authorizationRevision"] if analysis_binding else None,
                    successor_not_before,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_dto(row, reused=False)

    def count_jobs(self, *, job_type: str | None = None) -> int:
        with self._read() as connection:
            if job_type is None:
                return int(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0])
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM background_jobs WHERE job_type = ?",
                    (job_type,),
                ).fetchone()[0]
            )

    def list_jobs(self) -> list[dict]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM background_jobs ORDER BY created_at, id"
            ).fetchall()
        return [self._job_dto(row, reused=False) for row in rows]

    def get_background_job(self, job_id: str) -> dict | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM background_jobs WHERE id = ?",
                (_identifier(job_id, "job_id"),),
            ).fetchone()
        return self._job_dto(row, reused=False) if row is not None else None

    def claim_next_analysis_job(self, *, now: int, lease_seconds: int = 120) -> dict | None:
        for field, value in {"now": now, "lease_seconds": lease_seconds}.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        with self._immediate() as connection:
            candidates = connection.execute(
                """
                SELECT j.* FROM background_jobs j
                LEFT JOIN analysis_claim_owners fairness
                  ON fairness.billing_owner_id = j.billing_owner_id
                WHERE j.job_type = 'analyze_source'
                  AND (
                    (j.state = 'queued' AND COALESCE(j.next_attempt_at, 0) <= ?)
                    OR (j.state = 'retry_wait' AND COALESCE(j.next_attempt_at, 0) <= ?)
                    OR (j.state = 'running' AND COALESCE(j.claimed_until, 0) <= ?)
                  )
                ORDER BY COALESCE(fairness.last_claim_order, 0), j.rowid
                """,
                (now, now, now),
            ).fetchall()
            for candidate in candidates:
                source = connection.execute(
                    "SELECT * FROM source_records WHERE source_id = ?",
                    (candidate["source_id"],),
                ).fetchone()
                context = connection.execute(
                    "SELECT * FROM source_contexts WHERE source_id = ? AND context_id = ?",
                    (candidate["source_id"], candidate["context_id"]),
                ).fetchone()
                reservation = connection.execute(
                    "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                    (candidate["reservation_id"],),
                ).fetchone()
                stale_source = (
                    source is None
                    or source["processing_mode"] != "model"
                    or source["lifecycle"] != "active"
                    or int(source["source_revision"]) != int(candidate["source_revision"])
                    or source["latest_version"] != candidate["source_version_id"]
                )
                stale_authorization = (
                    context is None
                    or not bool(context["accessible"])
                    or int(context["authorization_valid_until"]) < now
                    or int(context["authorization_revision"])
                    != int(candidate["authorization_revision"])
                )
                stale_context = (
                    context is not None
                    and (
                        int(context["context_version"]) != int(candidate["context_version"])
                        or int(context["configuration_revision"])
                        != int(candidate["configuration_revision"])
                    )
                )
                analysis_disabled = context is not None and not bool(context["analysis_enabled"])
                reservation_invalid = (
                    reservation is None
                    or reservation["state"] != "reserved"
                    or context is None
                    or reservation["billing_owner_id"] != context["billing_owner_id"]
                )
                if stale_source or stale_context or stale_authorization or analysis_disabled or reservation_invalid:
                    terminal_state = (
                        "superseded"
                        if stale_source or stale_context
                        else "cancelled"
                        if analysis_disabled
                        else "blocked"
                    )
                    if reservation is not None and reservation["state"] == "reserved":
                        released_bucket = connection.execute(
                            """
                            UPDATE processing_usage_buckets
                            SET reserved = reserved - 1, updated_at = ?
                            WHERE billing_owner_id = ? AND period = ?
                              AND metric = 'intelligent_processing' AND reserved >= 1
                            """,
                            (now, reservation["billing_owner_id"], reservation["period"]),
                        )
                        if released_bucket.rowcount != 1:
                            raise RuntimeError("processing reservation bucket is inconsistent")
                        released_ledger = connection.execute(
                            """
                            UPDATE processing_usage_ledger
                            SET state = 'released', finished_at = ?
                            WHERE reservation_id = ? AND state = 'reserved'
                            """,
                            (now, candidate["reservation_id"]),
                        )
                        if released_ledger.rowcount != 1:
                            raise RuntimeError("processing reservation ledger is inconsistent")
                    connection.execute(
                        """
                        UPDATE background_jobs
                        SET state = ?, claim_token = NULL, claimed_until = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (terminal_state, now, candidate["id"]),
                    )
                    continue
                claim_token = f"claim_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET state = 'running', attempt = attempt + 1, claim_token = ?,
                        claimed_until = ?, next_attempt_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (claim_token, now + lease_seconds, now, candidate["id"]),
                )
                connection.execute(
                    """
                    UPDATE source_contexts SET processing_status = 'processing', updated_at = ?
                    WHERE source_id = ? AND context_id = ?
                    """,
                    (now, candidate["source_id"], candidate["context_id"]),
                )
                next_claim_order = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(last_claim_order), 0) + 1 FROM analysis_claim_owners"
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    INSERT INTO analysis_claim_owners(billing_owner_id, last_claim_order)
                    VALUES (?, ?)
                    ON CONFLICT(billing_owner_id) DO UPDATE SET
                        last_claim_order = excluded.last_claim_order
                    """,
                    (candidate["billing_owner_id"], next_claim_order),
                )
                claimed = connection.execute(
                    "SELECT * FROM background_jobs WHERE id = ?",
                    (candidate["id"],),
                ).fetchone()
                return self._job_dto(claimed, reused=False)
        return None

    def record_analysis_failure(
        self,
        *,
        job_id: str,
        claim_token: str,
        now: int,
        retryable: bool,
        next_attempt_at: int | None,
        max_attempts: int = 3,
    ) -> dict:
        job_id = _identifier(job_id, "job_id")
        claim_token = _identifier(claim_token, "claim_token")
        for field, value in {"now": now, "max_attempts": max_attempts}.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be boolean")
        if next_attempt_at is not None and (
            isinstance(next_attempt_at, bool)
            or not isinstance(next_attempt_at, int)
            or next_attempt_at <= now
        ):
            raise ValueError("next_attempt_at must be later than now")
        with self._immediate() as connection:
            job = connection.execute(
                "SELECT * FROM background_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["job_type"] != "analyze_source"
                or job["state"] != "running"
                or job["claim_token"] != claim_token
            ):
                raise ValueError("JOB_CLAIM_LOST")
            if job["claimed_until"] is None or int(job["claimed_until"]) <= now:
                raise ValueError("JOB_CLAIM_EXPIRED")
            will_retry = retryable and int(job["attempt"]) < max_attempts
            if will_retry and next_attempt_at is None:
                raise ValueError("next_attempt_at is required for retryable failures")
            if will_retry:
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET state = 'retry_wait', claim_token = NULL, claimed_until = NULL,
                        next_attempt_at = ?, updated_at = ?
                    WHERE id = ? AND state = 'running' AND claim_token = ?
                    """,
                    (next_attempt_at, now, job_id, claim_token),
                )
            else:
                reservation = connection.execute(
                    "SELECT * FROM processing_usage_ledger WHERE reservation_id = ?",
                    (job["reservation_id"],),
                ).fetchone()
                if reservation is not None and reservation["state"] == "reserved":
                    released_bucket = connection.execute(
                        """
                        UPDATE processing_usage_buckets
                        SET reserved = reserved - 1, updated_at = ?
                        WHERE billing_owner_id = ? AND period = ?
                          AND metric = 'intelligent_processing' AND reserved >= 1
                        """,
                        (now, reservation["billing_owner_id"], reservation["period"]),
                    )
                    if released_bucket.rowcount != 1:
                        raise RuntimeError("processing reservation bucket is inconsistent")
                    released_ledger = connection.execute(
                        """
                        UPDATE processing_usage_ledger
                        SET state = 'released', finished_at = ?
                        WHERE reservation_id = ? AND state = 'reserved'
                        """,
                        (now, job["reservation_id"]),
                    )
                    if released_ledger.rowcount != 1:
                        raise RuntimeError("processing reservation ledger is inconsistent")
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET state = 'failed', claim_token = NULL, claimed_until = NULL,
                        next_attempt_at = NULL, updated_at = ?
                    WHERE id = ? AND state = 'running' AND claim_token = ?
                    """,
                    (now, job_id, claim_token),
                )
                connection.execute(
                    """
                    UPDATE source_contexts SET processing_status = 'failed', updated_at = ?
                    WHERE source_id = ? AND context_id = ?
                    """,
                    (now, job["source_id"], job["context_id"]),
                )
            updated = connection.execute(
                "SELECT * FROM background_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._job_dto(updated, reused=False)

    def begin_idempotent_request(
        self,
        *,
        subject_id: str,
        method: str,
        path: str,
        idempotency_key: str,
        body_hash: str,
        timestamp: int,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> dict:
        subject_id = _identifier(subject_id, "subject_id")
        method = _identifier(method, "method").upper()
        path = _identifier(path, "path")
        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        body_hash = _identifier(body_hash, "body_hash")
        if len(idempotency_key) > 128:
            raise ValueError("idempotency_key must be at most 128 characters")
        for field, value in {"timestamp": timestamp, "ttl_seconds": ttl_seconds}.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        key = (subject_id, method, path, idempotency_key)
        with self._immediate() as connection:
            row = connection.execute(
                """
                SELECT * FROM request_idempotency
                WHERE subject_id = ? AND method = ? AND path = ? AND idempotency_key = ?
                """,
                key,
            ).fetchone()
            if row is not None and int(row["expires_at"]) <= timestamp:
                connection.execute(
                    """
                    DELETE FROM request_idempotency
                    WHERE subject_id = ? AND method = ? AND path = ? AND idempotency_key = ?
                    """,
                    key,
                )
                row = None
            if row is not None:
                if row["body_hash"] != body_hash:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if row["state"] == "completed":
                    return {
                        "state": "replay",
                        "statusCode": int(row["status_code"]),
                        "response": json.loads(row["response_json"]),
                    }
                return {"state": "pending"}
            connection.execute(
                """
                INSERT INTO request_idempotency(
                    subject_id, method, path, idempotency_key, body_hash, state,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (*key, body_hash, timestamp, timestamp + ttl_seconds),
            )
        return {"state": "reserved"}

    def complete_idempotent_request(
        self,
        *,
        subject_id: str,
        method: str,
        path: str,
        idempotency_key: str,
        body_hash: str,
        status_code: int,
        response: Mapping[str, object],
        timestamp: int,
    ) -> None:
        subject_id = _identifier(subject_id, "subject_id")
        method = _identifier(method, "method").upper()
        path = _identifier(path, "path")
        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        body_hash = _identifier(body_hash, "body_hash")
        if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
            raise ValueError("status_code is invalid")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 1:
            raise ValueError("timestamp must be a positive integer")
        response_json = _json(response)
        key = (subject_id, method, path, idempotency_key)
        with self._immediate() as connection:
            row = connection.execute(
                """
                SELECT * FROM request_idempotency
                WHERE subject_id = ? AND method = ? AND path = ? AND idempotency_key = ?
                """,
                key,
            ).fetchone()
            if row is None or row["body_hash"] != body_hash:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if row["state"] == "completed":
                if int(row["status_code"]) != status_code or row["response_json"] != response_json:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return
            connection.execute(
                """
                UPDATE request_idempotency
                SET state = 'completed', status_code = ?, response_json = ?, completed_at = ?
                WHERE subject_id = ? AND method = ? AND path = ? AND idempotency_key = ?
                """,
                (status_code, response_json, timestamp, *key),
            )

    def abandon_idempotent_request(
        self,
        *,
        subject_id: str,
        method: str,
        path: str,
        idempotency_key: str,
        body_hash: str,
    ) -> None:
        with self._immediate() as connection:
            connection.execute(
                """
                DELETE FROM request_idempotency
                WHERE subject_id = ? AND method = ? AND path = ? AND idempotency_key = ?
                  AND body_hash = ? AND state = 'pending'
                """,
                (
                    _identifier(subject_id, "subject_id"),
                    _identifier(method, "method").upper(),
                    _identifier(path, "path"),
                    _identifier(idempotency_key, "idempotency_key"),
                    _identifier(body_hash, "body_hash"),
                ),
            )

    @staticmethod
    def _watch_dto(row: sqlite3.Row) -> dict:
        return watch_dto(row)

    @staticmethod
    def _repository_service_dto(row: sqlite3.Row) -> dict:
        return {
            "repositoryId": row["repository_id"],
            "installationId": row["installation_id"],
            "billingOwnerId": row["billing_owner_id"],
            "enabled": bool(row["enabled"]),
            "modules": json.loads(row["modules_json"]),
            "analysisEnabled": json.loads(row["analysis_enabled_json"]),
            "allowMemberSync": bool(row["allow_member_sync"]),
            "defaultAssigneeId": row["default_assignee_id"],
            "priorityOrder": int(row["priority_order"]),
            "status": row["status"],
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _assessment_dto(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "billingOwnerId": row["billing_owner_id"],
            "sourceVersionId": row["source_version_id"],
            "contextHash": row["context_hash"],
            "evaluatedContextVersion": int(row["evaluated_context_version"]),
            "questionVersion": row["question_version"],
            "extractorVersion": row["extractor_version"],
            "model": row["model"],
            "inputHash": row["input_hash"],
            "dependencies": json.loads(row["dependencies_json"]),
            "answers": json.loads(row["answers_json"]),
            "usage": json.loads(row["usage_json"]),
            "status": row["status"],
        }

    @staticmethod
    def _source_context_dto(row: sqlite3.Row) -> dict:
        return source_context_dto(row)

    @staticmethod
    def _source_record_dto(row: sqlite3.Row, contexts: list[dict]) -> dict:
        return source_record_dto(row, contexts)

    @staticmethod
    def _item_read_dto(
        row: sqlite3.Row,
        sources: list[dict],
        snapshot: dict,
        handling_event: sqlite3.Row | None,
    ) -> dict:
        return item_read_dto(row, sources, snapshot, handling_event)

    @staticmethod
    def _item_dto(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "contextId": row["context_id"],
            "unit": {"type": row["unit_type"], "externalId": row["unit_key"]},
            "itemVersion": int(row["current_item_version"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _job_dto(row: sqlite3.Row, *, reused: bool) -> dict:
        result = {
            "id": row["id"],
            "jobType": row["job_type"],
            "logicalKey": row["logical_key"],
            "generation": int(row["generation"]),
            "trustedTrigger": row["trusted_trigger"],
            "requesterId": row["requester_id"],
            "status": row["state"],
            "attempt": int(row["attempt"]),
            "reused": reused,
        }
        if row["reservation_id"] is not None:
            result["reservationId"] = row["reservation_id"]
        if row["billing_owner_id"] is not None:
            result["billingOwnerId"] = row["billing_owner_id"]
        if row["source_version_id"] is not None:
            result["sourceVersionId"] = row["source_version_id"]
        if row["next_attempt_at"] is not None:
            result["nextAttemptAt"] = int(row["next_attempt_at"])
        if row["claim_token"] is not None:
            result["claimToken"] = row["claim_token"]
            result["claimedUntil"] = int(row["claimed_until"])
        return result
