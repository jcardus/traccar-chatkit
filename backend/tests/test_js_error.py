"""Tests for compacting node's syntax-error output.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import unittest

from app.chat import SYNTAX_SNIPPET_RADIUS, _compact_node_syntax_error

STACK = """    at new Function (<anonymous>)
    at [eval]:1:1
    at Script.runInThisContext (node:vm:122:12)

Node.js v18.19.1"""


def _node_error(source, column, message="SyntaxError: Unexpected identifier"):
    return f"undefined:5\n{source}\n{' ' * column}^^^^^^\n{message}\n{STACK}"


class CompactSyntaxErrorTests(unittest.TestCase):
    def test_keeps_message_and_window_around_the_caret(self):
        source = "a" * 500 + "BROKEN" + "b" * 500
        out = _compact_node_syntax_error(_node_error(source, 500))
        self.assertTrue(out.startswith("SyntaxError: Unexpected identifier near: ..."))
        self.assertIn("BROKEN", out)
        self.assertLess(len(out), 2 * SYNTAX_SNIPPET_RADIUS + 80)

    def test_drops_stack_trace_and_node_banner(self):
        out = _compact_node_syntax_error(_node_error("const x = ;", 10))
        self.assertNotIn("at new Function", out)
        self.assertNotIn("Node.js v", out)
        self.assertNotIn("\n", out)

    def test_short_source_is_kept_whole(self):
        out = _compact_node_syntax_error(_node_error("const x = ;", 10))
        self.assertIn("const x = ;", out)

    def test_unparseable_output_falls_back_to_truncated_stderr(self):
        out = _compact_node_syntax_error("something unexpected\n" * 100)
        self.assertEqual(len(out), 300)

    def test_blank_caret_line_gives_message_and_script_line(self):
        stderr = f"<anonymous_script>:3\n{'x' * 2000}\n{' ' * 1000}\n\nSyntaxError: Invalid or unexpected token\n{STACK}"
        self.assertEqual(
            _compact_node_syntax_error(stderr),
            "SyntaxError: Invalid or unexpected token (script line 1)",
        )

    def test_message_without_line_header(self):
        stderr = "oops\nSyntaxError: Unexpected token\n" + STACK
        self.assertEqual(_compact_node_syntax_error(stderr), "SyntaxError: Unexpected token")

    def test_reported_error_for_the_real_failure(self):
        source = (
            "const vals=[t.deviceName||'Veículo sem nome,stamp(t.startTime),"
            "`${t.startAddress||'Origem'}`,dur(t.duration)];"
        )
        column = source.index("stamp") - 2
        out = _compact_node_syntax_error(_node_error(source, column))
        self.assertIn("Unexpected identifier", out)
        self.assertIn("sem nome,stamp", out)


if __name__ == "__main__":
    unittest.main()
