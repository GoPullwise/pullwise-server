from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pullwise_server.legacy_product_inventory import inspect_legacy_database


class LegacyProductInventoryTest(unittest.TestCase):
    def test_inventory_counts_protected_state_and_unsettled_scan_work_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "legacy.sqlite3")
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE app_state(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE scan_jobs(job_id TEXT PRIMARY KEY, status TEXT NOT NULL);
                    CREATE TABLE quota_buckets(id TEXT PRIMARY KEY, reserved INTEGER NOT NULL);
                    CREATE TABLE quota_ledger(id TEXT PRIMARY KEY, reason TEXT NOT NULL);
                    """
                )
                connection.executemany(
                    "INSERT INTO app_state(name, payload) VALUES (?, ?)",
                    [
                        ("users", json.dumps({"usr_1": {"billing": {"plan": "pro"}}})),
                        ("sessions", json.dumps({"session_1": {"userId": "usr_1"}})),
                        ("githubStates", json.dumps({"state_1": {"userId": "usr_1"}})),
                        ("billingEvents", json.dumps({"event_1": {"type": "subscription.active"}})),
                    ],
                )
                connection.executemany(
                    "INSERT INTO scan_jobs(job_id, status) VALUES (?, ?)",
                    [("job_1", "running"), ("job_2", "succeeded")],
                )
                connection.execute("INSERT INTO quota_buckets(id, reserved) VALUES ('bucket_1', 2)")
                connection.execute("INSERT INTO quota_ledger(id, reason) VALUES ('ledger_1', 'scan_reserved')")

            before = os.path.getsize(database_path)
            inventory = inspect_legacy_database(database_path)
            after = os.path.getsize(database_path)

        self.assertEqual(inventory["protectedState"], {
            "users": 1,
            "sessions": 1,
            "githubStates": 1,
            "billingEvents": 1,
        })
        self.assertEqual(inventory["activeScanJobs"], 1)
        self.assertEqual(inventory["reservedScanUnits"], 2)
        self.assertEqual(inventory["scanReservationLedgerEntries"], 1)
        self.assertTrue(inventory["requiresSettlement"])
        self.assertEqual(after, before)

    def test_inventory_handles_missing_legacy_tables_without_creating_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "empty.sqlite3")
            with closing(sqlite3.connect(database_path)):
                pass

            inventory = inspect_legacy_database(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(inventory["activeScanJobs"], 0)
        self.assertFalse(inventory["requiresSettlement"])
        self.assertEqual(tables, [])


if __name__ == "__main__":
    unittest.main()
