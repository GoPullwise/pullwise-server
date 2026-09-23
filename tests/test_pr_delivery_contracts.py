from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from pullwise_server.product_discovery import FactPage, ProductFactSync
from pullwise_server.product_store import ProductStore
from pullwise_server.github_pr_reader import GitHubPRReader
from pullwise_server.github_transport import GitHubResponse


class PRDeliveryContractsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pr.sqlite3"
        self.store = ProductStore(self.path)
        self.store.initialize()
        self.now = 1_800_000_000
        clock = patch("pullwise_server.product_store._now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.store.put_repository_service(repository_id="github:123", installation_id="11", billing_owner_id="usr_1",
            expected_revision=0, enabled=True, modules={"pr": True, "ci": False},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id=None, priority_order=0)
        self.key = self.store.set_discovery_authorization(resource_kind="repository", resource_id="github:123",
            module="pr", github_repository_id="123", installation_id="11", app_id="7", authorization_revision=1,
            accessible=True, valid_until=self.now + 300, observed_at=self.now)
        self.reader = Mock(return_value=FactPage((), None, ""))
        self.sync = ProductFactSync(self.store, read_page=self.reader, processing_budget=lambda owner, now: ("fixture", 10),
                                   app_id="7", webhook_secret="fixture")

    def send(self, event="pull_request_review", number=42):
        payload = {"action": "submitted", "repository": {"id": 123}, "installation": {"id": 11},
                   "review": {"id": 456, "body": "UNTRUSTED BODY"}, "pull_request": {"id": 999, "number": number}}
        if event == "issue_comment":
            payload.update(action="created", comment={"id": 456}, issue={"number": number, "pull_request": {"url": "ignored"}})
            del payload["pull_request"]
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"fixture", raw, hashlib.sha256).hexdigest()
        return self.sync.receive_github_event(raw, signature=signature, event=event, delivery_id="delivery", now=self.now)

    def test_review_receipt_carries_pull_number_to_authoritative_reader(self):
        self.assertEqual(self.send(), {"accepted": 1})
        self.reader.assert_not_called()
        self.now += 30
        self.sync.run_events(now=self.now)
        hint = self.reader.call_args.kwargs["event"]
        self.assertEqual(hint["pull_number"], 42)
        self.assertEqual(hint["resource_id"], "456")
        self.assertNotIn("UNTRUSTED BODY", repr(hint))

    def test_pr_discussion_uses_issue_number_without_persisting_payload(self):
        self.assertEqual(self.send("issue_comment"), {"accepted": 1})
        self.now += 30
        self.sync.run_events(now=self.now)
        self.assertEqual(self.reader.call_args.kwargs["event"]["pull_number"], 42)

    def test_invalid_parent_locator_is_rejected_before_receipt_write(self):
        for number in (None, True, 0, "42", -1):
            with self.subTest(number=number), self.assertRaisesRegex(ValueError, "GITHUB_BINDING_INVALID"):
                self.send(number=number)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM github_deliveries").fetchone()[0], 0)

    def test_existing_receipts_survive_addition_of_parent_locator(self):
        path = Path(self.directory.name) / "old.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""CREATE TABLE github_delivery_targets(app_id TEXT, delivery_id TEXT,
                control_key TEXT, repository_id TEXT, installation_id TEXT, event TEXT, resource_id TEXT,
                ready_at INTEGER, state TEXT, PRIMARY KEY(app_id,delivery_id,control_key))""")
            connection.execute("INSERT INTO github_delivery_targets VALUES ('7','old','control','123','11','release','1',1,'pending')")
        store = ProductStore(path)
        store.initialize()
        store.initialize()
        with closing(store.connect()) as connection:
            saved = connection.execute("SELECT * FROM github_delivery_targets").fetchone()
        self.assertEqual(saved["delivery_id"], "old")
        self.assertIsNone(saved["pull_number"])

    def test_older_pr_state_cannot_reopen_a_newer_closed_snapshot(self):
        source = {"sourceId": "source_pr_state_123_42", "sourceType": "pr_state",
                  "externalKey": "github:pr_state:123:42", "repositoryId": "github:123",
                  "content": {"body": ""}, "sourceUrl": "https://github.com/acme/lib/pull/42",
                  "processingMode": "rules_only", "completeness": "complete",
                  "lifecycle": "source_closed", "sourceFacts": {"pullNumber": 42, "updatedAt": "2026-09-21T00:00:00Z"}}
        self.reader.return_value = FactPage((source,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        old = dict(source, lifecycle="active", sourceFacts={"pullNumber": 42, "updatedAt": "2026-09-20T00:00:00Z"})
        self.reader.return_value = FactPage((old,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        with closing(self.store.connect()) as connection:
            saved = connection.execute("SELECT lifecycle FROM source_records").fetchone()
        self.assertEqual(saved[0], "source_closed")

    def test_reconciliation_rotates_saved_open_parents_and_persists_check_after_fact(self):
        target = self.store.discovery_target(self.key)
        for number in (41, 42):
            self.store.upsert_source_snapshot(
                source_id=f"source_pr_state_{number}", source_type="pr_state",
                external_key=f"github:pr_state:{number}", repository_id="github:123",
                content={"body": ""}, source_facts={"pullNumber": number, "state": "open"},
                source_url=f"https://github.com/acme/lib/pull/{number}", processing_mode="rules_only",
                completeness="partial", lifecycle="active", observed_at=self.now)
            self.store.set_source_context(source_id=f"source_pr_state_{number}", context_id=target["context_id"],
                context_version=1, configuration_revision=1, authorization_revision=1,
                authorization_valid_until=self.now + 300, accessible=True, billing_owner_id="usr_1")
        first = self.store.next_open_pr_for_reconciliation(target)
        self.assertEqual(first["pullNumber"], 41)
        self.store.mark_pr_reconciled(first["sourceId"], observed_at=self.now)
        self.assertEqual(self.store.next_open_pr_for_reconciliation(target)["pullNumber"], 42)
        self.assertEqual(ProductStore(self.path).next_open_pr_for_reconciliation(target)["pullNumber"], 42)

    def test_rebuilt_scheduler_discovers_missed_close_and_closes_existing_review_item(self):
        from pullwise_server.github_sources import pr_review_source

        review = pr_review_source(repository_id="github:123", pull_number=42,
            review={"id": 456, "state": "CHANGES_REQUESTED", "body": "",
                    "submitted_at": "2026-09-20T00:00:00Z",
                    "html_url": "https://github.com/acme/lib/pull/42#pullrequestreview-456"})
        review["sourceFacts"].update(formalReviewStatus="effective", pullState="open")
        self.reader.return_value = FactPage((review,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        item = self.store.list_items_for_billing_owner("usr_1")[0]
        parent = self.store.upsert_source_snapshot(
            source_id="source_pr_state_700", source_type="pr_state",
            external_key="github:pr_state:700", repository_id="github:123",
            content={"body": ""}, source_facts={"pullNumber": 42, "state": "open",
                 "updatedAt": "2026-09-20T00:00:00Z"},
            source_url="https://github.com/acme/lib/pull/42", processing_mode="rules_only",
            completeness="partial", lifecycle="active", observed_at=self.now)
        target = self.store.discovery_target(self.key)
        self.store.set_source_context(source_id=parent["id"], context_id=target["context_id"],
            context_version=target["context_version"], configuration_revision=target["configuration_revision"],
            authorization_revision=target["authorization_revision"], authorization_valid_until=target["valid_until"],
            accessible=True, billing_owner_id="usr_1")

        calls = []
        def get(path, *, token):
            calls.append(path)
            if path in {"/repositories/123", "/repos/acme/lib"}:
                payload = {"id": 123, "full_name": "acme/lib", "private": False}
            elif path == "/repos/acme/lib/pulls/42":
                payload = {"id": 700, "number": 42, "state": "closed", "title": "Fix", "body": "",
                           "updated_at": "2026-09-21T00:00:00Z", "merged_at": None, "draft": False,
                           "user": {"id": 1}, "head": {"sha": "abc"}, "base": {"repo": {"id": 123}},
                           "requested_reviewers": [], "requested_teams": [],
                           "html_url": "https://github.com/acme/lib/pull/42"}
            else:
                raise AssertionError(path)
            return GitHubResponse(200, payload, {})
        reopened_store = ProductStore(self.path)
        reader = GitHubPRReader(get_json=get, token_for_target=lambda target: "fixture",
                                known_open_pull=reopened_store.next_open_pr_for_reconciliation)
        scheduler = ProductFactSync(reopened_store, read_page=reader.read_page,
            read_scheduled_page=lambda **kwargs: reader.read_page(**kwargs, reconcile=True),
            processing_budget=lambda owner, now: ("fixture", 10), app_id="7", webhook_secret="fixture")
        self.assertEqual(scheduler.run_scheduled(self.key, now=self.now)["sources"], 1)
        self.assertIn("/repos/acme/lib/pulls/42", calls)
        self.assertNotIn("/repos/acme/lib/pulls?per_page=20&state=open&sort=updated&direction=desc&page=1", calls)
        closed = reopened_store.list_items_for_billing_owner("usr_1")[0]
        self.assertEqual(closed["id"], item["id"])
        self.assertEqual(closed["closureReason"], "source_closed")
        self.assertEqual(reopened_store.next_open_pr_for_reconciliation(target), None)
        self.assertEqual(reopened_store.count_jobs(job_type="analyze_source"), 0)

    def test_parent_close_and_reopen_reconcile_existing_review_item_in_fact_transaction(self):
        from pullwise_server.github_sources import pr_review_source
        review = pr_review_source(repository_id="github:123", pull_number=42,
            review={"id": 456, "state": "CHANGES_REQUESTED", "body": "", "submitted_at": "2026-09-20T00:00:00Z",
                    "html_url": "https://github.com/acme/lib/pull/42#pullrequestreview-456"})
        review["sourceFacts"].update(formalReviewStatus="effective", pullState="open")
        self.reader.return_value = FactPage((review,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        item = self.store.list_items_for_billing_owner("usr_1")[0]
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"], actor_id="usr_1", disposition="done")
        parent = {"sourceId": "pr-parent", "sourceType": "pr_state", "externalKey": "github:pr_state:999",
                  "repositoryId": "github:123", "content": {"body": ""}, "sourceUrl": "https://github.com/acme/lib/pull/42",
                  "processingMode": "rules_only", "completeness": "partial", "lifecycle": "source_closed",
                  "sourceFacts": {"pullNumber": 42, "state": "closed", "requestedReviewers": [], "requestedTeams": [],
                                  "updatedAt": "2026-09-21T00:00:00Z"}}
        self.reader.return_value = FactPage((parent,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        closed = self.store.list_items_for_billing_owner("usr_1")[0]
        self.assertEqual(closed["closureReason"], "source_closed")
        self.assertEqual({s["sourceId"] for s in closed["sources"]}, {"pr-parent", review["sourceId"]})
        self.reader.return_value = FactPage((review,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        still_closed = self.store.list_items_for_billing_owner("usr_1")[0]
        self.assertEqual(still_closed["itemVersion"], closed["itemVersion"])
        parent["lifecycle"] = "active"
        parent["sourceFacts"].update(state="open", updatedAt="2026-09-22T00:00:00Z")
        self.reader.return_value = FactPage((parent,), None, "")
        self.sync.run_manual(self.key, now=self.now)
        reopened = self.store.list_items_for_billing_owner("usr_1")[0]
        self.assertEqual(reopened["id"], item["id"])
        self.assertEqual(reopened["attentionState"], "needs_confirmation")
        self.assertEqual(reopened["handling"]["disposition"], "open")


if __name__ == "__main__":
    unittest.main()
