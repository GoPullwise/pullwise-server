from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pullwise_server import deployment_status


class DeploymentStatusTest(unittest.TestCase):
    def test_running_commit_lookup_trusts_only_the_known_server_checkout(self) -> None:
        revision = "d" * 40
        result = type("GitResult", (), {"returncode": 0, "stdout": revision})()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            deployment_status.subprocess,
            "run",
            return_value=result,
        ) as run:
            root = Path(tmp).resolve()
            current = deployment_status.current_git_revision(root)

        self.assertEqual(current, revision)
        command = run.call_args.args[0]
        self.assertIn(f"safe.directory={root}", command)
        self.assertEqual(command[-3:], ["rev-parse", "--verify", "HEAD^{commit}"])

    def test_verified_payload_requires_running_commit_to_match_successful_watcher_commit(self) -> None:
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "git-watch.status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "succeeded",
                        "revision": revision,
                        "completedAt": "2026-07-16T10:20:30Z",
                    }
                ),
                encoding="utf-8",
            )

            payload = deployment_status.deployment_payload(
                status_file=status_file,
                running_revision=revision,
                server_started_at=1784197230,
            )

        self.assertEqual(payload["state"], "verified")
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["runningCommit"], revision)
        self.assertEqual(payload["lastSuccessfulCommit"], revision)
        self.assertEqual(payload["lastSuccessfulAt"], "2026-07-16T10:20:30Z")
        self.assertEqual(payload["serverStartedAt"], 1784197230)

    def test_mismatched_commit_is_pending_and_malformed_status_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "git-watch.status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "succeeded",
                        "revision": "b" * 40,
                        "completedAt": "2026-07-16T10:20:30Z",
                    }
                ),
                encoding="utf-8",
            )
            pending = deployment_status.deployment_payload(
                status_file=status_file,
                running_revision="a" * 40,
                server_started_at=1,
            )

            status_file.write_text(
                '{"schemaVersion":1,"status":"succeeded","revision":"not-a-commit"}',
                encoding="utf-8",
            )
            unreported = deployment_status.deployment_payload(
                status_file=status_file,
                running_revision="a" * 40,
                server_started_at=1,
            )

        self.assertEqual(pending["state"], "pending")
        self.assertFalse(pending["verified"])
        self.assertEqual(unreported["state"], "unreported")
        self.assertIsNone(unreported["lastSuccessfulCommit"])

    def test_missing_status_still_reports_the_full_running_commit(self) -> None:
        revision = "c" * 40
        with tempfile.TemporaryDirectory() as tmp:
            payload = deployment_status.deployment_payload(
                status_file=Path(tmp) / "missing.json",
                running_revision=revision,
                server_started_at=1,
            )

        self.assertEqual(payload["state"], "unreported")
        self.assertEqual(payload["runningCommit"], revision)
        self.assertIsNone(payload["lastSuccessfulCommit"])
