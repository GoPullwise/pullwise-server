"""Atomic SQLite persistence for verified release-evaluator documents."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Callable, Iterator, Mapping

from ._generated_agent_task_contract import PACKAGE_TUPLE


FaultInjector = Callable[[str], None]
PackageTuple = tuple[str, str, str, str]

RELEASE_EVALUATOR_FAULT_POINTS = (
    "before_benchmark",
    "after_benchmark",
    "before_policy",
    "after_policy",
    "before_report",
    "after_report",
)


class ReleaseEvaluatorStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredReleaseEvaluationRows:
    benchmark_bytes: bytes
    policy_bytes: bytes
    report_bytes: bytes
    verdict: str
    exit_code: int


class ReleaseEvaluatorStore:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection],
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._connect_factory = connect_factory
        self._fault_injector = fault_injector

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @contextmanager
    def _connection(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _document_values(document_bytes: bytes) -> tuple[str, int]:
        return hashlib.sha256(document_bytes).hexdigest(), len(document_bytes)

    @staticmethod
    def _package_values() -> PackageTuple:
        return PACKAGE_TUPLE

    @staticmethod
    def _insert_or_match(
        connection: sqlite3.Connection,
        *,
        table: str,
        digest_column: str,
        digest: str,
        id_column: str,
        document_id: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        selected = connection.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM {table}
            WHERE {digest_column} = ? OR {id_column} = ?
            """,
            (digest, document_id),
        ).fetchone()
        if selected is not None:
            if tuple(selected[column] for column in columns) != values:
                raise ReleaseEvaluatorStoreError(
                    "AUTHORITY_STORAGE_CORRUPT"
                    if selected[digest_column] == digest
                    else "IDEMPOTENCY_CONFLICT"
                )
            return
        placeholders = ", ".join("?" for _ in columns)
        try:
            connection.execute(
                f"""
                INSERT INTO {table} ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                values,
            )
        except sqlite3.IntegrityError:
            raise ReleaseEvaluatorStoreError("IDEMPOTENCY_CONFLICT") from None

    def store_evaluation(
        self,
        *,
        benchmark: Mapping[str, object],
        benchmark_bytes: bytes,
        policy: Mapping[str, object],
        policy_bytes: bytes,
        report: Mapping[str, object],
        report_bytes: bytes,
        verdict: str,
        exit_code: int,
    ) -> StoredReleaseEvaluationRows:
        package_values = self._package_values()
        benchmark_sha256, benchmark_size = self._document_values(benchmark_bytes)
        policy_sha256, policy_size = self._document_values(policy_bytes)
        report_sha256, report_size = self._document_values(report_bytes)
        benchmark_columns = (
            "bundle_digest",
            "benchmark_id",
            "document_sha256",
            "size_bytes",
            "package_identity",
            "package_version",
            "package_content_sha256",
            "package_root_sha256",
            "document_bytes",
        )
        benchmark_values = (
            benchmark["bundle_digest"],
            benchmark["benchmark_id"],
            benchmark_sha256,
            benchmark_size,
            *package_values,
            benchmark_bytes,
        )
        policy_columns = (
            "policy_digest",
            "policy_id",
            "benchmark_digest",
            "benchmark_ref_sha256",
            "benchmark_ref_size_bytes",
            "document_sha256",
            "size_bytes",
            "package_identity",
            "package_version",
            "package_content_sha256",
            "package_root_sha256",
            "document_bytes",
        )
        policy_values = (
            policy["policy_digest"],
            policy["policy_id"],
            policy["benchmark_digest"],
            policy["benchmark_ref"]["sha256"],
            policy["benchmark_ref"]["size_bytes"],
            policy_sha256,
            policy_size,
            *package_values,
            policy_bytes,
        )
        report_columns = (
            "report_digest",
            "report_id",
            "benchmark_digest",
            "policy_digest",
            "benchmark_ref_sha256",
            "benchmark_ref_size_bytes",
            "policy_ref_sha256",
            "policy_ref_size_bytes",
            "verdict",
            "exit_code",
            "document_sha256",
            "size_bytes",
            "package_identity",
            "package_version",
            "package_content_sha256",
            "package_root_sha256",
            "document_bytes",
        )
        report_values = (
            report["report_digest"],
            report["report_id"],
            report["benchmark_digest"],
            report["policy_digest"],
            report["benchmark_ref"]["sha256"],
            report["benchmark_ref"]["size_bytes"],
            report["policy_ref"]["sha256"],
            report["policy_ref"]["size_bytes"],
            verdict,
            exit_code,
            report_sha256,
            report_size,
            *package_values,
            report_bytes,
        )

        with self._connection(immediate=True) as connection:
            for name, table, digest_column, digest, id_column, document_id, columns, values in (
                (
                    "benchmark",
                    "agent_current_release_benchmark_bundles",
                    "bundle_digest",
                    benchmark["bundle_digest"],
                    "benchmark_id",
                    benchmark["benchmark_id"],
                    benchmark_columns,
                    benchmark_values,
                ),
                (
                    "policy",
                    "agent_current_release_gate_policies",
                    "policy_digest",
                    policy["policy_digest"],
                    "policy_id",
                    policy["policy_id"],
                    policy_columns,
                    policy_values,
                ),
                (
                    "report",
                    "agent_current_release_gate_reports",
                    "report_digest",
                    report["report_digest"],
                    "report_id",
                    report["report_id"],
                    report_columns,
                    report_values,
                ),
            ):
                self._fault(f"before_{name}")
                self._insert_or_match(
                    connection,
                    table=table,
                    digest_column=digest_column,
                    digest=str(digest),
                    id_column=id_column,
                    document_id=str(document_id),
                    columns=columns,
                    values=values,
                )
                self._fault(f"after_{name}")

        return StoredReleaseEvaluationRows(
            benchmark_bytes,
            policy_bytes,
            report_bytes,
            verdict,
            exit_code,
        )

    @staticmethod
    def _checked_bytes(
        row: sqlite3.Row,
        *,
        prefix: str,
    ) -> bytes:
        value = row[f"{prefix}_bytes"]
        if not isinstance(value, bytes):
            raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
        if (
            len(value) != row[f"{prefix}_size_bytes"]
            or hashlib.sha256(value).hexdigest()
            != row[f"{prefix}_document_sha256"]
        ):
            raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
        return value

    def load_evaluation(self, report_id: str) -> StoredReleaseEvaluationRows:
        report_present = False
        with self._connection(immediate=False) as connection:
            row = connection.execute(
                """
                SELECT
                    benchmark.document_bytes AS benchmark_bytes,
                    benchmark.document_sha256 AS benchmark_document_sha256,
                    benchmark.size_bytes AS benchmark_size_bytes,
                    benchmark.bundle_digest AS stored_benchmark_digest,
                    benchmark.benchmark_id AS stored_benchmark_id,
                    benchmark.package_identity AS benchmark_package_identity,
                    benchmark.package_version AS benchmark_package_version,
                    benchmark.package_content_sha256
                        AS benchmark_package_content_sha256,
                    benchmark.package_root_sha256
                        AS benchmark_package_root_sha256,
                    policy.document_bytes AS policy_bytes,
                    policy.document_sha256 AS policy_document_sha256,
                    policy.size_bytes AS policy_size_bytes,
                    policy.policy_digest AS stored_policy_digest,
                    policy.policy_id AS stored_policy_id,
                    policy.benchmark_digest AS policy_benchmark_digest,
                    policy.benchmark_ref_sha256,
                    policy.benchmark_ref_size_bytes,
                    policy.package_identity AS policy_package_identity,
                    policy.package_version AS policy_package_version,
                    policy.package_content_sha256 AS policy_package_content_sha256,
                    policy.package_root_sha256 AS policy_package_root_sha256,
                    report.document_bytes AS report_bytes,
                    report.document_sha256 AS report_document_sha256,
                    report.size_bytes AS report_size_bytes,
                    report.report_digest AS stored_report_digest,
                    report.report_id AS stored_report_id,
                    report.benchmark_digest AS report_benchmark_digest,
                    report.policy_digest AS report_policy_digest,
                    report.benchmark_ref_sha256 AS report_benchmark_ref_sha256,
                    report.benchmark_ref_size_bytes
                        AS report_benchmark_ref_size_bytes,
                    report.policy_ref_sha256,
                    report.policy_ref_size_bytes,
                    report.verdict,
                    report.exit_code,
                    report.package_identity AS report_package_identity,
                    report.package_version AS report_package_version,
                    report.package_content_sha256 AS report_package_content_sha256,
                    report.package_root_sha256 AS report_package_root_sha256
                FROM agent_current_release_gate_reports AS report
                JOIN agent_current_release_gate_policies AS policy
                    ON policy.policy_digest = report.policy_digest
                JOIN agent_current_release_benchmark_bundles AS benchmark
                    ON benchmark.bundle_digest = report.benchmark_digest
                WHERE report.report_id = ?
                """,
                (report_id,),
            ).fetchone()
            if row is None:
                report_present = connection.execute(
                    """
                    SELECT 1 FROM agent_current_release_gate_reports
                    WHERE report_id = ?
                    """,
                    (report_id,),
                ).fetchone() is not None
        if row is None:
            raise ReleaseEvaluatorStoreError(
                "AUTHORITY_STORAGE_CORRUPT"
                if report_present
                else "RELEASE_EVALUATION_NOT_FOUND"
            )
        expected_package = self._package_values()
        for prefix in ("benchmark", "policy", "report"):
            stored_package = (
                row[f"{prefix}_package_identity"],
                row[f"{prefix}_package_version"],
                row[f"{prefix}_package_content_sha256"],
                row[f"{prefix}_package_root_sha256"],
            )
            if stored_package != expected_package:
                raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
        benchmark_bytes = self._checked_bytes(row, prefix="benchmark")
        policy_bytes = self._checked_bytes(row, prefix="policy")
        report_bytes = self._checked_bytes(row, prefix="report")
        try:
            benchmark = json.loads(benchmark_bytes)
            policy = json.loads(policy_bytes)
            report = json.loads(report_bytes)
            metadata_matches = (
                benchmark["bundle_digest"] == row["stored_benchmark_digest"]
                and benchmark["benchmark_id"] == row["stored_benchmark_id"]
                and policy["policy_digest"] == row["stored_policy_digest"]
                and policy["policy_id"] == row["stored_policy_id"]
                and policy["benchmark_digest"]
                == row["policy_benchmark_digest"]
                and policy["benchmark_ref"]["sha256"]
                == row["benchmark_document_sha256"]
                == row["benchmark_ref_sha256"]
                and policy["benchmark_ref"]["size_bytes"]
                == row["benchmark_size_bytes"]
                == row["benchmark_ref_size_bytes"]
                and report["report_digest"] == row["stored_report_digest"]
                and report["report_id"] == row["stored_report_id"]
                and report["benchmark_digest"]
                == row["report_benchmark_digest"]
                == row["stored_benchmark_digest"]
                and report["policy_digest"]
                == row["report_policy_digest"]
                == row["stored_policy_digest"]
                and report["benchmark_ref"]["sha256"]
                == row["report_benchmark_ref_sha256"]
                == row["benchmark_document_sha256"]
                and report["benchmark_ref"]["size_bytes"]
                == row["report_benchmark_ref_size_bytes"]
                == row["benchmark_size_bytes"]
                and report["policy_ref"]["sha256"]
                == row["policy_ref_sha256"]
                == row["policy_document_sha256"]
                and report["policy_ref"]["size_bytes"]
                == row["policy_ref_size_bytes"]
                == row["policy_size_bytes"]
                and report["verdict"] == row["verdict"]
                and report["exit_code"] == row["exit_code"]
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT") from None
        if not metadata_matches:
            raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
        return StoredReleaseEvaluationRows(
            benchmark_bytes,
            policy_bytes,
            report_bytes,
            row["verdict"],
            row["exit_code"],
        )


__all__ = [
    "RELEASE_EVALUATOR_FAULT_POINTS",
    "ReleaseEvaluatorStore",
    "ReleaseEvaluatorStoreError",
    "StoredReleaseEvaluationRows",
]
