"""Append-only storage for verified current-package release attestations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Callable, Iterator, Mapping


PackageTuple = tuple[str, str, str, str]


class ReleaseAttestationStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredReleaseAttestationRow:
    attestation_bytes: bytes
    attestation_id: str
    report_id: str
    organization_id: str
    principal_id: str
    key_id: str
    verified_at: str


class ReleaseAttestationStore:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection],
        package_values: PackageTuple,
    ) -> None:
        self._connect_factory = connect_factory
        self._package_values = package_values

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

    def store_attestation(
        self,
        *,
        attestation: Mapping[str, object],
        attestation_bytes: bytes,
        principal_id: str,
        key_id: str,
        verified_at: str,
    ) -> StoredReleaseAttestationRow:
        document_sha256, size_bytes = self._document_values(attestation_bytes)
        columns = (
            "attestation_digest", "attestation_id", "organization_id",
            "principal_id", "key_id", "policy_id", "policy_digest",
            "policy_ref_sha256", "policy_ref_size_bytes", "report_id",
            "report_digest", "report_ref_sha256", "report_ref_size_bytes",
            "verified_at", "document_sha256", "size_bytes", "package_identity",
            "package_version", "package_content_sha256", "package_root_sha256",
            "document_bytes",
        )
        values = (
            attestation["attestation_digest"], attestation["attestation_id"],
            attestation["organization_id"], principal_id, key_id,
            attestation["policy_id"], attestation["policy_digest"],
            attestation["policy_ref"]["sha256"],
            attestation["policy_ref"]["size_bytes"], attestation["report_id"],
            attestation["report_digest"], attestation["report_ref"]["sha256"],
            attestation["report_ref"]["size_bytes"], verified_at,
            document_sha256, size_bytes, *self._package_values, attestation_bytes,
        )
        with self._connection(immediate=True) as connection:
            selected = connection.execute(
                f"""
                SELECT {", ".join(columns)}
                FROM agent_current_release_gate_attestations
                WHERE attestation_digest = ? OR attestation_id = ?
                """,
                (attestation["attestation_digest"], attestation["attestation_id"]),
            ).fetchone()
            if selected is not None:
                if tuple(selected[column] for column in columns) != values:
                    raise ReleaseAttestationStoreError(
                        "AUTHORITY_STORAGE_CORRUPT"
                        if selected["attestation_digest"]
                        == attestation["attestation_digest"]
                        else "IDEMPOTENCY_CONFLICT"
                    )
            else:
                try:
                    connection.execute(
                        f"""
                        INSERT INTO agent_current_release_gate_attestations
                            ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})
                        """,
                        values,
                    )
                except sqlite3.IntegrityError:
                    raise ReleaseAttestationStoreError(
                        "IDEMPOTENCY_CONFLICT"
                    ) from None
        return StoredReleaseAttestationRow(
            attestation_bytes,
            str(attestation["attestation_id"]),
            str(attestation["report_id"]),
            str(attestation["organization_id"]),
            principal_id,
            key_id,
            verified_at,
        )

    def load_attestation(self, attestation_id: str) -> StoredReleaseAttestationRow:
        present = False
        with self._connection(immediate=False) as connection:
            row = connection.execute(
                """
                SELECT
                    attestation.*,
                    policy.policy_id AS linked_policy_id,
                    policy.document_sha256 AS policy_document_sha256,
                    policy.size_bytes AS policy_size_bytes,
                    report.report_id AS linked_report_id,
                    report.policy_digest AS report_policy_digest,
                    report.document_sha256 AS report_document_sha256,
                    report.size_bytes AS report_size_bytes,
                    signing_key.organization_id AS key_organization_id,
                    signing_key.principal_id AS key_principal_id,
                    principal.organization_id AS principal_organization_id
                FROM agent_current_release_gate_attestations AS attestation
                JOIN agent_current_release_gate_policies AS policy
                    ON policy.policy_digest = attestation.policy_digest
                JOIN agent_current_release_gate_reports AS report
                    ON report.report_digest = attestation.report_digest
                JOIN agent_current_release_signing_keys AS signing_key
                    ON signing_key.key_id = attestation.key_id
                JOIN agent_current_release_principals AS principal
                    ON principal.principal_id = attestation.principal_id
                WHERE attestation.attestation_id = ?
                """,
                (attestation_id,),
            ).fetchone()
            if row is None:
                present = connection.execute(
                    """
                    SELECT 1 FROM agent_current_release_gate_attestations
                    WHERE attestation_id = ?
                    """,
                    (attestation_id,),
                ).fetchone() is not None
        if row is None:
            raise ReleaseAttestationStoreError(
                "AUTHORITY_STORAGE_CORRUPT"
                if present else "RELEASE_ATTESTATION_NOT_FOUND"
            )
        encoded = row["document_bytes"]
        if (
            not isinstance(encoded, bytes)
            or len(encoded) != row["size_bytes"]
            or hashlib.sha256(encoded).hexdigest() != row["document_sha256"]
        ):
            raise ReleaseAttestationStoreError("AUTHORITY_STORAGE_CORRUPT")
        package = (
            row["package_identity"], row["package_version"],
            row["package_content_sha256"], row["package_root_sha256"],
        )
        try:
            document = json.loads(encoded)
            metadata_matches = (
                package == self._package_values
                and document["attestation_id"] == row["attestation_id"]
                and document["attestation_digest"] == row["attestation_digest"]
                and document["organization_id"] == row["organization_id"]
                == row["key_organization_id"] == row["principal_organization_id"]
                and document["signer_id"] == row["principal_id"]
                == row["key_principal_id"]
                and document["key_id"] == row["key_id"]
                and document["policy_id"] == row["policy_id"]
                == row["linked_policy_id"]
                and document["policy_digest"] == row["policy_digest"]
                == row["report_policy_digest"]
                and document["policy_ref"]["sha256"]
                == row["policy_ref_sha256"] == row["policy_document_sha256"]
                and document["policy_ref"]["size_bytes"]
                == row["policy_ref_size_bytes"] == row["policy_size_bytes"]
                and document["report_id"] == row["report_id"]
                == row["linked_report_id"]
                and document["report_digest"] == row["report_digest"]
                and document["report_ref"]["sha256"]
                == row["report_ref_sha256"] == row["report_document_sha256"]
                and document["report_ref"]["size_bytes"]
                == row["report_ref_size_bytes"] == row["report_size_bytes"]
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ReleaseAttestationStoreError("AUTHORITY_STORAGE_CORRUPT") from None
        if not metadata_matches:
            raise ReleaseAttestationStoreError("AUTHORITY_STORAGE_CORRUPT")
        return StoredReleaseAttestationRow(
            encoded,
            row["attestation_id"],
            row["report_id"],
            row["organization_id"],
            row["principal_id"],
            row["key_id"],
            row["verified_at"],
        )


__all__ = [
    "ReleaseAttestationStore",
    "ReleaseAttestationStoreError",
    "StoredReleaseAttestationRow",
]
