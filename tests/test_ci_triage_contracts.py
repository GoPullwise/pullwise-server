from __future__ import annotations

import unittest

from pullwise_server.ci_triage import verified_successor


class CiTriageContractsTest(unittest.TestCase):
    def test_same_name_and_sha_without_identity_proof_stays_unknown(self) -> None:
        failure = {
            "repositoryId": "repo-1",
            "workflowId": "wf-1",
            "runId": "run-1",
            "runAttempt": 1,
            "jobId": "job-1",
            "jobName": "test (py3.10)",
            "headSha": "abc",
            "conclusion": "failure",
        }
        success = {
            **failure,
            "runId": "run-2",
            "jobId": "job-2",
            "conclusion": "success",
        }

        result = verified_successor(failure, success)

        self.assertIsNone(result["recovery"])
        self.assertEqual(result["recoveryStatus"], "unknown")
        self.assertEqual(result["reason"], "identity_proof_missing")

    def test_same_run_retry_requires_verified_job_identity(self) -> None:
        failure = {
            "repositoryId": "repo-1",
            "workflowId": "wf-1",
            "runId": "run-1",
            "runAttempt": 1,
            "jobId": "job-1",
            "jobIdentity": "test:python=3.10",
            "conclusion": "failure",
        }
        success = {
            **failure,
            "runAttempt": 2,
            "jobId": "job-2",
            "conclusion": "success",
        }
        proof = {
            "verified": True,
            "kind": "same_run_job_identity",
            "jobIdentity": "test:python=3.10",
            "matchRuleVersion": "ci-successor/v1",
            "evidence": {"runId": "run-1", "failureAttempt": 1, "successAttempt": 2},
        }

        result = verified_successor(failure, success, proof=proof)

        self.assertEqual(result["recoveryStatus"], "verified")
        self.assertEqual(result["recovery"]["kind"], "same_run_retry_succeeded")

    def test_later_run_requires_explicit_verified_lineage_not_only_job_identity(self) -> None:
        failure = {
            "repositoryId": "repo-1",
            "workflowId": "wf-1",
            "runId": "run-1",
            "runAttempt": 1,
            "jobId": "job-1",
            "jobIdentity": "test:python=3.10",
            "conclusion": "failure",
        }
        success = {
            **failure,
            "runId": "run-2",
            "jobId": "job-2",
            "conclusion": "success",
        }
        insufficient = {
            "verified": True,
            "kind": "same_run_job_identity",
            "jobIdentity": "test:python=3.10",
            "matchRuleVersion": "ci-successor/v1",
            "evidence": {},
        }
        verified = {
            "verified": True,
            "kind": "verified_run_lineage",
            "jobIdentity": "test:python=3.10",
            "matchRuleVersion": "ci-successor/v1",
            "evidence": {
                "failureRunId": "run-1",
                "successRunId": "run-2",
                "lineageKind": "pull_request_head_successor",
                "lineageId": "pr-42:head-transition-7",
            },
        }

        self.assertEqual(verified_successor(failure, success, proof=insufficient)["recoveryStatus"], "unknown")
        self.assertEqual(verified_successor(failure, success, proof=verified)["recovery"]["kind"], "later_run_succeeded")

    def test_different_workflow_or_job_identity_never_closes_failure(self) -> None:
        failure = {
            "repositoryId": "repo-1",
            "workflowId": "wf-1",
            "runId": "run-1",
            "runAttempt": 1,
            "jobId": "job-1",
            "jobIdentity": "test:python=3.10",
            "conclusion": "failure",
        }
        success = {
            **failure,
            "workflowId": "wf-2",
            "runId": "run-2",
            "jobId": "job-2",
            "conclusion": "success",
        }
        proof = {
            "verified": True,
            "kind": "verified_run_lineage",
            "jobIdentity": "test:python=3.10",
            "matchRuleVersion": "ci-successor/v1",
            "evidence": {
                "failureRunId": "run-1",
                "successRunId": "run-2",
                "lineageKind": "pull_request_head_successor",
                "lineageId": "pr-42:head-transition-7",
            },
        }

        result = verified_successor(failure, success, proof=proof)

        self.assertIsNone(result["recovery"])
        self.assertEqual(result["reason"], "execution_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
