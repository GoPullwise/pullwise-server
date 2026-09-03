from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Callable
from typing import Protocol

from .model_gateway_store import ConnectFactory


class RetirableSecretWriter(Protocol):
    def retire_version(self, secret_ref: str, version: str) -> None: ...


class SecretCleanupPending(RuntimeError):
    pass


class SecretCleanupCoordinator:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        secret_writer: RetirableSecretWriter,
        clock: Callable[[], int],
    ) -> None:
        self._connect_factory = connect_factory
        self._secret_writer = secret_writer
        self._clock = clock

    def retire_or_queue(self, secret_ref: str, version: str, *, reason: str) -> None:
        try:
            self._secret_writer.retire_version(secret_ref, version)
            return
        except BaseException:
            pass
        timestamp = int(self._clock())
        try:
            with closing(self._connect_factory()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO model_gateway_secret_cleanup_tasks (
                        secret_ref, secret_version, reason, status, attempts,
                        created_at, last_attempt_at, retired_at
                    ) VALUES (?, ?, ?, 'pending', 1, ?, ?, NULL)
                    ON CONFLICT(secret_ref, secret_version) DO UPDATE SET
                        reason = excluded.reason,
                        status = 'pending',
                        attempts = model_gateway_secret_cleanup_tasks.attempts + 1,
                        last_attempt_at = excluded.last_attempt_at,
                        retired_at = NULL
                    """,
                    (secret_ref, version, reason[:64], timestamp, timestamp),
                )
                connection.commit()
        except BaseException:
            raise SecretCleanupPending(
                "provider secret cleanup failed without durable retry state"
            ) from None
        raise SecretCleanupPending("provider secret cleanup queued for retry")

    def retry_pending(self, *, limit: int = 100) -> dict[str, int]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("secret cleanup retry limit is invalid")
        with closing(self._connect_factory()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT secret_ref, secret_version
                FROM model_gateway_secret_cleanup_tasks
                WHERE status = 'pending'
                ORDER BY created_at, secret_ref, secret_version
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        retired = 0
        for row in rows:
            succeeded = True
            try:
                self._secret_writer.retire_version(row["secret_ref"], row["secret_version"])
            except BaseException:
                succeeded = False
            timestamp = int(self._clock())
            with closing(self._connect_factory()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE model_gateway_secret_cleanup_tasks
                    SET status = ?, attempts = attempts + 1,
                        last_attempt_at = ?, retired_at = ?
                    WHERE secret_ref = ? AND secret_version = ? AND status = 'pending'
                    """,
                    (
                        "retired" if succeeded else "pending",
                        timestamp,
                        timestamp if succeeded else None,
                        row["secret_ref"],
                        row["secret_version"],
                    ),
                )
                connection.commit()
            retired += int(succeeded)
        pending = self.pending_count()
        return {"attempted": len(rows), "retired": retired, "pending": pending}

    def pending_count(self) -> int:
        with closing(self._connect_factory()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM model_gateway_secret_cleanup_tasks WHERE status = 'pending'"
            ).fetchone()
        return int(row[0])
