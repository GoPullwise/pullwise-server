"""Durable external trust-root pin policy for Agent-First releases."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
import sqlite3
from typing import Callable, Iterator

from .agent_first_release_root_pin_migrations import (
    CURRENT_RELEASE_ROOT_PIN_TABLE,
)


_ORGANIZATION_ID = re.compile(r"^org_[a-z0-9_]{1,64}$")
_ROOT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
FaultInjector = Callable[[str], None]
RELEASE_ROOT_PIN_FAULT_POINTS = (
    "before_root_pin",
    "after_root_pin",
)


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    base_code = error_code & 0xFF if isinstance(error_code, int) else None
    return base_code in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    } or any(word in str(error).lower() for word in ("busy", "locked"))


class ReleaseRootPinStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredReleaseRootPin:
    organization_id: str
    root_digest: str


class ReleaseRootPinStore:
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
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except ReleaseRootPinStoreError:
            connection.rollback()
            raise
        except sqlite3.OperationalError as error:
            connection.rollback()
            if _is_lock_contention(error):
                raise
            raise ReleaseRootPinStoreError("AUTHORITY_STORAGE_CORRUPT") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise ReleaseRootPinStoreError("AUTHORITY_STORAGE_CORRUPT") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate(organization_id: object, root_digest: object) -> tuple[str, str]:
        if (
            not isinstance(organization_id, str)
            or _ORGANIZATION_ID.fullmatch(organization_id) is None
            or not isinstance(root_digest, str)
            or _ROOT_DIGEST.fullmatch(root_digest) is None
        ):
            raise ReleaseRootPinStoreError("ROOT_PIN_INVALID")
        return organization_id, root_digest

    def enroll(
        self,
        organization_id: object,
        root_digest: object,
    ) -> StoredReleaseRootPin:
        organization_id, root_digest = self._validate(
            organization_id, root_digest
        )
        with self._connection(immediate=True) as connection:
            self._fault("before_root_pin")
            row = connection.execute(
                f"""
                SELECT organization_id, root_digest
                FROM {CURRENT_RELEASE_ROOT_PIN_TABLE}
                WHERE organization_id = ? AND root_digest = ?
                """,
                (organization_id, root_digest),
            ).fetchone()
            if row is None:
                connection.execute(
                    f"""
                    INSERT INTO {CURRENT_RELEASE_ROOT_PIN_TABLE}
                        (organization_id, root_digest)
                    VALUES (?, ?)
                    """,
                    (organization_id, root_digest),
                )
            elif tuple(row) != (organization_id, root_digest):
                raise ReleaseRootPinStoreError("AUTHORITY_STORAGE_CORRUPT")
            self._fault("after_root_pin")
        return StoredReleaseRootPin(organization_id, root_digest)

    def is_trusted(self, organization_id: object, root_digest: object) -> bool:
        organization_id, root_digest = self._validate(
            organization_id, root_digest
        )
        with self._connection(immediate=False) as connection:
            row = connection.execute(
                f"""
                SELECT organization_id, root_digest
                FROM {CURRENT_RELEASE_ROOT_PIN_TABLE}
                WHERE organization_id = ? AND root_digest = ?
                """,
                (organization_id, root_digest),
            ).fetchone()
            if row is None:
                return False
            if tuple(row) != (organization_id, root_digest):
                raise ReleaseRootPinStoreError("AUTHORITY_STORAGE_CORRUPT")
            return True


__all__ = [
    "RELEASE_ROOT_PIN_FAULT_POINTS",
    "ReleaseRootPinStore",
    "ReleaseRootPinStoreError",
    "StoredReleaseRootPin",
]
