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
