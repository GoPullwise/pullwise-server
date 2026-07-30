"""SQLite mutation guards shared by the current Server authority schema."""

from __future__ import annotations

import sqlite3
from typing import Iterable


def install_authority_guards(
    connection: sqlite3.Connection,
    immutable_tables: Iterable[str],
    state_tables: Iterable[tuple[str, str]],
) -> None:
    for table in immutable_tables:
        for operation in ("UPDATE", "DELETE"):
            trigger = f"{table}_{operation.lower()}_immutable"
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table.upper()}_IMMUTABLE');
                END
                """
            )
    for table, live_state in state_tables:
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_terminal_permanent
            BEFORE UPDATE ON {table}
            WHEN OLD.state!='{live_state}'
            BEGIN
                SELECT RAISE(ABORT, '{table.upper()}_TERMINAL_PERMANENT');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_delete_immutable
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table.upper()}_IMMUTABLE');
            END
            """
        )


__all__ = ["install_authority_guards"]
