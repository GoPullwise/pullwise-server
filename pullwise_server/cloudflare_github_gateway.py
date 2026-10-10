"""Bounded GitHub HTTPS and AES-GCM token storage for Python Workers."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlencode


class GitHubFailure(Exception):
    """Safe failure categories, never provider bodies, credentials or URLs."""
    def __init__(self, code: str, provider_status: int | None = None, *, request_rejected: bool = False):
        statuses = {"GITHUB_REAUTHORIZATION_REQUIRED": 403, "GITHUB_PERMISSION_DENIED": 403,
                    "GITHUB_RATE_LIMITED": 503, "GITHUB_UNAVAILABLE": 503,
                    "GITHUB_RESPONSE_INVALID": 502, "GITHUB_CONFIGURATION_ERROR": 503,
                    "GITHUB_TOKEN_UNREADABLE": 503}
        self.code, self.status = code, statuses[code]
        self.provider_status = provider_status if type(provider_status) is int and 100 <= provider_status <= 599 else None
        # Internal evidence that a refresh request was rejected without rotating
        # credentials. Never expose provider payloads through the exception.
        self.request_rejected = request_rejected is True
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class GitHubTokenBundle:
    access_token: str
    expires_in: int | None = None
    refresh_token: str | None = None
    refresh_token_expires_in: int | None = None


def _token_text(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 4096 and value.isascii()
            and all(32 < ord(char) < 127 for char in value))


def token_bundle(result: object, *, refreshing: bool = False) -> GitHubTokenBundle:
    """Validate provider token facts, including the all-or-none expiry bundle."""
    if not isinstance(result, dict):
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    if "error" in result:
        if not isinstance(result["error"], str) or not result["error"]:
            raise GitHubFailure("GITHUB_RESPONSE_INVALID")
        code = ("GITHUB_REAUTHORIZATION_REQUIRED" if refreshing and result["error"] in {
            "bad_refresh_token", "invalid_grant", "expired_token"} else "GITHUB_CONFIGURATION_ERROR")
        raise GitHubFailure(code, request_rejected=True)
    if not _token_text(result.get("access_token")) or str(result.get("token_type", "")).lower() != "bearer":
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    expiry_fields = ("expires_in", "refresh_token", "refresh_token_expires_in")
    present = [field in result for field in expiry_fields]
    if any(present):
        if (not all(present) or not _token_text(result.get("refresh_token"))
                or any(type(result.get(field)) is not int or not 1 <= result[field] <= 366 * 86400
                       for field in ("expires_in", "refresh_token_expires_in"))):
            raise GitHubFailure("GITHUB_RESPONSE_INVALID")
        return GitHubTokenBundle(result["access_token"], result["expires_in"],
                                 result["refresh_token"], result["refresh_token_expires_in"])
    if refreshing:
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    return GitHubTokenBundle(result["access_token"])


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
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except Exception:
        raw = b""
    if len(raw) != 32:
        raise GitHubFailure("GITHUB_CONFIGURATION_ERROR")
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
        accept = "application/json" if url == "https://github.com/login/oauth/access_token" else "application/vnd.github+json"
        headers = {"Accept": accept, "User-Agent": "Pullwise-Ledger/1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        init = {"method": method, "headers": headers, "redirect": "manual",
                "signal": AbortSignal.timeout(10000)}
        if body:
            init["body"] = body
        try:
            response = await fetch(url, to_js(init, dict_converter=Object.fromEntries))
            status = int(response.status)
            if status == 401:
                raise GitHubFailure("GITHUB_REAUTHORIZATION_REQUIRED" if token else "GITHUB_CONFIGURATION_ERROR", status,
                                    request_rejected=True)
            if status == 429 or status == 403 and (
                    response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after")):
                raise GitHubFailure("GITHUB_RATE_LIMITED", status, request_rejected=True)
            if not response.ok and status not in {403, 404}:
                raise GitHubFailure("GITHUB_UNAVAILABLE", status, request_rejected=400 <= status < 500)
            size = response.headers.get("content-length")
            if size and (not str(size).isdigit() or int(size) > 1024 * 1024):
                raise GitHubFailure("GITHUB_RESPONSE_INVALID", status, request_rejected=400 <= status < 500)
            raw = await response.text()
        except GitHubFailure:
            raise
        except Exception:
            raise GitHubFailure("GITHUB_UNAVAILABLE") from None
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise GitHubFailure("GITHUB_RESPONSE_INVALID", status, request_rejected=400 <= status < 500)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if status in {403, 404}:
            message = parsed.get("message", "") if isinstance(parsed, dict) else ""
            limited = status == 403 and isinstance(message, str) and (
                "secondary rate limit" in message.casefold() or "abuse detection" in message.casefold())
            raise GitHubFailure("GITHUB_RATE_LIMITED" if limited else "GITHUB_PERMISSION_DENIED", status,
                                request_rejected=True)
        if not isinstance(parsed, dict):
            raise GitHubFailure("GITHUB_RESPONSE_INVALID", status)
        return parsed

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> GitHubTokenBundle:
        if not self.client_id or not self.client_secret:
            raise GitHubFailure("GITHUB_CONFIGURATION_ERROR")
        result = await self._json("https://github.com/login/oauth/access_token", method="POST",
            body=urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                            "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}))
        return token_bundle(result)

    async def validate_refresh_configuration(self) -> None:
        """Finish local checks before claiming a single-use provider refresh."""
        if not self.client_id or not self.client_secret:
            raise GitHubFailure("GITHUB_CONFIGURATION_ERROR")
        await self._crypto_key("encrypt")

    async def refresh(self, refresh_token: str) -> GitHubTokenBundle:
        if not self.client_id or not self.client_secret or not _token_text(refresh_token):
            raise GitHubFailure("GITHUB_CONFIGURATION_ERROR", request_rejected=True)
        result = await self._json("https://github.com/login/oauth/access_token", method="POST",
            body=urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                            "grant_type": "refresh_token", "refresh_token": refresh_token}))
        return token_bundle(result, refreshing=True)

    async def profile(self, token: str) -> dict:
        return await self._json("https://api.github.com/user", token=token)

    async def _paged(self, endpoint: str, token: str, field: str) -> list[dict]:
        items = []
        for page in range(1, 11):
            result = await self._json(f"https://api.github.com{endpoint}?per_page=100&page={page}", token=token)
            batch = result.get(field)
            if not isinstance(batch, list) or len(batch) > 100:
                raise GitHubFailure("GITHUB_RESPONSE_INVALID")
            items.extend(batch)
            if len(batch) < 100:
                return items
            # A full final page can be a complete 1,000-repository grant. Both
            # GitHub endpoints expose total_count; accept that exact boundary
            # without an eleventh request or silently truncating larger grants.
            if page == 10 and type(result.get("total_count")) is int and result["total_count"] == len(items):
                return items
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")

    async def installations(self, token: str) -> list[dict]:
        return await self._paged("/user/installations", token, "installations")

    async def repositories(self, token: str, installation_id: int) -> list[dict]:
        if type(installation_id) is not int or installation_id <= 0:
            raise ValueError("invalid installation ID")
        return await self._paged(f"/user/installations/{installation_id}/repositories", token, "repositories")

    async def _crypto_key(self, use: str):
        raw = _decode_key(self._key)
        from js import crypto, Object
        from pyodide.ffi import to_js
        try:
            return await crypto.subtle.importKey("raw", _bytes_view(raw),
                to_js({"name": "AES-GCM"}, dict_converter=Object.fromEntries), False,
                to_js([use]))
        except Exception:
            raise GitHubFailure("GITHUB_CONFIGURATION_ERROR") from None

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
        try:
            if not isinstance(value, str) or not value.startswith("gcm1:"):
                raise GitHubFailure("GITHUB_TOKEN_UNREADABLE")
            raw = base64.urlsafe_b64decode(value[5:] + "=" * (-len(value[5:]) % 4))
            if len(raw) < 29:
                raise GitHubFailure("GITHUB_TOKEN_UNREADABLE")
            decrypted = await crypto.subtle.decrypt(
                to_js({"name": "AES-GCM", "iv": _bytes_view(raw[:12])}, dict_converter=Object.fromEntries),
                await self._crypto_key("decrypt"), _bytes_view(raw[12:]))
            token = _bytes_from_view(Uint8Array.new(decrypted)).decode("utf-8")
            if not token:
                raise GitHubFailure("GITHUB_TOKEN_UNREADABLE")
            return token
        except GitHubFailure:
            raise
        except Exception:
            raise GitHubFailure("GITHUB_TOKEN_UNREADABLE") from None
