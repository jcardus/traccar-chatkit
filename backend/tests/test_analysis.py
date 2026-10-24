"""Tests for storing oversized results and analysing them with the code interpreter.

Mocked: no OpenAI calls. Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents import CodeInterpreterTool, RunContextWrapper

from app.analysis import CONTAINER_IDLE_MINUTES, analyze_data, store_result
from app.chat import _summarize_large_result


def _fake_client():
    client = MagicMock()
    client.containers.create = AsyncMock(return_value=SimpleNamespace(id="cntr_1"))
    client.containers.files.create = AsyncMock(
        return_value=SimpleNamespace(path="/mnt/data/data.json")
    )
    return client


class StoreResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_json_to_an_expiring_container(self):
        client = _fake_client()
        with patch("app.analysis.AsyncOpenAI", return_value=client):
            stored = await store_result([{"id": 1}, {"id": 2}])
        self.assertEqual(stored, {"container_id": "cntr_1", "path": "/mnt/data/data.json"})
        create_kwargs = client.containers.create.await_args.kwargs
        self.assertEqual(
            create_kwargs["expires_after"],
            {"anchor": "last_active_at", "minutes": CONTAINER_IDLE_MINUTES},
        )
        args, kwargs = client.containers.files.create.await_args
        self.assertEqual(args, ("cntr_1",))
        name, payload, content_type = kwargs["file"]
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(payload), [{"id": 1}, {"id": 2}])

    async def test_returns_none_when_upload_fails(self):
        client = _fake_client()
        client.containers.create = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("app.analysis.AsyncOpenAI", return_value=client):
            self.assertIsNone(await store_result([1]))


class AnalyzeDataTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, run):
        ctx = RunContextWrapper(context=SimpleNamespace())
        args = json.dumps(
            {"container_id": "cntr_1", "path": "/mnt/data/data.json", "question": "max speed?"}
        )
        with patch("app.analysis.Runner.run", new=run):
            return await analyze_data.on_invoke_tool(ctx, args)

    async def test_runs_analyst_with_code_interpreter_on_the_container(self):
        run = AsyncMock(return_value=SimpleNamespace(final_output="Max speed was 88 km/h."))
        answer = await self._call(run)
        self.assertEqual(answer, "Max speed was 88 km/h.")
        analyst, prompt = run.await_args.args
        self.assertIn("/mnt/data/data.json", prompt)
        self.assertIn("max speed?", prompt)
        (tool,) = analyst.tools
        self.assertIsInstance(tool, CodeInterpreterTool)
        self.assertEqual(tool.tool_config, {"type": "code_interpreter", "container": "cntr_1"})

    async def test_failure_returns_a_recoverable_message(self):
        answer = await self._call(AsyncMock(side_effect=RuntimeError("expired")))
        self.assertIn("analysis failed", answer)
        self.assertIn("show_html", answer)


class SummaryWithStoredDataTests(unittest.TestCase):
    def test_includes_handle_and_analyze_hint_when_stored(self):
        stored = {"container_id": "cntr_1", "path": "/mnt/data/data.json"}
        summary = _summarize_large_result([{"a": 1}], 10, stored)
        self.assertEqual(summary["stored_data"], stored)
        self.assertIn("analyze_data", summary["next_step"])

    def test_no_handle_or_hint_when_upload_failed(self):
        summary = _summarize_large_result([{"a": 1}], 10, None)
        self.assertNotIn("stored_data", summary)
        self.assertNotIn("analyze_data", summary["next_step"])


if __name__ == "__main__":
    unittest.main()
