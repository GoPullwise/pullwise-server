"""Preview abuse protection using bounded, hashed DO counters, never D1."""
from __future__ import annotations

import hashlib

from .cloudflare_validation_budget import _field, _sql_number


class PreviewRateLimit(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, min(120, int(retry_after)))
        super().__init__("PREVIEW_RATE_LIMIT")

    def response(self):
        return {"error": {"code": "PREVIEW_RATE_LIMIT",
            "message": "Too many requests. Please try again shortly."}}


def _hash(channel, value):
    return hashlib.sha256((channel + ":" + value).encode("utf-8", errors="replace")).hexdigest()


def request_channel(method, path):
    if method == "DELETE" and (path.startswith("/api-keys/")
            or "/members/" in path or "/invitations/" in path):
        # Keep emergency revocation separate from ordinary write saturation.
        return "security"
    if method in {"POST", "PATCH", "DELETE"} or path in {
            "/auth/github/authorize", "/auth/github/callback",
            "/integrations/github/authorize", "/integrations/github/callback"}:
        return "write"
    return "read"


class PreviewRateLimiter:
    MAX_SUBJECTS = 20_000
    ACTOR_LIMITS = {"read": 120, "write": 60, "security": 120}

    def __init__(self, sql):
        self.sql = sql
        sql.exec("""CREATE TABLE IF NOT EXISTS preview_rate_clock (
            id INTEGER PRIMARY KEY CHECK(id=1), minute INTEGER NOT NULL,
            subjects INTEGER NOT NULL CHECK(subjects>=0))""")
        sql.exec("INSERT OR IGNORE INTO preview_rate_clock(id,minute,subjects) VALUES(1,0,0)")
        sql.exec("""CREATE TABLE IF NOT EXISTS preview_request_rates (
            subject TEXT PRIMARY KEY CHECK(length(subject)=64),
            minute INTEGER NOT NULL, calls INTEGER NOT NULL CHECK(calls>=1))""")
        sql.exec("""CREATE TRIGGER IF NOT EXISTS preview_rate_subject_added
            AFTER INSERT ON preview_request_rates BEGIN
            UPDATE preview_rate_clock SET subjects=subjects+1 WHERE id=1; END""")
        sql.exec("""CREATE TRIGGER IF NOT EXISTS preview_rate_subject_expired
            AFTER DELETE ON preview_request_rates BEGIN
            UPDATE preview_rate_clock SET subjects=subjects-1 WHERE id=1; END""")

    def _check(self, cases, *, now):
        # A late request cannot roll counters back into a previous minute.
        # Only these ephemeral rate buckets expire; accounting never resets.
        clock = self.sql.exec("SELECT minute,subjects FROM preview_rate_clock WHERE id=1").one()
        minute = max(int(now) // 60, int(_field(clock, "minute")))
        retry_after = max(1, (minute + 1) * 60 - int(now))
        if minute > _field(clock, "minute"):
            self.sql.exec("UPDATE preview_rate_clock SET minute=? WHERE id=1", _sql_number(minute))
            self.sql.exec("DELETE FROM preview_request_rates WHERE minute<?", _sql_number(max(0, minute - 1)))
            clock = self.sql.exec("SELECT minute,subjects FROM preview_rate_clock WHERE id=1").one()
        subjects = [subject for subject, _ in cases]
        rows = self.sql.exec("SELECT subject,minute,calls FROM preview_request_rates WHERE subject IN ("
            + ",".join("?" for _ in subjects) + ")", *subjects).toArray()
        existing = {_field(row, "subject"): row for row in rows}
        for subject, limit in cases:
            row = existing.get(subject)
            if row is not None and _field(row, "minute") == minute and _field(row, "calls") >= limit:
                raise PreviewRateLimit(retry_after)
        if _field(clock, "subjects") + sum(subject not in existing for subject in subjects) > self.MAX_SUBJECTS:
            raise PreviewRateLimit(120)
        # All checks and counter writes are synchronous in the same DO turn;
        # no application SQL/provider work can start before admission returns.
        for subject, _ in cases:
            self.sql.exec("""INSERT INTO preview_request_rates(subject,minute,calls) VALUES(?,?,1)
                ON CONFLICT(subject) DO UPDATE SET calls=CASE
                  WHEN preview_request_rates.minute=excluded.minute THEN preview_request_rates.calls+1
                  ELSE 1 END,minute=excluded.minute""", subject, _sql_number(minute))

    def ingress(self, headers, *, method, path, now):
        # Cloudflare supplies CF-Connecting-IP at ingress. No forwarded-IP or
        # client-selected account ID participates in trusted actor admission.
        get = headers.get if headers is not None else lambda _: None
        ip = str(get("cf-connecting-ip") or "missing-cloudflare-ip")[:64]
        cases = [(_hash("ip", ip), 600)]
        credential = str(get("authorization") or get("x-pullwise-api-key") or "")
        if not credential:
            for piece in str(get("cookie") or "").split(";"):
                name, _, value = piece.partition("=")
                if name.strip() == "pw_session" and value.strip():
                    credential = value.strip().strip('"')
                    break
        if credential:
            cases.append((_hash("credential", credential[:8192]), 240))
        if path in {"/auth/github/authorize", "/auth/github/callback",
                    "/integrations/github/authorize", "/integrations/github/callback"}:
            cases.append((_hash("oauth-ip", ip), 10))
        self._check(cases, now=now)

    def actor(self, actor_id, *, channel, now):
        if channel not in self.ACTOR_LIMITS or not isinstance(actor_id, str) or not actor_id:
            raise ValueError("invalid rate admission subject")
        self._check([(_hash("actor-" + channel, actor_id), self.ACTOR_LIMITS[channel])], now=now)


class EmailRateLimit(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, min(3600, int(retry_after)))
        super().__init__("EMAIL_RATE_LIMIT")

    def response(self):
        return {"error": {"code": "EMAIL_RATE_LIMIT",
            "message": "Too many requests. Please try again shortly."}}


class EmailRateLimiter:
    """Synchronous OTP admission in the existing coordinator's DO SQLite.

    Call before sending mail or checking a code. Successful admission remains
    consumed when delivery/checking fails or has an unknown outcome; there is
    no refund or automatic retry. This class does not touch D1 or its journal.
    Callers supply trusted, already-hashed lowercase 64-hex email/IP subjects.
    """
    MAX_SUBJECTS = 20_000
    CLEANUP_LIMIT = 64
    MAX_NOW = 9007199254740991 - 3600

    def __init__(self, sql):
        self.sql = sql
        sql.exec("""CREATE TABLE IF NOT EXISTS email_rate_clock (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_now INTEGER NOT NULL CHECK(last_now>=0),
            subjects INTEGER NOT NULL CHECK(subjects BETWEEN 0 AND 20000))""")
        sql.exec("INSERT OR IGNORE INTO email_rate_clock(id,last_now,subjects) VALUES(1,0,0)")
        sql.exec("""CREATE TABLE IF NOT EXISTS email_request_rates (
            subject TEXT PRIMARY KEY CHECK(length(subject)=64 AND subject NOT GLOB '*[^0-9a-f]*'),
            period_start INTEGER NOT NULL CHECK(period_start>=0),
            calls INTEGER NOT NULL CHECK(calls BETWEEN 1 AND 500),
            last_call INTEGER NOT NULL CHECK(last_call>=0),
            expires_at INTEGER NOT NULL CHECK(expires_at>last_call))""")
        sql.exec("""CREATE INDEX IF NOT EXISTS email_request_rates_expiry
            ON email_request_rates(expires_at,subject)""")
        sql.exec("""CREATE TRIGGER IF NOT EXISTS email_rate_subject_added
            AFTER INSERT ON email_request_rates BEGIN
            UPDATE email_rate_clock SET subjects=subjects+1 WHERE id=1; END""")
        sql.exec("""CREATE TRIGGER IF NOT EXISTS email_rate_subject_expired
            AFTER DELETE ON email_request_rates BEGIN
            UPDATE email_rate_clock SET subjects=subjects-1 WHERE id=1; END""")

    def email(self, kind, *, email_subject64hex, ip_subject64hex, now):
        """Admit one send/verify using UTC fixed windows and a send cooldown.

        Returns None or raises EmailRateLimit. Rejections do not advance clocks,
        consume other buckets, or perform cleanup. An accepted call expires at
        most 64 indexed stale rows, then publishes all its buckets atomically
        in one bounded VALUES INSERT, with no await before admission returns.
        """
        if (type(kind) is not str or kind not in {"send", "verify"} or type(now) not in {int, float}
                or not 0 <= now <= self.MAX_NOW
                or any(not isinstance(value, str) or len(value) != 64
                       or any(char not in "0123456789abcdef" for char in value)
                       for value in (email_subject64hex, ip_subject64hex))):
            raise ValueError("invalid email rate admission")
        clock = self.sql.exec("SELECT last_now,subjects FROM email_rate_clock WHERE id=1").one()
        # Late requests and object reconstruction cannot reopen an older window
        # or shorten the last admitted send's cross-hour cooldown.
        effective_now = max(int(now), int(_field(clock, "last_now")))
        window = 3600 if kind == "send" else 60
        period = effective_now // window * window
        cases = [(_hash("email-" + kind, email_subject64hex), 6 if kind == "send" else 30),
                 (_hash("email-ip-" + kind, ip_subject64hex), 30 if kind == "send" else 10)]
        if kind == "send":
            cases.append((_hash("email-send-global", "all"), 500))
        subjects = [subject for subject, _ in cases]
        placeholders = ",".join("?" for _ in subjects)
        rows = self.sql.exec("SELECT subject,period_start,calls,last_call FROM email_request_rates "
            "WHERE subject IN (" + placeholders + ")", *subjects).toArray()
        existing = {_field(row, "subject"): row for row in rows}
        retry_after = 0
        for subject, limit in cases:
            row = existing.get(subject)
            if row is not None and _field(row, "period_start") == period and _field(row, "calls") >= limit:
                retry_after = max(retry_after, period + window - effective_now)
        email_row = existing.get(subjects[0])
        if kind == "send" and email_row is not None:
            retry_after = max(retry_after, int(_field(email_row, "last_call")) + 60 - effective_now)
        if retry_after > 0:
            raise EmailRateLimit(retry_after)
        # Indexed expiry enumeration is bounded even at capacity. Excluding the
        # at-most-three target keys keeps existing bucket updates in place.
        expired = self.sql.exec("SELECT subject FROM email_request_rates "
            "INDEXED BY email_request_rates_expiry WHERE expires_at<=? AND subject NOT IN ("
            + placeholders + ") ORDER BY expires_at,subject LIMIT ?",
            _sql_number(effective_now), *subjects, _sql_number(self.CLEANUP_LIMIT)).toArray()
        expired_subjects = [_field(row, "subject") for row in expired]
        new_subjects = sum(subject not in existing for subject in subjects)
        if int(_field(clock, "subjects")) - len(expired_subjects) + new_subjects > self.MAX_SUBJECTS:
            raise EmailRateLimit(60)
        if expired_subjects:
            self.sql.exec("DELETE FROM email_request_rates WHERE subject IN ("
                + ",".join("?" for _ in expired_subjects) + ")", *expired_subjects)
        # Advance the durable clock before publishing counters. If SQL fails,
        # no provider work is admitted and time still cannot roll backwards.
        self.sql.exec("UPDATE email_rate_clock SET last_now=? WHERE id=1", _sql_number(effective_now))
        values = []
        for index, (subject, _) in enumerate(cases):
            row = existing.get(subject)
            calls = int(_field(row, "calls")) + 1 if row is not None and _field(row, "period_start") == period else 1
            expires = max(period + window, effective_now + 60) if kind == "send" and index == 0 else period + window
            values.extend((subject, _sql_number(period), _sql_number(calls),
                           _sql_number(effective_now), _sql_number(expires)))
        self.sql.exec("INSERT INTO email_request_rates(subject,period_start,calls,last_call,expires_at) VALUES "
            + ",".join("(?,?,?,?,?)" for _ in cases)
            + " ON CONFLICT(subject) DO UPDATE SET period_start=excluded.period_start,calls=excluded.calls,"
              "last_call=excluded.last_call,expires_at=excluded.expires_at", *values)
