from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from pullwise_server.jev_deadline_transport import BoundedJevRawTransport
from pullwise_server.product_analysis_runner import ProductAnalysisRunner
from pullwise_server.product_jobs import ProductJobExecutor, ProductJobScheduler, TrustedTrigger
from pullwise_server.product_store import ProductStore


def _bounded_release_provider(*, state, questions, model):
    assert state["interests"] == ["OAuth"] and model == "jev-1.13.0"
    return ProductAnalysisRunnerTest.response(questions)


def _blocked_release_provider(**_kwargs):
    time.sleep(5)
    return b"too late"


class ProductAnalysisRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProductStore(os.path.join(self.directory.name, "product.sqlite3"))
        self.store.initialize()
        self.watch = self.store.create_watch(owner_id="usr_1", target_repository_id=None,
            upstream_repository_id="upstream-1", billing_owner_id="usr_1",
            interests=["OAuth"], enabled=True, analysis_enabled=True)
        self.source = self.store.upsert_source_snapshot(source_id="release-1",
            source_type="release", external_key="github:release:1",
            repository_id="upstream-1", content={"body": "# OAuth\nMigrate tokens."},
            source_facts={"releaseId": "1"},
            source_url="https://github.com/acme/upstream/releases/tag/v1",
            processing_mode="model", completeness="complete", lifecycle="active",
            observed_at=1_800_000_000)
        self.store.set_source_context(source_id=self.source["id"],
            context_id="watch:release-1", watch_id=self.watch["id"],
            billing_owner_id="usr_1", context_version=1,
            configuration_revision=1, authorization_revision=1,
            authorization_valid_until=1_900_000_000, accessible=True,
            analysis_enabled=True)
        self.reservation = self.store.reserve_processing_unit(charge_key="release-1",
            billing_owner_id="usr_1", period="cycle:1800000000", module="updates", limit=10)
        self.job = ProductJobScheduler(self.store).schedule_analysis(
            source_context_key="release-1:watch:release-1", source_id=self.source["id"],
            context_id="watch:release-1", reservation_id=self.reservation["reservationId"],
            trigger=TrustedTrigger.GITHUB_EVENT)

    @staticmethod
    def response(questions: dict) -> bytes:
        answers = {}
        for key, question in questions.items():
            options = list(question["criteria"])
            choice = "relevant" if key.endswith("relevance") else (
                "present" if key.endswith("migration_stated") else "absent")
            answers[key] = {"type": "choice", "choice": choice,
                "probabilities": {option: float(option == choice) for option in options},
                "confidence": 1.0}
        return json.dumps({"model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 2}}).encode()

    def runner(self, invoke_raw, *, global_monthly_limit: int = 10) -> ProductAnalysisRunner:
        return ProductAnalysisRunner(self.store, invoke_raw=invoke_raw,
            clock=lambda: 1_800_000_002, owner_monthly_limit=10,
            global_monthly_limit=global_monthly_limit,
            owner_rolling_limit=10, global_rolling_limit=10)

    def test_claim_admission_provider_and_release_publication_are_one_flow(self) -> None:
        calls = []

        def provider(*, state, questions, model):
            calls.append((state, questions, model))
            return self.response(questions)

        result = self.runner(provider).run_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["interests"], ["OAuth"])
        self.assertEqual(calls[0][2], "jev-1.13.0")
        self.assertEqual(result["assessment"]["status"], "succeeded")
        self.assertEqual(self.store.get_background_job(self.job["id"])["status"], "succeeded")
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1")[0]["actionTypes"],
            ["review_update"])
        self.assertIsNone(self.runner(provider).run_once())
        self.assertEqual(len(calls), 1)

    def test_disabled_budget_never_calls_provider_and_releases_reservation(self) -> None:
        calls = []
        runner = self.runner(lambda **kwargs: calls.append(kwargs), global_monthly_limit=0)
        with self.assertRaisesRegex(ValueError, "PRODUCTION_JEV_DISABLED"):
            runner.run_once()
        self.assertEqual(calls, [])
        self.assertEqual(self.store.get_background_job(self.job["id"])["status"], "failed")
        self.assertEqual(self.store.processing_usage(billing_owner_id="usr_1",
            period="cycle:1800000000")["reserved"], 0)

    def test_reused_attempt_never_sends_a_second_provider_request(self) -> None:
        executor = ProductJobExecutor(self.store)
        claim = executor.claim_next_analysis(now=1_800_000_001)
        prepared = executor.prepare_claimed_analysis(job_id=claim["id"],
            claim_token=claim["claimToken"], now=1_800_000_002)
        executor.admit_claimed_provider_attempt(job_id=claim["id"],
            claim_token=claim["claimToken"], input_key=prepared["inputHash"],
            now=1_800_000_002, owner_monthly_limit=10, global_monthly_limit=10,
            owner_rolling_limit=10, global_rolling_limit=10)
        calls = []
        with self.assertRaisesRegex(ValueError, "PROVIDER_ATTEMPT_ALREADY_USED"):
            self.runner(lambda **kwargs: calls.append(kwargs)).process_claim(claim)
        self.assertEqual(calls, [])

    def test_provider_error_schedules_bounded_retry_without_consuming_unit(self) -> None:
        def provider(**_kwargs):
            raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.runner(provider).run_once()
        self.assertEqual(self.store.get_background_job(self.job["id"])["status"], "retry_wait")
        self.assertEqual(self.store.processing_usage(billing_owner_id="usr_1",
            period="cycle:1800000000")["used"], 0)

    def test_invalid_raw_response_releases_unit_and_marks_job_failed(self) -> None:
        with self.assertRaises(ValueError):
            self.runner(lambda **_kwargs: b"not-json").run_once()
        self.assertEqual(self.store.get_background_job(self.job["id"])["status"], "failed")
        usage = self.store.processing_usage(billing_owner_id="usr_1",
            period="cycle:1800000000")
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_source_edit_during_provider_call_cannot_publish_old_answer(self) -> None:
        def provider(*, questions, **_kwargs):
            self.store.upsert_source_snapshot(source_id="release-1", source_type="release",
                external_key="github:release:1", repository_id="upstream-1",
                content={"body": "# OAuth\nDifferent migration."},
                source_facts={"releaseId": "1"},
                source_url="https://github.com/acme/upstream/releases/tag/v1",
                processing_mode="model", completeness="complete", lifecycle="active",
                observed_at=1_800_000_003)
            return self.response(questions)

        with self.assertRaises(ValueError):
            self.runner(provider).run_once()
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1"), [])
        usage = self.store.processing_usage(billing_owner_id="usr_1",
            period="cycle:1800000000")
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_provider_return_after_claim_expiry_cannot_publish(self) -> None:
        clock = [1_800_000_002]

        def provider(*, questions, **_kwargs):
            clock[0] += 121
            return self.response(questions)

        runner = ProductAnalysisRunner(self.store, invoke_raw=provider,
            clock=lambda: clock[0], owner_monthly_limit=10,
            global_monthly_limit=10, owner_rolling_limit=10,
            global_rolling_limit=10, lease_seconds=120)
        with self.assertRaisesRegex(ValueError, "JOB_CLAIM"):
            runner.run_once()
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1"), [])
        self.assertEqual(self.store.processing_usage(billing_owner_id="usr_1",
            period="cycle:1800000000")["used"], 0)

    def test_spawned_provider_transport_completes_claimed_release(self) -> None:
        bounded = BoundedJevRawTransport(invoke=_bounded_release_provider, total_seconds=2)
        result = self.runner(bounded).run_once()
        self.assertEqual(result["assessment"]["status"], "succeeded")

    def test_spawned_provider_timeout_records_retry_without_item(self) -> None:
        bounded = BoundedJevRawTransport(invoke=_blocked_release_provider, total_seconds=0.4)
        with self.assertRaisesRegex(TimeoutError, "JEV_TOTAL_DEADLINE"):
            self.runner(bounded).run_once()
        self.assertEqual(self.store.get_background_job(self.job["id"])["status"], "retry_wait")
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1"), [])
