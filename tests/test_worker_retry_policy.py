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

    def test_simple_worker_runtime_item_passes_existing_public_gate(self) -> None:
        item = {
            "candidate": {
                "candidate_id": "cand-1",
                "issue_id": "cand-1",
                "claim": "The handler returns the wrong value.",
                "evidence": [
                    {
                        "file": "src/handler.py",
                        "lines": "1-2",
                        "why_it_matters": "This is the returned value.",
                    }
                ],
                "graph_evidence": {
                    "slice_id": "unit-0001",
                    "codegraph_files": ["src/handler.py"],
                    "path_summary": ["caller -> src/handler.py"],
                },
            },
            "verification": {
                "status": "confirmed",
                "verdict": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "reason": "Repeated runtime proof is stable.",
            },
            "repro": {
                "status": "reproduced",
                "level": "L2",
                "commands_run": [
                    {
                        "cmd": "python3 .codereview/repro/check.py",
                        "exit_code": 0,
                        "log_path": "workers/verification/cand-1/events.jsonl",
                    }
                ],
                "graph_path_exercised": True,
            },
            "judge": {
                "status": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "evidence_summary": {
                    "command": "python3 .codereview/repro/check.py",
                    "log_path": "workers/verification/cand-1/events.jsonl",
                    "observable": "OBSERVED_VALUE:false",
                },
            },
        }
        report = app.public_graph_verified_report(
            {
                "version": "graph-verified-code-review/1",
                "runId": "run-1",
                "mode": "standard",
                "finalJson": {"confirmed": [item]},
            }
        )
        self.assertEqual(report["confirmedCount"], 1)
        self.assertEqual(report["finalJson"]["confirmed"][0]["repro"]["status"], "reproduced")


if __name__ == "__main__":
    unittest.main()
