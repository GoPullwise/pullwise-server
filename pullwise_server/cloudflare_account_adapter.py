"""Async D1 boundary for persisted account revisions.

The caller owns authentication, payment-event validation and state encryption.
All next-account JSON arguments must already be state_for_storage output.
"""
from __future__ import annotations

from typing import Any

from . import cloudflare_d1_mapping as mapping
from .cloudflare_d1_batch import execute_d1_batch


class D1AccountTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def _snapshot(self, owner_id: str) -> str:
        row = await self.binding.prepare("""SELECT value AS snapshot FROM app_state,
            json_each(payload) WHERE name='users' AND key=?""").bind(owner_id).first()
        if not row or not isinstance(row.get("snapshot"), str):
            raise ValueError("persisted account is missing")
        return row["snapshot"]

    async def _state_snapshot(self, name: str) -> str:
        row = await self.binding.prepare("SELECT payload FROM app_state WHERE name=?").bind(name).first()
        if not row or not isinstance(row.get("payload"), str):
            raise ValueError("persisted billing state is missing")
        return row["payload"]

    async def initialize_account(self, *, owner_id: str, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.initialize_account(
            owner_id=owner_id, account_snapshot=snapshot, now=now))

    async def stage_account_write(self, *, owner_id: str, expected_revision: int,
                                  next_account_json: str, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.stage_account_write(
            owner_id=owner_id, expected_revision=expected_revision,
            account_snapshot=snapshot, next_account_json=next_account_json, now=now))

    async def stage_account_event(self, *, owner_id: str, expected_revision: int,
                                  next_account_json: str, event_id: str,
                                  event_record_json: str, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.stage_account_event(
            owner_id=owner_id, expected_revision=expected_revision,
            account_snapshot=snapshot, next_account_json=next_account_json,
            event_id=event_id, event_record_json=event_record_json, now=now))

    async def stage_pending_billing_updates(self, *, next_pending_json: str, now: int) -> Any:
        pending = await self._state_snapshot("billingPendingUpdates")
        return await execute_d1_batch(self.binding, mapping.stage_pending_billing_updates(
            expected_pending_json=pending, next_pending_json=next_pending_json, now=now))

    async def stage_billing_reconciliation(self, *, owner_id: str, expected_revision: int,
                                           next_account_json: str, next_events_json: str,
                                           next_pending_json: str, now: int) -> Any:
        account = await self._snapshot(owner_id)
        events = await self._state_snapshot("billingEvents")
        pending = await self._state_snapshot("billingPendingUpdates")
        return await execute_d1_batch(self.binding, mapping.stage_billing_reconciliation(
            owner_id=owner_id, expected_revision=expected_revision,
            account_snapshot=account, next_account_json=next_account_json,
            expected_events_json=events, next_events_json=next_events_json,
            expected_pending_json=pending, next_pending_json=next_pending_json, now=now))

    async def refresh_account_entitlement(self, *, owner_id: str,
                                          expected_revision: int, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.refresh_account_entitlement(
            owner_id=owner_id, expected_revision=expected_revision,
            account_snapshot=snapshot, now=now))
