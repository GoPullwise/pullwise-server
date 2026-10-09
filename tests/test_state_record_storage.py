"""Exact state records stay bounded without cross-account rewrites or GET writes."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed
from pullwise_server.cloudflare_state_records import (
    decode_record, encode_record, expired_record_commands, read_records,
    record_name, write_record_commands,
)


def email_key(email="alice@example.test"):
    return hashlib.sha256(("pullwise-email:" + email).encode("ascii")).hexdigest()


def email_challenge(email="alice@example.test", **changes):
    return {"email": email, "challengeId": "a" * 43, "purpose": "login", "codeHash": "b" * 64,
            "browserHash": "c" * 64, "attempts": 0, "createdAt": 100, "expiresAt": 700, **changes}


def test_email_and_github_identity_records_keep_exact_key_and_bounded_semantics():
    records = [
        ("emailIdentities", email_key(), {"email": "alice@example.test", "userId": "usr_email_random",
            "createdAt": 100, "verifiedAt": 101}),
        ("emailChallenges", email_key(), email_challenge()),
        ("emailChallenges", email_key(), email_challenge(purpose="link", userId="usr_github_77",
            sessionId="ses-existing")),
        ("githubIdentities", "77", {"githubId": "77", "userId": "usr_email_random", "createdAt": 100}),
    ]
    for kind, identity, value in records:
        assert decode_record(kind, identity, encode_record(kind, identity, value)) == value


@pytest.mark.parametrize("kind,identity", [
    ("emailIdentities", "alice@example.test"), ("emailChallenges", "A" * 64),
    ("emailChallenges", "a" * 63), ("githubIdentities", "0"), ("githubIdentities", "077"),
    ("githubIdentities", "9007199254740992"), ("githubIdentities", "７７"),
])
def test_auth_identity_keys_reject_raw_email_and_noncanonical_github_ids(kind, identity):
    with pytest.raises(ValueError):
        record_name(kind, identity)


@pytest.mark.parametrize("changes", [
    {"email": "other@example.test"}, {"email": "Alice@example.test"},
    {"challengeId": "a" * 42}, {"challengeId": "+" * 43}, {"codeHash": "B" * 64},
    {"browserHash": "secret"}, {"attempts": True}, {"attempts": -1}, {"attempts": 6},
    {"createdAt": True}, {"expiresAt": 100}, {"expiresAt": 701},
    {"purpose": "register"}, {"purpose": "link"}, {"userId": "unexpected"}, {"otp": "000001"},
])
def test_challenge_semantics_reject_mismatches_excess_attempts_and_plaintext_otp(changes):
    with pytest.raises(ValueError):
        encode_record("emailChallenges", email_key(), email_challenge(**changes))


@pytest.mark.parametrize("kind,identity,value", [
    ("emailIdentities", email_key(), {"email": "alice@example.test", "userId": "",
        "createdAt": 100, "verifiedAt": 101}),
    ("emailIdentities", email_key(), {"email": "alice@example.test", "userId": "owner",
        "createdAt": 100, "verifiedAt": 99}),
    ("githubIdentities", "77", {"githubId": "78", "userId": "owner", "createdAt": 100}),
    ("githubIdentities", "77", {"githubId": "77", "userId": "owner", "createdAt": 0}),
    ("githubIdentities", "77", {"githubId": "77", "userId": "owner", "createdAt": 100,
        "token": "forbidden"}),
])
def test_auth_identity_values_require_their_owner_clock_and_exact_shape(kind, identity, value):
    with pytest.raises(ValueError):
        encode_record(kind, identity, value)


def test_expired_email_cleanup_uses_email_keys_and_keeps_other_domains_untouched(tmp_path):
    fixture, _, _ = seed(tmp_path / "email-expiry.db")
    with fixture.store._immediate() as db:
        for number in range(10):
            email = f"alice{number}@example.test"
            value = email_challenge(email)
            db.execute("INSERT INTO app_state VALUES(?,?,?)", (
                record_name("emailChallenges", email_key(email)),
                encode_record("emailChallenges", email_key(email), value), 100))
        db.execute("INSERT INTO app_state VALUES(?,?,?)", (
            record_name("githubStates", "unchanged"), '{"kind":"login","expiresAt":1}', 100))
    binding = D1ShapedSQLite(fixture.store)
    commands = asyncio.run(expired_record_commands(binding, "emailChallenges", now=700))
    assert len(commands) == 16
    asyncio.run(binding.batch([binding.prepare(sql).bind(*params) for sql, params in commands]
                             + [binding.prepare("DELETE FROM d1_command_guard")]))
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:emailChallenges:*'").fetchone()[0] == 2
        assert db.execute("SELECT payload FROM app_state WHERE name=?", (
            record_name("githubStates", "unchanged"),)).fetchone()[0] == '{"kind":"login","expiresAt":1}'
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


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
