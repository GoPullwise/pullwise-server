from __future__ import annotations

import json
import math
import unittest

from pullwise_server.typesafe_client import DEFAULT_JEV_MODEL, build_request, validate_response


class TypeSafeClientContractsTest(unittest.TestCase):
    def test_current_model_is_exactly_jev_1_13(self) -> None:
        self.assertEqual(DEFAULT_JEV_MODEL, "jev-1.13.0")
        with self.assertRaisesRegex(ValueError, "fixed model"):
            build_request(
                state="text",
                questions={
                    "q": {
                        "type": "choice",
                        "instructions": "Evaluate the text.",
                        "criteria": {"yes": "yes", "no": "no"},
                    }
                },
                model="jev-latest",
            )

    def valid_response(self) -> dict:
        return {
            "model": "jev-1.13.0",
            "answers": {
                "s0_question": {
                    "type": "choice",
                    "choice": "present",
                    "probabilities": {"present": 0.9, "absent": 0.05, "unclear": 0.05},
                    "confidence": 0.9,
                }
            },
            "usage": {"input_tokens": 120, "output_tokens": None},
        }

    def test_request_builder_is_deterministic_and_enforces_question_and_byte_limits(self) -> None:
        questions = {
            "s0_question": {
                "type": "choice",
                "instructions": "Evaluate state.segments[0].",
                "criteria": {"present": "yes", "absent": "no", "unclear": "unknown"},
            }
        }
        first = build_request(
            state={"segments": [{"text": "Please add a test."}]},
            questions=questions,
            model="jev-1.13.0",
        )
        second = build_request(
            state={"segments": [{"text": "Please add a test."}]},
            questions=questions,
            model="jev-1.13.0",
        )

        self.assertEqual(first, second)
        self.assertEqual(set(first), {"state", "model", "questions"})
        with self.assertRaisesRegex(ValueError, "question limit"):
            build_request(
                state={},
                questions={f"q{index}": questions["s0_question"] for index in range(49)},
                model="jev-1.13.0",
            )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            build_request(
                state={"text": "x" * 50_000},
                questions=questions,
                model="jev-1.13.0",
            )

    def test_state_accepts_only_text_strings_objects_or_arrays(self) -> None:
        question = {
            "q": {
                "type": "choice",
                "instructions": "Evaluate the supplied text.",
                "criteria": {"yes": "yes", "no": "no"},
            }
        }
        valid_states = (
            "plain text",
            ["first", "second"],
            {"segments": [{"text": "Please add a test.", "role": "reviewer"}]},
        )
        for state in valid_states:
            with self.subTest(state=state):
                self.assertEqual(build_request(state=state, questions=question, model="jev-1.13.0")["state"], state)

        invalid_states = (
            {"count": 2},
            {"enabled": True},
            {"missing": None},
            {"image": "https://example.com/image.png"},
            {"text": "data:image/png;base64,AAAA"},
            ["text", b"binary"],
        )
        for state in invalid_states:
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, "text-only"):
                    build_request(state=state, questions=question, model="jev-1.13.0")

    def test_valid_choice_response_preserves_unknown_token_usage(self) -> None:
        response = self.valid_response()

        result = validate_response(
            json.dumps(response).encode(),
            expected_questions={"s0_question": ("present", "absent", "unclear")},
            requested_model="jev-1.13.0",
            request_id="request-1",
        )

        self.assertEqual(result["answers"]["s0_question"]["choice"], "present")
        self.assertEqual(result["usage"]["inputTokens"], 120)
        self.assertIsNone(result["usage"]["outputTokens"])

    def test_missing_extra_or_duplicate_answer_keys_are_rejected(self) -> None:
        missing = self.valid_response()
        missing["answers"] = {}
        extra = self.valid_response()
        extra["answers"]["extra"] = extra["answers"]["s0_question"]
        duplicate = b'{"model":"jev-1.13.0","answers":{},"answers":{},"usage":{}}'

        for payload in (json.dumps(missing).encode(), json.dumps(extra).encode(), duplicate):
            with self.subTest(payload=payload[:60]):
                with self.assertRaises(ValueError):
                    validate_response(
                        payload,
                        expected_questions={"s0_question": ("present", "absent", "unclear")},
                        requested_model="jev-1.13.0",
                    )

    def test_invalid_probabilities_choice_confidence_model_and_usage_are_rejected(self) -> None:
        mutations = []
        wrong_sum = self.valid_response()
        wrong_sum["answers"]["s0_question"]["probabilities"]["present"] = 0.7
        mutations.append(wrong_sum)
        not_max = self.valid_response()
        not_max["answers"]["s0_question"]["choice"] = "absent"
        mutations.append(not_max)
        boolean_confidence = self.valid_response()
        boolean_confidence["answers"]["s0_question"]["confidence"] = True
        mutations.append(boolean_confidence)
        model_drift = self.valid_response()
        model_drift["model"] = "jev-latest"
        mutations.append(model_drift)
        missing_usage = self.valid_response()
        missing_usage.pop("usage")
        mutations.append(missing_usage)
        invalid_usage = self.valid_response()
        invalid_usage["usage"]["input_tokens"] = -1
        mutations.append(invalid_usage)

        for payload in mutations:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_response(
                        json.dumps(payload).encode(),
                        expected_questions={"s0_question": ("present", "absent", "unclear")},
                        requested_model="jev-1.13.0",
                    )

    def test_nonfinite_numbers_are_rejected_even_when_supplied_as_python_json_extensions(self) -> None:
        response = self.valid_response()
        response["answers"]["s0_question"]["confidence"] = math.nan

        with self.assertRaises(ValueError):
            validate_response(
                json.dumps(response).encode(),
                expected_questions={"s0_question": ("present", "absent", "unclear")},
                requested_model="jev-1.13.0",
            )


if __name__ == "__main__":
    unittest.main()
