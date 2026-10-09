"""Trusted, single-use exact-key GitHub OAuth states over D1."""
from __future__ import annotations
from typing import Any
from .cloudflare_state_records import (
    record_name, read_record_json, decode_record, encode_record, expired_record_commands, changed_guard,
)
from .cloudflare_d1_batch import execute_d1_batch


class D1OAuthStates:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def issue(self, *, state_id: str, record: dict, now: int) -> None:
        if (not isinstance(state_id, str) or not state_id
                or any(char in state_id for char in '\r\n\x00')
                or not isinstance(record, dict) or type(now) is not int
                or record.get('kind') not in {'login', 'install', 'manage_installation', 'install_identity'}
                or type(record.get('expiresAt')) is not int or not now < record['expiresAt'] <= now + 600):
            raise ValueError('invalid trusted OAuth state')
        if record.get('intent') == 'link':
            if (record.get('kind') != 'login'
                    or not isinstance(record.get('userId'), str) or not record['userId']
                    or not isinstance(record.get('sessionId'), str) or not record['sessionId']):
                raise ValueError('invalid trusted OAuth link state')
            record_name('users', record['userId'])
            record_name('sessions', record['sessionId'])
        if await read_record_json(self.binding, 'githubStates', state_id) is not None:
            raise ValueError('OAUTH_STATE_ALREADY_EXISTS')
        name = record_name('githubStates', state_id)
        cleanup = await expired_record_commands(self.binding, 'githubStates', now=now)
        await execute_d1_batch(self.binding, [*cleanup,
            ('''INSERT INTO app_state(name,payload,updated_at)
                SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)''',
             (name, encode_record('githubStates', state_id, record), now, name)), changed_guard(),
            ('DELETE FROM d1_command_guard', ())])

    async def consume(self, *, state_id: str, expected_kind: str, now: int) -> dict:
        if not isinstance(state_id, str) or not state_id or not isinstance(expected_kind, str) or type(now) is not int:
            raise ValueError('OAUTH_STATE_INVALID')
        snapshot = await read_record_json(self.binding, 'githubStates', state_id)
        if snapshot is None:
            raise ValueError('OAUTH_STATE_INVALID')
        record = decode_record('githubStates', state_id, snapshot)
        await execute_d1_batch(self.binding, [
            ('DELETE FROM app_state WHERE name=? AND payload=?', (record_name('githubStates', state_id), snapshot)),
            changed_guard(), ('DELETE FROM d1_command_guard', ())])
        if (record.get('kind') != expected_kind or type(record.get('expiresAt')) is not int
                or record['expiresAt'] < now):
            raise ValueError('OAUTH_STATE_INVALID')
        return record
