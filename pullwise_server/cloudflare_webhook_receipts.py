"""Verified Creem receipt persistence; payment application remains in Server."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import cloudflare_d1_mapping as mapping
from .cloudflare_d1_batch import execute_d1_batch
from .creem_signature import verify_creem_signature


class D1WebhookReceipts:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def record_signed_update(self, *, raw_body: bytes, signature: str | None,
                                   secret: str, normalized_update: dict,
                                   now: int) -> Any:
        if not verify_creem_signature(raw_body, signature, secret):
            raise ValueError("invalid Creem webhook signature")
        event_id = normalized_update.get("eventId") if isinstance(normalized_update, dict) else None
        update_json = json.dumps(normalized_update, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        commands = mapping.record_billing_webhook_receipt(
            event_id=event_id, raw_sha256=hashlib.sha256(raw_body).hexdigest(),
            update_json=update_json, now=now)
        return await execute_d1_batch(self.binding, commands)
