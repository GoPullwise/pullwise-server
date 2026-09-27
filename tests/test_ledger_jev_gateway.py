"""Response size is enforced while bytes arrive, before buffering a body."""
import asyncio
import unittest

from pullwise_server.cloudflare_jev_gateway import _bounded_response


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
