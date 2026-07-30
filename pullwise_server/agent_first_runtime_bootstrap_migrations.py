"""Additional immutable rows for the canonical S4 runtime bootstrap."""

from __future__ import annotations

import sqlite3


RUNTIME_BOOTSTRAP_TABLES = (
    "agent_current_task_acceptances",
    "agent_current_runtime_bootstraps",
)
RUNTIME_BOOTSTRAP_IMMUTABLE_TABLES = RUNTIME_BOOTSTRAP_TABLES

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS agent_current_task_acceptances (
        task_id TEXT PRIMARY KEY,
        accept_request_digest TEXT NOT NULL UNIQUE
            CHECK(length(accept_request_digest) = 64),
        accept_request_bytes BLOB NOT NULL,
        requirement_ledger_digest TEXT NOT NULL
            CHECK(length(requirement_ledger_digest) = 64),
        requirement_ledger_version INTEGER NOT NULL
            CHECK(requirement_ledger_version >= 1),
        requirement_ledger_bytes BLOB NOT NULL,
        outer_job_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        accept_response_digest TEXT NOT NULL UNIQUE
            CHECK(length(accept_response_digest) = 64),
        accept_response_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_requests(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_runtime_bootstraps (
        claim_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL UNIQUE,
        bootstrap_digest TEXT NOT NULL UNIQUE
            CHECK(length(bootstrap_digest) = 64),
        bootstrap_bytes BLOB NOT NULL,
        task_record_bytes BLOB NOT NULL,
        attempt_record_bytes BLOB NOT NULL,
        owner_record_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(claim_id) REFERENCES agent_current_claims(claim_id),
        FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
        FOREIGN KEY(attempt_id) REFERENCES agent_current_attempts(attempt_id),
        FOREIGN KEY(session_id)
            REFERENCES agent_current_owner_incarnations(session_id)
    )
    """,
)


def install_runtime_bootstrap_tables(connection: sqlite3.Connection) -> None:
    for statement in _DDL:
        connection.execute(statement)


__all__ = [
    "RUNTIME_BOOTSTRAP_IMMUTABLE_TABLES",
    "RUNTIME_BOOTSTRAP_TABLES",
    "install_runtime_bootstrap_tables",
]
