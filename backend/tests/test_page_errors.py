"""Page failures captured during a screenshot must be handed back to the model."""

import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from app.chat import (
    attach_page_failure_listeners,
    note_page_failure,
    record_dom_page_errors,
    screenshot_review_text,
)

TEMPLATE = (Path(__file__).parents[1] / "app" / "map_template.html").read_text()

# Style never loads, then the map goes idle. That idle used to count as ready
# and the blank screenshot was sent back as a success.
FAILED_MAP_STUB = """
window.mapboxgl = {
  accessToken: '',
  Map: class {
    constructor() {}
    addControl() {}
    addSource() {}
    addLayer() {}
    fitBounds() {}
    isStyleLoaded() { return false; }
    on(event, cb) {
      if (event === 'error') queueMicrotask(() => cb({ error: { message: 'Unauthorized' } }));
      if (event === 'idle') queueMicrotask(() => cb());
    }
  },
  NavigationControl: class {},
  Popup: class {
    setLngLat() { return this; }
    setHTML() { return this; }
    addTo() { return this; }
  },
  LngLatBounds: class {
    constructor() {}
    extend() { return this; }
  },
};
"""

# An error before the style loads, then a real load. The map must end ready.
RECOVERED_MAP_STUB = FAILED_MAP_STUB.replace(
    """    on(event, cb) {
      if (event === 'error') queueMicrotask(() => cb({ error: { message: 'Unauthorized' } }));
      if (event === 'idle') queueMicrotask(() => cb());
    }""",
    """    on(event, cb) {
      if (event === 'click') return;
      if (event === 'error') queueMicrotask(() => cb({ error: { message: 'tile blip' } }));
      if (event === 'load') queueMicrotask(() => cb());
      if (event === 'idle') queueMicrotask(() => cb());
    }""",
)


class PageFailureTests(unittest.TestCase):
    def test_redacts_the_token_and_keeps_one_copy(self):
        notes: list[str] = []
        message = (
            "HTTP 401 https://api.mapbox.com/styles/v1/mapbox/streets-v12"
            "?access_token=pk.eyJ1IjoiamNhcmRlaXJhIn0.signature"
        )
        note_page_failure(notes, message)
        note_page_failure(notes, message)
        self.assertEqual(len(notes), 1)
        self.assertNotIn("pk.eyJ", notes[0])
        self.assertIn("access_token=...", notes[0])
        self.assertIn("HTTP 401", notes[0])

    def test_errors_require_another_render(self):
        text = screenshot_review_text("- HTTP 401 https://api.mapbox.com/styles/v1/mapbox/streets-v12")
        self.assertIn("call show_html again", text)
        self.assertIn("HTTP 401", text)
        self.assertNotIn("brief confirmation", text)

    def test_a_clean_page_still_asks_for_a_look(self):
        text = screenshot_review_text("")
        self.assertIn("brief confirmation", text)


class CapturedPageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()

    async def asyncTearDown(self):
        await self._browser.close()
        await self._playwright.stop()

    async def test_rejected_map_is_not_ready_and_asks_for_a_retry(self):
        page = await self._browser.new_page()
        failures: list[str] = []
        attach_page_failure_listeners(page, failures)
        await page.route(
            "**/mapbox-gl.js",
            lambda route: route.fulfill(content_type="application/javascript", body=FAILED_MAP_STUB),
        )
        await page.route(
            "**/mapbox-gl.css",
            lambda route: route.fulfill(content_type="text/css", body=""),
        )
        await page.set_content(TEMPLATE, wait_until="load")
        await page.wait_for_function("() => window.__PAGE_FAILED__ === true")
        ready = await page.evaluate("() => window.__SCREENSHOT_READY__ === true")
        self.assertFalse(ready)
        await record_dom_page_errors(page, failures)
        text = screenshot_review_text("\n".join(f"- {item}" for item in failures))
        self.assertIn("Unauthorized", text)
        self.assertIn("call show_html again", text)
        self.assertNotIn("brief confirmation", text)

    async def test_an_error_before_style_load_still_counts_as_ready(self):
        page = await self._browser.new_page()
        await page.route(
            "**/mapbox-gl.js",
            lambda route: route.fulfill(
                content_type="application/javascript", body=RECOVERED_MAP_STUB
            ),
        )
        await page.route(
            "**/mapbox-gl.css",
            lambda route: route.fulfill(content_type="text/css", body=""),
        )
        await page.set_content(TEMPLATE, wait_until="load")
        await page.wait_for_function("() => window.__SCREENSHOT_READY__ === true")
        failed = await page.evaluate("() => window.__PAGE_FAILED__ === true")
        self.assertFalse(failed)

    async def test_console_and_http_401_ask_for_a_retry_without_the_template(self):
        # A page the model retyped does not have the template's map error hook.
        # The style 401 and its console line are still enough to reject it.
        page = await self._browser.new_page()
        failures: list[str] = []
        attach_page_failure_listeners(page, failures)

        async def unauthorized(route):
            await route.fulfill(status=401, body="unauthorized")

        await page.route("**/streets-v12**", unauthorized)
        await page.set_content("<!DOCTYPE html><html><body></body></html>", wait_until="load")
        await page.evaluate(
            """async () => {
                console.error("Failed to load style access_token=pk.eyJ1abc.sig");
                await fetch("https://api.mapbox.com/styles/v1/mapbox/streets-v12?access_token=pk.eyJ1abc.sig");
            }"""
        )
        text = screenshot_review_text("\n".join(f"- {item}" for item in failures))
        self.assertIn("HTTP 401", text)
        self.assertIn("https://api.mapbox.com/styles/v1/mapbox/streets-v12", text)
        self.assertIn("call show_html again", text)
        self.assertNotIn("pk.eyJ", text)
        self.assertNotIn("brief confirmation", text)


if __name__ == "__main__":
    unittest.main()
