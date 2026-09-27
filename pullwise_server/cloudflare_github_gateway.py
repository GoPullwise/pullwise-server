"""Bounded GitHub HTTPS and AES-GCM token storage for Python Workers."""
from __future__ import annotations

import base64
import json
from urllib.parse import urlencode


def _bytes_view(raw: bytes):
    from js import Uint8Array
    view = Uint8Array.new(len(raw))
    for index, value in enumerate(raw):
        view[index] = value
    return view


def _bytes_from_view(view) -> bytes:
    return bytes(int(view[index]) for index in range(int(view.length)))


def _decode_key(encoded: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception:
        raw = b""
    if len(raw) != 32:
        raise ValueError("GitHub token encryption key must be 32 base64url bytes")
    return raw


class WorkerGitHubGateway:
    def __init__(self, env) -> None:
        self.client_id = str(getattr(env, "PULLWISE_GITHUB_CLIENT_ID", ""))
        self.client_secret = str(getattr(env, "PULLWISE_GITHUB_CLIENT_SECRET", ""))
        self.app_slug = str(getattr(env, "PULLWISE_GITHUB_APP_SLUG", ""))
        self._key = str(getattr(env, "PULLWISE_GITHUB_TOKEN_KEY", ""))

    async def _json(self, url: str, *, method: str = "GET", token: str = "", body: str = "") -> dict:
        from js import fetch, Object, AbortSignal
        from pyodide.ffi import to_js
        if not (url.startswith("https://github.com/") or url.startswith("https://api.github.com/")):
            raise ValueError("GitHub URL is not allowed")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Pullwise-Ledger/1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        init = {"method": method, "headers": headers, "redirect": "error",
                "signal": AbortSignal.timeout(10000)}
        if body:
            init["body"] = body
        response = await fetch(url, to_js(init, dict_converter=Object.fromEntries))
        size = response.headers.get("content-length")
        if size and int(size) > 1024 * 1024:
            raise ValueError("GitHub response too large")
        raw = await response.text()
        if len(raw) > 1024 * 1024 or not response.ok:
            raise ValueError("GitHub request failed or response too large")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("GitHub response must be an object")
        return parsed

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> str:
        if not self.client_id or not self.client_secret:
            raise ValueError("GitHub OAuth credentials are missing")
        result = await self._json("https://github.com/login/oauth/access_token", method="POST",
            body=urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                            "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}))
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub OAuth token missing")
        return token

    async def profile(self, token: str) -> dict:
        return await self._json("https://api.github.com/user", token=token)

    async def _paged(self, endpoint: str, token: str, field: str) -> list[dict]:
        items = []
        for page in range(1, 11):
            result = await self._json(f"https://api.github.com{endpoint}?per_page=100&page={page}", token=token)
            batch = result.get(field)
            if not isinstance(batch, list) or len(batch) > 100:
                raise ValueError("invalid GitHub page")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError("GitHub pagination limit reached")

    async def installations(self, token: str) -> list[dict]:
        return await self._paged("/user/installations", token, "installations")

    async def repositories(self, token: str, installation_id: int) -> list[dict]:
        if type(installation_id) is not int or installation_id <= 0:
            raise ValueError("invalid installation ID")
        return await self._paged(f"/user/installations/{installation_id}/repositories", token, "repositories")

    async def _crypto_key(self, use: str):
        from js import crypto, Object
        from pyodide.ffi import to_js
        return await crypto.subtle.importKey("raw", _bytes_view(_decode_key(self._key)),
            to_js({"name": "AES-GCM"}, dict_converter=Object.fromEntries), False,
            to_js([use]))

    async def seal(self, token: str) -> str:
        from js import crypto, Uint8Array, Object
        from pyodide.ffi import to_js
        nonce = Uint8Array.new(12)
        crypto.getRandomValues(nonce)
        encrypted = await crypto.subtle.encrypt(
            to_js({"name": "AES-GCM", "iv": nonce}, dict_converter=Object.fromEntries),
            await self._crypto_key("encrypt"), _bytes_view(token.encode("utf-8")))
        raw = _bytes_from_view(nonce) + _bytes_from_view(Uint8Array.new(encrypted))
        return "gcm1:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    async def unseal(self, value: str) -> str:
        from js import crypto, Uint8Array, Object
        from pyodide.ffi import to_js
        if not isinstance(value, str) or not value.startswith("gcm1:"):
            raise ValueError("GitHub token is not sealed")
        raw = base64.urlsafe_b64decode(value[5:] + "=" * (-len(value[5:]) % 4))
        if len(raw) < 29:
            raise ValueError("GitHub token is invalid")
        decrypted = await crypto.subtle.decrypt(
            to_js({"name": "AES-GCM", "iv": _bytes_view(raw[:12])}, dict_converter=Object.fromEntries),
            await self._crypto_key("decrypt"), _bytes_view(raw[12:]))
        return _bytes_from_view(Uint8Array.new(decrypted)).decode("utf-8")
