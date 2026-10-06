"""Creem responses stay bounded and provider failures preserve charge uncertainty."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pullwise_server.cloudflare_creem_gateway import (
    MAX_RESPONSE_BYTES, CreemRequestRejected, WorkerCreemGateway, _bounded_response,
)


class Reader:
    def __init__(self, *chunks):
        self.chunks = iter(chunks)
        self.cancelled = False

    async def read(self):
        value = next(self.chunks, b"")
        return SimpleNamespace(done=not value, value=value)

    async def cancel(self):
        self.cancelled = True


class Body:
    def __init__(self, *chunks):
        self.reader = Reader(*chunks)
        self.cancelled = False
        self.read = False

    def getReader(self):
        self.read = True
        return self.reader

    async def cancel(self):
        self.cancelled = True


def call_gateway(response):
    calls = []
    async def fetch(url, options):
        calls.append((url, options))
        return response
    js = SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
        AbortSignal=SimpleNamespace(timeout=lambda ms: ms))
    ffi = SimpleNamespace(to_js=lambda value, **_: value)
    gateway = WorkerCreemGateway(SimpleNamespace(PULLWISE_CREEM_API_KEY="synthetic-key",
        PULLWISE_CREEM_API_BASE_URL="https://test-api.creem.io"))
    with patch.dict("sys.modules", {"js": js, "pyodide": SimpleNamespace(ffi=ffi), "pyodide.ffi": ffi}):
        result = asyncio.run(gateway.post("v1/subscriptions/sub_1/upgrade", {"product_id": "prod-max"}))
    return result, calls


def test_gateway_reads_bounded_stream_and_preserves_manual_redirect_timeout():
    body = Body(b'{"id":', b'"sub_1"}')
    result, calls = call_gateway(SimpleNamespace(ok=True, status=200, body=body,
        headers=SimpleNamespace(get=lambda _: None)))
    assert result == {"id": "sub_1"}
    assert len(calls) == 1 and calls[0][0].startswith("https://test-api.creem.io/")
    assert calls[0][1]["redirect"] == "manual" and calls[0][1]["signal"] == 10000


def test_stream_limit_cancels_as_soon_as_bytes_cross_the_bound():
    reader = Reader(b"ab", b"cde", b"unread")
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(_bounded_response(reader, limit=4))
    assert reader.cancelled
    assert next(reader.chunks) == b"unread"


def test_interrupted_response_cancels_and_preserves_unknown_outcome():
    class InterruptedReader(Reader):
        async def read(self):
            raise TimeoutError("synthetic provider timeout")
    reader = InterruptedReader()
    with pytest.raises(TimeoutError):
        asyncio.run(_bounded_response(reader))
    assert reader.cancelled


@pytest.mark.parametrize("length", [str(MAX_RESPONSE_BYTES + 1), "invalid", "-1", "٣"])
def test_invalid_content_length_cancels_without_buffering(length):
    body = Body(b"must not be read")
    with pytest.raises(ValueError):
        call_gateway(SimpleNamespace(ok=True, status=200, body=body,
            headers=SimpleNamespace(get=lambda _: length)))
    assert body.cancelled and not body.read


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
def test_definitive_rejection_does_not_read_or_expose_provider_body(status):
    body = Body(b"provider sensitive diagnostic")
    with pytest.raises(CreemRequestRejected, match=f"HTTP {status}"):
        call_gateway(SimpleNamespace(ok=False, status=status, body=body))
    assert body.cancelled and not body.read


@pytest.mark.parametrize("status", [302, 408, 500, 503])
def test_uncertain_provider_outcome_is_not_treated_as_a_definitive_rejection(status):
    body = Body(b"provider sensitive diagnostic")
    with pytest.raises(ValueError) as error:
        call_gateway(SimpleNamespace(ok=False, status=status, body=body))
    assert not isinstance(error.value, CreemRequestRejected)
    assert body.cancelled and not body.read
