from __future__ import annotations

import unittest

from pullwise_server.update_filter import extract_release_units


class UpdateFilterContractsTest(unittest.TestCase):
    def test_long_release_selects_at_most_eight_complete_units_and_reports_partial_coverage(self) -> None:
        body = "\n\n".join(f"## Change {index}\nOAuth change {index}." for index in range(10))

        result = extract_release_units(
            body,
            interests=["OAuth"],
            max_units=8,
            state_budget_bytes=24 * 1024,
        )

        self.assertEqual(result["coverage"]["state"], "partial")
        self.assertEqual(result["coverage"]["totalUnits"], 10)
        self.assertEqual(result["coverage"]["selectedUnits"], 8)
        self.assertEqual(result["coverage"]["omittedUnits"], 2)
        self.assertEqual(len(result["units"]), 8)
        self.assertTrue(all(unit["text"].endswith(".") for unit in result["units"]))
        self.assertNotIn("relevance", result)
        self.assertTrue(all("actionTypes" not in unit for unit in result["units"]))

    def test_oversized_single_unit_is_not_truncated_and_requires_manual_review(self) -> None:
        text = "## Migration\n" + ("A" * 200)

        result = extract_release_units(
            text,
            interests=["migration"],
            max_units=8,
            state_budget_bytes=100,
        )

        self.assertEqual(result["status"], "needs_manual")
        self.assertEqual(result["units"], [])
        self.assertEqual(result["coverage"]["state"], "unavailable")
        self.assertEqual(result["coverage"]["omittedUnits"], 1)

    def test_multi_topic_unit_stays_whole_and_selection_is_deterministic(self) -> None:
        body = (
            "## Authentication and database\n"
            "OAuth token handling changed, while database connector migration is required.\n\n"
            "## Other\nMinor documentation cleanup."
        )

        first = extract_release_units(body, interests=["OAuth"], max_units=1, state_budget_bytes=2048)
        second = extract_release_units(body, interests=["OAuth"], max_units=1, state_budget_bytes=2048)

        self.assertEqual(first, second)
        self.assertIn("database connector migration", first["units"][0]["text"])
        self.assertEqual(first["units"][0]["changeUnitId"], "cu_0")

    def test_empty_release_has_zero_units_without_fabricating_not_relevant(self) -> None:
        result = extract_release_units("   \n", interests=["OAuth"])

        self.assertEqual(result["units"], [])
        self.assertEqual(result["status"], "needs_manual")
        self.assertEqual(result["coverage"]["totalUnits"], 0)
        self.assertNotIn("relevance", result)


if __name__ == "__main__":
    unittest.main()
