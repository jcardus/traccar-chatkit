"""Tests for the oversized invoke_api result summary.

Stdlib unittest; run from backend/ with project deps installed:
`uv run python -m unittest discover -s tests`
"""

import json
import unittest

from app.chat import SAMPLE_MAX_BYTES, SAMPLE_MAX_ITEMS, _summarize_large_result


class SummarizeLargeResultTests(unittest.TestCase):
    def test_list_reports_count_fields_and_sample(self):
        result = [{"id": i, "latitude": 1.0, "longitude": 2.0} for i in range(1000)]
        summary = _summarize_large_result(result, 999999)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["total_items"], 1000)
        self.assertEqual(summary["fields"], ["id", "latitude", "longitude"])
        self.assertEqual(summary["sample"], result[:SAMPLE_MAX_ITEMS])
        self.assertIn("999999", summary["reason"])
        self.assertIn("shorter", summary["next_step"])
        self.assertIn("client-side", summary["next_step"])
        self.assertIn("Do not retry", summary["next_step"])
        self.assertIn("one request per device", summary["next_step"])
        self.assertIn("too large to store", summary["next_step"])

    def test_fields_are_the_union_across_items(self):
        result = [{"a": 1}, {"b": 2}, "not-a-dict"]
        summary = _summarize_large_result(result, 1)
        self.assertEqual(summary["fields"], ["a", "b"])

    def test_sample_is_trimmed_to_byte_budget(self):
        big = {"blob": "x" * (SAMPLE_MAX_BYTES // 2)}
        summary = _summarize_large_result([big, big, big], 1)
        self.assertLessEqual(len(json.dumps(summary["sample"])), SAMPLE_MAX_BYTES)
        self.assertEqual(len(summary["sample"]), 1)

    def test_single_oversized_item_is_dropped_from_sample(self):
        huge = {"blob": "x" * (SAMPLE_MAX_BYTES * 2)}
        summary = _summarize_large_result([huge], 1)
        self.assertEqual(summary["sample"], [])
        self.assertEqual(summary["fields"], ["blob"])

    def test_dict_describes_top_level_fields(self):
        summary = _summarize_large_result({"events": [1, 2, 3], "name": "x"}, 1)
        self.assertEqual(summary["fields"], {"events": "list of 3", "name": "str"})
        self.assertNotIn("sample", summary)

    def test_summary_itself_is_small(self):
        result = [{"id": i, "attributes": {"k": "v" * 50}} for i in range(5000)]
        summary = _summarize_large_result(result, 10**7)
        self.assertLess(len(json.dumps(summary)), 6000)


if __name__ == "__main__":
    unittest.main()
