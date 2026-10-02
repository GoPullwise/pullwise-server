"""Response size is enforced while bytes arrive, before buffering a body."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_server.cloudflare_jev_gateway import _bounded_response, WorkerJevGateway


class Chunk:
    def __init__(self, value=b""):
        self.value = value
        self.done = not value


class Reader:
    def __init__(self, *chunks):
        self.chunks = iter(chunks)
        self.cancelled = False

    async def read(self):
        return Chunk(next(self.chunks, b""))

    async def cancel(self):
        self.cancelled = True


class BoundedResponseTests(unittest.TestCase):
    def test_accepts_chunks_up_to_limit(self):
        self.assertEqual(asyncio.run(_bounded_response(Reader(b"ab", b"cd"), 4)), b"abcd")

    def test_cancels_as_soon_as_limit_is_crossed(self):
        reader = Reader(b"ab", b"cde")
        with self.assertRaises(ValueError):
            asyncio.run(_bounded_response(reader, 4))
        self.assertTrue(reader.cancelled)

    def test_gateway_refuses_redirect_without_forwarding_credential(self):
        calls = []
        async def fetch(url, options):
            calls.append((url, options))
            return SimpleNamespace(ok=False, status=302)
        js = SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
            AbortSignal=SimpleNamespace(timeout=lambda ms: ms))
        ffi = SimpleNamespace(to_js=lambda value, **_: value)
        gateway = WorkerJevGateway(SimpleNamespace(TYPESAFE_API_KEY="synthetic-key",
            PULLWISE_JEV_SUGGESTIONS_ENABLED="1", PULLWISE_JEV_SUGGESTIONS_EVALUATED="1"))
        with patch.dict("sys.modules", {"js": js, "pyodide": SimpleNamespace(ffi=ffi), "pyodide.ffi": ffi}):
            with self.assertRaises(OSError):
                asyncio.run(gateway.evaluate({"state": "synthetic"}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["redirect"], "manual")
        self.assertEqual(calls[0][1]["signal"], 3000)
