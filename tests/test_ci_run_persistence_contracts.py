from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from pullwise_server.product_discovery import FactPage, ProductFactSync
from pullwise_server.product_store import ProductStore


class CIRunPersistenceContractsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ProductStore(Path(directory.name) / "ci.sqlite3")
        self.store.initialize()
        self.now = 1_800_000_000
        clock = patch("pullwise_server.product_store._now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.service = self.store.put_repository_service(repository_id="github:123", installation_id="11",
            billing_owner_id="usr_1", enabled=True, modules={"pr": False, "ci": True},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id=None, priority_order=0, expected_revision=0)
        self.key = self.store.set_discovery_authorization(resource_kind="repository", resource_id="github:123",
            module="ci", github_repository_id="123", installation_id="11", app_id="7",
            authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        self.reader = Mock()
        self.sync = ProductFactSync(self.store, read_page=self.reader,
            processing_budget=lambda owner, now: ("fixture", 10), app_id="7", webhook_secret="fixture")
        self.state = {"repositoryId": "github:123", "runId": "9", "runAttempt": 1,
            "workflowId": "3", "headSha": "abc", "status": "completed", "conclusion": "success",
            "updatedAt": "2026-09-21T00:00:00Z", "pullRequests": [],
            "jobs": [{"jobId": "10", "name": "test", "status": "completed", "conclusion": "success",
                      "completedAt": "2026-09-21T00:00:00Z", "steps": []}],
            "coverage": {"jobsComplete": True, "jobsPage": 1, "nextJobsPage": None}}

    def page(self, states):
        return FactPage((), None, "2026-09-21T00:00:00Z", run_states=tuple(states))

    def rows(self):
        with closing(self.store.connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM github_run_states ORDER BY run_attempt")]

    def test_success_is_persisted_without_failure_source_or_model_job(self):
        self.reader.return_value = self.page([self.state])
        self.assertEqual(self.sync.run_manual(self.key, now=self.now), {"status": "completed", "sources": 0})
        self.assertEqual(json.loads(self.rows()[0]["snapshot_json"])["conclusion"], "success")
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM processing_usage_ledger").fetchone()[0], 0)

    def test_attempt_history_is_not_overwritten_and_success_does_not_infer_recovery(self):
        first = dict(self.state, conclusion="failure", jobs=[])
        second = dict(self.state, runAttempt=2, updatedAt="2026-09-21T01:00:00Z")
        for state in (first, second):
            self.reader.return_value = self.page([state])
            self.sync.run_manual(self.key, now=self.now)
        rows = self.rows()
        self.assertEqual([row["run_attempt"] for row in rows], [1, 2])
        self.assertEqual([json.loads(row["snapshot_json"])["conclusion"] for row in rows], ["failure", "success"])
        self.assertNotIn("recovery", json.loads(rows[1]["snapshot_json"]))

    def test_older_authoritative_run_response_cannot_regress_completed_state(self):
        self.reader.return_value = self.page([self.state])
        self.sync.run_manual(self.key, now=self.now)
        self.reader.return_value = self.page([dict(self.state, status="in_progress", conclusion=None,
                                                 updatedAt="2026-09-20T00:00:00Z")])
        self.sync.run_manual(self.key, now=self.now)
        self.assertEqual(json.loads(self.rows()[0]["snapshot_json"])["status"], "completed")

    def test_same_attempt_completed_run_never_regresses_to_in_progress(self):
        self.reader.return_value = self.page([self.state])
        self.sync.run_manual(self.key, now=self.now)
        for timestamp in (self.state["updatedAt"], "2026-09-21T00:01:00Z"):
            self.reader.return_value = self.page([dict(self.state, status="in_progress", conclusion=None,
                                                     updatedAt=timestamp)])
            self.sync.run_manual(self.key, now=self.now)
            with self.subTest(timestamp=timestamp):
                saved = json.loads(self.rows()[0]["snapshot_json"])
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["conclusion"], "success")

    def test_wrong_repository_or_invalid_attempt_rolls_back_entire_page(self):
        for change in ({"repositoryId": "github:999"}, {"runAttempt": True}, {"runId": "-1"},
                       {"updatedAt": None}, {"updatedAt": "not-a-time"}):
            self.reader.return_value = self.page([self.state, dict(self.state, **change)])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.sync.run_manual(self.key, now=self.now)
            self.assertEqual(self.rows(), [])

    def test_revocation_during_read_discards_run_state(self):
        def read(**kwargs):
            self.store.revoke_discovery_installation(app_id="7", installation_id="11",
                                                     repository_ids=None, observed_at=self.now)
            return self.page([self.state])
        self.reader.side_effect = read
        self.assertEqual(self.sync.run_manual(self.key, now=self.now), {"status": "unauthorized"})
        self.assertEqual(self.rows(), [])

    def test_late_overlapping_read_cannot_replace_newer_run_state(self):
        def older(**kwargs):
            self.reader.side_effect = None
            self.reader.return_value = self.page([self.state])
            self.sync.run_manual(self.key, now=self.now)
            return self.page([dict(self.state, conclusion="failure")])
        self.reader.side_effect = older
        self.assertEqual(self.sync.run_manual(self.key, now=self.now), {"status": "superseded"})
        self.assertEqual(json.loads(self.rows()[0]["snapshot_json"])["conclusion"], "success")

    def test_partial_jobs_remain_partial_across_pages(self):
        for page in (1, 2):
            state = copy.deepcopy(self.state)
            state["coverage"] = {"jobsComplete": False, "jobsPage": page, "nextJobsPage": 2 if page == 1 else None}
            self.reader.return_value = self.page([state])
            self.sync.run_manual(self.key, now=self.now)
        saved = json.loads(self.rows()[0]["snapshot_json"])
        self.assertFalse(saved["coverage"]["jobsComplete"])
        self.assertEqual(saved["coverage"]["jobsPage"], 2)


if __name__ == "__main__":
    unittest.main()
