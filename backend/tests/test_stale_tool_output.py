"""Tests for dropping a threads.add_client_tool_output that has no pending tool call.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatkit.types import ClientToolCallItem
from fastapi.testclient import TestClient

from app.main import _is_stale_tool_output, app, get_chatkit_server


def _tool_call(status):
    return ClientToolCallItem(
        id="tc_1",
        thread_id="thr_1",
        created_at=datetime.now(),
        status=status,
        call_id="call_1",
        name="show_html",
        arguments={},
    )


def _server(items=None, error=None):
    load = (
        AsyncMock(side_effect=error)
        if error
        else AsyncMock(return_value=SimpleNamespace(data=items or []))
    )
    return SimpleNamespace(store=SimpleNamespace(load_thread_items=load))


def _output_payload():
    return json.dumps(
        {
            "type": "threads.add_client_tool_output",
            "params": {"thread_id": "thr_1", "result": {"success": True}},
        }
    ).encode()


class StaleToolOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_tool_call_is_not_stale(self):
        server = _server([_tool_call("pending")])
        self.assertFalse(await _is_stale_tool_output(server, _output_payload(), {}))

    async def test_newer_user_message_makes_it_stale(self):
        server = _server([SimpleNamespace(type="user_message")])
        self.assertTrue(await _is_stale_tool_output(server, _output_payload(), {}))

    async def test_already_completed_tool_call_is_stale(self):
        server = _server([_tool_call("completed")])
        self.assertTrue(await _is_stale_tool_output(server, _output_payload(), {}))

    async def test_empty_thread_is_stale(self):
        self.assertTrue(await _is_stale_tool_output(_server([]), _output_payload(), {}))

    async def test_checks_only_the_latest_item(self):
        server = _server()
        await _is_stale_tool_output(server, _output_payload(), {"k": 1})
        server.store.load_thread_items.assert_awaited_once_with("thr_1", None, 1, "desc", {"k": 1})

    async def test_other_requests_are_never_stale(self):
        payload = json.dumps({"type": "threads.add_user_message", "params": {}}).encode()
        server = _server([])
        self.assertFalse(await _is_stale_tool_output(server, payload, {}))
        server.store.load_thread_items.assert_not_awaited()

    async def test_invalid_payload_is_left_to_chatkit(self):
        self.assertFalse(await _is_stale_tool_output(_server([]), b"not json", {}))

    async def test_store_failure_is_left_to_chatkit(self):
        server = _server(error=RuntimeError("db down"))
        self.assertFalse(await _is_stale_tool_output(server, _output_payload(), {}))


class EndpointTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def _post(self, server):
        app.dependency_overrides[get_chatkit_server] = lambda: server
        return TestClient(app).post("/chatkit", content=_output_payload())

    def test_stale_output_gets_an_empty_stream_and_is_not_processed(self):
        server = _server([SimpleNamespace(type="user_message")])
        server.process = AsyncMock()
        response = self._post(server)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.content, b"")
        server.process.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
