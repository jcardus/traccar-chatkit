"""Tests for the per-turn invoke_api byte budget.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import RunContextWrapper

from app.chat import invoke_api

STORED = {"container_id": "cntr_1", "path": "/mnt/data/data.json"}


def _rows(n):
    return [{"id": i, "v": "x" * 20} for i in range(n)]


class TurnBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = RunContextWrapper(
            context=SimpleNamespace(request_context={}, tool_result_bytes=0)
        )

    async def _call(self, rows):
        args = json.dumps({"method": "GET", "path": "/positions"})
        with (
            patch("app.chat.invoke", return_value=rows),
            patch("app.chat.store_result", new=AsyncMock(return_value=STORED)) as store,
            patch("app.chat.MAX_RESPONSE_SIZE", 2000),
            patch("app.chat.MAX_TURN_RESULT_BYTES", 3000),
        ):
            result = await invoke_api.on_invoke_tool(self.ctx, args)
        return result, store

    async def test_small_result_is_returned_and_counted(self):
        rows = _rows(10)
        result, store = await self._call(rows)
        self.assertEqual(result, rows)
        store.assert_not_awaited()
        self.assertEqual(self.ctx.context.tool_result_bytes, len(json.dumps(rows)))

    async def test_single_oversized_result_is_stored_and_not_counted(self):
        result, store = await self._call(_rows(200))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stored_data"], STORED)
        self.assertIn("too large", result["reason"])
        store.assert_awaited_once()
        self.assertEqual(self.ctx.context.tool_result_bytes, 0)

    async def test_many_small_results_hit_the_turn_budget(self):
        results = [(await self._call(_rows(30)))[0] for _ in range(5)]
        raw = [r for r in results if isinstance(r, list)]
        summarized = [r for r in results if isinstance(r, dict)]
        self.assertTrue(raw)
        self.assertTrue(summarized)
        self.assertEqual(results[0], _rows(30))
        self.assertIn("already returned this turn", summarized[0]["reason"])
        self.assertEqual(summarized[0]["stored_data"], STORED)
        self.assertLessEqual(self.ctx.context.tool_result_bytes, 3000)


if __name__ == "__main__":
    unittest.main()
