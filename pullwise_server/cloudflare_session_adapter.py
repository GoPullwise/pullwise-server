"""Trusted exact-key session commands over persisted account authority."""
from __future__ import annotations
from typing import Any
from .cloudflare_state_records import (
    record_name, read_record_json, decode_record, encode_record, expired_record_commands, changed_guard,
)
from .cloudflare_d1_batch import execute_d1_batch


class D1SessionTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def issue_session(self, *, owner_id: str, session_id: str,
                            now: int, expires_at: int) -> dict:
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(session_id, str) or not session_id.startswith('ses-')
                or any(char in session_id for char in '\r\n\x00')
                or type(now) is not int or type(expires_at) is not int
                or not now < expires_at <= now + 30 * 86400):
            raise ValueError('invalid trusted session command')
        user = await read_record_json(self.binding, 'users', owner_id)
        if user is None:
            raise ValueError('UNAUTHENTICATED')
        if await read_record_json(self.binding, 'sessions', session_id) is not None:
            raise ValueError('SESSION_ALREADY_EXISTS')
        session = {'id': session_id, 'userId': owner_id, 'createdAt': now, 'expiresAt': expires_at}
        name = record_name('sessions', session_id)
        cleanup = await expired_record_commands(self.binding, 'sessions', now=now)
        await execute_d1_batch(self.binding, [*cleanup,
            ('''INSERT INTO app_state(name,payload,updated_at)
                SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)
                  AND EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)''',
             (name, encode_record('sessions', session_id, session), now, name,
              record_name('users', owner_id), user)), changed_guard(),
            ('DELETE FROM d1_command_guard', ())])
        return session

    async def revoke_session(self, *, owner_id: str, session_id: str, now: int) -> bool:
        if not isinstance(owner_id, str) or not owner_id or type(now) is not int:
            raise ValueError('invalid session revocation')
        snapshot = await read_record_json(self.binding, 'sessions', session_id)
        if snapshot is None:
            return False
        session = decode_record('sessions', session_id, snapshot)
        if session.get('userId') != owner_id:
            return False
        await execute_d1_batch(self.binding, [
            ('DELETE FROM app_state WHERE name=? AND payload=?', (record_name('sessions', session_id), snapshot)),
            changed_guard(), ('DELETE FROM d1_command_guard', ())])
        return True
