"""Isolated HTTPS GET for one GitHub Actions job log redirect chain.

The parent owns a monotonic deadline and kills a blocked child, including one
stuck in DNS, TLS or a slow response. This is a local Python reference adapter;
production enablement still needs platform and live permission validation.
"""
from __future__ import annotations

import http.client
import ipaddress
import multiprocessing
from multiprocessing.sharedctypes import RawArray
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .github_ci_logs import _download_url


def _public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _allowed_url(value: str) -> tuple[str, str]:
    if (not isinstance(value, str) or not 0 < len(value) <= 8192
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
        raise ValueError("GITHUB_CI_LOG_URL_INVALID")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None
            or parsed.fragment or not parsed.path.startswith("/") or not parsed.hostname):
        raise ValueError("GITHUB_CI_LOG_URL_INVALID")
    host = parsed.hostname
    if host != "api.github.com":
        _download_url(value)
    elif parsed.netloc != host:
        raise ValueError("GITHUB_CI_LOG_URL_INVALID")
    return host, parsed.path + ("?" + parsed.query if parsed.query else "")


def _read_https(url: str, headers: Mapping[str, str], deadline: float, max_bytes: int):
    host, target = _allowed_url(url)
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    candidates = [(family, kind, protocol, address) for family, kind, protocol, _, address in addresses
                  if _public_address(address[0])]
    if not candidates:
        raise OSError("no public peer")
    last_error = None
    for family, kind, protocol, address in candidates:
        raw = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            raw = socket.socket(family, kind, protocol)
            raw.settimeout(remaining)
            raw.connect(address)
            if not _public_address(raw.getpeername()[0]):
                raise OSError("nonpublic connected peer")
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as secure:
                raw = None
                secure.settimeout(max(0.001, deadline - time.monotonic()))
                request = f"GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                for key, value in headers.items():
                    request += f"{key}: {value}\r\n"
                secure.sendall((request + "\r\n").encode("ascii"))
                response = http.client.HTTPResponse(secure)
                secure.settimeout(max(0.001, deadline - time.monotonic()))
                response.begin()
                metadata = dict(response.getheaders())
                length = response.getheader("Content-Length")
                if length is not None and (not length.isascii() or not length.isdigit()
                                           or int(length) > max_bytes):
                    raise OSError("body too large")
                body = bytearray()
                while True:
                    secure.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = response.read(min(8192, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise OSError("body too large")
                return response.status, metadata, bytes(body)
        except (OSError, TimeoutError, ssl.SSLError) as error:
            last_error = error
        finally:
            if raw is not None:
                raw.close()
    raise OSError("HTTPS read unavailable") from last_error


def _child(send, shared, fetch: Callable, url: str, headers: Mapping[str, str], deadline: float, max_bytes: int):
    try:
        status, metadata, body = fetch(url, headers, deadline, max_bytes)
        if (type(status) is not int or not isinstance(metadata, dict)
                or not isinstance(body, bytes) or len(body) > max_bytes
                or len(metadata) > 64
                or sum(len(str(key)) + len(str(value)) for key, value in metadata.items()) > 8192):
            raise ValueError("GITHUB_CI_LOG_RESPONSE_INVALID")
        shared[:len(body)] = body
        send.send((True, (status, metadata, len(body))))
    except BaseException:
        send.send((False, None))
    finally:
        send.close()


@dataclass
class _Result:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        pass


class GitHubCILogTransport:
    def __init__(self, *, fetch: Callable = _read_https, max_bytes: int = 5 * 1024 * 1024,
                 clock: Callable = time.monotonic):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 5 * 1024 * 1024:
            raise ValueError("GITHUB_CI_LOG_LIMIT_INVALID")
        self.fetch, self.max_bytes, self.clock = fetch, max_bytes, clock

    def __call__(self, url: str, *, headers: Mapping[str, str], stream: bool,
                 allow_redirects: bool, deadline: float) -> _Result:
        host, _ = _allowed_url(url)
        if (stream is not True or allow_redirects is not False or not isinstance(headers, Mapping)
                or deadline <= self.clock()):
            raise ValueError("GITHUB_CI_LOG_REQUEST_INVALID")
        if (len(headers) > 16 or sum(len(str(key)) + len(str(value)) for key, value in headers.items()) > 8192
                or any(not isinstance(key, str) or not isinstance(value, str) or not key
                       or not key.isascii() or not value.isascii()
                       or any(char in key for char in ":\r\n")
                       or any(char in value for char in "\r\n")
                       for key, value in headers.items())):
            raise ValueError("GITHUB_CI_LOG_HEADER_INVALID")
        if host != "api.github.com" and any(key.lower() == "authorization" for key in headers):
            raise ValueError("GITHUB_CI_LOG_REQUEST_INVALID")
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        shared = RawArray("B", self.max_bytes)
        process = context.Process(target=_child,
            args=(send, shared, self.fetch, url, dict(headers), deadline, self.max_bytes), daemon=True)
        try:
            process.start()
            send.close()
            process.join(timeout=max(0, deadline - self.clock()))
            if process.is_alive():
                raise TimeoutError("GITHUB_CI_LOG_DEADLINE")
            if not receive.poll(0):
                raise OSError("GITHUB_CI_LOG_UNAVAILABLE")
            success, result = receive.recv()
            if not success or not isinstance(result, tuple) or len(result) != 3:
                raise OSError("GITHUB_CI_LOG_UNAVAILABLE")
            status, response_headers, length = result
            if (type(status) is not int or not isinstance(response_headers, dict)
                    or type(length) is not int or not 0 <= length <= self.max_bytes):
                raise OSError("GITHUB_CI_LOG_UNAVAILABLE")
            return _Result(status, response_headers, bytes(shared[:length]))
        finally:
            receive.close()
            send.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
