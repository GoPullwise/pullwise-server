from __future__ import annotations

import unittest

from pullwise_server import app


CODEX_READINESS_ERROR_CODES = (
    "CODEX_AUTH_REQUIRED",
    "CODEX_AUTH_EXPIRED",
    "CODEX_AUTHORIZATION_FAILED",
    "CODEX_SUBSCRIPTION_INACTIVE",
    "CODEX_QUOTA_EXHAUSTED",
    "CODEX_VERSION_UNSUPPORTED",
)


class WorkerRetryPolicyTest(unittest.TestCase):
    def test_codex_readiness_error_codes_are_preserved(self) -> None:
        for error_code in CODEX_READINESS_ERROR_CODES:
            with self.subTest(error_code=error_code):
                self.assertEqual(app.public_scan_error_code(error_code), error_code)

    def test_terminal_readiness_failures_are_refundable(self) -> None:
        expected = {"REPOSITORY_TOO_LARGE", *CODEX_READINESS_ERROR_CODES}
        self.assertTrue(expected.issubset(app.WORKER_TERMINAL_REFUNDABLE_ERROR_CODES))

    def test_codex_readiness_failures_are_not_automatically_retried(self) -> None:
        for error_code in CODEX_READINESS_ERROR_CODES:
            with self.subTest(error_code=error_code):
                self.assertFalse(
                    app.worker_result_allows_auto_retry(
                        {"error_code": error_code},
                        status="failed",
                    )
                )

    def test_transient_worker_failure_remains_retryable(self) -> None:
        self.assertTrue(
            app.worker_result_allows_auto_retry(
                {"error_code": "GRAPH_VERIFIED_COMPLETION_FAILED"},
                status="failed",
            )
        )

    def test_success_is_never_sent_to_failure_retry_policy(self) -> None:
        self.assertFalse(app.worker_result_allows_auto_retry({}, status="done"))


if __name__ == "__main__":
    unittest.main()
