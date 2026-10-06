"""Response size is enforced while bytes arrive, before buffering a body."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_server.cloudflare_jev_gateway import _bounded_response, WorkerJevGateway
from pullwise_server.typesafe_client import DEFAULT_JEV_MODEL


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
    @staticmethod
    def request():
        return {"state": "synthetic", "model": DEFAULT_JEV_MODEL, "questions": {
            "q": {"type": "choice", "instructions": "Classify.",
                  "criteria": {"yes": "Yes", "no": "No"}}}}

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
            return SimpleNamespace(ok=False, status=302, body=None)
        js = SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
            AbortSignal=SimpleNamespace(timeout=lambda ms: ms))
        ffi = SimpleNamespace(to_js=lambda value, **_: value)
        gateway = WorkerJevGateway(SimpleNamespace(TYPESAFE_API_KEY="synthetic-key",
            PULLWISE_JEV_SUGGESTIONS_ENABLED="1", PULLWISE_JEV_SUGGESTIONS_EVALUATED="1"))
        with patch.dict("sys.modules", {"js": js, "pyodide": SimpleNamespace(ffi=ffi), "pyodide.ffi": ffi}):
            with self.assertRaises(OSError):
                asyncio.run(gateway.evaluate(self.request()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["redirect"], "manual")
        self.assertEqual(calls[0][1]["signal"], 3000)

    def test_missing_or_whitespace_secret_does_not_enable_provider(self):
        for secret in (None, "", "  \n"):
            gateway = WorkerJevGateway(SimpleNamespace(TYPESAFE_API_KEY=secret,
                PULLWISE_JEV_SUGGESTIONS_ENABLED="1", PULLWISE_JEV_SUGGESTIONS_EVALUATED="1"))
            self.assertFalse(gateway.enabled)
            with self.assertRaisesRegex(OSError, "JEV_DISABLED"):
                asyncio.run(gateway.evaluate(self.request()))

    def test_gateway_rejects_invalid_request_before_importing_or_calling_fetch(self):
        gateway = WorkerJevGateway(SimpleNamespace(TYPESAFE_API_KEY="synthetic-key",
            PULLWISE_JEV_SUGGESTIONS_ENABLED="1", PULLWISE_JEV_SUGGESTIONS_EVALUATED="1"))
        cases = [{"state": "synthetic"}, {**self.request(), "model": "jev-latest"},
                 {**self.request(), "state": {"image": "https://example.test/a.png"}},
                 {**self.request(), "state": "x" * 50000}]
        for request in cases:
            with self.subTest(request=str(request)[:40]), self.assertRaises(ValueError):
                asyncio.run(gateway.evaluate(request))

    def test_interrupted_body_cancels_and_preserves_error(self):
        class BrokenReader(Reader):
            async def read(self):
                raise TimeoutError("synthetic timeout")
        reader = BrokenReader()
        with self.assertRaises(TimeoutError):
            asyncio.run(_bounded_response(reader))
        self.assertTrue(reader.cancelled)

    def test_header_rejection_cancels_body_without_reading_it(self):
        class Body:
            cancelled = False
            async def cancel(self):
                self.cancelled = True
            def getReader(self):
                raise AssertionError("rejected body must not be read")
        for length in ("65537", "invalid", "-1", "٣"):
            body = Body()
            async def fetch(url, options):
                return SimpleNamespace(ok=True, body=body,
                    headers=SimpleNamespace(get=lambda _: length))
            js = SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
                AbortSignal=SimpleNamespace(timeout=lambda ms: ms))
            ffi = SimpleNamespace(to_js=lambda value, **_: value)
            gateway = WorkerJevGateway(SimpleNamespace(TYPESAFE_API_KEY="synthetic-key",
                PULLWISE_JEV_SUGGESTIONS_ENABLED="1", PULLWISE_JEV_SUGGESTIONS_EVALUATED="1"))
            with self.subTest(length=length), patch.dict("sys.modules", {
                    "js": js, "pyodide": SimpleNamespace(ffi=ffi), "pyodide.ffi": ffi}):
                with self.assertRaises(ValueError):
                    asyncio.run(gateway.evaluate(self.request()))
            self.assertTrue(body.cancelled)
