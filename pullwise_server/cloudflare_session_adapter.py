"""Trusted session map commands over the persisted D1 account authority."""
from __future__ import annotations

import json
from typing import Any


class D1SessionTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def issue_session(self, *, owner_id: str, session_id: str,
                            now: int, expires_at: int) -> dict:
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(session_id, str) or not session_id.startswith("ses-")
                or any(char in session_id for char in "\r\n\x00")
                or type(now) is not int or type(expires_at) is not int
                or not now < expires_at <= now + 30 * 86400):
            raise ValueError("invalid trusted session command")
        user = await self.binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
        if not user:
            raise ValueError("UNAUTHENTICATED")
        row = await self.binding.prepare("SELECT payload FROM app_state WHERE name='sessions'").first()
        sessions = json.loads(row["payload"]) if row else {}
        if not isinstance(sessions, dict) or session_id in sessions:
            raise ValueError("SESSION_ALREADY_EXISTS")
        session = {"id": session_id, "userId": owner_id,
                   "createdAt": now, "expiresAt": expires_at}
        next_payload = json.dumps({**sessions, session_id: session},
            separators=(",", ":"), ensure_ascii=False)
        if row is None:
            command = self.binding.prepare("""INSERT INTO app_state(name,payload,updated_at)
                SELECT 'sessions',?,? WHERE NOT EXISTS(
                    SELECT 1 FROM app_state WHERE name='sessions')
                  AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                    WHERE a.name='users' AND u.key=? AND u.value=?)""").bind(
                        next_payload, now, owner_id, user["snapshot"])
        else:
            command = self.binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
                WHERE name='sessions' AND payload=?
                  AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                    WHERE a.name='users' AND u.key=? AND u.value=?)""").bind(
                        next_payload, now, row["payload"], owner_id, user["snapshot"])
        await self.binding.batch([command,
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("DELETE FROM d1_command_guard")])
        return session

    async def revoke_session(self, *, owner_id: str, session_id: str,
                             now: int) -> bool:
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(session_id, str) or not session_id
                or type(now) is not int):
            raise ValueError("invalid session revocation")
        row = await self.binding.prepare("SELECT payload FROM app_state WHERE name='sessions'").first()
        if row is None:
            return False
        sessions = json.loads(row["payload"])
        session = sessions.get(session_id) if isinstance(sessions, dict) else None
        if not isinstance(session, dict) or session.get("userId") != owner_id:
            return False
        next_sessions = dict(sessions)
        del next_sessions[session_id]
        next_payload = json.dumps(next_sessions, separators=(",", ":"), ensure_ascii=False)
        await self.binding.batch([
            self.binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
                WHERE name='sessions' AND payload=?""").bind(next_payload, now, row["payload"]),
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
        return True
