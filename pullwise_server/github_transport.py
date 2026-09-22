"""Read-only GitHub JSON boundary; domain adapters inject this callable.

The requests implementation is the CPython reference transport. It bounds
decoded response size and socket inactivity, not total slow-stream wall time.
Cloudflare must supply and validate its native transport before live enablement.
No retries, sleeps, credentials lookup, persistence or model work belongs here.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    payload: object
    headers: Mapping[str, str] = field(default_factory=dict)


class GitHubUnavailable(RuntimeError):
    def __init__(self, message: str = "GITHUB_UNAVAILABLE", *, retry_at: int | None = None):
        super().__init__(message)
        self.retry_at = retry_at


_METADATA = frozenset({"etag", "link", "last-modified", "retry-after", "x-poll-interval",
                       "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-limit"})
_PATH = re.compile(r"/[A-Za-z0-9_./-]*\Z")
_TOKEN = re.compile(r"[!-~]*\Z")


def _validate_path(path: str) -> None:
    if not isinstance(path, str) or len(path) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise GitHubUnavailable("GITHUB_PATH_INVALID")
    parsed = urlsplit(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not _PATH.fullmatch(parsed.path)
            or "//" in parsed.path or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise GitHubUnavailable("GITHUB_PATH_INVALID")
    if any(key.lower() in {"access_token", "token", "client_secret", "authorization"}
           for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise GitHubUnavailable("GITHUB_PATH_INVALID")


def _retry_at(headers: Mapping[str, str], now: int) -> int:
    deadlines = [now + 60]
    value = headers.get("retry-after", "")
    if value.isascii() and value.isdigit():
        deadlines.append(now + int(value))
    elif value:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                deadlines.append(int(date.timestamp()))
        except (TypeError, ValueError, OverflowError):
            pass
    reset = headers.get("x-ratelimit-reset", "")
    if headers.get("x-ratelimit-remaining") == "0" and reset.isascii() and reset.isdigit():
        deadlines.append(int(reset))
    return max(deadlines)


def _reject_constant(value: str):
    raise ValueError("GITHUB_JSON_INVALID")


class GitHubRESTTransport:
    def __init__(self, *, request: Callable | None = None, clock: Callable = time.time,
                 max_body_bytes: int = 1024 * 1024):
        if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= 1024 * 1024:
            raise ValueError("GITHUB_BODY_LIMIT_INVALID")
        self.request = request
        self.clock = clock
        self.max_body_bytes = max_body_bytes

    def __call__(self, path: str, *, token: str) -> GitHubResponse:
        _validate_path(path)
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise GitHubUnavailable("GITHUB_TOKEN_INVALID")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "Pullwise/1.4"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = self.request
        if request is None:
            import requests
            request = requests.get
        response = None
        try:
            response = request("https://api.github.com" + path, headers=headers,
                               timeout=(5, 10), stream=True, allow_redirects=False)
            metadata = {key.lower(): value for key, value in response.headers.items()
                        if key.lower() in _METADATA and isinstance(value, str)}
            status = response.status_code
            if status == 429 or (status in {403, 503} and
                    ("retry-after" in metadata or metadata.get("x-ratelimit-remaining") == "0")):
                raise GitHubUnavailable("GITHUB_RATE_LIMITED", retry_at=_retry_at(metadata, int(self.clock())))
            if type(status) is not int or not 200 <= status < 500 or 300 <= status < 400:
                raise GitHubUnavailable()
            length = response.headers.get("Content-Length")
            if length is not None and (not str(length).isascii() or not str(length).isdigit()
                                       or int(length) > self.max_body_bytes):
                raise GitHubUnavailable("GITHUB_BODY_LIMIT")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                if len(body) + len(chunk) > self.max_body_bytes:
                    raise GitHubUnavailable("GITHUB_BODY_LIMIT")
                body.extend(chunk)
            payload = None if status == 204 and not body else json.loads(
                body.decode("utf-8"), parse_constant=_reject_constant)
            return GitHubResponse(status=status, payload=payload, headers=metadata)
        except GitHubUnavailable:
            raise
        except Exception:
            # requests exceptions may embed headers, URLs and private bodies.
            raise GitHubUnavailable() from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
