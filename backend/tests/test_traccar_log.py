"""Traccar calls are written to the INFO log, without cookies or passwords.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from app.traccar import invoke, log_traccar_request


def _response(status_code=200, payload=None, history=()):
    payload = {"ok": True} if payload is None else payload
    raw = json.dumps(payload).encode()
    response = MagicMock()
    response.status_code = status_code
    response.history = list(history)
    response.encoding = "utf-8"
    response.iter_content = lambda chunk_size=65536: iter([raw])
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status_code))
    return response


class TraccarRequestLogTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(headers={"cookie": "JSESSIONID=secret-session.node0"})

    def test_successful_get_is_logged_at_info_without_the_cookie(self):
        with (
            patch("app.traccar.requests.request", return_value=_response()) as outbound,
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            result = invoke("get", "/devices", "", self.request)

        self.assertEqual(result, {"ok": True})
        outbound.assert_called_once()
        self.assertEqual(outbound.call_args.kwargs["headers"]["Cookie"], "JSESSIONID=secret-session.node0")
        line = logs.output[0]
        self.assertIn("INFO", line)
        self.assertIn("GET http://gps.frotaweb.com/api/devices -> 200", line)
        self.assertNotIn("body=", line)
        self.assertNotIn("secret-session", line)
        self.assertNotIn("JSESSIONID", line)

    def test_post_body_is_logged_and_password_is_redacted(self):
        with (
            patch("app.traccar.requests.request", return_value=_response()),
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            invoke(
                "post",
                "/session",
                '{"email":"a@b.c","password":"super-secret"}',
                self.request,
            )

        line = logs.output[0]
        self.assertIn("POST http://gps.frotaweb.com/api/session", line)
        self.assertIn('"email": "a@b.c"', line)
        self.assertIn('"password": "***"', line)
        self.assertNotIn("super-secret", line)

    def test_failed_call_is_still_logged(self):
        with (
            patch("app.traccar.requests.request", side_effect=requests.Timeout("hung")),
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            with self.assertRaises(requests.Timeout):
                invoke("get", "/reports/route", "", self.request)

        self.assertIn("-> no response", logs.output[0])

    def test_slow_call_is_a_warning(self):
        def clock():
            clock.calls += 1
            return 10.0 if clock.calls == 1 else 16.2

        clock.calls = 0
        with (
            patch("app.traccar.time.monotonic", side_effect=clock),
            patch("app.traccar.requests.request", return_value=_response()),
            self.assertLogs("app.traccar", level="INFO") as logs,
        ):
            invoke("get", "/positions", "", self.request)

        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertIn("took 6.20s", logs.output[0])

    def test_proxy_body_bytes_are_clipped_and_redacted(self):
        body = ('{"password":"super-secret","note":"' + ("x" * 400) + '"}').encode()
        with self.assertLogs("app.traccar", level="INFO") as logs:
            log_traccar_request("PUT", "http://gps.frotaweb.com/api/devices/1", 200, 0.2, body=body)

        line = logs.output[0]
        self.assertIn("PUT http://gps.frotaweb.com/api/devices/1", line)
        self.assertIn('"password": "***"', line)
        self.assertNotIn("super-secret", line)
        self.assertLess(len(line), 500)
        self.assertIn("…", line)
