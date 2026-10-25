"""Tests for storing oversized results and analysing them with the code interpreter.

Mocked: no OpenAI calls. Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import gzip
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents import CodeInterpreterTool, RunContextWrapper

from app.analysis import (
    ANALYST_INSTRUCTIONS,
    CONTAINER_IDLE_MINUTES,
    FILE_CHECK_INSTRUCTIONS,
    MANIFEST_NAME,
    analyze_data,
    file_can_be_analyzed,
    store_result,
    store_unparsed,
)
from app.traccar import UnparsedBody
from app.chat import _summarize_large_result


def _fake_client():
    client = MagicMock()
    client.containers.create = AsyncMock(return_value=SimpleNamespace(id="cntr_1"))
    created: list[SimpleNamespace] = []

    async def create_file(_container_id, file):
        record = SimpleNamespace(
            id=f"cfile_{len(created)}",
            path=f"/mnt/data/{file[0]}",
            bytes=len(file[1]),
            source="user",
            container_id="cntr_1",
        )
        created.append(record)
        return record

    class _Page:
        def __aiter__(self):
            async def generate():
                for item in created:
                    yield item

            return generate()

    client.containers.files.create = AsyncMock(side_effect=create_file)
    client.containers.files.list = lambda container_id: _Page()
    return client


class StoreResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_json_to_an_expiring_container(self):
        client = _fake_client()
        with patch("app.analysis.AsyncOpenAI", return_value=client):
            stored = await store_result([{"id": 1}, {"id": 2}])
        self.assertEqual(stored["container_id"], "cntr_1")
        self.assertEqual(stored["path"], "/mnt/data/data.json")
        self.assertEqual(stored["files"][0]["id"], "cfile_0")
        self.assertEqual(stored["files"][0]["path"], "/mnt/data/data.json")
        self.assertEqual(stored["files"][0]["source"], "user")
        self.assertGreater(stored["files"][0]["bytes"], 0)
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

    async def test_gzips_when_raw_json_exceeds_container_limit(self):
        items = [
            {
                "deviceId": 1,
                "fixTime": "2026-10-01T00:00:00Z",
                "speed": 0,
                "latitude": 38.7,
                "longitude": -9.1,
            }
        ] * 80
        raw = json.dumps(items, separators=(",", ":"), ensure_ascii=False).encode()
        client = _fake_client()
        with (
            patch("app.analysis.AsyncOpenAI", return_value=client),
            patch("app.analysis.MAX_CONTAINER_FILE_BYTES", len(raw) - 1),
        ):
            stored = await store_result(items)
        self.assertEqual(stored["path"], "/mnt/data/data.json.gz")
        name, payload, content_type = client.containers.files.create.await_args.kwargs["file"]
        self.assertEqual(name, "data.json.gz")
        self.assertEqual(content_type, "application/gzip")
        self.assertLessEqual(len(payload), len(raw) - 1)
        self.assertEqual(json.loads(gzip.decompress(payload)), items)

    async def test_splits_when_gzip_exceeds_container_limit(self):
        items = [
            {
                "deviceId": i % 3,
                "fixTime": f"2026-10-01T00:{i % 60:02d}:00Z",
                "speed": i,
                "note": "p" * 180,
            }
            for i in range(40)
        ]
        raw = json.dumps(items, separators=(",", ":"), ensure_ascii=False).encode()
        compressed = gzip.compress(raw)
        one = gzip.compress(
            json.dumps([items[0]], separators=(",", ":"), ensure_ascii=False).encode()
        )
        limit = max(len(one) + 32, len(compressed) // 3)
        self.assertLess(limit, len(compressed))
        client = _fake_client()
        with (
            patch("app.analysis.AsyncOpenAI", return_value=client),
            patch("app.analysis.MAX_CONTAINER_FILE_BYTES", limit),
        ):
            stored = await store_result(items)
        self.assertEqual(stored["path"], f"/mnt/data/{MANIFEST_NAME}")
        uploads = [call.kwargs["file"] for call in client.containers.files.create.await_args_list]
        part_uploads = uploads[:-1]
        manifest_name, manifest_body, manifest_type = uploads[-1]
        self.assertEqual(manifest_name, MANIFEST_NAME)
        self.assertEqual(manifest_type, "application/json")
        self.assertGreaterEqual(len(part_uploads), 2)
        loaded = []
        for name, payload, content_type in part_uploads:
            self.assertTrue(name.endswith(".json.gz"))
            self.assertEqual(content_type, "application/gzip")
            self.assertLessEqual(len(payload), limit)
            loaded.extend(json.loads(gzip.decompress(payload)))
        self.assertEqual(loaded, items)
        manifest = json.loads(manifest_body)
        self.assertEqual(
            manifest["parts"], [f"/mnt/data/{name}" for name, _, _ in part_uploads]
        )

    async def test_returns_none_without_creating_a_container_when_it_cannot_fit(self):
        client = _fake_client()
        blob = {"blob": os.urandom(800).hex()}
        with (
            patch("app.analysis.AsyncOpenAI", return_value=client),
            patch("app.analysis.MAX_CONTAINER_FILE_BYTES", 32),
        ):
            self.assertIsNone(await store_result(blob))
        client.containers.create.assert_not_awaited()


class StoreUnparsedTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_the_gzip_bytes_unchanged(self):
        items = [{"id": 1, "deviceId": 761, "note": 'keep "]"'}]
        payload = gzip.compress(json.dumps(items).encode())
        body = UnparsedBody(payload, compressed=True, uncompressed_size=10_000_000)
        client = _fake_client()
        with patch("app.analysis.AsyncOpenAI", return_value=client):
            stored = await store_unparsed(body)
        self.assertEqual(stored["path"], "/mnt/data/data.json.gz")
        name, uploaded, content_type = client.containers.files.create.await_args.kwargs["file"]
        self.assertEqual(name, "data.json.gz")
        self.assertEqual(content_type, "application/gzip")
        self.assertEqual(uploaded, payload)
        self.assertEqual(json.loads(gzip.decompress(uploaded)), items)

    async def test_splits_a_gzip_that_exceeds_the_container_limit(self):
        items = [
            {"deviceId": i % 3, "speed": i, "note": f'say "]" {i}'}
            for i in range(40)
        ]
        raw = json.dumps(items, separators=(",", ":")).encode()
        payload = gzip.compress(raw)
        one_sizes = [
            len(gzip.compress(b"[" + json.dumps(item, separators=(",", ":")).encode() + b"]"))
            for item in items
        ]
        limit = max(one_sizes)
        self.assertLess(limit, len(payload))
        body = UnparsedBody(payload, compressed=True, uncompressed_size=len(raw))
        client = _fake_client()
        with (
            patch("app.analysis.AsyncOpenAI", return_value=client),
            patch("app.analysis.MAX_CONTAINER_FILE_BYTES", limit),
            patch("app.analysis._PART_RAW_LIMIT", 1),
        ):
            stored = await store_unparsed(body)
        self.assertEqual(stored["path"], f"/mnt/data/{MANIFEST_NAME}")
        uploads = [call.kwargs["file"] for call in client.containers.files.create.await_args_list]
        loaded = []
        for name, part, content_type in uploads[:-1]:
            self.assertTrue(name.endswith(".json.gz"))
            self.assertEqual(content_type, "application/gzip")
            self.assertLessEqual(len(part), limit)
            loaded.extend(json.loads(gzip.decompress(part)))
        self.assertEqual(loaded, items)

    async def test_returns_none_when_a_non_array_cannot_fit(self):
        payload = gzip.compress(json.dumps({"blob": "x" * 200}).encode())
        body = UnparsedBody(payload, compressed=True, uncompressed_size=len(payload) + 50)
        client = _fake_client()
        with (
            patch("app.analysis.AsyncOpenAI", return_value=client),
            patch("app.analysis.MAX_CONTAINER_FILE_BYTES", 32),
        ):
            self.assertIsNone(await store_unparsed(body))
        client.containers.create.assert_not_awaited()


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
        self.assertIn("Decide how to read", prompt)
        self.assertIn(".json.gz", ANALYST_INSTRUCTIONS)
        self.assertIn("fleet-parts-manifest.json", ANALYST_INSTRUCTIONS)
        self.assertIn("You decide how to read", ANALYST_INSTRUCTIONS)
        (tool,) = analyst.tools
        self.assertIsInstance(tool, CodeInterpreterTool)
        self.assertEqual(tool.tool_config, {"type": "code_interpreter", "container": "cntr_1"})

    async def test_failure_returns_a_recoverable_message(self):
        answer = await self._call(AsyncMock(side_effect=RuntimeError("expired")))
        self.assertIn("analysis failed", answer)
        self.assertIn("Narrow the query", answer)
        self.assertNotIn("client-side", answer)


class FileCanBeAnalyzedTests(unittest.IsolatedAsyncioTestCase):
    async def test_asks_from_the_description_without_opening_the_file(self):
        ctx = RunContextWrapper(context=SimpleNamespace())
        args = json.dumps(
            {
                "container_id": "cntr_1",
                "path": "/mnt/data/data.json.gz",
                "file_bytes": 16665642,
                "uncompressed_bytes": 222290669,
                "contents": "JSON array of positions. Fields: deviceId, latitude. Sample: [{\"deviceId\": 761}]",
            }
        )
        run = AsyncMock(return_value=SimpleNamespace(final_output="not analyzable\nThe JSON is too large to load."))
        with patch("app.analysis.Runner.run", new=run):
            answer = await file_can_be_analyzed.on_invoke_tool(ctx, args)
        self.assertIn("not analyzable", answer)
        checker, prompt = run.await_args.args
        self.assertEqual(checker.tools, [])
        self.assertIn("whether it is analyzable", FILE_CHECK_INSTRUCTIONS)
        self.assertIn("/mnt/data/data.json.gz", prompt)
        self.assertIn("222290669", prompt)
        self.assertIn("deviceId", prompt)
        self.assertIn("Can this file be analyzed?", prompt)
        self.assertNotIn("json.load", prompt)


class SummaryWithStoredDataTests(unittest.TestCase):
    def test_includes_handle_and_analyze_hint_when_stored(self):
        stored = {"container_id": "cntr_1", "path": "/mnt/data/data.json"}
        summary = _summarize_large_result([{"a": 1}], 10, stored)
        self.assertEqual(summary["stored_data"], stored)
        self.assertIn("You decide the next step", summary["next_step"])
        self.assertIn("file_can_be_analyzed", summary["next_step"])
        self.assertIn("analyze_data", summary["next_step"])
        self.assertIn("one request per device", summary["next_step"])

    def test_no_handle_or_hint_when_upload_failed(self):
        summary = _summarize_large_result([{"a": 1}], 10, None)
        self.assertNotIn("stored_data", summary)
        self.assertNotIn("analyze_data", summary["next_step"])


if __name__ == "__main__":
    unittest.main()
