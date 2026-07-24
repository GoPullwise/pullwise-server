"""SQLite schema for current-package release trust authorities."""

from __future__ import annotations

import sqlite3


CURRENT_RELEASE_TRUST_TABLES = (
    "agent_current_release_trust_roots",
    "agent_current_release_principals",
    "agent_current_release_signing_keys",
    "agent_current_release_key_revocations",
)


_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_trust_roots (
        root_digest TEXT PRIMARY KEY CHECK(length(root_digest) = 64),
        trust_root_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        root_principal_id TEXT NOT NULL,
        root_key_id TEXT NOT NULL,
        public_key TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_principals (
        principal_digest TEXT PRIMARY KEY CHECK(length(principal_digest) = 64),
        principal_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('benchmark_owner', 'release_operator')),
        trust_root_id TEXT NOT NULL,
        root_digest TEXT NOT NULL,
        root_ref_sha256 TEXT NOT NULL CHECK(length(root_ref_sha256) = 64),
        root_ref_size_bytes INTEGER NOT NULL CHECK(root_ref_size_bytes >= 0),
        signer_id TEXT NOT NULL,
        signer_key_id TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(root_digest)
            REFERENCES agent_current_release_trust_roots(root_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_signing_keys (
        signing_key_digest TEXT PRIMARY KEY CHECK(length(signing_key_digest) = 64),
        key_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        principal_digest TEXT NOT NULL,
        principal_ref_sha256 TEXT NOT NULL
            CHECK(length(principal_ref_sha256) = 64),
        principal_ref_size_bytes INTEGER NOT NULL
            CHECK(principal_ref_size_bytes >= 0),
        key_purpose TEXT NOT NULL
            CHECK(key_purpose IN ('benchmark_signing', 'release_signing')),
        trust_root_id TEXT NOT NULL,
        root_digest TEXT NOT NULL,
        signer_id TEXT NOT NULL,
        signer_key_id TEXT NOT NULL,
        public_key TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(principal_digest)
            REFERENCES agent_current_release_principals(principal_digest),
        FOREIGN KEY(root_digest)
            REFERENCES agent_current_release_trust_roots(root_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_key_revocations (
        revocation_digest TEXT PRIMARY KEY CHECK(length(revocation_digest) = 64),
        revocation_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        root_digest TEXT NOT NULL,
        root_ref_sha256 TEXT NOT NULL CHECK(length(root_ref_sha256) = 64),
        root_ref_size_bytes INTEGER NOT NULL CHECK(root_ref_size_bytes >= 0),
        revoked_key_id TEXT NOT NULL,
        signing_key_digest TEXT NOT NULL,
        key_ref_sha256 TEXT NOT NULL CHECK(length(key_ref_sha256) = 64),
        key_ref_size_bytes INTEGER NOT NULL CHECK(key_ref_size_bytes >= 0),
        revoked_principal_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        signer_id TEXT NOT NULL,
        signer_key_id TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(root_digest)
            REFERENCES agent_current_release_trust_roots(root_digest),
        FOREIGN KEY(signing_key_digest)
            REFERENCES agent_current_release_signing_keys(signing_key_digest)
    )
    """,
)


def _immutable_trigger(table: str, operation: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
    BEFORE {operation} ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'agent current release trust rows are immutable');
    END
    """


def install_current_release_trust_tables(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for table in CURRENT_RELEASE_TRUST_TABLES:
        connection.execute(_immutable_trigger(table, "UPDATE"))
        connection.execute(_immutable_trigger(table, "DELETE"))


__all__ = [
    "CURRENT_RELEASE_TRUST_TABLES",
    "install_current_release_trust_tables",
]
