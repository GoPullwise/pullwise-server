"""Small async HTTP boundary for the candidate Cloudflare Server entry."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable, Mapping

from .cloudflare_creem_handler import (
    MAX_CREEM_WEBHOOK_BYTES,
    accept_signed_creem_webhook,
)
from .creem_signature import verify_creem_signature
from .cloudflare_product_read import read_product, patch_item, patch_watch, delete_watch, post_manual_sync, _cookie_sessions
from .cloudflare_api_key_read import list_api_keys
from .cloudflare_api_key_write import revoke_api_key, create_api_key
from .cloudflare_billing_read import read_billing
from .cloudflare_billing_catalog import read_public_plan


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
                              configured_products: dict, now: int,
                              params: Mapping[str, object] | None = None,
                              cookie_same_site: str = "Lax",
                              trusted_origins: set[str] | None = None) -> tuple[int, dict]:
    if method == "GET" and path == "/health":
        try:
            row = await binding.prepare("""SELECT COUNT(*) AS table_count FROM sqlite_master
                WHERE type='table' AND name IN ('app_state','account_entitlement_authority',
                    'billing_webhook_receipts','processing_usage_buckets',
                    'processing_usage_ledger','provider_attempts','api_keys',
                    'd1_command_guard','watch_controls',
                    'update_watches','source_records','source_versions',
                    'source_contexts','source_assessment_publications',
                    'items','item_versions','item_handling_events',
                    'background_jobs','repository_services','request_idempotency',
                    'processing_controls','discovery_targets',
                    'billing_public_catalog')""").first()
            if row and row.get("table_count") == 23:
                return 200, {"ok": True, "service": "pullwise-server",
                             "database": {"type": "d1", "configured": True}}
        except Exception:
            pass
        return 503, {"ok": False, "service": "pullwise-server"}
    if method == "GET" and path == "/api-keys":
        try:
            return await list_api_keys(binding=binding, headers=headers, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if method == "GET" and path == "/billing":
        try:
            return await read_billing(binding=binding, headers=headers, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if method == "GET" and path == "/billing/plan":
        try:
            return await read_public_plan(binding=binding, headers=headers, now=now)
        except Exception:
            return 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
    if method == "POST" and path == "/api-keys":
        if cookie_same_site.casefold() == "none" and _cookie_sessions(headers):
            claimed_origin = _header(headers, "Origin") or _header(headers, "Referer")
            parsed = urlsplit(claimed_origin)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if origin not in (trusted_origins or set()):
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}
        length_text = _header(headers, "Content-Length")
        if not length_text.isdigit() or int(length_text) > 8192:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            raw = await read_body()
            if not isinstance(raw, bytes) or len(raw) != int(length_text):
                raise ValueError("invalid body length")
            body = json.loads(raw)
        except Exception:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            return await create_api_key(binding=binding, headers=headers,
                body=body, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if method == "DELETE" and path.startswith("/api-keys/"):
        key_id = path[len("/api-keys/"):]
        if not key_id or "/" in key_id:
            return 404, {"error": {"code": "NOT_FOUND"}}
        if cookie_same_site.casefold() == "none" and _cookie_sessions(headers):
            claimed_origin = _header(headers, "Origin") or _header(headers, "Referer")
            parsed = urlsplit(claimed_origin)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if origin not in (trusted_origins or set()):
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}
        try:
            return await revoke_api_key(binding=binding, key_id=key_id,
                headers=headers, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    segments = path.strip("/").split("/")
    if method == "POST" and len(segments) == 5 and segments[:2] == ["api", "v1"] and segments[2] in {"watches", "repositories"} and segments[4] == "sync":
        resource_id = segments[3]
        if not resource_id:
            return 404, {"error": {"code": "NOT_FOUND"}}
        if cookie_same_site.casefold() == "none" and _cookie_sessions(headers):
            claimed_origin = _header(headers, "Origin") or _header(headers, "Referer")
            parsed = urlsplit(claimed_origin)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if origin not in (trusted_origins or set()):
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}
        key = _header(headers, "Idempotency-Key")
        if not key or len(key) > 128:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        length_text = _header(headers, "Content-Length")
        if not length_text.isdigit() or int(length_text) > 8192:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            raw = await read_body()
            if not isinstance(raw, bytes) or len(raw) != int(length_text) or json.loads(raw) != {}:
                return 400, {"error": {"code": "INVALID_REQUEST"}}
        except Exception:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            return await post_manual_sync(binding=binding,
                resource_kind="watch" if segments[2] == "watches" else "repository",
                resource_id=resource_id, headers=headers,
                idempotency_key=key, now=now)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if method == "GET" and path in {"/api/v1/me", "/api/v1/usage", "/api/v1/usage/events",
                                       "/api/v1/watches", "/api/v1/sources"} or (
            method == "GET" and (path.startswith("/api/v1/sources/")
                or path == "/api/v1/items" or path.startswith("/api/v1/items/")
                or path.startswith("/api/v1/jobs/")
                or path.startswith("/api/v1/watches/")
                or path.startswith("/api/v1/repositories/"))):
        try:
            return await read_product(binding=binding, path=path,
                headers=headers, now=now, params=params)
        except Exception:
            return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    if ((method == "PATCH" and (path.startswith("/api/v1/items/")
                                 or path.startswith("/api/v1/watches/")))
            or (method == "DELETE" and path.startswith("/api/v1/watches/"))):
        if cookie_same_site.casefold() == "none" and _cookie_sessions(headers):
            claimed_origin = _header(headers, "Origin") or _header(headers, "Referer")
            parsed = urlsplit(claimed_origin)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if origin not in (trusted_origins or set()):
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}
        is_item = path.startswith("/api/v1/items/")
        resource_id = path[len("/api/v1/items/"):] if is_item else path[len("/api/v1/watches/"):]
        if not resource_id or "/" in resource_id:
            return 404, {"error": {"code": "NOT_FOUND"}}
        length_text = _header(headers, "Content-Length")
        if method == "DELETE":
            try:
                return await delete_watch(binding=binding, watch_id=resource_id,
                    headers=headers, now=now)
            except Exception:
                return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
        if not length_text.isdigit() or int(length_text) > 8192:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            raw = await read_body()
            if not isinstance(raw, bytes) or len(raw) != int(length_text):
                raise ValueError("invalid body length")
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        try:
            if is_item:
                return await patch_item(binding=binding, item_id=resource_id,
                    headers=headers, body=body, now=now)
            return await patch_watch(binding=binding, watch_id=resource_id,
                headers=headers, body=body, now=now)
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
