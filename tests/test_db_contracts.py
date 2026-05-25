from __future__ import annotations

import unittest
from unittest.mock import patch

from pullwise_server import app, db


class FakeConnection:
    def __init__(self, rows: list[tuple[str, str]] | None = None) -> None:
        self.rows = rows or []
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def execute(self, *_args, **_kwargs) -> "FakeConnection":
        return self

    def executemany(self, *_args, **_kwargs) -> None:
        return None

    def fetchall(self) -> list[tuple[str, str]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class DatabaseContractsTest(unittest.TestCase):
    def test_initialize_closes_sqlite_connection(self) -> None:
        connection = FakeConnection()

        with patch("pullwise_server.db.connect", return_value=connection):
            db.initialize()

        self.assertTrue(connection.closed)

    def test_load_state_closes_initialize_and_read_connections(self) -> None:
        initialize_connection = FakeConnection()
        read_connection = FakeConnection([("users", "{}")])

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, read_connection]):
            self.assertEqual(db.load_state(), {"users": {}})

        self.assertTrue(initialize_connection.closed)
        self.assertTrue(read_connection.closed)

    def test_load_state_ignores_malformed_json_rows(self) -> None:
        initialize_connection = FakeConnection()
        read_connection = FakeConnection([
            ("users", '{"usr_1": {"id": "usr_1"}}'),
            ("sessions", "{not-json"),
        ])

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, read_connection]):
            self.assertEqual(db.load_state(), {"users": {"usr_1": {"id": "usr_1"}}})

    def test_save_state_closes_initialize_and_write_connections(self) -> None:
        initialize_connection = FakeConnection()
        write_connection = FakeConnection()

        with patch("pullwise_server.db.connect", side_effect=[initialize_connection, write_connection]):
            db.save_state({"users": {}})

        self.assertTrue(initialize_connection.closed)
        self.assertTrue(write_connection.closed)

    def test_persist_state_keeps_dirty_when_save_fails(self) -> None:
        with (
            patch.object(app, "STATE_LOADED", True),
            patch.object(app, "STATE_DIRTY", True),
            patch.object(app.db, "save_state", side_effect=RuntimeError("disk full")),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.persist_state()

            self.assertTrue(app.STATE_DIRTY)
            log_exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()
