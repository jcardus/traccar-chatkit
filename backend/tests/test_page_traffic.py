"""Tests for waiting on a page's /api/ requests before taking a screenshot.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import asyncio
import unittest
from types import SimpleNamespace

from app.page_traffic import ApiTraffic, wait_until_quiet


def _req(path, host="https://abc.rastreon.net"):
    return SimpleNamespace(url=f"{host}{path}")


class ApiTrafficTests(unittest.TestCase):
    def test_counts_only_api_requests(self):
        t = ApiTraffic()
        t.started(_req("/api/positions?deviceId=1"))
        t.started(_req("/styles/v1/mapbox/standard", host="https://api.mapbox.com"))
        t.started(_req("/chatkit/report.html"))
        self.assertEqual(t.inflight, 1)

    def test_finished_decrements_and_never_goes_negative(self):
        t = ApiTraffic()
        t.started(_req("/api/devices"))
        t.finished(_req("/api/devices"))
        t.finished(_req("/api/devices"))
        self.assertEqual(t.inflight, 0)

    def test_failed_non_api_request_is_ignored(self):
        t = ApiTraffic()
        t.finished(_req("/tiles/1/2/3.png"))
        self.assertEqual(t.inflight, 0)


class WaitUntilQuietTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_true_when_nothing_is_pending(self):
        self.assertTrue(await wait_until_quiet(ApiTraffic(), quiet=0.05, timeout=1))

    async def test_waits_for_inflight_request_to_finish(self):
        t = ApiTraffic()
        req = _req("/api/reports/route")
        t.started(req)
        asyncio.get_running_loop().call_later(0.2, t.finished, req)
        loop = asyncio.get_running_loop()
        begin = loop.time()
        self.assertTrue(await wait_until_quiet(t, quiet=0.05, timeout=2))
        self.assertGreaterEqual(loop.time() - begin, 0.2)

    async def test_quiet_period_bridges_back_to_back_requests(self):
        t = ApiTraffic()
        first, second = _req("/api/devices"), _req("/api/reports/route")
        t.started(first)
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, t.finished, first)
        loop.call_later(0.2, t.started, second)
        loop.call_later(0.4, t.finished, second)
        begin = loop.time()
        self.assertTrue(await wait_until_quiet(t, quiet=0.3, timeout=3))
        self.assertGreaterEqual(loop.time() - begin, 0.4)

    async def test_times_out_when_a_request_never_finishes(self):
        t = ApiTraffic()
        t.started(_req("/api/reports/route"))
        self.assertFalse(await wait_until_quiet(t, quiet=0.05, timeout=0.3))


if __name__ == "__main__":
    unittest.main()
