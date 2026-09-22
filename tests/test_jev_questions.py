from __future__ import annotations

import unittest

from pullwise_server.jev_questions import (
    CI_QUESTION_VERSION,
    PR_QUESTION_VERSION,
    UPDATES_QUESTION_VERSION,
    ci_questions,
    pr_questions,
    update_questions,
)


class JevQuestionsTest(unittest.TestCase):
    def test_pr_questions_delegate_all_six_semantic_choices_to_jev(self) -> None:
        questions = pr_questions(segment_count=2)

        self.assertEqual(PR_QUESTION_VERSION, "pr-followup/v3")
        self.assertEqual(len(questions), 12)
        self.assertEqual(
            set(questions["s0_change_request"]["criteria"]),
            {"present", "absent", "unclear"},
        )
        self.assertEqual(
            set(questions["s1_blocking_language"]["criteria"]),
            {"explicit_blocking", "explicit_nonblocking", "not_stated", "unclear"},
        )
        self.assertIn("state.segments[0]", questions["s0_question"]["instructions"])

    def test_ci_questions_ask_each_symptom_independently_per_window(self) -> None:
        questions = ci_questions(window_count=2)

        self.assertEqual(CI_QUESTION_VERSION, "ci-triage/v2")
        self.assertEqual(len(questions), 18)
        self.assertEqual(set(questions["w0_connection_timeout"]["criteria"]), {"present", "absent", "unclear"})
        self.assertIn("state.windows[1]", questions["w1_configuration_error"]["instructions"])

    def test_update_questions_keep_relevance_and_actions_on_same_unit(self) -> None:
        questions = update_questions(unit_count=2)

        self.assertEqual(UPDATES_QUESTION_VERSION, "updates-filter/v3")
        self.assertEqual(len(questions), 10)
        self.assertEqual(
            set(questions["u0_relevance"]["criteria"]),
            {"relevant", "not_relevant", "unclear"},
        )
        for signal in (
            "migration_stated",
            "deprecation_stated",
            "breaking_change_stated",
            "security_fix_stated",
        ):
            instructions = questions[f"u1_{signal}"]["instructions"]
            self.assertIn("state.changeUnits[1]", instructions)
            self.assertIn("same change", instructions)

    def test_question_builders_enforce_product_unit_limits(self) -> None:
        for builder, invalid in ((pr_questions, 9), (ci_questions, 6), (update_questions, 9)):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ValueError):
                    builder(invalid)


if __name__ == "__main__":
    unittest.main()
