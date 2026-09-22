from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


_PROTECTED_STATE_NAMES = ("users", "sessions", "githubStates", "billingEvents")
_ACTIVE_SCAN_STATUSES = ("queued", "claimed", "running", "uploading_result")


def _read_only_connection(database_path: str | Path) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _collection_size(payload: object) -> int:
    if isinstance(payload, dict | list):
        return len(payload)
    return 0


def inspect_legacy_database(database_path: str | Path) -> dict:
    protected = {name: 0 for name in _PROTECTED_STATE_NAMES}
    active_scan_jobs = 0
    reserved_scan_units = 0
    reservation_entries = 0
    with closing(_read_only_connection(database_path)) as connection:
        if _table_exists(connection, "app_state"):
            placeholders = ",".join("?" for _ in _PROTECTED_STATE_NAMES)
            rows = connection.execute(
                f"SELECT name, payload FROM app_state WHERE name IN ({placeholders})",
                _PROTECTED_STATE_NAMES,
            ).fetchall()
            for name, payload_json in rows:
                try:
                    protected[str(name)] = _collection_size(json.loads(payload_json))
                except (TypeError, json.JSONDecodeError):
                    protected[str(name)] = 0
        if _table_exists(connection, "scan_jobs"):
            placeholders = ",".join("?" for _ in _ACTIVE_SCAN_STATUSES)
            active_scan_jobs = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM scan_jobs WHERE status IN ({placeholders})",
                    _ACTIVE_SCAN_STATUSES,
                ).fetchone()[0]
            )
        if _table_exists(connection, "quota_buckets"):
            rows = connection.execute("SELECT reserved FROM quota_buckets").fetchall()
            for (value,) in rows:
                if isinstance(value, bool):
                    continue
                try:
                    reserved_scan_units += max(0, int(value))
                except (TypeError, ValueError):
                    continue
        if _table_exists(connection, "quota_ledger"):
            reservation_entries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM quota_ledger WHERE reason = 'scan_reserved'"
                ).fetchone()[0]
            )
    return {
        "protectedState": protected,
        "activeScanJobs": active_scan_jobs,
        "reservedScanUnits": reserved_scan_units,
        "scanReservationLedgerEntries": reservation_entries,
        "requiresSettlement": bool(active_scan_jobs or reserved_scan_units or reservation_entries),
    }
