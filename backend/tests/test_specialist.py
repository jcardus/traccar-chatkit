"""Maintained tests for the specialist-agent client.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents import RunContextWrapper

from app.support import (
    _agent_config,
    _parse_agent_response,
    ask_support_specialist,
    forward_to_real_agent,
)


class ParseTests(unittest.TestCase):
    def test_contract_shape(self):
        payload = {"answer": "Restart it.", "needs_human": False, "confidence": "High"}
        self.assertEqual(_parse_agent_response(payload, ""), ("Restart it.", False, "high"))

    def test_needs_human_without_answer(self):
        payload = {"needs_human": True, "confidence": "high"}
        self.assertEqual(_parse_agent_response(payload, ""), (None, True, "high"))

    def test_needs_human_must_be_boolean_true(self):
        payload = {"answer": "Maybe.", "needs_human": "yes"}
        answer, needs_human, _ = _parse_agent_response(payload, "")
        self.assertEqual(answer, "Maybe.")
        self.assertFalse(needs_human)

    def test_alternate_answer_field(self):
        self.assertEqual(_parse_agent_response({"response": "Hi."}, "")[0], "Hi.")

    def test_plain_text_fallback(self):
        self.assertEqual(_parse_agent_response(None, "Just text."), ("Just text.", False, ""))

    def test_empty(self):
        self.assertEqual(_parse_agent_response(None, ""), (None, False, ""))
        self.assertEqual(_parse_agent_response({}, "  "), (None, False, ""))


class ConfigTests(unittest.TestCase):
    def test_values_from_env(self):
        env = {
            "CLAUDE_AGENT_URL": "https://bot.example/ask",
            "CLAUDE_AGENT_TIMEOUT": "30",
            "CLAUDE_AGENT_API_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(_agent_config(), ("https://bot.example/ask", 30.0, "secret"))

    def test_defaults_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            url, timeout, api_key = _agent_config()
        self.assertIsNone(url)
        self.assertEqual(timeout, 120.0)
        self.assertIsNone(api_key)

    def test_invalid_timeout_falls_back(self):
        with patch.dict(os.environ, {"CLAUDE_AGENT_TIMEOUT": "soon"}):
            self.assertEqual(_agent_config()[1], 120.0)


def _tool_context():
    return RunContextWrapper(
        context=SimpleNamespace(
            request_context={},
            thread=SimpleNamespace(id="thread-1"),
            store=SimpleNamespace(),
        )
    )


class ToolChainTests(unittest.IsolatedAsyncioTestCase):
    """Specialist first, forward_to_real_agent when it can't answer."""

    async def _invoke(self, tool, **patches):
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.support.invoke", return_value={"email": "user@example.com"})
            )
            stack.enter_context(
                patch(
                    "app.support._format_support_thread",
                    new=AsyncMock(return_value="[t] User:\nhi"),
                )
            )
            for target, new in patches.items():
                stack.enter_context(patch(target, new=new))
            return await tool.on_invoke_tool(_tool_context(), json.dumps({"question": "hi"}))

    async def test_specialist_answer_is_relayed(self):
        result = await self._invoke(
            ask_support_specialist,
            **{
                "app.support._ask_support_specialist": AsyncMock(
                    return_value=("Do X.", False, "high")
                )
            },
        )
        self.assertIn("Do X.", result)
        self.assertIn("own words", result)
        self.assertNotIn("forward_to_real_agent", result)

    async def test_specialist_failure_escalates(self):
        result = await self._invoke(
            ask_support_specialist,
            **{"app.support._ask_support_specialist": AsyncMock(return_value=(None, False, ""))},
        )
        self.assertIn("forward_to_real_agent", result)

    async def test_specialist_needs_human_escalates(self):
        result = await self._invoke(
            ask_support_specialist,
            **{"app.support._ask_support_specialist": AsyncMock(return_value=(None, True, "high"))},
        )
        self.assertIn("forward_to_real_agent", result)

    async def test_forward_sends_email(self):
        ses = MagicMock()
        ses.send_email.return_value = {
            "MessageId": "mid",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
        boto3 = MagicMock()
        boto3.client.return_value = ses
        result = await self._invoke(forward_to_real_agent, **{"app.support.boto3": boto3})
        self.assertIn("forwarded to our support team", result)
        ses.send_email.assert_called_once()
        _, kwargs = ses.send_email.call_args
        self.assertEqual(kwargs["Destination"], {"ToAddresses": ["support@fleetmap.io"]})
        self.assertIn("thread-1", kwargs["Message"]["Body"]["Text"]["Data"])

    async def test_forward_failure_gives_contact(self):
        boto3 = MagicMock()
        boto3.client.side_effect = RuntimeError("no network")
        with patch.dict(os.environ, {}, clear=True):
            result = await self._invoke(forward_to_real_agent, **{"app.support.boto3": boto3})
        self.assertIn("support@fleetmap.io", result)


if __name__ == "__main__":
    unittest.main()
