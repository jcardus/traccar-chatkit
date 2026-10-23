"""Maintained tests for server.py. Stdlib only: `python3 test_server.py`."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server


class ParseTests(unittest.TestCase):
    def test_envelope_with_answer_json(self):
        inner = {
            "answer": "Restart the device.",
            "needs_human": False,
            "confidence": "high",
        }
        stdout = json.dumps({"result": json.dumps(inner)})
        self.assertEqual(
            server.parse_agent_output(stdout), ("Restart the device.", False, "high")
        )

    def test_envelope_with_plain_result(self):
        stdout = json.dumps({"result": "Check the ignition.", "usage": {}})
        answer, needs_human, confidence = server.parse_agent_output(stdout)
        self.assertEqual(answer, "Check the ignition.")
        self.assertFalse(needs_human)
        self.assertEqual(confidence, "low")

    def test_envelope_with_needs_human(self):
        inner = {"needs_human": True, "confidence": "high"}
        stdout = json.dumps({"result": json.dumps(inner)})
        self.assertEqual(server.parse_agent_output(stdout), (None, True, "high"))

    def test_bare_answer_object(self):
        stdout = json.dumps({"answer": "Update the app.", "confidence": "medium"})
        self.assertEqual(
            server.parse_agent_output(stdout), ("Update the app.", False, "medium")
        )

    def test_plain_prose_is_low_confidence_answer(self):
        self.assertEqual(
            server.parse_agent_output("Just reboot it."),
            ("Just reboot it.", False, "low"),
        )

    def test_fenced_json(self):
        stdout = '```json\n{"answer": "Hi.", "confidence": "high"}\n```'
        self.assertEqual(server.parse_agent_output(stdout), ("Hi.", False, "high"))

    def test_empty_output(self):
        self.assertEqual(server.parse_agent_output(""), (None, False, ""))
        self.assertEqual(server.parse_agent_output(None), (None, False, ""))

    def test_truncation(self):
        stdout = json.dumps({"result": "x" * 100})
        answer, _, _ = server.parse_agent_output(stdout, max_answer=10)
        self.assertEqual(answer, "x" * 10)


class PromptTests(unittest.TestCase):
    def test_prompt_contents(self):
        prompt = server.build_prompt(
            "Why?", "User: hi", "a@b.c", "/solver", "/repos", 100
        )
        self.assertIn("LIVE-ANSWER MODE", prompt)
        self.assertIn("Why?", prompt)
        self.assertIn("/solver", prompt)
        self.assertIn("/repos", prompt)
        self.assertIn("bin/fs_note.sh", prompt)  # named only as forbidden
        self.assertIn("Traccar", prompt)  # named only as forbidden


class AuthTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(server.is_authorized("Bearer secret", "secret"))

    def test_wrong_missing_or_empty(self):
        self.assertFalse(server.is_authorized("Bearer wrong", "secret"))
        self.assertFalse(server.is_authorized(None, "secret"))
        self.assertFalse(server.is_authorized("Bearer secret", ""))


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = server.load_config({})
        self.assertEqual((cfg.bind, cfg.port, cfg.api_key), ("127.0.0.1", 8765, ""))
        self.assertEqual(cfg.claude_cmd, ["claude"])
        self.assertIn("--max-turns", cfg.claude_args)

    def test_invalid_numbers_fall_back(self):
        cfg = server.load_config({"AGENT_PORT": "abc", "CLAUDE_TIMEOUT": "-5"})
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.claude_timeout, 100.0)

    def test_log_level(self):
        self.assertEqual(server.load_config({}).log_level, "INFO")
        self.assertEqual(
            server.load_config({"AGENT_LOG_LEVEL": "debug"}).log_level, "DEBUG"
        )
        self.assertEqual(
            server.load_config({"AGENT_LOG_LEVEL": "verbose"}).log_level, "INFO"
        )


if __name__ == "__main__":
    unittest.main()
