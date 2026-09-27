"""Creem HTTPS gateway with fixed host, bounded responses and secret binding."""
from __future__ import annotations

import json
import re
from urllib.parse import quote


def product_bindings(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for plan in ("pro", "max"):
        entry = raw.get(plan)
        if not isinstance(entry, dict) or any(interval not in {"month", "year"} for interval in entry):
            return {}
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", value)
               for value in entry.values()):
            return {}
        result[plan] = entry
    values = [value for group in result.values() for value in group.values()]
    return result if len(values) == len(set(values)) else {}


def webhook_product_ids(products: dict) -> dict:
    return {plan: tuple(group.values()) for plan, group in products.items()}


class WorkerCreemGateway:
    def __init__(self, env):
        self.api_key = str(getattr(env, "PULLWISE_CREEM_API_KEY", ""))
        base = str(getattr(env, "PULLWISE_CREEM_API_BASE_URL", "https://api.creem.io")).rstrip("/")
        if base not in {"https://api.creem.io", "https://test-api.creem.io"}:
            raise ValueError("Creem API origin is not allowed")
        self.base = base

    async def _json(self, path: str, *, method: str, payload: dict | None = None) -> dict:
        from js import fetch, Object, AbortSignal
        from pyodide.ffi import to_js
        if not self.api_key or not re.fullmatch(r"v1/(checkouts|products\?product_id=[A-Za-z0-9_-]{3,128}|subscriptions/[A-Za-z0-9_-]{3,128}/(upgrade|cancel|resume))", path):
            raise ValueError("Creem request is not configured or path is invalid")
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        init = {"method": method, "headers": headers, "redirect": "error",
                "signal": AbortSignal.timeout(10000)}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            init["body"] = json.dumps(payload, separators=(",", ":"))
        response = await fetch(f"{self.base}/{path}", to_js(init, dict_converter=Object.fromEntries))
        length = response.headers.get("content-length")
        if length and int(length) > 1024 * 1024:
            raise ValueError("Creem response too large")
        body = await response.text()
        if not response.ok or len(body) > 1024 * 1024:
            raise ValueError("Creem request failed")
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("Creem response malformed")
        return parsed

    async def post(self, path: str, payload: dict) -> dict:
        return await self._json(path, method="POST", payload=payload)

    async def product(self, product_id: str) -> dict:
        if not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", product_id):
            raise ValueError("Creem product ID invalid")
        return await self._json("v1/products?product_id=" + quote(product_id), method="GET")
