"""Trusted async Creem request composition; HTTP routing and secrets stay external."""
from __future__ import annotations

from typing import Any

from . import creem_event_rules
from .cloudflare_account_adapter import D1AccountTransactions, billing_owner_for_update
from .cloudflare_webhook_receipts import D1WebhookReceipts
from .cloudflare_state_records import read_record

MAX_CREEM_WEBHOOK_BYTES = 64 * 1024


async def _refresh_dirty_owner(binding: Any, *, owner_id: str, now: int) -> None:
    row = await binding.prepare("""SELECT revision,dirty FROM account_entitlement_authority
        WHERE owner_id=?""").bind(owner_id).first()
    if not row:
        raise ValueError("persisted entitlement authority is missing")
    if row["dirty"]:
        await D1AccountTransactions(binding).refresh_account_entitlement(
            owner_id=owner_id, expected_revision=row["revision"], now=now)


async def accept_signed_creem_webhook(*, binding: Any, raw_body: bytes,
                                      signature: str | None, secret: str,
                                      configured_products: dict, now: int) -> dict:
    """Durably accept and settle a signed event without any provider or model call."""
    if not isinstance(raw_body, bytes) or len(raw_body) > MAX_CREEM_WEBHOOK_BYTES:
        raise ValueError("invalid Creem webhook body size")
    if not isinstance(configured_products, dict) or type(now) is not int or now < 0:
        raise ValueError("invalid trusted Creem configuration")
    update = await D1WebhookReceipts(binding).record_signed_creem_event(
        raw_body=raw_body, signature=signature, secret=secret,
        normalize_event=lambda event: creem_event_rules.billing_update_from_creem_event(
            event, configured_products), now=now)
    if update is None:
        return {"received": True, "state": "ignored"}
    event_id = update["eventId"]
    receipt = await binding.prepare("""SELECT state FROM billing_webhook_receipts
        WHERE event_id=?""").bind(event_id).first()
    if not receipt:
        raise RuntimeError("accepted Creem receipt is missing")
    if receipt["state"] == "applied":
        event = await read_record(binding, "billingEvents", event_id)
        owner_id = event.get("ownerId") if event else None
        if owner_id is None:
            owner_id = await billing_owner_for_update(binding, update)
        if owner_id:
            await _refresh_dirty_owner(binding, owner_id=owner_id, now=now)
        return {"received": True, "state": "duplicate", "eventId": event_id}
    owner_id = await billing_owner_for_update(binding, update)
    account = D1AccountTransactions(binding)
    if owner_id is None:
        await account.park_webhook_receipt(receipt_event_id=event_id, now=now)
        return {"received": True, "state": "pending", "eventId": event_id}
    await account.settle_webhook_receipt(receipt_event_id=event_id,
        owner_id=owner_id, now=now)
    await _refresh_dirty_owner(binding, owner_id=owner_id, now=now)
    return {"received": True, "state": "applied", "eventId": event_id}
