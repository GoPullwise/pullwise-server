"""Direct, verified HTTPS transport whose blocked reads can be cancelled."""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
from contextlib import suppress
from urllib.parse import urlsplit

import certifi

from .model_gateway_streaming import RequestCancellation


class UpstreamTransportError(RuntimeError):
    pass


class _Connection(http.client.HTTPSConnection):
    def __init__(self, *args, cancellation: RequestCancellation, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancellation = cancellation
        self.active_socket = None
        cancellation.add_callback(self.abort)

    def connect(self):
        if self.cancellation.is_set():
            raise UpstreamTransportError("upstream request cancelled")
        super().connect()
        self.active_socket = self.sock
        if self.cancellation.is_set():
            self.abort()
            raise UpstreamTransportError("upstream request cancelled")

    def abort(self):
        # HTTPResponse may own the socket after an HTTP/1.0 or Connection: close response.
        # shutdown, rather than only close, interrupts the reader's file object too.
        if self.active_socket is not None:
            with suppress(OSError):
                self.active_socket.shutdown(socket.SHUT_RDWR)


class UpstreamResponse:
    def __init__(self, connection: _Connection, response: http.client.HTTPResponse):
        self.connection = connection
        self.response = response
        self.status = response.status
        self.headers = response.headers

    def iter_bytes(self, chunk_size: int):
        try:
            while not self.connection.cancellation.is_set():
                chunk = self.response.read1(chunk_size)
                if not chunk:
                    return
                if self.connection.cancellation.is_set():
                    return
                yield chunk
        except (OSError, http.client.HTTPException) as error:
            if not self.connection.cancellation.is_set():
                raise UpstreamTransportError("upstream response failed") from error
        finally:
            self.close()

    def close(self):
        self.response.close()
        self.connection.close()


def open_upstream(url: str, *, headers: dict[str, str], payload: dict[str, object],
                  timeout_seconds: float, cancellation: RequestCancellation | None = None) -> UpstreamResponse:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpstreamTransportError("upstream URL is invalid")
    cancellation = cancellation or RequestCancellation()
    context = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where())
    context.set_alpn_protocols(["http/1.1"])
    connection = _Connection(parsed.hostname, parsed.port or 443, context=context,
        timeout=timeout_seconds, cancellation=cancellation)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if cancellation.is_set():
            raise UpstreamTransportError("upstream request cancelled")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request("POST", target, body=encoded, headers={"Accept-Encoding": "identity", **headers})
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
            response.close()
            raise UpstreamTransportError("upstream response encoding is unsupported")
        return UpstreamResponse(connection, response)
    except (OSError, http.client.HTTPException, UnicodeError, ValueError) as error:
        connection.close()
        raise UpstreamTransportError("upstream request failed") from error
    except BaseException:
        connection.close()
        raise
