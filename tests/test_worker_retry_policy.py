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

    def test_scan_status_mapper_preserves_partial_completed(self) -> None:
        self.assertEqual(app.scan_status_from_job_status("partial_completed"), "partial_completed")

    def test_done_scan_filter_includes_partial_completed_results(self) -> None:
        self.assertEqual(app.db.scan_job_status_values_for_public_status("done"), ["done", "partial_completed"])

if __name__ == "__main__":
    unittest.main()
