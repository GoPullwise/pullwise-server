from __future__ import annotations

import os
import tempfile
import unittest
import json

from pullwise_server.product_analysis_input import prepare_source_questions
from pullwise_server.product_jobs import ProductJobExecutor, ProductJobScheduler, TrustedTrigger
from pullwise_server.product_store import ProductStore


class ProductAnalysisInputTest(unittest.TestCase):
    def source(self, source_type, content, *, facts=None, completeness="complete"):
        return {"id": "source-1", "sourceVersion": "version-1", "sourceType": source_type,
                "content": content, "sourceFacts": facts or {}, "completeness": completeness}

    def test_pr_body_has_speaker_context_and_exact_evidence_bindings(self):
        source = self.source("pr_review_comment", {"body": "Please add a timeout test."},
            facts={"author": {"githubId": "8"}, "pullAuthor": {"githubId": "9"}})
        prepared = prepare_source_questions(source)
        self.assertEqual(prepared["questionVersion"], "pr-followup/v3")
        self.assertEqual(len(prepared["questions"]), 6)
        self.assertEqual(prepared["state"]["segments"][0]["speakerRole"], "reviewer")
        self.assertEqual(prepared["bindings"]["s0_change_request"]["evidenceIds"],
                         [prepared["evidence"][0]["id"]])
        self.assertEqual(prepared["coverage"]["state"], "complete")
        self.assertEqual(prepared, prepare_source_questions(source))

    def test_ci_windows_keep_visible_text_and_partial_coverage(self):
        source = self.source("ci_failure", {"windows": [{"windowId": "w1", "stage": "test",
            "text": "Authorization: secret\nAssertionError: failed", "coverage": "partial"}]},
            completeness="partial")
        prepared = prepare_source_questions(source)
        self.assertEqual(prepared["questionVersion"], "ci-triage/v2")
        self.assertEqual(len(prepared["questions"]), 9)
        self.assertNotIn("secret", prepared["state"]["windows"][0]["text"])
        self.assertEqual(prepared["bindings"]["w0_assertion_failure"]["windowId"], "w1")
        self.assertEqual(prepared["coverage"]["state"], "partial")

    def test_updates_uses_saved_interests_and_reports_omitted_units(self):
        source = self.source("release", {"body": "# OAuth\nMigrate tokens.\n# Other\nUnrelated change."})
        prepared = prepare_source_questions(source, interests=["OAuth"], max_units=1)
        self.assertEqual(prepared["questionVersion"], "updates-filter/v3")
        self.assertEqual(prepared["state"]["interests"], ["OAuth"])
        self.assertEqual(len(prepared["questions"]), 5)
        self.assertEqual(prepared["coverage"]["state"], "partial")
        self.assertEqual(prepared["coverage"]["omittedUnits"], 1)
        self.assertEqual(prepared["bindings"]["u0_relevance"]["changeUnitId"], "cu_0")

    def test_missing_material_or_identity_never_builds_questions(self):
        for source in (self.source("pr_comment", {"body": ""}),
                       self.source("ci_failure", {"windows": []}),
                       self.source("release", {"body": ""}),
                       self.source("release", {"body": "change"}) | {"sourceVersion": ""}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                prepare_source_questions(source, interests=["OAuth"])

    def test_claimed_job_prepares_saved_release_with_saved_watch_interests(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(os.path.join(directory, "product.sqlite3"))
            store.initialize()
            watch = store.create_watch(owner_id="usr_1", target_repository_id=None,
                upstream_repository_id="upstream-1", billing_owner_id="usr_1",
                interests=["OAuth"], enabled=True, analysis_enabled=True)
            source = store.upsert_source_snapshot(source_id="release-1", source_type="release",
                external_key="github:release:1", repository_id="upstream-1",
                content={"body": "# OAuth\nMigrate tokens."}, source_facts={"releaseId": "1"},
                source_url="https://github.com/acme/upstream/releases/tag/v1",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_000)
            store.set_source_context(source_id=source["id"], context_id="watch:release-1",
                watch_id=watch["id"], billing_owner_id="usr_1", context_version=1,
                configuration_revision=1, authorization_revision=1,
                authorization_valid_until=1_900_000_000, accessible=True,
                analysis_enabled=True)
            reservation = store.reserve_processing_unit(charge_key="release-1",
                billing_owner_id="usr_1", period="cycle:1800000000", module="updates", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="release-1:watch:release-1",
                source_id=source["id"], context_id="watch:release-1",
                reservation_id=reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            executor = ProductJobExecutor(store)
            claim = executor.claim_next_analysis(now=1_800_000_001)
            prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
                claim_token=claim["claimToken"], now=1_800_000_002)
            self.assertEqual(prepared["state"]["interests"], ["OAuth"])
            self.assertEqual(prepared["source"]["sourceVersion"], source["sourceVersion"])
            self.assertEqual(prepared["context"]["billingOwnerId"], "usr_1")
            with self.assertRaisesRegex(ValueError, "JOB_CLAIM_LOST"):
                executor.prepare_claimed_analysis(job_id=claim["id"],
                    claim_token="wrong-token", now=1_800_000_002)
            answers = {}
            for key, question in prepared["questions"].items():
                options = list(question["criteria"])
                selected = "relevant" if key.endswith("relevance") else (
                    "present" if key.endswith("migration_stated") else "absent")
                answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected) for option in options},
                    "confidence": 1.0}
            raw_body = json.dumps({"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 2}}).encode()
            with self.assertRaisesRegex(ValueError, "PROVIDER_ATTEMPT_NOT_ADMITTED"):
                executor.publish_claimed_release_response(job_id=claim["id"],
                    claim_token=claim["claimToken"], raw_body=raw_body, observed_at=1_800_000_002)
            executor.admit_claimed_provider_attempt(job_id=claim["id"],
                claim_token=claim["claimToken"], input_key=prepared["inputHash"],
                now=1_800_000_002, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            result = executor.publish_claimed_release_response(job_id=claim["id"],
                claim_token=claim["claimToken"], raw_body=raw_body, observed_at=1_800_000_002)
            self.assertEqual(result["assessment"]["status"], "succeeded")
            self.assertEqual(result["item"]["unit"],
                {"type": "update_release", "externalId": source["id"]})
            self.assertEqual(store.list_items_for_billing_owner("usr_1")[0]["actionTypes"],
                ["review_update"])
            visible = store.list_sources_for_billing_owner("usr_1", include_content=True)
            self.assertEqual(visible[0]["contexts"][0]["relevance"], "relevant")
            self.assertEqual(visible[0]["contexts"][0]["updateSignals"]["migration_stated"], "present")
            self.assertEqual(store.get_background_job(claim["id"])["status"], "succeeded")
            item_id = result["item"]["id"]
            store.patch_item_handling(item_id=item_id,
                item_version=result["item"]["itemVersion"],
                expected_revision=result["item"]["revision"], actor_id="usr_1",
                disposition="done", note="Migration handled")
            store.upsert_source_snapshot(source_id="release-1", source_type="release",
                external_key="github:release:1", repository_id="upstream-1",
                content={"body": "# Documentation\nUnrelated content."},
                source_facts={"releaseId": "1"},
                source_url="https://github.com/acme/upstream/releases/tag/v1",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_003)
            second_reservation = store.reserve_processing_unit(charge_key="release-1:edit",
                billing_owner_id="usr_1", period="cycle:1800000000", module="updates", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="release-1:watch:release-1",
                source_id=source["id"], context_id="watch:release-1",
                reservation_id=second_reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            second_claim = executor.claim_next_analysis(now=1_800_000_004)
            second_prepared = executor.prepare_claimed_analysis(job_id=second_claim["id"],
                claim_token=second_claim["claimToken"], now=1_800_000_005)
            second_answers = {}
            for key, question in second_prepared["questions"].items():
                selected = "not_relevant" if key.endswith("relevance") else "absent"
                second_answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=second_claim["id"],
                claim_token=second_claim["claimToken"], input_key=second_prepared["inputHash"],
                now=1_800_000_005, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            closed = executor.publish_claimed_release_response(job_id=second_claim["id"],
                claim_token=second_claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": second_answers, "usage": {"input_tokens": 10, "output_tokens": 2}}).encode(),
                observed_at=1_800_000_005)
            self.assertEqual(closed["item"]["id"], item_id)
            latest = store.list_items_for_billing_owner("usr_1")[0]
            self.assertEqual(latest["lifecycle"], "superseded")
            self.assertEqual(latest["actionTypes"], [])
            history = store.list_items_for_billing_owner("usr_1", include_history=True)[0]
            self.assertEqual(history["handlingHistory"][0]["note"], "Migration handled")
            unrelated = store.upsert_source_snapshot(source_id="release-2",
                source_type="release", external_key="github:release:2",
                repository_id="upstream-1", content={"body": "# Docs\nFormatting only."},
                source_facts={"releaseId": "2"},
                source_url="https://github.com/acme/upstream/releases/tag/v2",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_006)
            store.set_source_context(source_id=unrelated["id"], context_id="watch:release-2",
                watch_id=watch["id"], billing_owner_id="usr_1", context_version=1,
                configuration_revision=1, authorization_revision=1,
                authorization_valid_until=1_900_000_000, accessible=True,
                analysis_enabled=True)
            third_reservation = store.reserve_processing_unit(charge_key="release-2",
                billing_owner_id="usr_1", period="cycle:1800000000", module="updates", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="release-2:watch:release-2",
                source_id=unrelated["id"], context_id="watch:release-2",
                reservation_id=third_reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            third_claim = executor.claim_next_analysis(now=1_800_000_007)
            third_prepared = executor.prepare_claimed_analysis(job_id=third_claim["id"],
                claim_token=third_claim["claimToken"], now=1_800_000_008)
            third_answers = {}
            for key, question in third_prepared["questions"].items():
                selected = "not_relevant" if key.endswith("relevance") else "absent"
                third_answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=third_claim["id"],
                claim_token=third_claim["claimToken"], input_key=third_prepared["inputHash"],
                now=1_800_000_008, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            ignored = executor.publish_claimed_release_response(job_id=third_claim["id"],
                claim_token=third_claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": third_answers, "usage": {"input_tokens": 8, "output_tokens": 2}}).encode(),
                observed_at=1_800_000_008)
            self.assertIsNone(ignored["item"])
            self.assertEqual(len(store.list_items_for_billing_owner("usr_1")), 1)

    def test_claimed_ci_response_updates_saved_item_symptoms(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(os.path.join(directory, "product.sqlite3"))
            store.initialize()
            source = store.upsert_source_snapshot(source_id="ci-1", source_type="ci_failure",
                external_key="github:ci:1", repository_id="repo-1",
                content={"windows": [{"windowId": "w1", "stage": "test",
                    "text": "AssertionError: expected 2", "coverage": "partial"}]},
                source_facts={"runId": "1", "jobId": "2", "runAttempt": 1,
                    "windows": [{"windowId": "w1", "symptoms": [], "evidenceIds": []}]},
                source_url="https://github.com/acme/repo/actions/runs/1",
                processing_mode="model", completeness="partial", lifecycle="active",
                observed_at=1_800_000_000)
            item = store.create_item(context_id="repo:ci", unit_type="ci_job", unit_key="github:ci:1")
            store.set_source_context(source_id=source["id"], context_id="repo:ci",
                billing_owner_id="usr_1", item_id=item["id"], context_version=1,
                configuration_revision=1, authorization_revision=1,
                authorization_valid_until=1_900_000_000, accessible=True,
                analysis_enabled=True)
            dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                          "sourceRevision": source["sourceRevision"]}
            fence = {"sourceId": source["id"], "contextId": "repo:ci", "contextVersion": 1,
                "configurationRevision": 1, "authorizationRevision": 1}
            store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
                sources=[dependency], context_fences=[fence],
                snapshot={"module": "ci", "actionTypes": ["investigate_failure"],
                    "attentionState": "needs_action", "lifecycle": "active", "evidence": [],
                    "sourceFacts": {"windows": [{"windowId": "w1", "symptoms": [], "evidenceIds": []}]}},
                observed_at=1_800_000_000)
            reservation = store.reserve_processing_unit(charge_key="ci-1",
                billing_owner_id="usr_1", period="cycle:1800000000", module="ci", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="ci-1:repo:ci",
                source_id=source["id"], context_id="repo:ci",
                reservation_id=reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            executor = ProductJobExecutor(store)
            claim = executor.claim_next_analysis(now=1_800_000_001)
            prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
                claim_token=claim["claimToken"], now=1_800_000_002)
            answers = {}
            for key, question in prepared["questions"].items():
                selected = "present" if key.endswith("assertion_failure") else "absent"
                answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=claim["id"],
                claim_token=claim["claimToken"], input_key=prepared["inputHash"],
                now=1_800_000_002, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            result = executor.publish_claimed_ci_response(job_id=claim["id"],
                claim_token=claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": answers, "usage": {"input_tokens": 9, "output_tokens": 3}}).encode(),
                observed_at=1_800_000_002)
            self.assertEqual(result["item"]["itemVersion"], 2)
            visible = store.list_items_for_billing_owner("usr_1", item_id=item["id"])[0]
            self.assertEqual(visible["sourceFacts"]["windows"][0]["symptoms"],
                ["assertion_failure"])
            self.assertEqual(visible["sourceFacts"]["autoLabelPolicyVersion"], "ci-auto-label/v1")

    def test_claimed_review_thread_response_creates_followup_item(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(os.path.join(directory, "product.sqlite3"))
            store.initialize()
            source = store.upsert_source_snapshot(source_id="comment-1",
                source_type="pr_review_comment", external_key="github:pr_review_comment:1",
                repository_id="repo-1", content={"body": "Please add a timeout test."},
                source_facts={"commentId": "1", "pullNumber": 42, "threadId": "thread-1",
                    "associationVerified": True, "threadCoverage": "complete",
                    "threadCommentIds": ["1"], "isResolved": False,
                    "author": {"githubId": "reviewer"},
                    "pullAuthor": {"githubId": "author"}},
                source_url="https://github.com/acme/repo/pull/42",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_000)
            store.set_source_context(source_id=source["id"], context_id="repo:pr",
                billing_owner_id="usr_1", context_version=1, configuration_revision=1,
                authorization_revision=1, authorization_valid_until=1_900_000_000,
                accessible=True, analysis_enabled=True)
            reservation = store.reserve_processing_unit(charge_key="comment-1",
                billing_owner_id="usr_1", period="cycle:1800000000", module="pr", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="comment-1:repo:pr",
                source_id=source["id"], context_id="repo:pr",
                reservation_id=reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            executor = ProductJobExecutor(store)
            claim = executor.claim_next_analysis(now=1_800_000_001)
            prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
                claim_token=claim["claimToken"], now=1_800_000_002)
            answers = {}
            for key, question in prepared["questions"].items():
                selected = "present" if key.endswith("change_request") else (
                    "not_stated" if key.endswith("blocking_language") else "absent")
                answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=claim["id"],
                claim_token=claim["claimToken"], input_key=prepared["inputHash"],
                now=1_800_000_002, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            result = executor.publish_claimed_pr_thread_response(job_id=claim["id"],
                claim_token=claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": answers, "usage": {"input_tokens": 12, "output_tokens": 2}}).encode(),
                observed_at=1_800_000_002)
            self.assertIsNotNone(result["item"])
            visible = store.list_items_for_billing_owner("usr_1")[0]
            self.assertEqual(visible["actionTypes"], ["change_requested"])
            self.assertEqual(visible["unit"]["type"], "pr_thread")
            reply = store.upsert_source_snapshot(source_id="comment-2",
                source_type="pr_review_comment", external_key="github:pr_review_comment:2",
                repository_id="repo-1", content={"body": "Why is this timeout needed?"},
                source_facts={"commentId": "2", "pullNumber": 42, "threadId": "thread-1",
                    "associationVerified": True, "threadCoverage": "complete",
                    "threadCommentIds": ["1", "2"], "inReplyToId": "1",
                    "isResolved": False, "author": {"githubId": "reviewer"},
                    "pullAuthor": {"githubId": "author"}},
                source_url="https://github.com/acme/repo/pull/42",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_003)
            store.set_source_context(source_id=reply["id"], context_id="repo:pr",
                billing_owner_id="usr_1", context_version=1, configuration_revision=1,
                authorization_revision=1, authorization_valid_until=1_900_000_000,
                accessible=True, analysis_enabled=True)
            next_reservation = store.reserve_processing_unit(charge_key="comment-2",
                billing_owner_id="usr_1", period="cycle:1800000000", module="pr", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="comment-2:repo:pr",
                source_id=reply["id"], context_id="repo:pr",
                reservation_id=next_reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            next_claim = executor.claim_next_analysis(now=1_800_000_004)
            next_prepared = executor.prepare_claimed_analysis(job_id=next_claim["id"],
                claim_token=next_claim["claimToken"], now=1_800_000_005)
            self.assertEqual([row["sourceId"] for row in next_prepared["dependencies"]],
                [source["id"]])
            reply_answers = {}
            for key, question in next_prepared["questions"].items():
                selected = "present" if key.endswith("question") else (
                    "not_stated" if key.endswith("blocking_language") else "absent")
                reply_answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=next_claim["id"],
                claim_token=next_claim["claimToken"], input_key=next_prepared["inputHash"],
                now=1_800_000_005, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            executor.publish_claimed_pr_thread_response(job_id=next_claim["id"],
                claim_token=next_claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": reply_answers, "usage": {"input_tokens": 12, "output_tokens": 2}}).encode(),
                observed_at=1_800_000_005)
            self.assertEqual(set(store.list_items_for_billing_owner("usr_1")[0]["actionTypes"]),
                {"change_requested", "reply_needed"})

    def test_claimed_pr_comment_creates_then_supersedes_followup_item(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(os.path.join(directory, "product.sqlite3"))
            store.initialize()
            facts = {"commentId": "7", "pullNumber": 42,
                "author": {"githubId": "reviewer"},
                "pullAuthor": {"githubId": "author"}}
            def save_comment(body, observed_at, *, completeness="complete"):
                return store.upsert_source_snapshot(source_id="comment-7",
                    source_type="pr_comment", external_key="github:pr_comment:7",
                    repository_id="repo-1", content={"body": body}, source_facts=facts,
                    source_url="https://github.com/acme/repo/pull/42#issuecomment-7",
                    processing_mode="model", completeness=completeness, lifecycle="active",
                    observed_at=observed_at)
            source = save_comment("Please add a timeout test.", 1_800_000_000)
            store.set_source_context(source_id=source["id"], context_id="repo:pr",
                billing_owner_id="usr_1", context_version=1, configuration_revision=1,
                authorization_revision=1, authorization_valid_until=1_900_000_000,
                accessible=True, analysis_enabled=True)
            executor = ProductJobExecutor(store)
            def process_comment(charge_key, now, *, requested, uncertain=False):
                reservation = store.reserve_processing_unit(charge_key=charge_key,
                    billing_owner_id="usr_1", period="cycle:1800000000", module="pr", limit=10)
                ProductJobScheduler(store).schedule_analysis(source_context_key="comment-7:repo:pr",
                    source_id=source["id"], context_id="repo:pr",
                    reservation_id=reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
                claim = executor.claim_next_analysis(now=now)
                prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
                    claim_token=claim["claimToken"], now=now + 1)
                answers = {}
                for key, question in prepared["questions"].items():
                    selected = ("unclear" if uncertain and key.endswith("change_request") else
                        "present" if requested and key.endswith("change_request") else
                        "not_stated" if key.endswith("blocking_language") else "absent")
                    answers[key] = {"type": "choice", "choice": selected,
                        "probabilities": {option: float(option == selected)
                            for option in question["criteria"]}, "confidence": 1.0}
                executor.admit_claimed_provider_attempt(job_id=claim["id"],
                    claim_token=claim["claimToken"], input_key=prepared["inputHash"],
                    now=now + 1, owner_monthly_limit=10, global_monthly_limit=10,
                    owner_rolling_limit=10, global_rolling_limit=10)
                return executor.publish_claimed_pr_comment_response(job_id=claim["id"],
                    claim_token=claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                        "answers": answers, "usage": {"input_tokens": 9, "output_tokens": 2}}).encode(),
                    observed_at=now + 1)
            first = process_comment("comment-7:first", 1_800_000_001, requested=True)
            self.assertEqual(first["item"]["unit"]["type"], "pr_comment")
            self.assertEqual(store.list_items_for_billing_owner("usr_1")[0]["actionTypes"],
                ["change_requested"])
            store.patch_item_handling(item_id=first["item"]["id"],
                item_version=first["item"]["itemVersion"],
                expected_revision=first["item"]["revision"], actor_id="usr_1",
                disposition="done", note="Addressed")
            save_comment("Maybe this is resolved?", 1_800_000_003, completeness="partial")
            uncertain = process_comment("comment-7:uncertain", 1_800_000_004,
                requested=False, uncertain=True)
            self.assertEqual(uncertain["item"]["id"], first["item"]["id"])
            pending = store.list_items_for_billing_owner("usr_1")[0]
            self.assertEqual(pending["attentionState"], "needs_confirmation")
            self.assertEqual(pending["actionTypes"], ["change_requested"])
            save_comment("Thanks, that's all.", 1_800_000_006)
            second = process_comment("comment-7:edit", 1_800_000_007, requested=False)
            self.assertEqual(second["item"]["id"], first["item"]["id"])
            latest = store.list_items_for_billing_owner("usr_1", include_history=True)[0]
            self.assertEqual(latest["lifecycle"], "superseded")
            self.assertEqual(latest["actionTypes"], [])
            self.assertEqual(latest["handlingHistory"][0]["note"], "Addressed")

    def test_claimed_formal_review_enriches_rule_item_without_erasing_rule_action(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(os.path.join(directory, "product.sqlite3"))
            store.initialize()
            facts = {"reviewId": "5", "pullNumber": 42,
                "reviewState": "CHANGES_REQUESTED", "formalReviewStatus": "effective",
                "reviewer": {"githubId": "reviewer"},
                "pullAuthor": {"githubId": "author"}}
            source = store.upsert_source_snapshot(source_id="review-5",
                source_type="pr_review_body", external_key="github:pr_review_body:5",
                repository_id="repo-1", content={"body": "Why was the timeout removed?"},
                source_facts=facts, source_url="https://github.com/acme/repo/pull/42#review-5",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_000)
            item = store.create_item(context_id="repo:pr", unit_type="pr_review_body",
                unit_key="github:pr_review_body:5")
            store.set_source_context(source_id=source["id"], context_id="repo:pr",
                billing_owner_id="usr_1", item_id=item["id"], context_version=1,
                configuration_revision=1, authorization_revision=1,
                authorization_valid_until=1_900_000_000, accessible=True,
                analysis_enabled=True)
            dependency = {"sourceId": source["id"], "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"]}
            fence = {"sourceId": source["id"], "contextId": "repo:pr",
                "contextVersion": 1, "configurationRevision": 1, "authorizationRevision": 1}
            store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
                sources=[dependency], context_fences=[fence],
                snapshot={"module": "pr", "repositoryId": "repo-1", "watchId": None,
                    "title": "Changes requested", "sourceUrl": "https://github.com/acme/repo/pull/42",
                    "sourceFacts": facts, "actionTypes": ["change_requested"],
                    "attentionState": "needs_action", "lifecycle": "active",
                    "nextActors": [{"kind": "user", "githubId": "author"}],
                    "evidence": [{"id": "review-5:body", "sourceId": source["id"],
                        "sourceVersion": source["sourceVersion"],
                        "text": "Why was the timeout removed?", "actionTypes": ["change_requested"]}]},
                observed_at=1_800_000_000)
            reservation = store.reserve_processing_unit(charge_key="review-5",
                billing_owner_id="usr_1", period="cycle:1800000000", module="pr", limit=10)
            ProductJobScheduler(store).schedule_analysis(source_context_key="review-5:repo:pr",
                source_id=source["id"], context_id="repo:pr",
                reservation_id=reservation["reservationId"], trigger=TrustedTrigger.GITHUB_EVENT)
            executor = ProductJobExecutor(store)
            claim = executor.claim_next_analysis(now=1_800_000_001)
            prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
                claim_token=claim["claimToken"], now=1_800_000_002)
            answers = {}
            for key, question in prepared["questions"].items():
                selected = "present" if key.endswith("question") else (
                    "not_stated" if key.endswith("blocking_language") else "absent")
                answers[key] = {"type": "choice", "choice": selected,
                    "probabilities": {option: float(option == selected)
                        for option in question["criteria"]}, "confidence": 1.0}
            executor.admit_claimed_provider_attempt(job_id=claim["id"],
                claim_token=claim["claimToken"], input_key=prepared["inputHash"],
                now=1_800_000_002, owner_monthly_limit=10, global_monthly_limit=10,
                owner_rolling_limit=10, global_rolling_limit=10)
            result = executor.publish_claimed_pr_review_response(job_id=claim["id"],
                claim_token=claim["claimToken"], raw_body=json.dumps({"model": "jev-1.13.0",
                    "answers": answers, "usage": {"input_tokens": 9, "output_tokens": 2}}).encode(),
                observed_at=1_800_000_002)
            self.assertEqual(result["item"]["itemVersion"], 2)
            visible = store.list_items_for_billing_owner("usr_1")[0]
            self.assertEqual(set(visible["actionTypes"]),
                {"change_requested", "reply_needed"})
            self.assertEqual(len(visible["assessments"]), 1)


if __name__ == "__main__":
    unittest.main()
