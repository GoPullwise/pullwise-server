"""Bounded account/receipt reads and atomic payment state publication."""
from __future__ import annotations

import json
from typing import Any

from . import billing_account_rules, cloudflare_d1_mapping as mapping
from .cloudflare_d1_batch import execute_d1_batch
from .cloudflare_state_records import encode_record, read_record_json, record_name
from .cloudflare_validation_budget import _field


async def billing_owner_for_update(binding: Any, update: dict) -> str | None:
    sql, params = mapping.billing_owner_candidates(update)
    result = await binding.prepare(sql).bind(*params).all()
    matches = [_field(row, "ownerId") for row in _field(result, "results", [])]
    if len(matches) > 1:
        raise ValueError("billing owner is ambiguous")
    return matches[0] if matches else None


class D1AccountTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def _snapshot(self, owner_id: str) -> str:
        snapshot = await read_record_json(self.binding, "users", owner_id)
        if snapshot is None:
            raise ValueError("persisted account is missing")
        return snapshot

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

    async def park_webhook_receipt(self, *, receipt_event_id: str, now: int) -> dict:
        receipt = await self.binding.prepare("""SELECT update_json FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending'""").bind(receipt_event_id).first()
        if not receipt:
            raise ValueError("pending webhook receipt is missing")
        update = json.loads(receipt["update_json"])
        if not isinstance(update, dict) or update.get("eventId") != receipt_event_id:
            raise ValueError("invalid pending webhook receipt")
        pending_json = await read_record_json(self.binding, "billingPendingUpdates", receipt_event_id)
        if pending_json is not None:
            if json.loads(pending_json) != update:
                raise ValueError("pending receipt conflicts with saved update")
            return {"parked": False}
        await execute_d1_batch(self.binding, mapping.park_webhook_receipt(
            receipt_event_id=receipt_event_id, expected_update_json=receipt["update_json"], now=now))
        return {"parked": True}

    async def settle_webhook_receipt(self, *, receipt_event_id: str,
                                     owner_id: str, now: int) -> dict:
        receipt = await self.binding.prepare("""SELECT update_json FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending'""").bind(receipt_event_id).first()
        account = await self.binding.prepare("""SELECT u.payload AS snapshot,
            authority.revision AS revision FROM account_entitlement_authority authority
            JOIN app_state u ON u.name=? WHERE authority.owner_id=?""").bind(
                record_name("users", owner_id), owner_id).first()
        if not receipt or not account:
            raise ValueError("pending receipt or persisted account is missing")
        update = json.loads(receipt["update_json"])
        stored_user = json.loads(account["snapshot"])
        if (not isinstance(update, dict) or update.get("eventId") != receipt_event_id
                or not isinstance(stored_user, dict) or stored_user.get("id") != owner_id):
            raise ValueError("receipt is not eligible for account settlement")
        if not billing_account_rules.billing_update_matches_user(update, stored_user):
            raise ValueError("receipt owner does not match persisted account")
        if await billing_owner_for_update(self.binding, update) != owner_id:
            raise ValueError("receipt owner does not uniquely match persisted account")
        pending_json = await read_record_json(self.binding, "billingPendingUpdates", receipt_event_id)
        if pending_json is not None and json.loads(pending_json) != update:
            raise ValueError("pending receipt conflicts with saved update")
        decision = billing_account_rules.reduce_billing_update(stored_user, update, processed_at=now)
        if decision["eventRecord"] is None:
            raise ValueError("receipt has no billing event record")
        await execute_d1_batch(self.binding, mapping.apply_webhook_receipt(
            receipt_event_id=receipt_event_id, expected_update_json=receipt["update_json"],
            owner_id=owner_id, expected_revision=account["revision"],
            account_snapshot=account["snapshot"],
            next_account_json=encode_record("users", owner_id, decision["user"]),
            event_record_json=encode_record("billingEvents", receipt_event_id, decision["eventRecord"]),
            expected_pending_json=pending_json, now=now))
        return {"applied": decision["applied"], "ownerId": owner_id, "eventId": receipt_event_id}

    async def reconcile_pending_for_owner(self, *, owner_id: str, now: int,
                                          limit: int = 16) -> dict:
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("pending reconciliation limit must be 1..16")
        account = json.loads(await self._snapshot(owner_id))
        billing = account.get("billing") or {}
        checkout = account.get("billingCheckout") or {}
        params = (owner_id, billing.get("customerId") or "", billing.get("subscriptionId") or "",
                  checkout.get("requestId") or "")
        predicate = """name GLOB 'record:billingPendingUpdates:*' AND (
            json_extract(payload,'$.userId')=? OR
            (json_extract(payload,'$.customerId')<>'' AND json_extract(payload,'$.customerId')=?) OR
            (json_extract(payload,'$.subscriptionId')<>'' AND json_extract(payload,'$.subscriptionId')=?) OR
            (json_extract(payload,'$.requestId')<>'' AND json_extract(payload,'$.requestId')=?))"""
        count = await self.binding.prepare("SELECT COUNT(*) AS total FROM app_state WHERE " + predicate).bind(*params).first()
        result = await self.binding.prepare("SELECT json_extract(payload,'$.eventId') AS eventId FROM app_state WHERE "
            + predicate + " ORDER BY CAST(json_extract(payload,'$.eventCreated') AS REAL),name LIMIT ?").bind(*params, limit).all()
        event_ids = [_field(row, "eventId") for row in _field(result, "results", [])]
        if not all(isinstance(event_id, str) and event_id for event_id in event_ids) or len(set(event_ids)) != len(event_ids):
            raise ValueError("matching pending receipt identities are invalid")
        settled = []
        for event_id in event_ids:
            await self.settle_webhook_receipt(receipt_event_id=event_id, owner_id=owner_id, now=now)
            settled.append(event_id)
        return {"settled": settled, "remaining": count["total"] - len(settled)}

    async def refresh_account_entitlement(self, *, owner_id: str,
                                          expected_revision: int, now: int) -> Any:
        snapshot = await self._snapshot(owner_id)
        return await execute_d1_batch(self.binding, mapping.refresh_account_entitlement(
            owner_id=owner_id, expected_revision=expected_revision, account_snapshot=snapshot, now=now))
