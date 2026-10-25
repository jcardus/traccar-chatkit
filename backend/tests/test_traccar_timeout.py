"""Traccar reads: split timeouts, a tool error, and the /api proxy.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import asyncio
import gzip
import json
import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import requests
from agents import RunContextWrapper
from fastapi.testclient import TestClient

from app.chat import invoke_api
from app.main import app
from app.traccar import (
    IncompleteResponse,
    UnparsedBody,
    _read_full_body,
    body_limits,
    invoke,
    traccar_timeout,
)


def _ok(payload=None):
    payload = {"ok": True} if payload is None else payload
    raw = json.dumps(payload).encode()
    return SimpleNamespace(
        status_code=200,
        history=[],
        encoding="utf-8",
        raw=None,
        raise_for_status=lambda: None,
        iter_content=lambda chunk_size=65536: iter([raw]),
        close=lambda: None,
    )


class TimeoutSelectionTests(unittest.TestCase):
    def test_ordinary_calls_keep_the_short_read_timeout(self):
        self.assertEqual(traccar_timeout("/devices"), (10.0, 25.0))
        self.assertEqual(traccar_timeout("session"), (10.0, 25.0))
        self.assertEqual(traccar_timeout("/positions"), (10.0, 25.0))
        self.assertEqual(traccar_timeout("/positions", "deviceId=761"), (10.0, 25.0))
        self.assertEqual(
            traccar_timeout("/positions", "deviceId=761&from=2026-01-01T00:00:00Z"),
            (10.0, 25.0),
        )

    def test_reports_and_ranged_positions_wait_longer(self):
        self.assertEqual(traccar_timeout("/reports/route"), (10.0, 300.0))
        self.assertEqual(traccar_timeout("reports/trips"), (10.0, 300.0))
        ranged = "deviceId=761&from=2026-01-01T00:00:00Z&to=2026-10-25T23:59:59Z"
        self.assertEqual(traccar_timeout("positions", ranged), (10.0, 300.0))
        self.assertEqual(
            traccar_timeout(f"/positions?{ranged}"),
            (10.0, 300.0),
        )
        self.assertEqual(
            traccar_timeout("/positions", "From=2026-01-01T00:00:00Z&To=2026-10-25T23:59:59Z"),
            (10.0, 300.0),
        )

    def test_invoke_passes_the_split_timeout(self):
        request = SimpleNamespace(headers={})
        with patch("app.traccar.requests.request", return_value=_ok()) as outbound:
            invoke(
                "get",
                "/positions?deviceId=761&from=2026-01-01T00:00:00Z&to=2026-10-25T23:59:59Z",
                "",
                request,
            )
        self.assertEqual(outbound.call_args.kwargs["timeout"], (10.0, 300.0))
        self.assertTrue(outbound.call_args.kwargs["stream"])

        with patch("app.traccar.requests.request", return_value=_ok()) as outbound:
            invoke("get", "/devices", "", request)
        self.assertEqual(outbound.call_args.kwargs["timeout"], (10.0, 25.0))
        self.assertTrue(outbound.call_args.kwargs["stream"])

    def test_long_body_silence_matches_the_first_byte_wait(self):
        self.assertEqual(body_limits("/devices"), (60.0, 90.0))
        self.assertEqual(body_limits("/positions"), (60.0, 90.0))
        self.assertEqual(body_limits("/reports/route"), (300.0, 600.0))
        self.assertEqual(body_limits("/positions", "from=a&to=b"), (300.0, 600.0))


class InvokeApiTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = RunContextWrapper(
            context=SimpleNamespace(request_context={}, tool_result_bytes=0)
        )

    async def test_timeout_is_a_tool_error_the_model_can_act_on(self):
        path = (
            "/reports/route?deviceId=761"
            "&from=2026-01-01T00:00:00Z&to=2026-10-25T23:59:59Z"
        )
        args = json.dumps({"method": "GET", "path": path})
        with patch("app.chat.invoke", side_effect=requests.ReadTimeout("hung")):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        self.assertNotIn("stored_data", result)
        message = result["error"]
        self.assertIn("did not answer", message)
        self.assertIn("GET /reports/route", message)
        self.assertIn("within 300s", message)
        self.assertIn("Do not retry this same request", message)
        self.assertNotIn("one device", message)
        self.assertNotIn("Please try again", message)
        self.assertNotIn("Traceback", message)

    async def test_a_short_read_reports_the_short_budget(self):
        args = json.dumps({"method": "GET", "path": "/devices"})
        with patch("app.chat.invoke", side_effect=requests.ReadTimeout("hung")):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        self.assertIn("within 25s", result["error"])
        self.assertIn("/devices", result["error"])
        self.assertIn("Do not retry this same request", result["error"])

    async def test_connect_timeout_names_the_connect_budget(self):
        args = json.dumps({"method": "GET", "path": "/devices"})
        with patch("app.chat.invoke", side_effect=requests.ConnectTimeout("hung")):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        self.assertIn("within 10s", result["error"])
        self.assertNotIn("within 25s", result["error"])
        self.assertIn("Do not retry this same request", result["error"])

    async def test_other_failures_keep_the_generic_tool_error(self):
        args = json.dumps({"method": "GET", "path": "/devices"})
        with patch("app.chat.invoke", side_effect=requests.HTTPError("500")):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        self.assertIsInstance(result, str)
        self.assertIn("Please try again", result)
        self.assertNotIn("shorter date range", result)

    async def test_incomplete_body_names_the_status_and_the_bytes(self):
        path = (
            "/positions?deviceId=761"
            "&from=2026-01-01T00:00:00Z&to=2026-10-25T23:59:59Z"
        )
        args = json.dumps({"method": "GET", "path": path})
        exc = IncompleteResponse(200, 12000)
        with patch("app.chat.invoke", side_effect=exc):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        message = result["error"]
        self.assertIn("HTTP 200", message)
        self.assertIn("incomplete", message)
        self.assertIn("12000 bytes", message)
        self.assertIn("Do not treat a partial result as the total", message)
        self.assertIn("Do not retry this same request", message)
        self.assertNotIn("did not answer", message)
        self.assertNotIn("Please try again", message)

    async def test_slow_invoke_does_not_block_the_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*_args, **_kwargs):
            started.set()
            if not release.wait(timeout=2):
                raise AssertionError("timed out waiting to release invoke")
            return [{"id": 1}]

        args = json.dumps({"method": "GET", "path": "/devices"})
        with patch("app.chat.invoke", side_effect=slow):
            task = asyncio.create_task(invoke_api.on_invoke_tool(self.ctx, args))
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            else:
                release.set()
                self.fail("invoke did not start")
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            result = await asyncio.wait_for(task, timeout=2)
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(self.ctx.context.tool_result_bytes, len(json.dumps([{"id": 1}])))


class BodyReadTests(unittest.TestCase):
    def test_a_finished_body_is_returned(self):
        raw = b'[{"id": 1}]'
        response = SimpleNamespace(
            status_code=200,
            encoding="utf-8",
            raw=None,
            iter_content=lambda chunk_size=65536: iter([raw]),
        )
        body = _read_full_body(response, time.monotonic() + 5, 180.0)
        self.assertEqual(json.loads(body.decode()), [{"id": 1}])

    def test_total_cap_stops_before_the_body_ends(self):
        def chunks(chunk_size=65536):
            while True:
                yield b"x"

        response = SimpleNamespace(status_code=200, raw=None, iter_content=chunks)
        clock = iter((0.0, 0.0, 500.0))
        with patch("app.traccar.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(IncompleteResponse) as caught:
                _read_full_body(response, deadline=300.0, silence=180.0)
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(caught.exception.received, 2)

    def test_mid_body_timeout_keeps_the_status_and_the_bytes(self):
        def chunks(chunk_size=65536):
            yield b'{"a":'
            raise requests.ConnectionError("Read timed out.")

        response = SimpleNamespace(
            status_code=200,
            history=[],
            encoding="utf-8",
            raw=None,
            raise_for_status=lambda: None,
            iter_content=chunks,
            close=lambda: None,
        )
        request = SimpleNamespace(headers={})
        with (
            patch("app.traccar.requests.request", return_value=response),
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            with self.assertRaises(IncompleteResponse) as caught:
                invoke("get", "/reports/route?deviceId=1&from=a&to=b", "", request)
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(caught.exception.received, 5)
        self.assertIn("200 incomplete, 5 bytes", logs.output[0])
        self.assertNotIn("no response", logs.output[0])
        self.assertEqual(logs.records[0].levelno, logging.WARNING)

    def test_headers_raise_the_body_silence_timeout(self):
        class Sock:
            def __init__(self):
                self.timeouts: list[float] = []

            def settimeout(self, value):
                self.timeouts.append(value)

        sock = Sock()
        response = _ok()
        response.raw = SimpleNamespace(_connection=SimpleNamespace(sock=sock))
        request = SimpleNamespace(headers={})
        with patch("app.traccar.requests.request", return_value=response):
            invoke(
                "get",
                "/positions?from=2026-01-01T00:00:00Z&to=2026-10-25T23:59:59Z",
                "",
                request,
            )
        self.assertEqual(sock.timeouts, [300.0, 300.0])


class _Wire:
    """Socket stream that returns the on-the-wire bytes, still compressed."""

    decode_content = True

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, amt=None, decode_content=None, cache_content=False):
        if self._pos >= len(self._data):
            return b""
        if amt is None:
            amt = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + amt]
        self._pos += len(chunk)
        return chunk


def _gzip_response(payload: bytes):
    def decoded(chunk_size=65536):
        raise AssertionError("gzip body was inflated while reading")

    return SimpleNamespace(
        status_code=200,
        history=[],
        encoding="utf-8",
        headers={"Content-Encoding": "gzip"},
        raw=_Wire(payload),
        raise_for_status=lambda: None,
        iter_content=decoded,
        close=lambda: None,
    )


class GzipHandoffTests(unittest.TestCase):
    def test_small_gzip_is_still_parsed(self):
        request = SimpleNamespace(headers={})
        with patch("app.traccar.requests.request", return_value=_gzip_response(gzip.compress(b'{"ok": true}'))):
            result = invoke("get", "/devices", "", request)
        self.assertEqual(result, {"ok": True})

    def test_large_gzip_is_handed_off_compressed(self):
        document = json.dumps(
            [{"id": 1, "deviceId": 761}, {"id": 2, "note": "y" * 80}]
        ).encode()
        compressed = gzip.compress(document)
        request = SimpleNamespace(headers={})
        with (
            patch("app.traccar.requests.request", return_value=_gzip_response(compressed)),
            patch("app.traccar.MAX_PARSED_BODY_BYTES", 64),
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            result = invoke(
                "get",
                "/positions?deviceId=761&from=a&to=b",
                "",
                request,
            )
        self.assertIsInstance(result, UnparsedBody)
        self.assertTrue(result.compressed)
        self.assertEqual(result.payload, compressed)
        self.assertEqual(result.uncompressed_size, len(document))
        self.assertGreater(len(document), 64)
        self.assertEqual(result.sample[0], {"id": 1, "deviceId": 761})
        self.assertIn("without parsing", "\n".join(logs.output))

    def test_large_plain_json_is_handed_off_without_parsing(self):
        document = json.dumps([{"id": i} for i in range(30)]).encode()
        response = SimpleNamespace(
            status_code=200,
            history=[],
            encoding="utf-8",
            headers={},
            raw=None,
            raise_for_status=lambda: None,
            iter_content=lambda chunk_size=65536: iter([document]),
            close=lambda: None,
        )
        request = SimpleNamespace(headers={})
        with (
            patch("app.traccar.requests.request", return_value=response),
            patch("app.traccar.MAX_PARSED_BODY_BYTES", 64),
        ):
            result = invoke("get", "/reports/route", "", request)
        self.assertIsInstance(result, UnparsedBody)
        self.assertFalse(result.compressed)
        self.assertEqual(result.payload, document)
        self.assertEqual(result.sample[0], {"id": 0})


class _FakeResponse:
    def __init__(self, content=b'{"ok":true}', status_code=200):
        self.status_code = status_code
        self.headers = {
            "content-type": "application/json",
            "transfer-encoding": "chunked",
        }
        self.content = content
        self.history = []


class ProxyTimeoutTests(unittest.TestCase):
    def _client(self, timeout_box, outcome):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                timeout_box.append(kwargs["timeout"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def request(self, *_args, **_kwargs):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return FakeClient

    def test_devices_use_the_short_read_timeout(self):
        seen = []
        client = self._client(seen, _FakeResponse())
        with patch("app.main.httpx.AsyncClient", client):
            response = TestClient(app).get("/api/devices")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'{"ok":true}')
        self.assertNotIn("transfer-encoding", {k.lower() for k in response.headers})
        self.assertEqual(seen[0].connect, 10.0)
        self.assertEqual(seen[0].read, 25.0)
        self.assertEqual(seen[0].write, 25.0)
        self.assertEqual(seen[0].pool, 10.0)

    def test_ranged_positions_use_the_long_read_timeout(self):
        seen = []
        client = self._client(seen, _FakeResponse(b"[]"))
        with patch("app.main.httpx.AsyncClient", client):
            response = TestClient(app).get(
                "/api/positions",
                params={
                    "deviceId": "761",
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-10-25T23:59:59Z",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0].read, 300.0)
        self.assertEqual(seen[0].connect, 10.0)
        self.assertEqual(seen[0].write, 25.0)

    def test_report_timeout_is_a_504_with_the_same_guidance(self):
        seen = []
        client = self._client(seen, httpx.ReadTimeout("slow"))
        with patch("app.main.httpx.AsyncClient", client):
            response = TestClient(app).get(
                "/api/reports/route",
                params={
                    "deviceId": "761",
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-10-25T23:59:59Z",
                },
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(seen[0].read, 300.0)
        message = response.json()["error"]
        self.assertIn("did not answer", message)
        self.assertIn("within 300s", message)
        self.assertIn("Do not retry this same request", message)
        self.assertNotIn("one device", message)
        self.assertIn("reports/route", message)


if __name__ == "__main__":
    unittest.main()
