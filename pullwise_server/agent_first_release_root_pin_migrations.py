"""SQLite schema for externally enrolled release trust-root pins."""

from __future__ import annotations

import sqlite3


CURRENT_RELEASE_ROOT_PIN_TABLE = "agent_current_release_trust_root_pins"


_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {CURRENT_RELEASE_ROOT_PIN_TABLE} (
    organization_id TEXT NOT NULL
        CHECK(length(organization_id) BETWEEN 5 AND 68)
        CHECK(substr(organization_id, 1, 4) = 'org_')
        CHECK(organization_id NOT GLOB '*[^a-z0-9_]*'),
    root_digest TEXT NOT NULL
        CHECK(length(root_digest) = 64)
        CHECK(root_digest NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (organization_id, root_digest)
)
"""


def _immutable_trigger(operation: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS
        {CURRENT_RELEASE_ROOT_PIN_TABLE}_immutable_{operation.lower()}
    BEFORE {operation} ON {CURRENT_RELEASE_ROOT_PIN_TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'agent current release root pins are immutable');
    END
    """


def install_current_release_root_pin_table(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(_CREATE_TABLE)
    connection.execute(_immutable_trigger("UPDATE"))
    connection.execute(_immutable_trigger("DELETE"))


__all__ = [
    "CURRENT_RELEASE_ROOT_PIN_TABLE",
    "install_current_release_root_pin_table",
]
