"""show_html must keep working for the current panel and the deployed Checaruta bundle."""

import json
import unittest

from chatkit.types import ImageAttachment

from app.attachments import attachment_path
from app.chat import show_html_client_arguments

SCREENSHOT_URL = "https://abc123.rastreon.net/chatkit/20261024_120000_joaquim_fleetmap.io.png"
HTML_URL = SCREENSHOT_URL.replace(".png", ".html")


class ShowHtmlClientArgumentsTests(unittest.TestCase):
    def test_keeps_screenshot_url_for_the_current_panel(self):
        args = show_html_client_arguments("<p>hi</p>", HTML_URL, SCREENSHOT_URL)
        self.assertEqual(args["html"], "<p>hi</p>")
        self.assertEqual(args["html_url"], HTML_URL)
        self.assertEqual(args["screenshot_url"], SCREENSHOT_URL)

    def test_attachment_matches_the_checaruta_bundle(self):
        args = show_html_client_arguments("<p>hi</p>", HTML_URL, SCREENSHOT_URL)
        payload = json.loads(args["attachment"])
        self.assertEqual(
            payload,
            {
                "id": "20261024_120000_joaquim_fleetmap.io",
                "type": "image",
                "name": "screenshot.png",
                "mime_type": "image/png",
                "preview_url": SCREENSHOT_URL,
            },
        )
        ImageAttachment.model_validate(payload)
        self.assertEqual(
            attachment_path(payload["id"], payload["mime_type"]).name,
            "20261024_120000_joaquim_fleetmap.io.png",
        )


if __name__ == "__main__":
    unittest.main()
