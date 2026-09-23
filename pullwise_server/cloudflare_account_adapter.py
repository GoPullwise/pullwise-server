"""Async D1 boundary for persisted account revisions.

The caller owns authentication, payment-event validation and state encryption.
All next-account JSON arguments must already be state_for_storage output.
"""
from __future__ import annotations

import json
from typing import Any

from . import billing_account_rules, cloudflare_d1_mapping as mapping
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

    async def apply_webhook_receipt(self, *, receipt_event_id: str, owner_id: str,
                                    expected_revision: int, next_account_json: str,
                                    next_events_json: str, next_pending_json: str,
                                    now: int) -> Any:
        receipt = await self.binding.prepare("""SELECT update_json FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending'""").bind(receipt_event_id).first()
        if not receipt:
            raise ValueError("pending webhook receipt is missing")
        account = await self._snapshot(owner_id)
        events = await self._state_snapshot("billingEvents")
        pending = await self._state_snapshot("billingPendingUpdates")
        commands = mapping.apply_webhook_receipt(
            receipt_event_id=receipt_event_id,
            expected_update_json=receipt["update_json"], owner_id=owner_id,
            expected_revision=expected_revision, account_snapshot=account,
            next_account_json=next_account_json, expected_events_json=events,
            next_events_json=next_events_json, expected_pending_json=pending,
            next_pending_json=next_pending_json, now=now)
        return await execute_d1_batch(self.binding, commands)

    async def park_webhook_receipt(self, *, receipt_event_id: str, now: int) -> dict:
        receipt = await self.binding.prepare("""SELECT update_json FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending'""").bind(receipt_event_id).first()
        if not receipt:
            raise ValueError("pending webhook receipt is missing")
        update = json.loads(receipt["update_json"])
        pending_json = await self._state_snapshot("billingPendingUpdates")
        pending = json.loads(pending_json)
        if (not isinstance(update, dict) or update.get("eventId") != receipt_event_id
                or not isinstance(pending, list)):
            raise ValueError("invalid pending webhook receipt")
        existing = [item for item in pending
                    if isinstance(item, dict) and item.get("eventId") == receipt_event_id]
        if existing:
            if len(existing) != 1 or existing[0] != update:
                raise ValueError("pending receipt conflicts with saved update")
            return {"parked": False}
        next_pending_json = json.dumps([*pending, update], ensure_ascii=False,
            allow_nan=False, separators=(",", ":"))
        commands = mapping.park_webhook_receipt(
            receipt_event_id=receipt_event_id,
            expected_update_json=receipt["update_json"],
            expected_pending_json=pending_json,
            next_pending_json=next_pending_json, now=now)
        await execute_d1_batch(self.binding, commands)
        return {"parked": True}

    async def settle_webhook_receipt(self, *, receipt_event_id: str,
                                     owner_id: str, now: int) -> dict:
        receipt = await self.binding.prepare("""SELECT update_json FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending'""").bind(receipt_event_id).first()
        account = await self.binding.prepare("""SELECT u.value AS snapshot,
            authority.revision AS revision FROM account_entitlement_authority authority
            JOIN app_state a ON a.name='users'
            JOIN json_each(a.payload) u ON u.key=authority.owner_id
            WHERE authority.owner_id=?""").bind(owner_id).first()
        if not receipt or not account:
            raise ValueError("pending receipt or persisted account is missing")
        update = json.loads(receipt["update_json"])
        stored_user = json.loads(account["snapshot"])
        events = json.loads(await self._state_snapshot("billingEvents"))
        pending = json.loads(await self._state_snapshot("billingPendingUpdates"))
        if (not isinstance(update, dict) or update.get("eventId") != receipt_event_id
                or not isinstance(events, dict) or receipt_event_id in events
                or not isinstance(pending, list)):
            raise ValueError("receipt is not eligible for account settlement")
        if not billing_account_rules.billing_update_matches_user(update, stored_user):
            raise ValueError("receipt owner does not match persisted account")
        matching_pending = [item for item in pending if isinstance(item, dict)
                            and item.get("eventId") == receipt_event_id]
        if any(item != update for item in matching_pending):
            raise ValueError("pending receipt conflicts with saved update")
        next_pending = [item for item in pending if not (isinstance(item, dict)
            and item.get("eventId") == receipt_event_id)]
        decision = billing_account_rules.reduce_billing_update(stored_user, update,
            processed_at=now)
        if decision["eventRecord"] is None:
            raise ValueError("receipt has no billing event record")
        next_events = billing_account_rules.with_billing_event(
            events, receipt_event_id, decision["eventRecord"])
        await self.apply_webhook_receipt(receipt_event_id=receipt_event_id,
            owner_id=owner_id, expected_revision=account["revision"],
            next_account_json=json.dumps(decision["user"], ensure_ascii=False,
                allow_nan=False, separators=(",", ":")),
            next_events_json=json.dumps(next_events, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")),
            next_pending_json=json.dumps(next_pending, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")), now=now)
        return {"applied": decision["applied"], "ownerId": owner_id,
                "eventId": receipt_event_id}

    async def reconcile_pending_for_owner(self, *, owner_id: str, now: int,
                                          limit: int = 16) -> dict:
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("pending reconciliation limit must be 1..16")
        account = json.loads(await self._snapshot(owner_id))
        pending = json.loads(await self._state_snapshot("billingPendingUpdates"))
        if not isinstance(pending, list):
            raise ValueError("persisted pending billing state is invalid")
        matching = [update for update in pending
            if isinstance(update, dict) and billing_account_rules.billing_update_matches_user(
                update, account)]
        matching.sort(key=lambda update: billing_account_rules.billing_event_created(update) or 0)
        event_ids = [billing_account_rules.billing_event_id(update) for update in matching]
        if not all(event_ids) or len(set(event_ids)) != len(event_ids):
            raise ValueError("matching pending receipt identities are invalid")
        settled = []
        for event_id in event_ids[:limit]:
            await self.settle_webhook_receipt(receipt_event_id=event_id,
                owner_id=owner_id, now=now)
            settled.append(event_id)
        return {"settled": settled, "remaining": len(event_ids) - len(settled)}

    async def refresh_account_entitlement(self, *, owner_id: str,
                                          expected_revision: int, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.refresh_account_entitlement(
            owner_id=owner_id, expected_revision=expected_revision,
            account_snapshot=snapshot, now=now))
