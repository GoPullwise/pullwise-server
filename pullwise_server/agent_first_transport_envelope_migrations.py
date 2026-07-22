"""SQLite schema for atomic TaskResult transport-envelope publication."""

from __future__ import annotations

import sqlite3


TERMINAL_RESULT_TABLE = "agent_current_terminal_results"


_TERMINAL_RESULT_DDL = """
CREATE TABLE IF NOT EXISTS agent_current_terminal_results (
    transport_envelope_digest TEXT PRIMARY KEY CHECK(
        length(transport_envelope_digest) = 64
    ),
    task_result_digest TEXT NOT NULL UNIQUE CHECK(length(task_result_digest) = 64),
    task_result_core_digest TEXT NOT NULL CHECK(length(task_result_core_digest) = 64),
    result_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL CHECK(
        outcome IN (
            'COMPLETED', 'NO_CHANGE_NEEDED', 'COMPLETED_WITH_WAIVERS',
            'PARTIAL', 'BLOCKED', 'FAILED', 'CANCELLED'
        )
    ),
    published_from_version INTEGER NOT NULL CHECK(published_from_version >= 1),
    terminal_task_version INTEGER NOT NULL CHECK(
        terminal_task_version = published_from_version + 1
    ),
    diagnostics_state TEXT NOT NULL CHECK(
        diagnostics_state IN ('uploaded','local_only','unavailable','not_applicable')
    ),
    receipt_digest TEXT UNIQUE,
    receipt_ref_sha256 TEXT CHECK(
        receipt_ref_sha256 IS NULL OR length(receipt_ref_sha256) = 64
    ),
    attempt_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
    owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
    native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
    transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
    grant_digest TEXT NOT NULL CHECK(length(grant_digest) = 64),
    authority_digest TEXT NOT NULL CHECK(length(authority_digest) = 64),
    package_identity TEXT NOT NULL,
    package_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
    task_result_bytes BLOB NOT NULL,
    task_result_core_bytes BLOB NOT NULL,
    worker_debug_descriptor_bytes BLOB,
    envelope_bytes BLOB NOT NULL,
    response_bytes BLOB NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    CHECK(
        (diagnostics_state='uploaded' AND receipt_digest IS NOT NULL
         AND receipt_ref_sha256 IS NOT NULL)
        OR
        (diagnostics_state!='uploaded' AND receipt_digest IS NULL
         AND receipt_ref_sha256 IS NULL)
    ),
    CHECK(
        (diagnostics_state IN ('uploaded','local_only')
         AND worker_debug_descriptor_bytes IS NOT NULL)
        OR
        (diagnostics_state IN ('unavailable','not_applicable')
         AND worker_debug_descriptor_bytes IS NULL)
    ),
    FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
    FOREIGN KEY(receipt_digest)
        REFERENCES agent_current_transport_receipts(receipt_digest)
)
"""


def install_transport_envelope_tables(connection: sqlite3.Connection) -> None:
    connection.execute(_TERMINAL_RESULT_DDL)
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_terminal_results_update_immutable
        BEFORE UPDATE ON agent_current_terminal_results
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TERMINAL_RESULTS_IMMUTABLE');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_terminal_results_delete_immutable
        BEFORE DELETE ON agent_current_terminal_results
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TERMINAL_RESULTS_IMMUTABLE');
        END
        """
    )
    connection.execute(
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
            AND (
              NEW.current_authority_schema_id IS NOT OLD.current_authority_schema_id
              OR NEW.current_authority_digest IS NOT OLD.current_authority_digest
              OR NEW.current_attempt_id IS NOT OLD.current_attempt_id
              OR NEW.current_session_id IS NOT OLD.current_session_id
              OR NEW.current_grant_id IS NOT OLD.current_grant_id
              OR NEW.current_lease_id IS NOT OLD.current_lease_id
            )
          )
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_CAS_INVALID');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_task_head_delete_immutable
        BEFORE DELETE ON agent_current_task_heads
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_IMMUTABLE');
        END
        """
    )


__all__ = [
    "TERMINAL_RESULT_TABLE",
    "install_transport_envelope_tables",
]
