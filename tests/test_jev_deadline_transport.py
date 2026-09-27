from __future__ import annotations

import time
import unittest

from pullwise_server.jev_deadline_transport import BoundedJevRawTransport


QUESTIONS = {"q": {"type": "choice", "instructions": "Is action required?",
    "criteria": {"present": "Action is required", "absent": "No action is required"}}}
STATE = {"segments": [{"text": "Please fix this."}]}


def _fixture_invoke(*, state, questions, model):
    assert state == STATE and questions == QUESTIONS and model == "jev-1.13.0"
    return b'{"model":"jev-1.13.0"}'


def _slow_invoke(**_kwargs):
    time.sleep(5)
    return b"too late"


def _large_invoke(**_kwargs):
    return b"x" * (1024 * 1024 + 1)


def _failed_invoke(**_kwargs):
    raise RuntimeError("private provider details")


class JevDeadlineTransportTest(unittest.TestCase):
    def test_child_response_passes_with_fixed_model_and_text_only_request(self):
        transport = BoundedJevRawTransport(invoke=_fixture_invoke, total_seconds=2)
        self.assertEqual(transport(state=STATE, questions=QUESTIONS, model="jev-1.13.0"),
            b'{"model":"jev-1.13.0"}')

    def test_parent_kills_blocked_child_with_total_deadline(self):
        transport = BoundedJevRawTransport(invoke=_slow_invoke, total_seconds=0.4)
        start = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "JEV_TOTAL_DEADLINE"):
            transport(state=STATE, questions=QUESTIONS, model="jev-1.13.0")
        self.assertLess(time.monotonic() - start, 2.5)

    def test_oversized_raw_response_is_rejected_before_ipc(self):
        transport = BoundedJevRawTransport(invoke=_large_invoke, total_seconds=2)
        with self.assertRaisesRegex(ValueError, "JEV_RESPONSE_INVALID"):
            transport(state=STATE, questions=QUESTIONS, model="jev-1.13.0")

    def test_child_failure_does_not_expose_provider_details(self):
        transport = BoundedJevRawTransport(invoke=_failed_invoke, total_seconds=2)
        with self.assertRaisesRegex(OSError, "JEV_PROVIDER_UNAVAILABLE") as caught:
            transport(state=STATE, questions=QUESTIONS, model="jev-1.13.0")
        self.assertNotIn("private provider details", str(caught.exception))

    def test_invalid_or_nontext_request_fails_before_child(self):
        transport = BoundedJevRawTransport(invoke=_fixture_invoke, total_seconds=2)
        with self.assertRaises(ValueError):
            transport(state={"image": "https://example.com/private.png"},
                questions=QUESTIONS, model="jev-1.13.0")
        with self.assertRaises(ValueError):
            transport(state=STATE, questions=QUESTIONS, model="different-model")


if __name__ == "__main__":
    unittest.main()
