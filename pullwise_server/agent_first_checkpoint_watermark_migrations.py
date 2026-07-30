"""Append-only Server schema for acknowledged current checkpoint watermarks."""

from __future__ import annotations

import sqlite3


CHECKPOINT_WATERMARK_TABLES = (
    "agent_current_checkpoint_watermarks",
    "agent_current_checkpoint_watermark_heads",
)
CHECKPOINT_WATERMARK_IMMUTABLE_TABLES = (
    "agent_current_checkpoint_watermarks",
)

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS agent_current_checkpoint_watermarks (
        manifest_hash TEXT PRIMARY KEY CHECK(length(manifest_hash) = 64),
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        previous_manifest_hash TEXT,
        committed_from_task_version INTEGER NOT NULL
            CHECK(committed_from_task_version >= 1),
        committed_task_version INTEGER NOT NULL CHECK(
            committed_task_version = committed_from_task_version + 1
        ),
        attempt_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
        lease_id TEXT NOT NULL,
        authority_digest TEXT NOT NULL CHECK(length(authority_digest) = 64),
        grant_id TEXT NOT NULL,
        grant_digest TEXT NOT NULL CHECK(length(grant_digest) = 64),
        deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
        native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
        same_run_resume_selected INTEGER NOT NULL
            CHECK(same_run_resume_selected = 1),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
        request_digest TEXT NOT NULL UNIQUE CHECK(length(request_digest) = 64),
        request_bytes BLOB NOT NULL,
        ack_digest TEXT NOT NULL UNIQUE CHECK(length(ack_digest) = 64),
        ack_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(task_id, generation),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(attempt_id) REFERENCES agent_current_attempts(attempt_id),
        FOREIGN KEY(grant_id) REFERENCES agent_current_grants(grant_id),
        FOREIGN KEY(previous_manifest_hash)
            REFERENCES agent_current_checkpoint_watermarks(manifest_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_checkpoint_watermark_heads (
        task_id TEXT PRIMARY KEY,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        manifest_hash TEXT NOT NULL UNIQUE CHECK(length(manifest_hash) = 64),
        committed_task_version INTEGER NOT NULL CHECK(committed_task_version >= 2),
        updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(manifest_hash)
            REFERENCES agent_current_checkpoint_watermarks(manifest_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_current_checkpoint_task_generation
    ON agent_current_checkpoint_watermarks(task_id, generation DESC)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_checkpoint_head_monotonic
    BEFORE UPDATE ON agent_current_checkpoint_watermark_heads
    WHEN NEW.task_id IS NOT OLD.task_id
      OR NEW.generation != OLD.generation + 1
      OR NEW.committed_task_version != OLD.committed_task_version + 1
      OR NOT EXISTS (
          SELECT 1 FROM agent_current_checkpoint_watermarks w
          WHERE w.task_id=NEW.task_id
            AND w.generation=NEW.generation
            AND w.manifest_hash=NEW.manifest_hash
            AND w.previous_manifest_hash=OLD.manifest_hash
            AND w.committed_from_task_version=OLD.committed_task_version
            AND w.committed_task_version=NEW.committed_task_version
      )
    BEGIN
        SELECT RAISE(ABORT, 'AGENT_CURRENT_CHECKPOINT_HEAD_CAS_INVALID');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_current_checkpoint_head_delete_immutable
    BEFORE DELETE ON agent_current_checkpoint_watermark_heads
    BEGIN
        SELECT RAISE(ABORT, 'AGENT_CURRENT_CHECKPOINT_HEAD_IMMUTABLE');
    END
    """,
)


def install_checkpoint_watermark_tables(connection: sqlite3.Connection) -> None:
    for statement in _DDL:
        connection.execute(statement)


__all__ = [
    "CHECKPOINT_WATERMARK_IMMUTABLE_TABLES",
    "CHECKPOINT_WATERMARK_TABLES",
    "install_checkpoint_watermark_tables",
]
