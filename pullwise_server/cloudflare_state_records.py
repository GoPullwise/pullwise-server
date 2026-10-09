"""Bounded, exact-key state records in the existing canonical app_state table.

No reader migrates or writes state. Runtime cutover is an explicit, journaled
operation; local fixtures call their explicit normalization helper.
"""
from __future__ import annotations

import hashlib
import json
import re

STATE_KINDS = ("users", "sessions", "githubStates", "billingEvents", "billingPendingUpdates",
               "emailIdentities", "emailChallenges", "githubIdentities")
STATE_STORAGE_VERSION = 1
USER_RECORD_BYTES = 512 * 1024
SMALL_RECORD_BYTES = 8192
MAX_RECORD_ID_BYTES = 4096
MAX_RECORD_PAGE_BYTES = 8 * 1024 * 1024
MAX_SAFE_INTEGER = 9007199254740991
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CHALLENGE_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def record_name(kind, identity):
    if kind not in STATE_KINDS or not isinstance(identity, str) or not identity:
        raise ValueError("invalid state record identity")
    if any(ord(char) < 32 or ord(char) == 127 for char in identity):
        raise ValueError("invalid state record identity")
    if len(identity.encode("utf-8")) > MAX_RECORD_ID_BYTES:
        raise ValueError("state record identity is too large")
    if kind in {"emailIdentities", "emailChallenges"} and not _HEX_DIGEST.fullmatch(identity):
        raise ValueError("invalid email state identity")
    if kind == "githubIdentities" and not (
            identity.isascii() and identity.isdigit() and not identity.startswith("0")
            and len(identity) <= 16 and int(identity) <= MAX_SAFE_INTEGER):
        raise ValueError("invalid GitHub state identity")
    return f"record:{kind}:{identity}"


def split_record_name(name):
    if not isinstance(name, str) or not name.startswith("record:"):
        raise ValueError("invalid state record name")
    _, kind, identity = name.split(":", 2)
    if record_name(kind, identity) != name:
        raise ValueError("invalid state record name")
    return kind, identity


def record_limit(kind):
    if kind not in STATE_KINDS:
        raise ValueError("invalid state record kind")
    return USER_RECORD_BYTES if kind == "users" else SMALL_RECORD_BYTES


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate state record key")
        value[key] = item
    return value


def _clock(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _email_identity(identity, email):
    return (isinstance(email, str) and len(email) <= 254 and _EMAIL.fullmatch(email)
            and hashlib.sha256(("pullwise-email:" + email).encode("ascii")).hexdigest() == identity)


def _decode_identity_record(kind, identity, value):
    if kind == "emailIdentities":
        if (set(value) != {"email", "userId", "createdAt", "verifiedAt"}
                or not _email_identity(identity, value.get("email"))
                or not _clock(value.get("createdAt")) or not _clock(value.get("verifiedAt"))
                or not value["createdAt"] <= value["verifiedAt"]):
            raise ValueError("invalid email identity record")
        record_name("users", value.get("userId"))
    elif kind == "emailChallenges":
        fields = {"email", "challengeId", "purpose", "codeHash", "browserHash", "attempts",
                  "createdAt", "expiresAt"}
        if value.get("purpose") == "link":
            fields |= {"userId", "sessionId"}
        if (set(value) != fields or value.get("purpose") not in {"login", "link"}
                or not _email_identity(identity, value.get("email"))
                or not isinstance(value.get("challengeId"), str)
                or not _CHALLENGE_ID.fullmatch(value["challengeId"])
                or any(not isinstance(value.get(field), str) or not _HEX_DIGEST.fullmatch(value[field])
                       for field in ("codeHash", "browserHash"))
                or type(value.get("attempts")) is not int or not 0 <= value["attempts"] <= 5
                or not _clock(value.get("createdAt")) or not _clock(value.get("expiresAt"))
                or not value["createdAt"] < value["expiresAt"] <= value["createdAt"] + 600):
            raise ValueError("invalid email challenge record")
        if value["purpose"] == "link":
            record_name("users", value["userId"])
            record_name("sessions", value["sessionId"])
    elif kind == "githubIdentities":
        if (set(value) != {"githubId", "userId", "createdAt"}
                or value.get("githubId") != identity
                or not _clock(value.get("createdAt")) or value["createdAt"] == 0):
            raise ValueError("invalid GitHub identity record")
        record_name("users", value.get("userId"))


def decode_record(kind, identity, payload):
    record_name(kind, identity)
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > record_limit(kind):
        raise ValueError("state record exceeds its byte bound")
    value = json.loads(payload, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("state record must be an object")
    # Validate non-finite numbers and Unicode throughout opaque retained fields.
    json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    if kind == "users" and value.get("id") != identity:
        raise ValueError("state user identity mismatch")
    if kind == "sessions" and value.get("id", identity) != identity:
        raise ValueError("state session identity mismatch")
    if kind == "billingPendingUpdates" and value.get("eventId") != identity:
        raise ValueError("pending event identity mismatch")
    _decode_identity_record(kind, identity, value)
    return value


def encode_record(kind, identity, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    decode_record(kind, identity, payload)
    return payload


async def read_record_json(binding, kind, identity):
    row = await binding.prepare("SELECT payload AS snapshot FROM app_state WHERE name=?").bind(
        record_name(kind, identity)).first()
    if not row:
        return None
    payload = row.get("snapshot")
    decode_record(kind, identity, payload)
    return payload


async def read_record(binding, kind, identity):
    payload = await read_record_json(binding, kind, identity)
    return decode_record(kind, identity, payload) if payload is not None else None


async def read_records(binding, kind, *, limit=100, cursor=None):
    """Bounded keyset enumeration; callers never load an unbounded global map."""
    page_limit = min(1000, MAX_RECORD_PAGE_BYTES // record_limit(kind))
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("state record page limit must be 1..1000")
    limit = min(limit, page_limit)
    prefix = f"record:{kind}:"
    after = record_name(kind, cursor) if cursor is not None else prefix
    result = await binding.batch([binding.prepare("""SELECT name,payload FROM app_state
        WHERE name GLOB ? AND name>? ORDER BY name LIMIT ?""").bind(prefix + "*", after, limit)])
    from .cloudflare_validation_budget import _field
    rows = list(_field(result[0], "results", []))
    records = []
    for row in rows:
        actual_kind, identity = split_record_name(_field(row, "name"))
        if actual_kind != kind:
            raise ValueError("state record page kind mismatch")
        payload = _field(row, "payload")
        records.append({"id": identity, "snapshot": payload,
                        "record": decode_record(kind, identity, payload)})
    return records


def changed_guard():
    return ("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)", ())


def write_record_commands(kind, identity, value, *, expected_json, now):
    name, payload = record_name(kind, identity), encode_record(kind, identity, value)
    if type(now) is not int or now < 0:
        raise ValueError("invalid state record clock")
    if expected_json is None:
        command = ("""INSERT INTO app_state(name,payload,updated_at)
            SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)""",
            (name, payload, now, name))
    else:
        decode_record(kind, identity, expected_json)
        command = ("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?",
                   (payload, now, name, expected_json))
    return [command, changed_guard(), ("DELETE FROM d1_command_guard", ())]


async def expired_record_commands(binding, kind, *, now, limit=8):
    """Clean only a finite page of expired ephemeral records during issuance."""
    if (kind not in {"sessions", "githubStates", "emailChallenges"}
            or not _clock(now) or type(limit) is not int or not 1 <= limit <= 8):
        raise ValueError("invalid ephemeral state cleanup")
    expiry = "<=" if kind == "emailChallenges" else "<"
    result = await binding.batch([binding.prepare("""SELECT name,payload FROM app_state
        WHERE name GLOB ? AND json_type(payload,'$.expiresAt')='integer'
          AND json_extract(payload,'$.expiresAt')""" + expiry + "? ORDER BY name LIMIT ?").bind(
              f"record:{kind}:*", now, limit)])
    from .cloudflare_validation_budget import _field
    commands = []
    for row in list(_field(result[0], "results", [])):
        name, payload = _field(row, "name"), _field(row, "payload")
        actual, identity = split_record_name(name)
        if actual != kind:
            raise ValueError("invalid ephemeral state cleanup result")
        decode_record(kind, identity, payload)
        commands.extend([("DELETE FROM app_state WHERE name=? AND payload=?", (name, payload)), changed_guard()])
    return commands


def legacy_record_rows(rows):
    """Pure, finite copy input. Preserve every accepted legacy field verbatim."""
    if not isinstance(rows, (list, tuple)) or len(rows) > 6:
        raise ValueError("invalid legacy state rows")
    records, sources, seen = [], [], set()
    for row in rows:
        name, payload = row.get("name"), row.get("payload")
        if (not isinstance(name, str) or name in seen or not isinstance(payload, str)
                or len(payload.encode("utf-8")) > SMALL_RECORD_BYTES):
            raise ValueError("invalid legacy state source")
        if name.startswith("record:"):
            raise ValueError("state cutover refuses pre-existing record rows")
        seen.add(name)
        value = json.loads(payload, object_pairs_hook=_unique_object)
        if name not in STATE_KINDS:
            if value not in ({}, []):
                raise ValueError("unknown nonempty legacy state")
            continue
        if name == "billingPendingUpdates":
            if not isinstance(value, list):
                raise ValueError("invalid legacy pending state")
            items = [(item.get("eventId"), item) for item in value if isinstance(item, dict)]
            if len(items) != len(value) or len({identity for identity, _ in items}) != len(items):
                raise ValueError("invalid legacy pending identities")
            empty = "[]"
        else:
            if not isinstance(value, dict):
                raise ValueError("invalid legacy state map")
            items, empty = list(value.items()), "{}"
        for identity, item in items:
            records.append((record_name(name, identity), encode_record(name, identity, item)))
        sources.append((name, payload, empty))
    return records, sources


def record_parameter_limits(sql, params):
    """A large user JSON requires its exact user-record key in this SQL group.

    Application SQL is trusted and reviewed separately; client input cannot
    choose SQL. All other parameters keep their existing small envelope.
    """
    if "app_state" not in sql.lower():
        return {}
    names = []
    for value in params:
        if isinstance(value, str) and value.startswith("record:"):
            try:
                names.append(split_record_name(value))
            except ValueError:
                continue
    limits = {}
    for index, value in enumerate(params):
        if not isinstance(value, str) or not value.lstrip().startswith("{"):
            continue
        try:
            decoded = json.loads(value, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            continue
        for kind, identity in names:
            if kind == "users" and isinstance(decoded, dict) and decoded.get("id") == identity:
                decode_record(kind, identity, value)
                limits[index] = record_limit(kind)
    # Every scalar payload replacement/insertion is an independently proven
    # record. No ordinary runtime command may update a legacy global map.
    source = " ".join(sql.lower().split())
    if source.startswith(("insert into app_state", "update app_state")):
        if not names or "json_set(" in source:
            raise ValueError("state writes require exact typed records")
        candidates = [value for value in params if isinstance(value, str) and value.lstrip().startswith("{")]
        if not candidates:
            raise ValueError("state write has no typed payload")
        name_index, payload_index = (0, 1) if source.startswith("insert") else (2, 0)
        try:
            target_kind, target_identity = split_record_name(params[name_index])
            decode_record(target_kind, target_identity, params[payload_index])
        except (ValueError, IndexError, UnicodeError, RecursionError):
            raise ValueError("state mutation target is not a typed record") from None
        for candidate in candidates:
            valid = False
            for kind, identity in names:
                try:
                    decode_record(kind, identity, candidate)
                    valid = True
                    break
                except (ValueError, UnicodeError, RecursionError):
                    pass
            if not valid:
                raise ValueError("state write payload does not match a typed record")
    return limits
