"""SSE comment frames while a chat stream is silent.

Cloudflare closes a proxied response after 125s with no bytes from the origin.
Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import asyncio
import contextvars
import unittest
from unittest.mock import patch

from app.main import SSE_KEEPALIVE, _timed_sse_stream


async def _collect(source, interval):
    chunks = []
    with patch("app.main.SSE_KEEPALIVE_INTERVAL", interval):
        async for chunk in _timed_sse_stream(source, "test"):
            chunks.append(chunk)
    return chunks


class SseKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stream_has_no_keepalive(self):
        async def source():
            yield b"data: a\n\n"
            yield b"data: b\n\n"

        chunks = await _collect(source(), 20)
        self.assertEqual(chunks, [b"data: a\n\n", b"data: b\n\n"])

    async def test_silence_yields_comments_then_the_chunk(self):
        async def source():
            await asyncio.sleep(0.18)
            yield b"data: ok\n\n"

        chunks = await _collect(source(), 0.05)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[-1], b"data: ok\n\n")
        self.assertTrue(all(chunk == SSE_KEEPALIVE for chunk in chunks[:-1]))

    async def test_source_error_propagates(self):
        async def source():
            yield b"data: a\n\n"
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await _collect(source(), 20)

    async def test_context_reset_stays_on_the_reader_task(self):
        probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)

        async def source():
            token = probe.set("on")
            try:
                yield b"data: a\n\n"
                await asyncio.sleep(0.12)
                yield b"data: b\n\n"
            finally:
                probe.reset(token)

        chunks = await _collect(source(), 0.05)
        self.assertEqual(chunks[0], b"data: a\n\n")
        self.assertEqual(chunks[-1], b"data: b\n\n")
        self.assertTrue(any(chunk == SSE_KEEPALIVE for chunk in chunks))
        self.assertIsNone(probe.get())

    async def test_disconnect_cancels_the_source(self):
        cancelled = asyncio.Event()

        async def source():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield b"data: late\n\n"

        agen = _timed_sse_stream(source(), "test")
        with patch("app.main.SSE_KEEPALIVE_INTERVAL", 0.05):
            consumer = asyncio.create_task(agen.__anext__())
            await asyncio.sleep(0.01)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
