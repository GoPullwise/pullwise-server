"""Atomic SQLite persistence for verified release-evaluator documents."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Callable, Iterator, Mapping

from ._generated_agent_task_contract import PACKAGE_TUPLE
from .agent_first_release_evaluator_store_sql import (
    LOAD_RELEASE_EVALUATION_SQL,
)


FaultInjector = Callable[[str], None]
PackageTuple = tuple[str, str, str, str]
InputRow = tuple[
    str, str, str, object, str, object, tuple[str, ...], tuple[object, ...]
]

RELEASE_EVALUATOR_FAULT_POINTS = (
    "before_benchmark", "after_benchmark", "before_policy", "after_policy",
    "before_sample_set", "after_sample_set", "before_report", "after_report",
)


class ReleaseEvaluatorStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredReleaseInputRows:
    benchmark_bytes: bytes
    policy_bytes: bytes


@dataclass(frozen=True)
class StoredReleaseEvaluationRows:
    benchmark_bytes: bytes
    policy_bytes: bytes
    sample_set_bytes: bytes
    report_bytes: bytes
    verdict: str
    exit_code: int


class ReleaseEvaluatorStore:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection],
        fault_injector: FaultInjector | None = None,
        package_values: PackageTuple = PACKAGE_TUPLE,
    ) -> None:
        self._connect_factory = connect_factory
        self._fault_injector = fault_injector
        self._current_package = package_values

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

    def _package_values(self) -> PackageTuple:
        return self._current_package

    @staticmethod
    def _selected_document(
        connection: sqlite3.Connection,
        *,
        table: str,
        digest_column: str,
        digest: str,
        id_column: str,
        document_id: str,
        columns: tuple[str, ...],
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM {table}
            WHERE {digest_column} = ? OR {id_column} = ?
            """,
            (digest, document_id),
        ).fetchone()

    @staticmethod
    def _require_selected_match(
        selected: sqlite3.Row,
        *,
        digest_column: str,
        digest: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        if tuple(selected[column] for column in columns) != values:
            raise ReleaseEvaluatorStoreError(
                "AUTHORITY_STORAGE_CORRUPT"
                if selected[digest_column] == digest
                else "IDEMPOTENCY_CONFLICT"
            )

    @classmethod
    def _insert_or_match(
        cls,
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
        selected = cls._selected_document(
            connection,
            table=table,
            digest_column=digest_column,
            digest=digest,
            id_column=id_column,
            document_id=document_id,
            columns=columns,
        )
        if selected is not None:
            cls._require_selected_match(
                selected,
                digest_column=digest_column,
                digest=digest,
                columns=columns,
                values=values,
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

    @classmethod
    def _matching_presence(
        cls,
        connection: sqlite3.Connection,
        input_rows: tuple[InputRow, InputRow],
    ) -> tuple[bool, bool]:
        presence: list[bool] = []
        for (
            _, table, digest_column, digest, id_column, document_id, columns, values
        ) in input_rows:
            selected = cls._selected_document(
                connection,
                table=table,
                digest_column=digest_column,
                digest=str(digest),
                id_column=id_column,
                document_id=str(document_id),
                columns=columns,
            )
            if selected is not None:
                cls._require_selected_match(
                    selected,
                    digest_column=digest_column,
                    digest=str(digest),
                    columns=columns,
                    values=values,
                )
            presence.append(selected is not None)
        return presence[0], presence[1]

    def _input_rows(
        self,
        *,
        benchmark: Mapping[str, object],
        benchmark_bytes: bytes,
        policy: Mapping[str, object],
        policy_bytes: bytes,
    ) -> tuple[InputRow, InputRow]:
        package_values = self._package_values()
        benchmark_sha256, benchmark_size = self._document_values(benchmark_bytes)
        policy_sha256, policy_size = self._document_values(policy_bytes)
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
        return (
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
        )

    def freeze_inputs(
        self,
        *,
        benchmark: Mapping[str, object],
        benchmark_bytes: bytes,
        policy: Mapping[str, object],
        policy_bytes: bytes,
    ) -> StoredReleaseInputRows:
        input_rows = self._input_rows(
            benchmark=benchmark,
            benchmark_bytes=benchmark_bytes,
            policy=policy,
            policy_bytes=policy_bytes,
        )
        with self._connection(immediate=True) as connection:
            presence = self._matching_presence(connection, input_rows)
            if presence in ((True, False), (False, True)):
                raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
            for (
                name, table, digest_column, digest, id_column,
                document_id, columns, values,
            ) in input_rows:
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
        return StoredReleaseInputRows(benchmark_bytes, policy_bytes)

    def store_evaluation(
        self,
        *,
        benchmark: Mapping[str, object],
        benchmark_bytes: bytes,
        policy: Mapping[str, object],
        policy_bytes: bytes,
        sample_set: Mapping[str, object],
        sample_set_bytes: bytes,
        report: Mapping[str, object],
        report_bytes: bytes,
        verdict: str,
        exit_code: int,
    ) -> StoredReleaseEvaluationRows:
        package_values = self._package_values()
        sample_set_sha256, sample_set_size = self._document_values(
            sample_set_bytes
        )
        report_sha256, report_size = self._document_values(report_bytes)
        input_rows = self._input_rows(
            benchmark=benchmark,
            benchmark_bytes=benchmark_bytes,
            policy=policy,
            policy_bytes=policy_bytes,
        )
        sample_set_columns = (
            "sample_set_digest",
            "sample_set_id",
            "benchmark_digest",
            "policy_digest",
            "benchmark_ref_sha256",
            "benchmark_ref_size_bytes",
            "policy_ref_sha256",
            "policy_ref_size_bytes",
            "document_sha256",
            "size_bytes",
            "package_identity",
            "package_version",
            "package_content_sha256",
            "package_root_sha256",
            "document_bytes",
        )
        sample_set_values = (
            sample_set["sample_set_digest"],
            sample_set["sample_set_id"],
            sample_set["benchmark_digest"],
            sample_set["policy_digest"],
            sample_set["benchmark_ref"]["sha256"],
            sample_set["benchmark_ref"]["size_bytes"],
            sample_set["policy_ref"]["sha256"],
            sample_set["policy_ref"]["size_bytes"],
            sample_set_sha256,
            sample_set_size,
            *package_values,
            sample_set_bytes,
        )
        report_columns = (
            "report_digest",
            "report_id",
            "benchmark_digest",
            "policy_digest",
            "sample_set_digest",
            "benchmark_ref_sha256",
            "benchmark_ref_size_bytes",
            "policy_ref_sha256",
            "policy_ref_size_bytes",
            "sample_set_ref_sha256",
            "sample_set_ref_size_bytes",
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
            report["sample_set_digest"],
            report["benchmark_ref"]["sha256"],
            report["benchmark_ref"]["size_bytes"],
            report["policy_ref"]["sha256"],
            report["policy_ref"]["size_bytes"],
            report["sample_set_ref"]["sha256"],
            report["sample_set_ref"]["size_bytes"],
            verdict,
            exit_code,
            report_sha256,
            report_size,
            *package_values,
            report_bytes,
        )

        with self._connection(immediate=True) as connection:
            presence = self._matching_presence(connection, input_rows)
            if presence == (False, False):
                raise ReleaseEvaluatorStoreError("RELEASE_EVALUATION_NOT_FOUND")
            if presence != (True, True):
                raise ReleaseEvaluatorStoreError("AUTHORITY_STORAGE_CORRUPT")
            self._fault("before_sample_set")
            self._insert_or_match(
                connection,
                table="agent_current_release_gate_sample_sets",
                digest_column="sample_set_digest",
                digest=str(sample_set["sample_set_digest"]),
                id_column="sample_set_id",
                document_id=str(sample_set["sample_set_id"]),
                columns=sample_set_columns,
                values=sample_set_values,
            )
            self._fault("after_sample_set")
            self._fault("before_report")
            self._insert_or_match(
                connection,
                table="agent_current_release_gate_reports",
                digest_column="report_digest",
                digest=str(report["report_digest"]),
                id_column="report_id",
                document_id=str(report["report_id"]),
                columns=report_columns,
                values=report_values,
            )
            self._fault("after_report")

        return StoredReleaseEvaluationRows(
            benchmark_bytes,
            policy_bytes,
            sample_set_bytes,
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
                LOAD_RELEASE_EVALUATION_SQL,
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
        for prefix in ("benchmark", "policy", "sample_set", "report"):
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
        sample_set_bytes = self._checked_bytes(row, prefix="sample_set")
        report_bytes = self._checked_bytes(row, prefix="report")
        try:
            benchmark = json.loads(benchmark_bytes)
            policy = json.loads(policy_bytes)
            sample_set = json.loads(sample_set_bytes)
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
                and sample_set["sample_set_digest"]
                == row["stored_sample_set_digest"]
                and sample_set["sample_set_id"]
                == row["stored_sample_set_id"]
                and sample_set["benchmark_digest"]
                == row["sample_set_benchmark_digest"]
                == row["stored_benchmark_digest"]
                and sample_set["policy_digest"]
                == row["sample_set_policy_digest"]
                == row["stored_policy_digest"]
                and sample_set["benchmark_ref"]["sha256"]
                == row["sample_set_benchmark_ref_sha256"]
                == row["benchmark_document_sha256"]
                and sample_set["benchmark_ref"]["size_bytes"]
                == row["sample_set_benchmark_ref_size_bytes"]
                == row["benchmark_size_bytes"]
                and sample_set["policy_ref"]["sha256"]
                == row["sample_set_policy_ref_sha256"]
                == row["policy_document_sha256"]
                and sample_set["policy_ref"]["size_bytes"]
                == row["sample_set_policy_ref_size_bytes"]
                == row["policy_size_bytes"]
                and report["report_digest"] == row["stored_report_digest"]
                and report["report_id"] == row["stored_report_id"]
                and report["benchmark_digest"]
                == row["report_benchmark_digest"]
                == row["stored_benchmark_digest"]
                and report["policy_digest"]
                == row["report_policy_digest"]
                == row["stored_policy_digest"]
                and report["sample_set_digest"]
                == row["report_sample_set_digest"]
                == row["stored_sample_set_digest"]
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
                and report["sample_set_ref"]["sha256"]
                == row["sample_set_ref_sha256"]
                == row["sample_set_document_sha256"]
                and report["sample_set_ref"]["size_bytes"]
                == row["sample_set_ref_size_bytes"]
                == row["sample_set_size_bytes"]
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
            sample_set_bytes,
            report_bytes,
            row["verdict"],
            row["exit_code"],
        )


__all__ = [
    "RELEASE_EVALUATOR_FAULT_POINTS",
    "ReleaseEvaluatorStore",
    "ReleaseEvaluatorStoreError",
    "StoredReleaseEvaluationRows",
    "StoredReleaseInputRows",
]
