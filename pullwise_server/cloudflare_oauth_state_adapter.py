"""Trusted, single-use GitHub OAuth state map over D1."""
from __future__ import annotations

import json
from typing import Any


class D1OAuthStates:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def issue(self, *, state_id: str, record: dict, now: int) -> None:
        if (not isinstance(state_id, str) or not state_id
                or any(char in state_id for char in "\r\n\x00")
                or not isinstance(record, dict) or type(now) is not int
                or record.get("kind") not in {"login", "manage_installation", "install_identity"}
                or type(record.get("expiresAt")) is not int
                or not now < record["expiresAt"] <= now + 600):
            raise ValueError("invalid trusted OAuth state")
        row = await self.binding.prepare("SELECT payload FROM app_state WHERE name='githubStates'").first()
        states = json.loads(row["payload"]) if row else {}
        if not isinstance(states, dict) or state_id in states:
            raise ValueError("OAUTH_STATE_ALREADY_EXISTS")
        next_payload = json.dumps({**states, state_id: record},
            separators=(",", ":"), ensure_ascii=False)
        if row is None:
            command = self.binding.prepare("""INSERT INTO app_state(name,payload,updated_at)
                SELECT 'githubStates',?,? WHERE NOT EXISTS(
                    SELECT 1 FROM app_state WHERE name='githubStates')""").bind(
                        next_payload, now)
        else:
            command = self.binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
                WHERE name='githubStates' AND payload=?""").bind(
                    next_payload, now, row["payload"])
        await self.binding.batch([command,
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("DELETE FROM d1_command_guard")])

    async def consume(self, *, state_id: str, expected_kind: str,
                      now: int) -> dict:
        if (not isinstance(state_id, str) or not state_id
                or not isinstance(expected_kind, str) or type(now) is not int):
            raise ValueError("OAUTH_STATE_INVALID")
        row = await self.binding.prepare("SELECT payload FROM app_state WHERE name='githubStates'").first()
        states = json.loads(row["payload"]) if row else None
        if not isinstance(states, dict) or state_id not in states:
            raise ValueError("OAUTH_STATE_INVALID")
        record = states[state_id]
        next_states = dict(states)
        del next_states[state_id]
        next_payload = json.dumps(next_states, separators=(",", ":"), ensure_ascii=False)
        await self.binding.batch([
            self.binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
                WHERE name='githubStates' AND payload=?""").bind(
                    next_payload, now, row["payload"]),
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
        if (not isinstance(record, dict) or record.get("kind") != expected_kind
                or type(record.get("expiresAt")) is not int
                or record["expiresAt"] < now):
            raise ValueError("OAUTH_STATE_INVALID")
        return record
