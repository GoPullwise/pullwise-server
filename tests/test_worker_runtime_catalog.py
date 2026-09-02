from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_worker_admin_routes import RouteHarness, reset_state
from tests.test_worker_pull_routes import protocol_artifact_manifest, protocol_summary, upload_protocol_artifacts_for_test


RUNTIME_CATALOG = {
    "schema_id": "pullwise-pi-runtime-catalog/v1",
    "credentials": [
        {
            "credential_id": "anthropic_primary",
            "label": "Anthropic primary",
            "provider": "anthropic",
            "auth_type": "api_key",
            "models": [
                {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
            ],
        },
        {
            "credential_id": "openai_team",
            "label": "OpenAI team",
            "provider": "openai",
            "auth_type": "api_key",
            "models": [
                {"id": "gpt-5.1", "name": "GPT-5.1"},
            ],
        },
    ],
}

OPENAI_ONLY_CATALOG = {
    "schema_id": "pullwise-pi-runtime-catalog/v1",
    "credentials": [RUNTIME_CATALOG["credentials"][1]],
}


class WorkerRuntimeCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_ADMIN_USER_IDS": "usr_admin",
                "PULLWISE_ADMIN_EMAILS": "admin@example.com",
                "PULLWISE_SERVER_URL": "http://localhost:8080",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
        self.admin_cookie = "pw_session=ses_admin"
        self.user_cookie = "pw_session=ses_user"

    def create_worker(self) -> tuple[str, str]:
        handler = RouteHarness(
            "/admin/workers",
            {"name": "Pi worker", "region": "us-east"},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(handler, "POST")
        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertTrue(handler.payload["configuration"]["secretsStoredOnWorker"])
        self.assertEqual(handler.payload["worker"]["provider"], "unconfigured")
        self.assertEqual(handler.payload["worker"]["providerChain"], [])
        self.assertIn("PULLWISE_PI_PROFILE_ROOT", handler.payload["suggested_env"])
        self.assertIn("PULLWISE_WORKER_STATE_ROOT", handler.payload["suggested_env"])
        self.assertFalse(
            any(key.startswith("PULLWISE_CODEX") for key in handler.payload["suggested_env"])
        )
        self.assertEqual(
            [item["key"] for item in handler.payload["configuration_commands"]],
            ["add_runtime_profile", "sync_runtime_catalog"],
        )
        self.assertNotIn(handler.payload["worker_token"], json.dumps(handler.payload["configuration_commands"]))
        return handler.payload["worker_id"], handler.payload["worker_token"]

    def register(self, worker_id: str, token: str, catalog: dict) -> RouteHarness:
        handler = RouteHarness(
            "/v1/workers/register",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker": {
                    "worker_id": worker_id,
                    "worker_group": "default",
                    "worker_version": "0.10.24",
                    "hostname": "pi-host",
                    "concurrency": {
                        "max_active_jobs": 1,
                        "maintains_local_queue": False,
                        "prefetch_jobs": False,
                    },
                    "platform": {"os": "linux", "arch": "x86_64"},
                    "capabilities": {
                        "pi_agent_session": True,
                        "isolated_pi_profiles": True,
                        "full_repo_scan": True,
                        "progress_events": True,
                        "cancellation": True,
                        "intent_test_validation": True,
                        "max_active_jobs": 1,
                    },
                    "runtime_catalog": catalog,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def heartbeat(self, worker_id: str, token: str, catalog: dict) -> RouteHarness:
        handler = RouteHarness(
            f"/v1/workers/{worker_id}/heartbeat",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker_id": worker_id,
                "status": "idle",
                "active_run_id": None,
                "concurrency": {
                    "max_active_jobs": 1,
                    "active_jobs": 0,
                    "available_job_slots": 1,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "agent_session": {"status": "idle", "transport": "embedded", "active_session_id": None},
                "provider": "anthropic",
                "providerChain": ["anthropic", "openai"],
                "readyProviders": ["anthropic", "openai"],
                "doctor_status": "ok",
                "runtime_catalog": catalog,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def test_registration_catalog_and_public_union_are_persisted(self) -> None:
        worker_id, token = self.create_worker()
        registration = self.register(worker_id, token, RUNTIME_CATALOG)
        self.assertEqual(registration.status, HTTPStatus.OK, registration.payload)
        heartbeat = self.heartbeat(worker_id, token, RUNTIME_CATALOG)
        self.assertEqual(heartbeat.status, HTTPStatus.OK, heartbeat.payload)

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")
        worker = detail.payload["worker"]
        self.assertEqual(worker["runtimeCatalog"]["schemaId"], "pullwise-pi-runtime-catalog/v1")
        self.assertNotIn("runtimeSelection", worker)
        self.assertEqual(
            [(item["provider"], item["model"]) for item in worker["availableModels"]],
            [("anthropic", "claude-sonnet-4-5"), ("openai", "gpt-5.1")],
        )

        stored = db.get_worker(worker_id)
        self.assertIsNone(stored["selected_credential_id"])
        self.assertIsNone(stored["selected_provider"])
        self.assertIsNone(stored["selected_model"])
        self.assertEqual(json.loads(stored["runtime_catalog"]), RUNTIME_CATALOG)

        status = RouteHarness("/status/system", cookie=self.user_cookie)
        app.PullwiseHandler.route(status, "GET")
        self.assertEqual(
            status.payload["availableReviewModels"],
            [
                {"provider": "anthropic", "model": "claude-sonnet-4-5"},
                {"provider": "openai", "model": "gpt-5.1"},
            ],
        )

    def test_selection_must_exist_in_catalog_and_catalog_rejects_secret_fields(self) -> None:
        worker_id, token = self.create_worker()
        secret_catalog = json.loads(json.dumps(RUNTIME_CATALOG))
        secret_catalog["credentials"][0]["api_key"] = "secret"
        rejected = self.register(worker_id, token, secret_catalog)
        self.assertEqual(rejected.status, HTTPStatus.BAD_REQUEST)

        accepted = self.register(worker_id, token, RUNTIME_CATALOG)
        self.assertEqual(accepted.status, HTTPStatus.OK)
        invalid = RouteHarness(
            f"/admin/workers/{worker_id}",
            {
                "runtimeSelection": {
                    "credentialId": "anthropic_primary",
                    "provider": "anthropic",
                    "model": "missing-model",
                }
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(invalid, "PATCH")
        self.assertEqual(invalid.status, HTTPStatus.BAD_REQUEST)

    def test_lease_resolves_the_plan_runtime_from_the_worker_catalog(self) -> None:
        worker_id, token = self.create_worker()
        self.assertEqual(self.register(worker_id, token, RUNTIME_CATALOG).status, HTTPStatus.OK)
        self.assertEqual(self.heartbeat(worker_id, token, RUNTIME_CATALOG).status, HTTPStatus.OK)

        plan_update = RouteHarness(
            "/admin/subscription-plans/agent-configs/free",
            {
                "provider": "openai",
                "model": "gpt-5.1",
                "thinkingLevel": "high",
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(plan_update, "PATCH")
        self.assertEqual(plan_update.status, HTTPStatus.OK, plan_update.payload)

        timestamp = app.now()
        scan = {
            "id": "sc_plan_runtime",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {},
        }
        app.SCANS = [scan]
        created = app.create_scan_job_for_scan(scan)
        self.assertEqual(created["runtime_provider"], "openai")
        self.assertEqual(created["runtime_model"], "gpt-5.1")
        self.assertEqual(created["runtime_thinking_level"], "high")
        changed_policy = RouteHarness(
            "/admin/subscription-plans/agent-configs/free",
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "thinkingLevel": "low",
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(changed_policy, "PATCH")
        self.assertEqual(changed_policy.status, HTTPStatus.OK)

        lease = RouteHarness(
            f"/v1/workers/{worker_id}/lease",
            {
                "protocol_version": "review-worker-protocol/v1",
                "capacity": {
                    "available_job_slots": 1,
                    "active_jobs": 0,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "capabilities": {
                    "full_repo_scan": True,
                    "pi_agent_session": True,
                    "isolated_pi_profiles": True,
                    "progress_events": True,
                    "cancellation": True,
                    "intent_test_validation": True,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(lease, "POST")

        self.assertEqual(lease.status, HTTPStatus.OK, lease.payload)
        self.assertEqual(
            lease.payload["job"]["runtime_selection"],
            {
                "credential_id": "openai_team",
                "provider": "openai",
                "model": "gpt-5.1",
                "thinking_level": "high",
            },
        )
        self.assertEqual(lease.payload["job"]["model_profile"]["provider"], "openai")
        self.assertEqual(lease.payload["job"]["model_profile"]["default_model"], "gpt-5.1")
        self.assertEqual(lease.payload["job"]["model_profile"]["thinking_level"], "high")

    def test_worker_skips_an_older_incompatible_job_and_claims_the_oldest_compatible_job(self) -> None:
        worker_id, token = self.create_worker()
        self.assertEqual(self.register(worker_id, token, OPENAI_ONLY_CATALOG).status, HTTPStatus.OK)
        self.assertEqual(self.heartbeat(worker_id, token, OPENAI_ONLY_CATALOG).status, HTTPStatus.OK)

        def set_policy(provider: str, model: str, thinking_level: str) -> None:
            update = RouteHarness(
                "/admin/subscription-plans/agent-configs/free",
                {
                    "provider": provider,
                    "model": model,
                    "thinkingLevel": thinking_level,
                },
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(update, "PATCH")
            self.assertEqual(update.status, HTTPStatus.OK, update.payload)

        def queue(scan_id: str, created_at: int) -> dict:
            scan = {
                "id": scan_id,
                "repo": f"acme/{scan_id}",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_user",
                "createdAt": created_at,
                "queuedAt": created_at,
                "progress": 0,
                "phase": None,
                "issues": {},
            }
            app.SCANS.append(scan)
            return app.create_scan_job_for_scan(scan)

        set_policy("anthropic", "claude-sonnet-4-5", "low")
        incompatible = queue("sc_incompatible_older", 100)
        set_policy("openai", "gpt-5.1", "medium")
        compatible = queue("sc_compatible_newer", 200)

        lease = RouteHarness(
            f"/v1/workers/{worker_id}/lease",
            {
                "protocol_version": "review-worker-protocol/v1",
                "capacity": {
                    "available_job_slots": 1,
                    "active_jobs": 0,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "capabilities": {
                    "full_repo_scan": True,
                    "pi_agent_session": True,
                    "isolated_pi_profiles": True,
                    "progress_events": True,
                    "cancellation": True,
                    "intent_test_validation": True,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(lease, "POST")

        self.assertEqual(lease.status, HTTPStatus.OK, lease.payload)
        self.assertEqual(lease.payload["job"]["job_id"], compatible["job_id"])
        self.assertEqual(db.get_scan_job(incompatible["job_id"])["status"], "queued")

    def test_per_worker_runtime_selection_is_rejected_and_plan_policy_is_used(self) -> None:
        worker_id, token = self.create_worker()
        self.assertEqual(self.register(worker_id, token, RUNTIME_CATALOG).status, HTTPStatus.OK)
        self.assertEqual(self.heartbeat(worker_id, token, RUNTIME_CATALOG).status, HTTPStatus.OK)
        selection = {
            "credentialId": "openai_team",
            "provider": "openai",
            "model": "gpt-5.1",
        }
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"runtimeSelection": selection},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("retired", update.payload["message"])
        plan_update = RouteHarness(
            "/admin/subscription-plans/agent-configs/free",
            {
                "provider": "openai",
                "model": "gpt-5.1",
                "thinkingLevel": "low",
            },
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(plan_update, "PATCH")
        self.assertEqual(plan_update.status, HTTPStatus.OK)

        timestamp = app.now()
        scan = {
            "id": "sc_pi_runtime",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        lease = RouteHarness(
            f"/v1/workers/{worker_id}/lease",
            {
                "protocol_version": "review-worker-protocol/v1",
                "capacity": {
                    "available_job_slots": 1,
                    "active_jobs": 0,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "capabilities": {
                    "full_repo_scan": True,
                    "pi_agent_session": True,
                    "isolated_pi_profiles": True,
                    "progress_events": True,
                    "cancellation": True,
                    "intent_test_validation": True,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(lease, "POST")
        self.assertEqual(lease.status, HTTPStatus.OK, lease.payload)
        self.assertEqual(lease.payload["job"]["runtime_selection"], {
            "credential_id": "openai_team",
            "provider": "openai",
            "model": "gpt-5.1",
            "thinking_level": "low",
        })
        self.assertEqual(lease.payload["job"]["model_profile"]["provider"], "openai")
        self.assertEqual(lease.payload["job"]["model_profile"]["default_model"], "gpt-5.1")

    def test_server_accepts_pi_engine_on_the_existing_v1_result_envelope(self) -> None:
        worker_id, _token = self.create_worker()
        timestamp = app.now()
        scan = {
            "id": "sc_pi_result",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abcdef1234567890",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        job = db.claim_next_scan_job(worker_id, create_review_run=True)
        self.assertIsNotNone(job)
        run_id = app.scan_job_attempt_run_id(job)
        lease_id = str(job.get("lease_id") or f"lease_{job['job_id']}")
        manifest = protocol_artifact_manifest(run_id, "completed")
        upload_protocol_artifacts_for_test(job, manifest)
        body = {
            "status": "done",
            "attempt_id": f"{worker_id}-{job['attempt']}",
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "reviewWorkerProtocol": {
                "protocol_version": "review-worker-protocol/v1",
                "message_type": "review_run_result",
                "job": {
                    "job_id": job["job_id"],
                    "run_id": run_id,
                    "lease_id": lease_id,
                    "job_type": "repo_review.full_scan",
                },
                "worker": {
                    "worker_id": worker_id,
                    "worker_version": "0.10.24",
                    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False},
                    "engine": {"type": "pi_agent_session", "app_server_transport": "embedded"},
                },
                "execution": {"status": "completed", "review_mode": "full_repo"},
                "progress_final": {
                    "overall_percent": 100,
                    "current_phase": "submit_result_envelope",
                    "status": "completed",
                    "message": "Pi review completed.",
                },
                "quality_gate": {"status": "pass", "errors": [], "warnings": []},
                "artifact_manifest": manifest,
                "summary": protocol_summary([], "completed"),
            },
        }
        app.validate_review_worker_protocol_artifacts(job, body, status="done")


if __name__ == "__main__":
    unittest.main()
