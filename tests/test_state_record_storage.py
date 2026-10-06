"""Exact state records stay bounded without cross-account rewrites or GET writes."""
import asyncio
import json
import sqlite3

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed
from pullwise_server.cloudflare_state_records import (
    decode_record, encode_record, expired_record_commands, read_records,
    record_name, write_record_commands,
)


def test_state_pages_do_not_repeat_skip_or_write_records(tmp_path):
    fixture, _, _ = seed(tmp_path / "pages.db")
    identities = ["cursor:0", "cursor:1", "cursor:2", "cursor:3", "cursor:4", "cursor:雪", "last"]
    with fixture.store._immediate() as db:
        for identity in identities:
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                       (record_name("githubStates", identity),
                        encode_record("githubStates", identity, {"marker": identity, "expiresAt": fixture.now + 60}),
                        fixture.now))
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                   (record_name("sessions", "cursor:1"), '{"userId":"owner","expiresAt":1}', fixture.now))
        before = [tuple(row) for row in db.execute("SELECT * FROM app_state ORDER BY name")]
    binding = D1ShapedSQLite(fixture.store)

    async def enumerate_pages():
        seen, cursor = [], None
        for _ in range(4):
            page = await read_records(binding, "githubStates", limit=3, cursor=cursor)
            assert len(page) <= 3
            if not page:
                return seen
            assert all(item["record"]["marker"] == item["id"] for item in page)
            seen.extend(item["id"] for item in page)
            cursor = page[-1]["id"]
        raise AssertionError("bounded enumeration did not finish")

    assert asyncio.run(enumerate_pages()) == sorted(identities)
    with fixture.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM app_state ORDER BY name")] == before


@pytest.mark.parametrize("payload", [
    '{"id":"owner","id":"intruder"}',
    '{"id":"owner","opaque":{"key":1,"key":2}}',
    '{"id":"owner","opaque":NaN}',
    '{"id":"owner","opaque":Infinity}',
    '{"id":"owner","opaque":"\\ud800"}',
    '{"id":"intruder"}',
])
def test_untrustworthy_user_json_cannot_become_a_stored_record(payload):
    with pytest.raises((ValueError, UnicodeError)):
        decode_record("users", "owner", payload)


def test_unrelated_account_changes_do_not_abort_owner_cas(tmp_path):
    fixture, _, frozen = seed(tmp_path / "owner-cas.db")
    other = {"id": "another-user", "opaque": {"保留": ["field", 7]}}
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                   (record_name("users", other["id"]), encode_record("users", other["id"], other), fixture.now))
    changed = json.loads(frozen)
    changed["opaque"] = {"保留": ["unchanged", 7]}
    binding = D1ShapedSQLite(fixture.store)
    commands = write_record_commands("users", "owner", changed, expected_json=frozen, now=fixture.now)
    other["name"] = "Changed concurrently"
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
                   (encode_record("users", other["id"], other), record_name("users", other["id"])))
    asyncio.run(binding.batch([binding.prepare(sql).bind(*params) for sql, params in commands]))
    with fixture.store.connect() as db:
        assert json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                                    (record_name("users", "owner"),)).fetchone()[0]) == changed
        assert json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                                    (record_name("users", other["id"]),)).fetchone()[0]) == other


def test_expired_session_cleanup_is_finite_and_stale_snapshot_rolls_back(tmp_path):
    fixture, _, _ = seed(tmp_path / "expiry.db")
    with fixture.store._immediate() as db:
        for number in range(10):
            identity = f"expired-{number:02}"
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                       (record_name("sessions", identity),
                        encode_record("sessions", identity, {"userId": "owner", "expiresAt": fixture.now - 1}),
                        fixture.now))
    binding = D1ShapedSQLite(fixture.store)
    commands = asyncio.run(expired_record_commands(binding, "sessions", now=fixture.now))
    assert len(commands) == 16  # Eight selected sessions, each with its own CAS guard.
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
                   (encode_record("sessions", "expired-07", {"userId": "owner", "expiresAt": fixture.now + 60}),
                    record_name("sessions", "expired-07")))
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(binding.batch([binding.prepare(sql).bind(*params) for sql, params in commands] +
                                  [binding.prepare("DELETE FROM d1_command_guard")]))
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:sessions:*'").fetchone()[0] == 10
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0
