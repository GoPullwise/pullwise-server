from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any


_LOCK = threading.Lock()


def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def database_path() -> str:
    configured = os.environ.get("PULLWISE_DB_PATH") or os.environ.get("PULLWISE_SQLITE_PATH")
    if configured:
        return configured

    database_url = os.environ.get("PULLWISE_DATABASE_URL", "")
    if database_url.startswith("sqlite:///"):
        return database_url.removeprefix("sqlite:///")

    return os.path.join(project_root(), ".pullwise", "pullwise.sqlite3")


def connect() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    with _LOCK, connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                name TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            )
            """
        )


def load_state() -> dict[str, Any]:
    initialize()
    with _LOCK, connect() as connection:
        rows = connection.execute("SELECT name, payload FROM app_state").fetchall()
    return {name: json.loads(payload) for name, payload in rows}


def save_state(state: dict[str, Any]) -> None:
    initialize()
    with _LOCK, connect() as connection:
        connection.executemany(
            """
            INSERT INTO app_state (name, payload, updated_at)
            VALUES (?, ?, strftime('%s', 'now'))
            ON CONFLICT(name) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            [(name, json.dumps(payload, ensure_ascii=False)) for name, payload in state.items()],
        )
