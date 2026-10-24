"""Map controls must receive clicks that would otherwise hit the canvas.

The Mapbox script is stubbed so the test checks stacking, not tiles.
"""

import unittest
from pathlib import Path

from playwright.async_api import async_playwright

TEMPLATE = (Path(__file__).parents[1] / "app" / "map_template.html").read_text()

STUB = """
window.mapboxgl = {
  accessToken: '',
  Map: class {
    constructor(opts) {
      const node = document.getElementById(opts.container);
      node.classList.add('mapboxgl-map');
      const canvas = document.createElement('canvas');
      canvas.id = 'stub-canvas';
      canvas.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%';
      node.appendChild(canvas);
    }
    addControl() {}
    addSource() {}
    addLayer() {}
    fitBounds() {}
    on(event, cb) {
      if (event === 'load' || event === 'idle') queueMicrotask(() => cb());
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


async def _open(page):
    await page.route(
        "**/mapbox-gl.js",
        lambda route: route.fulfill(content_type="application/javascript", body=STUB),
    )
    await page.route(
        "**/mapbox-gl.css",
        lambda route: route.fulfill(content_type="text/css", body=""),
    )
    await page.set_content(TEMPLATE, wait_until="load")
    await page.wait_for_function("() => typeof addControl === 'function'")


async def _hit_target(page, selector):
    box = await page.locator(selector).bounding_box()
    return await page.evaluate(
        """([x, y]) => document.elementFromPoint(x, y).id""",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )


class MapControlHitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()
        self._page = await self._browser.new_page(viewport={"width": 800, "height": 600})
        await _open(self._page)

    async def asyncTearDown(self):
        await self._browser.close()
        await self._playwright.stop()

    async def test_add_control_button_receives_the_click(self):
        await self._page.evaluate(
            """() => {
              window.__clicks = 0;
              const button = document.createElement('button');
              button.id = 'replay';
              button.textContent = 'Replay';
              button.addEventListener('click', () => { window.__clicks += 1; });
              addControl(button);
            }"""
        )
        self.assertEqual(await _hit_target(self._page, "#replay"), "replay")
        box = await self._page.locator("#replay").bounding_box()
        await self._page.mouse.click(box["x"] + 8, box["y"] + 8)
        self.assertEqual(await self._page.evaluate("window.__clicks"), 1)
        # The rest of the overlay must not block panning.
        self.assertEqual(await _hit_target(self._page, "#stub-canvas"), "stub-canvas")

    async def test_button_placed_before_the_map_stays_clickable(self):
        await self._page.evaluate(
            """() => {
              const button = document.createElement('button');
              button.id = 'early';
              button.textContent = 'Early';
              button.style.cssText = 'position:absolute;top:80px;left:16px';
              document.body.insertBefore(button, document.getElementById('map'));
            }"""
        )
        self.assertEqual(await _hit_target(self._page, "#early"), "early")

    async def test_button_inside_the_map_stays_above_the_canvas(self):
        await self._page.evaluate(
            """() => {
              const button = document.createElement('button');
              button.id = 'inside';
              button.textContent = 'Inside';
              button.style.cssText = 'position:absolute;top:80px;left:16px';
              document.getElementById('map').appendChild(button);
            }"""
        )
        self.assertEqual(await _hit_target(self._page, "#inside"), "inside")


if __name__ == "__main__":
    unittest.main()
