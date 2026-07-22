"""Self-contained SQLite schema for the current Agent-First Server authority."""

from __future__ import annotations

import sqlite3


CURRENT_AUTHORITY_TABLES = (
    "agent_current_worker_registrations",
    "agent_current_worker_registration_heads",
    "agent_current_task_requests",
    "agent_current_task_heads",
    "agent_current_attempts",
    "agent_current_owner_incarnations",
    "agent_current_claims",
    "agent_current_grants",
    "agent_current_grant_authority",
    "agent_current_control_events",
    "agent_current_transport_receipts",
    "agent_current_transport_receipt_bindings",
    "agent_current_abandonments",
    "agent_current_fences",
)

IMMUTABLE_TABLES = (
    "agent_current_worker_registrations",
    "agent_current_task_requests",
    "agent_current_claims",
    "agent_current_grants",
    "agent_current_control_events",
    "agent_current_transport_receipts",
    "agent_current_abandonments",
    "agent_current_fences",
)
FENCE_STATE_TABLES = (
    "agent_current_attempts",
    "agent_current_owner_incarnations",
    "agent_current_grant_authority",
)

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS agent_current_worker_registrations (
        registration_id TEXT PRIMARY KEY,
        worker_id TEXT NOT NULL,
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
        supported_schema_ids BLOB NOT NULL,
        tool_catalog_digest TEXT NOT NULL CHECK(length(tool_catalog_digest) = 64),
        request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
        request_bytes BLOB NOT NULL,
        response_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(worker_id, request_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_worker_registration_heads (
        worker_id TEXT PRIMARY KEY,
        registration_id TEXT NOT NULL UNIQUE,
        updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(registration_id)
            REFERENCES agent_current_worker_registrations(registration_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_task_requests (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
        policy_digest TEXT NOT NULL CHECK(length(policy_digest) = 64),
        policy_bytes BLOB NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
        request_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_task_heads (
        task_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(
            lifecycle IN ('QUEUED', 'ACTIVE', 'FINALIZING', 'TERMINAL')
        ),
        desired_state TEXT NOT NULL CHECK(desired_state IN ('RUN', 'CANCEL')),
        task_version INTEGER NOT NULL CHECK(task_version >= 1),
        deletion_version INTEGER NOT NULL DEFAULT 0 CHECK(deletion_version >= 0),
        native_epoch INTEGER NOT NULL DEFAULT 0 CHECK(native_epoch >= 0),
        owner_epoch INTEGER NOT NULL DEFAULT 0 CHECK(owner_epoch >= 0),
        transport_epoch INTEGER NOT NULL DEFAULT 0 CHECK(transport_epoch >= 0),
        current_attempt_id TEXT,
        current_session_id TEXT,
        current_grant_id TEXT,
        current_authority_schema_id TEXT,
        current_authority_digest TEXT CHECK(
            current_authority_digest IS NULL OR length(current_authority_digest) = 64
        ),
        current_lease_id TEXT,
        terminal_kind TEXT,
        result_ref TEXT,
        result_digest TEXT,
        outcome TEXT,
        terminal_at INTEGER,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        CHECK(
            (current_authority_schema_id IS NULL) =
            (current_authority_digest IS NULL)
        ),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_requests(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
        lease_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('CLAIMED', 'FENCED')),
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        fenced_at INTEGER,
        fence_reason TEXT,
        UNIQUE(task_id, native_epoch),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_owner_incarnations (
        session_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
        state TEXT NOT NULL CHECK(state IN ('STARTING', 'FENCED')),
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        fenced_at INTEGER,
        fence_reason TEXT,
        UNIQUE(task_id, owner_epoch),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(attempt_id) REFERENCES agent_current_attempts(attempt_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_grants (
        grant_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
        grant_digest TEXT NOT NULL UNIQUE CHECK(length(grant_digest) = 64),
        grant_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_grant_authority (
        grant_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'FENCED')),
        authority_version INTEGER NOT NULL DEFAULT 1 CHECK(authority_version >= 1),
        fenced_at INTEGER,
        fence_reason TEXT,
        FOREIGN KEY(grant_id) REFERENCES agent_current_grants(grant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_claims (
        claim_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL UNIQUE,
        grant_id TEXT NOT NULL UNIQUE,
        worker_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        task_version INTEGER NOT NULL CHECK(task_version >= 2),
        deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
        owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
        native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
        claim_digest TEXT NOT NULL UNIQUE CHECK(length(claim_digest) = 64),
        claim_bytes BLOB NOT NULL,
        authority_digest TEXT NOT NULL UNIQUE CHECK(length(authority_digest) = 64),
        authority_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(task_id, native_epoch),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(attempt_id) REFERENCES agent_current_attempts(attempt_id),
        FOREIGN KEY(session_id) REFERENCES agent_current_owner_incarnations(session_id),
        FOREIGN KEY(grant_id) REFERENCES agent_current_grants(grant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_control_events (
        event_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        event_seq INTEGER NOT NULL CHECK(event_seq >= 1),
        event_type TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
        response_digest TEXT NOT NULL CHECK(length(response_digest) = 64),
        response_bytes BLOB NOT NULL,
        applied_task_version INTEGER NOT NULL CHECK(applied_task_version >= 1),
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(task_id, event_seq),
        UNIQUE(task_id, idempotency_key),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_transport_receipts (
        receipt_digest TEXT PRIMARY KEY CHECK(length(receipt_digest) = 64),
        receipt_id TEXT NOT NULL UNIQUE,
        receipt_kind TEXT NOT NULL CHECK(receipt_kind = 'server_transport'),
        task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        authority_digest TEXT NOT NULL CHECK(length(authority_digest) = 64),
        grant_digest TEXT NOT NULL CHECK(length(grant_digest) = 64),
        task_version INTEGER NOT NULL CHECK(task_version >= 1),
        deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
        owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
        native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
        receipt_bytes_sha256 TEXT NOT NULL UNIQUE CHECK(
            length(receipt_bytes_sha256) = 64
        ),
        receipt_size_bytes INTEGER NOT NULL CHECK(receipt_size_bytes > 0),
        receipt_bytes BLOB NOT NULL,
        response_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(attempt_id) REFERENCES agent_current_attempts(attempt_id),
        FOREIGN KEY(session_id) REFERENCES agent_current_owner_incarnations(session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_transport_receipt_bindings (
        receipt_digest TEXT PRIMARY KEY,
        transport_envelope_digest TEXT CHECK(
            transport_envelope_digest IS NULL OR length(transport_envelope_digest) = 64
        ),
        response_bytes BLOB,
        bound_at INTEGER,
        CHECK(
            (transport_envelope_digest IS NULL AND response_bytes IS NULL AND bound_at IS NULL)
            OR
            (transport_envelope_digest IS NOT NULL AND response_bytes IS NOT NULL AND bound_at IS NOT NULL)
        ),
        FOREIGN KEY(receipt_digest)
            REFERENCES agent_current_transport_receipts(receipt_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_abandonments (
        abandonment_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL UNIQUE,
        grant_id TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        previous_task_version INTEGER NOT NULL CHECK(previous_task_version >= 1),
        abandoned_task_version INTEGER NOT NULL CHECK(
            abandoned_task_version = previous_task_version + 1
        ),
        deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
        owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
        native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
        reason TEXT NOT NULL,
        grant_digest TEXT NOT NULL CHECK(length(grant_digest) = 64),
        superseded_authority_digest TEXT NOT NULL CHECK(
            length(superseded_authority_digest) = 64
        ),
        abandonment_digest TEXT NOT NULL UNIQUE CHECK(length(abandonment_digest) = 64),
        abandonment_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_fences (
        fence_id TEXT PRIMARY KEY,
        abandonment_id TEXT NOT NULL,
        target_type TEXT NOT NULL CHECK(
            target_type IN ('transport', 'attempt', 'owner', 'grant')
        ),
        target_id TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(abandonment_id, target_type),
        UNIQUE(target_type, target_id),
        FOREIGN KEY(abandonment_id)
            REFERENCES agent_current_abandonments(abandonment_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_current_events_task_version
    ON agent_current_control_events(task_id, applied_task_version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_current_receipts_task
    ON agent_current_transport_receipts(task_id, attempt_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_binding_one_shot
    BEFORE UPDATE
    ON agent_current_transport_receipt_bindings
    WHEN OLD.transport_envelope_digest IS NOT NULL
      OR NEW.transport_envelope_digest IS NULL
      OR NEW.response_bytes IS NULL
      OR NEW.bound_at IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'TRANSPORT_RECEIPT_BINDING_IMMUTABLE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_binding_delete_immutable
    BEFORE DELETE ON agent_current_transport_receipt_bindings
    BEGIN
        SELECT RAISE(ABORT, 'TRANSPORT_RECEIPT_BINDING_IMMUTABLE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_task_head_monotonic
    BEFORE UPDATE ON agent_current_task_heads
    WHEN NEW.task_id IS NOT OLD.task_id
      OR NEW.owner_id IS NOT OLD.owner_id
      OR NEW.task_version != OLD.task_version + 1
      OR NEW.deletion_version < OLD.deletion_version
      OR NEW.owner_epoch < OLD.owner_epoch
      OR NEW.native_epoch < OLD.native_epoch
      OR NEW.transport_epoch < OLD.transport_epoch
      OR OLD.lifecycle='TERMINAL'
      OR (
        OLD.current_authority_schema_id='agent-claim-abandon-response/v1'
        AND NEW.lifecycle!='TERMINAL'
      )
    BEGIN
        SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_CAS_INVALID');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_task_head_delete_immutable
    BEFORE DELETE ON agent_current_task_heads
    BEGIN
        SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_IMMUTABLE');
    END
    """,
)


def install_current_authority_tables(connection: sqlite3.Connection) -> None:
    """Install the clean current-only authority schema in the caller transaction."""

    connection.execute("PRAGMA foreign_keys=ON")
    if not getattr(connection, "in_transaction", True):
        connection.execute("PRAGMA journal_mode=WAL")
    for statement in _DDL:
        connection.execute(statement)
    for table in IMMUTABLE_TABLES:
        for operation in ("UPDATE", "DELETE"):
            trigger = f"{table}_{operation.lower()}_immutable"
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table.upper()}_IMMUTABLE');
                END
                """
            )
    for table in FENCE_STATE_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_fence_permanent
            BEFORE UPDATE ON {table}
            WHEN OLD.state='FENCED'
            BEGIN
                SELECT RAISE(ABORT, '{table.upper()}_FENCE_PERMANENT');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_delete_immutable
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table.upper()}_IMMUTABLE');
            END
            """
        )


__all__ = [
    "CURRENT_AUTHORITY_TABLES",
    "IMMUTABLE_TABLES",
    "install_current_authority_tables",
]
