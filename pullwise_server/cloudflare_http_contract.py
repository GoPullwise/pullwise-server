"""Small async HTTP boundary for the candidate Cloudflare Server entry."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

from .cloudflare_creem_handler import (
    MAX_CREEM_WEBHOOK_BYTES,
    accept_signed_creem_webhook,
)
from .creem_signature import verify_creem_signature
from .cloudflare_product_read import read_product


def _header(headers: Mapping[str, object], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value).strip()
    return ""


async def handle_http_request(*, method: str, path: str,
                              headers: Mapping[str, object],
                              read_body: Callable[[], Awaitable[bytes]],
                              binding: Any, creem_secret: str,
                              configured_products: dict, now: int) -> tuple[int, dict]:
    if method == "GET" and path == "/health":
        try:
            row = await binding.prepare("""SELECT COUNT(*) AS table_count FROM sqlite_master
                WHERE type='table' AND name IN ('app_state','account_entitlement_authority',
                    'billing_webhook_receipts','processing_usage_buckets',
                    'processing_usage_ledger','provider_attempts','api_keys',
                    'd1_command_guard')""").first()
            if row and row.get("table_count") == 8:
                return 200, {"ok": True, "service": "pullwise-server",
                             "database": {"type": "d1", "configured": True}}
        except Exception:
            pass
        return 503, {"ok": False, "service": "pullwise-server"}
    if method == "GET" and path in {"/api/v1/me", "/api/v1/usage"}:
        try:
            return await read_product(binding=binding, path=path,
                headers=headers, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if method != "POST" or path != "/webhooks/creem":
        return 404, {"error": {"code": "NOT_FOUND"}}
    if not isinstance(creem_secret, str) or not creem_secret or not isinstance(configured_products, dict):
        return 503, {"error": {"code": "BILLING_UNAVAILABLE"}}
    length_text = _header(headers, "Content-Length")
    if not length_text.isdigit():
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    length = int(length_text)
    if length > MAX_CREEM_WEBHOOK_BYTES:
        return 413, {"error": {"code": "REQUEST_TOO_LARGE"}}
    try:
        raw = await read_body()
    except Exception:
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    if not isinstance(raw, bytes) or len(raw) != length:
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    if len(raw) > MAX_CREEM_WEBHOOK_BYTES:
        return 413, {"error": {"code": "REQUEST_TOO_LARGE"}}
    if not verify_creem_signature(raw, _header(headers, "creem-signature"), creem_secret):
        return 400, {"error": {"code": "INVALID_SIGNATURE"}}
    try:
        event = json.loads(raw)
    except (UnicodeError, ValueError):
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    if not isinstance(event, dict):
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    try:
        await accept_signed_creem_webhook(binding=binding, raw_body=raw,
            signature=_header(headers, "creem-signature"),
            secret=creem_secret, configured_products=configured_products,
            now=now)
    except Exception:
        # A verified receipt may already be durable; ask the provider to retry.
        return 503, {"error": {"code": "BILLING_RETRY"}}
    return 200, {"received": True}
