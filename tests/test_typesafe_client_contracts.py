from __future__ import annotations

import json
import math
import sys
import types
import unittest
from unittest.mock import patch

from pullwise_server.typesafe_client import (DEFAULT_JEV_MODEL, MAX_STATE_QUESTION_BYTES,
                                           build_request, run_jev_sdk, validate_response)


class TypeSafeClientContractsTest(unittest.TestCase):
    def test_sdk_boundary_pins_model_disables_retries_and_validates_raw_response(self) -> None:
        calls = []

        class RetryPolicy:
            def __init__(self, *, max_retries: int) -> None:
                self.max_retries = max_retries

        class Client:
            def system_one(self, **kwargs):
                calls.append(kwargs)
                raw = types.SimpleNamespace(content=json.dumps(self_response).encode())
                return types.SimpleNamespace(raw_http_response=raw, request_id="jev-request-1")

        self_response = self.valid_response()
        question = {"s0_question": {"type": "choice", "instructions": "Evaluate text.",
                                    "criteria": {"present": "yes", "absent": "no", "unclear": "unknown"}}}
        with patch.dict(sys.modules, {"typesafe_sdk": types.SimpleNamespace(RetryPolicy=RetryPolicy)}):
            result = run_jev_sdk(Client(), state={"segments": [{"text": "Please fix this."}]},
                                 questions=question)
        self.assertEqual(result["requestId"], "jev-request-1")
        self.assertEqual(result["answers"]["s0_question"]["choice"], "present")
        self.assertEqual(calls[0]["model"], DEFAULT_JEV_MODEL)
        self.assertEqual(calls[0]["retry"].max_retries, 0)
        self.assertEqual(calls[0]["questions"], question)

        self_response["model"] = "jev-latest"
        with patch.dict(sys.modules, {"typesafe_sdk": types.SimpleNamespace(RetryPolicy=RetryPolicy)}):
            with self.assertRaisesRegex(ValueError, "returned model"):
                run_jev_sdk(Client(), state="text", questions=question)

    def test_sdk_boundary_rejects_invalid_input_before_provider_call(self) -> None:
        class Client:
            def system_one(self, **kwargs):
                self.fail("provider must not be called")

        with self.assertRaisesRegex(ValueError, "text-only"):
            run_jev_sdk(Client(), state={"image": "https://example.com/x.png"},
                        questions={"q": {"type": "choice", "instructions": "Classify.",
                                         "criteria": {"yes": "yes", "no": "no"}}})

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

    def test_choice_criteria_order_survives_canonicalization_and_permutation(self):
        def request(criteria):
            return build_request(state={"note": "Shared cloud hosting", "purpose": "Cloud"},
                questions={"q": {"type": "choice", "instructions": "Classify.", "criteria": criteria}},
                model=DEFAULT_JEV_MODEL)
        criteria = {"z_host": "Hosting", "a_tools": "Tools", "uncertain": "Unknown"}
        first = request(criteria)
        reversed_options = dict(reversed(list(criteria.items())))
        second = request(reversed_options)
        self.assertEqual(list(first["questions"]["q"]["criteria"]), list(criteria))
        self.assertEqual(list(second["questions"]["q"]["criteria"]), list(reversed_options))
        self.assertEqual(first, request(criteria))

    def test_separate_state_plus_longest_question_context_envelope(self):
        question = {"type": "choice", "instructions": "Classify.",
                    "criteria": {"yes": "Yes", "no": "No"}}
        with self.assertRaisesRegex(ValueError, "state plus question"):
            build_request(state="x" * MAX_STATE_QUESTION_BYTES, questions={"q": question},
                          model=DEFAULT_JEV_MODEL)
        oversized_question = {**question, "instructions": "x" * MAX_STATE_QUESTION_BYTES}
        with self.assertRaisesRegex(ValueError, "state plus question"):
            build_request(state="small state", questions={"q": oversized_question}, model=DEFAULT_JEV_MODEL)
        # Parallel questions can exceed that smaller envelope collectively.
        # Each state/question pair still fits and the full request stays bounded.
        questions = {f"q{index}": {**question, "instructions": "x" * 1000} for index in range(30)}
        self.assertEqual(len(build_request(state="small state", questions=questions,
                                          model=DEFAULT_JEV_MODEL)["questions"]), 30)

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
