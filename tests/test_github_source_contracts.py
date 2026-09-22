from __future__ import annotations

import unittest

from pullwise_server.github_sources import (
    ci_failure_source,
    pr_issue_comment_source,
    pr_review_source,
    release_source,
)


class GitHubSourceContractsTest(unittest.TestCase):
    def test_empty_changes_requested_review_is_rules_only_action_source(self) -> None:
        source = pr_review_source(
            repository_id="repo-1",
            pull_number=42,
            review={
                "id": 101,
                "state": "CHANGES_REQUESTED",
                "body": "",
                "html_url": "https://github.com/acme/repo/pull/42#pullrequestreview-101",
                "submitted_at": "2026-09-20T08:00:00Z",
                "user": {"id": 5, "login": "reviewer"},
            },
        )

        self.assertEqual(source["sourceType"], "pr_review_body")
        self.assertEqual(source["processingMode"], "rules_only")
        self.assertEqual(source["ruleActions"], ["change_requested"])
        self.assertEqual(source["content"], {"body": ""})

    def test_issue_comment_requires_pull_request_marker(self) -> None:
        ordinary_issue = pr_issue_comment_source(
            repository_id="repo-1",
            issue={"number": 7},
            comment={"id": 1, "body": "Please fix this."},
        )
        pull_request = pr_issue_comment_source(
            repository_id="repo-1",
            issue={"number": 7, "pull_request": {"url": "https://api.github.com/repos/acme/repo/pulls/7"}},
            comment={
                "id": 2,
                "body": "Please fix this.",
                "html_url": "https://github.com/acme/repo/pull/7#issuecomment-2",
                "updated_at": "2026-09-20T08:00:00Z",
                "user": {"id": 8, "login": "reviewer"},
            },
        )

        self.assertIsNone(ordinary_issue)
        self.assertEqual(pull_request["sourceType"], "pr_comment")
        self.assertEqual(pull_request["externalKey"], "github:pr_comment:2")

    def test_ci_source_requires_failed_job_and_preserves_run_attempt_identity(self) -> None:
        success = ci_failure_source(
            repository_id="repo-1",
            run={"id": 50, "run_attempt": 2, "head_sha": "abc", "workflow_id": 9},
            job={"id": 70, "conclusion": "success", "name": "test"},
            evidence_windows=[],
        )
        failure = ci_failure_source(
            repository_id="repo-1",
            run={
                "id": 50,
                "run_attempt": 2,
                "head_sha": "abc",
                "workflow_id": 9,
                "html_url": "https://github.com/acme/repo/actions/runs/50/attempts/2",
            },
            job={
                "id": 71,
                "conclusion": "failure",
                "name": "test (py3.10)",
                "steps": [{"number": 2, "name": "Install dependencies", "conclusion": "failure"}],
                "html_url": "https://github.com/acme/repo/actions/runs/50/job/71",
                "completed_at": "2026-09-20T08:00:00Z",
            },
            evidence_windows=[{
                "windowId": "w1",
                "stepNumber": 2,
                "stepName": "Install dependencies",
                "stage": "dependency_install",
                "stageRuleVersion": "ci-stage/v1",
                "text": "connection timed out",
            }],
        )

        self.assertIsNone(success)
        self.assertEqual(failure["externalKey"], "github:ci_failure:repo-1:50:2:71")
        self.assertEqual(failure["sourceFacts"]["runAttempt"], 2)
        self.assertEqual(failure["sourceFacts"]["windows"][0]["stage"], "dependency_install")
        self.assertIsNone(failure["sourceFacts"]["recovery"])
        self.assertEqual(failure["sourceFacts"]["recoveryStatus"], "unknown")

    def test_release_source_keeps_unanalysed_release_fact_and_lifecycle(self) -> None:
        source = release_source(
            upstream_repository_id="upstream-1",
            release={
                "id": 90,
                "tag_name": "v2.0.0",
                "name": "Version 2",
                "body": "OAuth migration required.",
                "html_url": "https://github.com/acme/upstream/releases/tag/v2.0.0",
                "published_at": "2026-09-20T08:00:00Z",
                "updated_at": "2026-09-20T08:10:00Z",
                "draft": False,
                "prerelease": False,
            },
        )
        unavailable = release_source(
            upstream_repository_id="upstream-1",
            release={"id": 91, "tag_name": "v2.1.0", "draft": True, "body": "draft"},
        )

        self.assertEqual(source["sourceType"], "release")
        self.assertEqual(source["processingMode"], "model")
        self.assertEqual(source["sourceFacts"]["tagName"], "v2.0.0")
        self.assertEqual(unavailable["lifecycle"], "source_closed")


if __name__ == "__main__":
    unittest.main()
