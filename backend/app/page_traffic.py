"""Tracks a rendered page's in-flight /api/ requests so a screenshot can wait for its data."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse


class ApiTraffic:
    """Counts requests to /api/ made by a page; hook the methods to Playwright's request events."""

    def __init__(self) -> None:
        self.inflight = 0
        self.last_activity = time.monotonic()

    @staticmethod
    def _is_api(request) -> bool:
        return urlparse(request.url).path.startswith("/api/")

    def started(self, request) -> None:
        if self._is_api(request):
            self.inflight += 1
            self.last_activity = time.monotonic()

    def finished(self, request) -> None:
        if self._is_api(request):
            self.inflight = max(0, self.inflight - 1)
            self.last_activity = time.monotonic()


async def wait_until_quiet(traffic: ApiTraffic, quiet: float = 0.8, timeout: float = 15.0) -> bool:
    """Wait until no /api/ request is in flight and none started or ended for `quiet` seconds.

    The quiet period bridges the gap between one fetch finishing and the next one starting
    (for example devices, then one route request per device). Returns False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if traffic.inflight == 0 and time.monotonic() - traffic.last_activity >= quiet:
            return True
        await asyncio.sleep(0.05)
    return False
