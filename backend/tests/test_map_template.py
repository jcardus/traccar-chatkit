"""Guards for the map template that the assistant is told to start from.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import re
import shutil
import unittest
from pathlib import Path

from app.chat import _validate_js_syntax

TEMPLATE = (Path(__file__).parent.parent / "app" / "map_template.html").read_text()


class MapTemplateTests(unittest.TestCase):
    def test_keeps_what_the_instructions_rely_on(self):
        for needle in (
            "async function getData()",
            "window.__SCREENSHOT_READY__",
            "type: 'html-error'",
            "window.onerror",
            "unhandledrejection",
            "map.on('error'",
            "mapboxgl.accessToken",
            "setDOMContent",
        ):
            self.assertIn(needle, TEMPLATE)

    def test_does_not_set_a_map_style(self):
        self.assertNotRegex(TEMPLATE, r"new mapboxgl\.Map\(\{[^}]*\bstyle\s*:")

    def test_popups_never_inject_html_strings(self):
        self.assertNotIn("setHTML", TEMPLATE)
        self.assertNotIn("innerHTML", TEMPLATE)

    def test_pins_one_mapbox_version_for_script_and_css(self):
        versions = set(re.findall(r"mapbox-gl-js/(v[\d.]+)/", TEMPLATE))
        self.assertEqual(len(versions), 1)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_script_has_valid_syntax(self):
        self.assertIsNone(_validate_js_syntax(TEMPLATE))


if __name__ == "__main__":
    unittest.main()
