"""SQLite schema for current-package release-evaluator documents."""

from __future__ import annotations

import sqlite3


CURRENT_RELEASE_EVALUATOR_TABLES = (
    "agent_current_release_benchmark_bundles",
    "agent_current_release_gate_policies",
    "agent_current_release_gate_reports",
)


_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_benchmark_bundles (
        bundle_digest TEXT PRIMARY KEY CHECK(length(bundle_digest) = 64),
        benchmark_id TEXT NOT NULL UNIQUE,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        package_content_sha256 TEXT NOT NULL
            CHECK(length(package_content_sha256) = 64),
        package_root_sha256 TEXT NOT NULL
            CHECK(length(package_root_sha256) = 64),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_gate_policies (
        policy_digest TEXT PRIMARY KEY CHECK(length(policy_digest) = 64),
        policy_id TEXT NOT NULL UNIQUE,
        benchmark_digest TEXT NOT NULL,
        benchmark_ref_sha256 TEXT NOT NULL
            CHECK(length(benchmark_ref_sha256) = 64),
        benchmark_ref_size_bytes INTEGER NOT NULL
            CHECK(benchmark_ref_size_bytes >= 0),
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        package_content_sha256 TEXT NOT NULL
            CHECK(length(package_content_sha256) = 64),
        package_root_sha256 TEXT NOT NULL
            CHECK(length(package_root_sha256) = 64),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY(benchmark_digest)
            REFERENCES agent_current_release_benchmark_bundles(bundle_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_current_release_gate_reports (
        report_digest TEXT PRIMARY KEY CHECK(length(report_digest) = 64),
        report_id TEXT NOT NULL UNIQUE,
        benchmark_digest TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        benchmark_ref_sha256 TEXT NOT NULL
            CHECK(length(benchmark_ref_sha256) = 64),
        benchmark_ref_size_bytes INTEGER NOT NULL
            CHECK(benchmark_ref_size_bytes >= 0),
        policy_ref_sha256 TEXT NOT NULL CHECK(length(policy_ref_sha256) = 64),
        policy_ref_size_bytes INTEGER NOT NULL CHECK(policy_ref_size_bytes >= 0),
        verdict TEXT NOT NULL CHECK(verdict IN ('PASS', 'FAIL', 'INDETERMINATE')),
        exit_code INTEGER NOT NULL,
        document_sha256 TEXT NOT NULL CHECK(length(document_sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        package_content_sha256 TEXT NOT NULL
            CHECK(length(package_content_sha256) = 64),
        package_root_sha256 TEXT NOT NULL
            CHECK(length(package_root_sha256) = 64),
        document_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        CHECK(
            (verdict = 'PASS' AND exit_code = 0)
            OR (verdict = 'FAIL' AND exit_code = 1)
            OR (verdict = 'INDETERMINATE' AND exit_code = 2)
        ),
        FOREIGN KEY(benchmark_digest)
            REFERENCES agent_current_release_benchmark_bundles(bundle_digest),
        FOREIGN KEY(policy_digest)
            REFERENCES agent_current_release_gate_policies(policy_digest)
    )
    """,
)


def _immutable_trigger(table: str, operation: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
    BEFORE {operation} ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'agent current release evaluator rows are immutable');
    END
    """


def install_current_release_evaluator_tables(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for table in CURRENT_RELEASE_EVALUATOR_TABLES:
        connection.execute(_immutable_trigger(table, "UPDATE"))
        connection.execute(_immutable_trigger(table, "DELETE"))


__all__ = [
    "CURRENT_RELEASE_EVALUATOR_TABLES",
    "install_current_release_evaluator_tables",
]
