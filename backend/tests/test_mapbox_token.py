"""The model must not have to copy the Mapbox token. Run from backend/."""

import unittest

from app.chat import restore_mapbox_token
from app.constants import MAPBOX_ACCESS_TOKEN, MAPBOX_TOKEN_PLACEHOLDER


class RestoreMapboxTokenTests(unittest.TestCase):
    def test_placeholder_is_replaced(self):
        html = f"mapboxgl.accessToken = '{MAPBOX_TOKEN_PLACEHOLDER}';"
        self.assertIn(MAPBOX_ACCESS_TOKEN, restore_mapbox_token(html))
        self.assertNotIn(MAPBOX_TOKEN_PLACEHOLDER, restore_mapbox_token(html))

    def test_truncated_token_is_replaced(self):
        truncated = MAPBOX_ACCESS_TOKEN[:-1]
        html = f'mapboxgl.accessToken = "{truncated}";'
        restored = restore_mapbox_token(html)
        self.assertIn(f'"{MAPBOX_ACCESS_TOKEN}"', restored)
        self.assertNotIn(truncated + '"', restored)

    def test_page_without_a_map_is_unchanged(self):
        html = "<p>No map here.</p>"
        self.assertEqual(restore_mapbox_token(html), html)


if __name__ == "__main__":
    unittest.main()
