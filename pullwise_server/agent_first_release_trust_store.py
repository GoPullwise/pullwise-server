"""Atomic SQLite persistence for release trust authority documents."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Callable, Iterator, Mapping


class ReleaseTrustStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredReleaseAuthorityRows:
    root_bytes: bytes
    principal_bytes: bytes
    key_bytes: bytes


class ReleaseTrustStore:
    def __init__(self, connect_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connect_factory = connect_factory

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
    def _insert_or_match(
        connection: sqlite3.Connection,
        *,
        table: str,
        digest_column: str,
        digest: object,
        id_column: str,
        document_id: object,
        columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        selected = connection.execute(
            f"""
            SELECT {", ".join(columns)} FROM {table}
            WHERE {digest_column} = ? OR {id_column} = ?
            """,
            (digest, document_id),
        ).fetchone()
        if selected is not None:
            if tuple(selected[column] for column in columns) != values:
                raise ReleaseTrustStoreError(
                    "AUTHORITY_STORAGE_CORRUPT"
                    if selected[digest_column] == digest
                    else "IDEMPOTENCY_CONFLICT"
                )
            return
        try:
            connection.execute(
                f"""
                INSERT INTO {table} ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                values,
            )
        except sqlite3.IntegrityError:
            raise ReleaseTrustStoreError("IDEMPOTENCY_CONFLICT") from None

    def store_authority(
        self,
        *,
        root: Mapping[str, object],
        root_bytes: bytes,
        principal: Mapping[str, object],
        principal_bytes: bytes,
        signing_key: Mapping[str, object],
        key_bytes: bytes,
    ) -> StoredReleaseAuthorityRows:
        root_sha, root_size = self._document_values(root_bytes)
        principal_sha, principal_size = self._document_values(principal_bytes)
        key_sha, key_size = self._document_values(key_bytes)
        root_columns = (
            "root_digest", "trust_root_id", "organization_id",
            "root_principal_id", "root_key_id", "public_key", "issued_at",
            "expires_at", "document_sha256", "size_bytes", "document_bytes",
        )
        root_values = (
            root["root_digest"], root["trust_root_id"], root["organization_id"],
            root["root_principal_id"], root["root_key_id"], root["public_key"],
            root["issued_at"], root["expires_at"], root_sha, root_size, root_bytes,
        )
        principal_columns = (
            "principal_digest", "principal_id", "organization_id", "role",
            "trust_root_id", "root_digest", "root_ref_sha256",
            "root_ref_size_bytes", "signer_id", "signer_key_id", "issued_at",
            "expires_at", "document_sha256", "size_bytes", "document_bytes",
        )
        principal_values = (
            principal["principal_digest"], principal["principal_id"],
            principal["organization_id"], principal["role"],
            principal["trust_root_id"], principal["trust_root_digest"],
            principal["trust_root_ref"]["sha256"],
            principal["trust_root_ref"]["size_bytes"], principal["signer_id"],
            principal["key_id"], principal["issued_at"], principal["expires_at"],
            principal_sha, principal_size, principal_bytes,
        )
        key_columns = (
            "signing_key_digest", "key_id", "organization_id", "principal_id",
            "principal_digest", "principal_ref_sha256",
            "principal_ref_size_bytes", "key_purpose", "trust_root_id",
            "root_digest", "signer_id", "signer_key_id", "public_key",
            "issued_at", "expires_at", "document_sha256", "size_bytes",
            "document_bytes",
        )
        key_values = (
            signing_key["signing_key_digest"], signing_key["key_id"],
            signing_key["organization_id"], signing_key["principal_id"],
            signing_key["principal_digest"],
            signing_key["principal_ref"]["sha256"],
            signing_key["principal_ref"]["size_bytes"],
            signing_key["key_purpose"], signing_key["trust_root_id"],
            signing_key["trust_root_digest"], signing_key["signer_id"],
            signing_key["signer_key_id"], signing_key["public_key"],
            signing_key["issued_at"], signing_key["expires_at"], key_sha,
            key_size, key_bytes,
        )
        rows = (
            ("agent_current_release_trust_roots", "root_digest", root["root_digest"],
             "trust_root_id", root["trust_root_id"], root_columns, root_values),
            ("agent_current_release_principals", "principal_digest",
             principal["principal_digest"], "principal_id", principal["principal_id"],
             principal_columns, principal_values),
            ("agent_current_release_signing_keys", "signing_key_digest",
             signing_key["signing_key_digest"], "key_id", signing_key["key_id"],
             key_columns, key_values),
        )
        with self._connection(immediate=True) as connection:
            for table, digest_column, digest, id_column, document_id, columns, values in rows:
                self._insert_or_match(
                    connection,
                    table=table,
                    digest_column=digest_column,
                    digest=digest,
                    id_column=id_column,
                    document_id=document_id,
                    columns=columns,
                    values=values,
                )
        return StoredReleaseAuthorityRows(root_bytes, principal_bytes, key_bytes)

    @staticmethod
    def _checked_bytes(row: sqlite3.Row, prefix: str) -> bytes:
        value = row[f"{prefix}_bytes"]
        if not isinstance(value, bytes):
            raise ReleaseTrustStoreError("AUTHORITY_STORAGE_CORRUPT")
        if (
            len(value) != row[f"{prefix}_size_bytes"]
            or hashlib.sha256(value).hexdigest()
            != row[f"{prefix}_document_sha256"]
        ):
            raise ReleaseTrustStoreError("AUTHORITY_STORAGE_CORRUPT")
        return value

    def load_authority(
        self, organization_id: str, key_id: str
    ) -> StoredReleaseAuthorityRows:
        with self._connection(immediate=False) as connection:
            row = connection.execute(
                """
                SELECT
                    root.document_bytes AS root_bytes,
                    root.document_sha256 AS root_document_sha256,
                    root.size_bytes AS root_size_bytes,
                    principal.document_bytes AS principal_bytes,
                    principal.document_sha256 AS principal_document_sha256,
                    principal.size_bytes AS principal_size_bytes,
                    signing_key.document_bytes AS key_bytes,
                    signing_key.document_sha256 AS key_document_sha256,
                    signing_key.size_bytes AS key_size_bytes
                FROM agent_current_release_signing_keys AS signing_key
                JOIN agent_current_release_principals AS principal
                    ON principal.principal_digest = signing_key.principal_digest
                JOIN agent_current_release_trust_roots AS root
                    ON root.root_digest = signing_key.root_digest
                WHERE signing_key.organization_id = ? AND signing_key.key_id = ?
                """,
                (organization_id, key_id),
            ).fetchone()
        if row is None:
            raise ReleaseTrustStoreError("RELEASE_TRUST_NOT_FOUND")
        return StoredReleaseAuthorityRows(
            self._checked_bytes(row, "root"),
            self._checked_bytes(row, "principal"),
            self._checked_bytes(row, "key"),
        )


__all__ = [
    "ReleaseTrustStore",
    "ReleaseTrustStoreError",
    "StoredReleaseAuthorityRows",
]
