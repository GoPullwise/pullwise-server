"""Fixed-endpoint TypeSafe call from the Python Worker FFI."""
from __future__ import annotations

import json

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 64 * 1024


async def _bounded_response(reader, limit=MAX_RESPONSE_BYTES) -> bytes:
    body = bytearray()
    while True:
        part = await reader.read()
        if part.done:
            return bytes(body)
        chunk = part.value
        size = int(chunk.length) if hasattr(chunk, "length") else len(chunk)
        if len(body) + size > limit:
            await reader.cancel()
            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        body.extend(int(chunk[index]) for index in range(size))


class WorkerJevGateway:
    def __init__(self, env):
        self.api_key = str(getattr(env, "TYPESAFE_API_KEY", ""))
        self.daily_limit = 20
        self.enabled = (str(getattr(env, "PULLWISE_JEV_SUGGESTIONS_ENABLED", "")) == "1"
            and str(getattr(env, "PULLWISE_JEV_SUGGESTIONS_EVALUATED", "")) == "1"
            and bool(self.api_key))

    async def evaluate(self, request: dict) -> bytes:
        if not self.enabled:
            raise OSError("JEV_DISABLED")
        from js import fetch, Object, AbortSignal
        from pyodide.ffi import to_js
        options = to_js({"method": "POST", "redirect": "manual", "headers": {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json"},
            "body": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            "signal": AbortSignal.timeout(3000)}, dict_converter=Object.fromEntries)
        response = await fetch(JEV_URL, options)
        if not response.ok:
            raise OSError("JEV_PROVIDER_UNAVAILABLE")
        length = response.headers.get("content-length")
        if length and int(length) > MAX_RESPONSE_BYTES:
            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        if response.body is None:
            raise OSError("JEV_EMPTY_RESPONSE")
        return await _bounded_response(response.body.getReader())
