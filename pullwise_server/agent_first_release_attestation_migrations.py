"""SQLite schema for verified current-package release attestations."""

from __future__ import annotations

import sqlite3


CURRENT_RELEASE_ATTESTATION_TABLES = (
    "agent_current_release_gate_attestations",
)


_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS agent_current_release_gate_attestations (
    attestation_digest TEXT PRIMARY KEY CHECK(length(attestation_digest) = 64),
    attestation_id TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_ref_sha256 TEXT NOT NULL CHECK(length(policy_ref_sha256) = 64),
    policy_ref_size_bytes INTEGER NOT NULL CHECK(policy_ref_size_bytes >= 0),
    report_id TEXT NOT NULL,
    report_digest TEXT NOT NULL,
    report_ref_sha256 TEXT NOT NULL CHECK(length(report_ref_sha256) = 64),
    report_ref_size_bytes INTEGER NOT NULL CHECK(report_ref_size_bytes >= 0),
    verified_at TEXT NOT NULL,
    document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    package_identity TEXT NOT NULL,
    package_version TEXT NOT NULL,
    package_content_sha256 TEXT NOT NULL CHECK(length(package_content_sha256) = 64),
    package_root_sha256 TEXT NOT NULL CHECK(length(package_root_sha256) = 64),
    document_bytes BLOB NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY(principal_id)
        REFERENCES agent_current_release_principals(principal_id),
    FOREIGN KEY(key_id)
        REFERENCES agent_current_release_signing_keys(key_id),
    FOREIGN KEY(policy_digest)
        REFERENCES agent_current_release_gate_policies(policy_digest),
    FOREIGN KEY(report_digest)
        REFERENCES agent_current_release_gate_reports(report_digest)
)
"""


def _immutable_trigger(operation: str) -> str:
    table = CURRENT_RELEASE_ATTESTATION_TABLES[0]
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
    BEFORE {operation} ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'agent current release attestation rows are immutable');
    END
    """


def install_current_release_attestation_tables(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(_TABLE_STATEMENT)
    connection.execute(_immutable_trigger("UPDATE"))
    connection.execute(_immutable_trigger("DELETE"))


__all__ = [
    "CURRENT_RELEASE_ATTESTATION_TABLES",
    "install_current_release_attestation_tables",
]
