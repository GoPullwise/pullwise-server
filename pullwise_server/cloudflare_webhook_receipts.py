"""Verified Creem receipt persistence; payment application remains in Server."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from . import cloudflare_d1_mapping as mapping
from .cloudflare_d1_batch import execute_d1_batch
from .creem_signature import verify_creem_signature


class D1WebhookReceipts:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def record_signed_creem_event(self, *, raw_body: bytes, signature: str | None,
                                        secret: str, normalize_event: Callable[[dict], dict | None],
                                        now: int) -> dict | None:
        """Bind a verified event to the Server's existing Creem normalization."""
        if not verify_creem_signature(raw_body, signature, secret):
            raise ValueError("invalid Creem webhook signature")
        try:
            event = json.loads(raw_body)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid signed Creem event") from exc
        if not isinstance(event, dict):
            raise ValueError("invalid signed Creem event")
        update = normalize_event(event)
        if update is None:
            return None
        await self.record_signed_update(raw_body=raw_body, signature=signature,
            secret=secret, normalized_update=update, now=now)
        return update

    async def record_signed_update(self, *, raw_body: bytes, signature: str | None,
                                   secret: str, normalized_update: dict,
                                   now: int) -> Any:
        if not verify_creem_signature(raw_body, signature, secret):
            raise ValueError("invalid Creem webhook signature")
        event_id = normalized_update.get("eventId") if isinstance(normalized_update, dict) else None
        try:
            event = json.loads(raw_body)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid signed Creem event") from exc
        raw_event_id = (event.get("id") or event.get("eventId")) if isinstance(event, dict) else None
        if not isinstance(raw_event_id, str) or not raw_event_id or event_id != raw_event_id:
            raise ValueError("normalized event ID does not match signed Creem event")
        update_json = json.dumps(normalized_update, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        commands = mapping.record_billing_webhook_receipt(
            event_id=event_id, raw_sha256=hashlib.sha256(raw_body).hexdigest(),
            update_json=update_json, now=now)
        return await execute_d1_batch(self.binding, commands)
