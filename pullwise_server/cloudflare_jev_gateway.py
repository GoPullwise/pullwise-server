"""Fixed-endpoint TypeSafe call from the Python Worker FFI."""
from __future__ import annotations

import json

from .typesafe_client import build_request

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT_MS = 3000


async def _bounded_response(reader, limit=MAX_RESPONSE_BYTES) -> bytes:
    body = bytearray()
    try:
        while True:
            part = await reader.read()
            if part.done:
                return bytes(body)
            chunk = part.value
            size = int(chunk.length) if hasattr(chunk, "length") else len(chunk)
            if len(body) + size > limit:
                raise ValueError("JEV_RESPONSE_TOO_LARGE")
            body.extend(int(chunk[index]) for index in range(size))
    except Exception:
        # Also close an interrupted/erroring body. Cancellation must not hide
        # the original provider/size error or retain an unbounded live stream.
        try:
            await reader.cancel()
        except Exception:
            pass
        raise


async def _cancel_body(response):
    if response.body is not None:
        try:
            await response.body.cancel()
        except Exception:
            pass


class WorkerJevGateway:
    def __init__(self, env):
        api_key = getattr(env, "TYPESAFE_API_KEY", "")
        self.api_key = str(api_key).strip() if api_key is not None else ""
        self.daily_limit = 20
        self.enabled = (str(getattr(env, "PULLWISE_JEV_SUGGESTIONS_ENABLED", "")) == "1"
            and str(getattr(env, "PULLWISE_JEV_SUGGESTIONS_EVALUATED", "")) == "1"
            and bool(self.api_key))

    async def evaluate(self, request: dict) -> bytes:
        if not self.enabled:
            raise OSError("JEV_DISABLED")
        if not isinstance(request, dict) or set(request) != {"state", "model", "questions"}:
            raise ValueError("JEV_INVALID_REQUEST")
        request = build_request(state=request["state"], model=request["model"],
                                questions=request["questions"])
        from js import fetch, Object, AbortSignal
        from pyodide.ffi import to_js
        options = to_js({"method": "POST", "redirect": "manual", "headers": {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json"},
            "body": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            "signal": AbortSignal.timeout(REQUEST_TIMEOUT_MS)}, dict_converter=Object.fromEntries)
        response = await fetch(JEV_URL, options)
        if not response.ok:
            await _cancel_body(response)
            raise OSError("JEV_PROVIDER_UNAVAILABLE")
        length = response.headers.get("content-length")
        if length and (not str(length).isascii() or not str(length).isdecimal()):
            await _cancel_body(response)
            raise ValueError("JEV_INVALID_RESPONSE_LENGTH")
        if length and int(length) > MAX_RESPONSE_BYTES:
            await _cancel_body(response)
            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        if response.body is None:
            raise OSError("JEV_EMPTY_RESPONSE")
        return await _bounded_response(response.body.getReader())
