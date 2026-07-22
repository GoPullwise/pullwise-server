from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
from pathlib import Path

from pullwise_server._generated_agent_task_contract import (
    package_tuple,
    schema_ids,
    seal_document,
    fixture,
    tool_catalog,
    verify_document_digest,
)
from pullwise_server.agent_first_authority import AgentFirstAuthority, AuthorityError
from pullwise_server.agent_first_authority_migrations import (
    install_current_authority_tables,
)


WORKER_ID = "worker_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TASK_ID = "task_11111111111111111111111111111111"
LEASE_ID = "lease_22222222222222222222222222222222"
NOW = "2026-07-22T12:00:00.000Z"


def policy() -> dict[str, object]:
    document = copy.deepcopy(
        fixture("task_control_golden_effective_policy")["document"]
    )
    document["budgets"]["tool_calls"] = 7
    document.pop("digest")
    return seal_document("effective-execution-policy/v1", document)


class AuthorityHarness:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "authority.sqlite3"
        self.connections: list[sqlite3.Connection] = []
        with self.connect() as connection:
            install_current_authority_tables(connection)
        self.authority = AgentFirstAuthority(self.connect)

    def tearDown(self) -> None:
        for connection in reversed(self.connections):
            connection.close()
        self.connections.clear()
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        self.connections.append(connection)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def register_request(
        self,
        *,
        worker_id: str = WORKER_ID,
        supported_schema_ids: list[str] | None = None,
        tool_catalog_digest: str | None = None,
    ) -> dict[str, object]:
        supported = (
            list(schema_ids())
            if supported_schema_ids is None
            else supported_schema_ids
        )
        catalog_digest = (
            tool_catalog()["catalog_digest"]
            if tool_catalog_digest is None
            else tool_catalog_digest
        )
        return seal_document(
            "agent-worker-register/v1",
            {
                "schema_id": "agent-worker-register/v1",
                "package": package_tuple(),
                "worker_id": worker_id,
                "supported_schema_ids": supported,
                "tool_catalog_digest": catalog_digest,
            },
        )

    def register(self) -> bytes:
        return self.authority.register_worker(self.register_request())

    def accept_request(self, task_id: str = TASK_ID) -> dict[str, object]:
        task_request = copy.deepcopy(
            fixture("task_control_golden_task_request")["document"]
        )
        task_request["task_id"] = task_id
        return {
            "package": package_tuple(),
            "idempotency_key": f"accept:{task_id}",
            "task_request": task_request,
            "effective_policy": policy(),
        }

    def accept(self, task_id: str = TASK_ID) -> bytes:
        return self.authority.accept_current_task(self.accept_request(task_id))

    def claim_request(
        self,
        *,
        idempotency_key: str = "claim:one",
        lease_id: str = LEASE_ID,
        task_id: str = TASK_ID,
        worker_id: str = WORKER_ID,
        transport_epoch: int = 1,
    ) -> dict[str, object]:
        return {
            "schema_id": "agent-task-claim-request/v1",
            "package": package_tuple(),
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_id": lease_id,
            "transport_epoch": transport_epoch,
            "idempotency_key": idempotency_key,
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "elapsed_limit_ms": 60_000,
            "tool_call_limit": 7,
        }

    def prepare_claim(self) -> tuple[dict[str, object], dict[str, object]]:
        self.register()
        self.accept()
        request = self.claim_request()
        response = self.authority.claim_and_issue_current_grant(request)
        return request, json.loads(response)

    def counts(self, *tables: str) -> tuple[int, ...]:
        with self.connect() as connection:
            return tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            )

    def assert_error(self, code: str, callback) -> AuthorityError:
        with self.assertRaises(AuthorityError) as raised:
            callback()
        error = raised.exception
        payload = json.loads(error.response_bytes)
        self.assertEqual(error.code, code)
        self.assertEqual(payload["schema_id"], "error-response/v1")
        self.assertEqual(payload["error"]["code"], code)
        verify_document_digest("stable-error/v1", payload["error"])
        self.assertEqual(error.response_bytes, error.canonical_bytes)
        return error

    def assert_fault_rolls_back(self, points, callback, tables) -> None:
        before = self.counts(*tables)
        for point in points:
            def inject(actual: str) -> None:
                if actual == point:
                    raise RuntimeError(f"injected:{point}")

            with self.subTest(point=point):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    authority = AgentFirstAuthority(
                        self.connect,
                        fault_injector=inject,
                    )
                    callback(authority)
            self.assertEqual(before, self.counts(*tables))

    def receipt(
        self,
        envelope: dict[str, object],
        *,
        content_ref: dict[str, object] | None = None,
    ) -> dict[str, object]:
        fence = (
            "task_id",
            "attempt_id",
            "session_id",
            "owner_id",
            "lease_id",
            "authority_digest",
            "task_version",
            "deletion_version",
            "owner_epoch",
            "native_epoch",
            "transport_epoch",
        )
        return seal_document(
            "server-transport-receipt/v1",
            {
                "schema_id": "server-transport-receipt/v1",
                "receipt_kind": "server_transport",
                "package": package_tuple(),
                "receipt_id": "receipt_55555555555555555555555555555555",
                **{key: envelope[key] for key in fence},
                "grant_digest": envelope["grant"]["grant_digest"],
                "content_ref": content_ref or {
                    "schema_id": "content-ref/v1",
                    "artifact_id": "art_66666666666666666666666666666666",
                    "content_schema_id": "worker-debug-fragment/v1",
                    "sha256": "7" * 64,
                    "size_bytes": 1,
                    "media_type": "application/json",
                    "encoding": "utf-8",
                },
                "accepted_at": NOW,
            },
        )

    def task_result(
        self,
        envelope: dict[str, object],
        *,
        outcome: str = "COMPLETED",
    ) -> dict[str, object]:
        result = copy.deepcopy(fixture("task_result_golden_completed")["document"])
        result.update(
            {
                "result_id": "result_77777777777777777777777777777777",
                "task_id": envelope["task_id"],
                "published_from_version": envelope["task_version"],
                "terminal_task_version": envelope["task_version"] + 1,
                "attempt_identity": {
                    "kind": "started",
                    "attempt_id": envelope["attempt_id"],
                    "native_epoch": envelope["native_epoch"],
                },
                "owner_identity": {
                    "kind": "started",
                    "owner_id": envelope["owner_id"],
                    "owner_epoch": envelope["owner_epoch"],
                },
            }
        )
        result["provenance"]["attempt_ids"] = [envelope["attempt_id"]]
        if outcome == "FAILED":
            self._make_failed_result(result)
        elif outcome == "CANCELLED":
            self._make_cancelled_result(result)
        elif outcome != "COMPLETED":
            raise AssertionError(f"unsupported test outcome: {outcome}")
        return result

    @staticmethod
    def _make_failed_result(result: dict[str, object]) -> None:
        result.update(
            {
                "outcome": "FAILED",
                "reason_code": "RUNTIME_FAILURE",
                "outcome_details": {
                    "kind": "failed",
                    "failures": [
                        {
                            "code": "RUNTIME_FAILURE",
                            "evidence_refs": [result["evidence_closure_ref"]],
                        }
                    ],
                },
                "completion_proposal": {
                    "availability": "unavailable",
                    "reason_code": "PROPOSAL_NOT_CREATED",
                },
                "attestations": {
                    "availability": "unavailable",
                    "reason_code": "ATTESTATIONS_NOT_CREATED",
                },
                "report": {
                    "availability": "unavailable",
                    "reason_code": "REPORT_NOT_CREATED",
                },
            }
        )
        result["requirement_results"][0]["verdict"] = "FAIL"

    @staticmethod
    def _make_cancelled_result(result: dict[str, object]) -> None:
        result.update(
            {
                "outcome": "CANCELLED",
                "reason_code": "USER_CANCELLED",
                "outcome_details": {
                    "kind": "cancelled",
                    "request_id": "cancel_88888888888888888888888888888888",
                    "linearized_at": NOW,
                    "requested_by": {
                        "schema_id": "actor/v1",
                        "kind": "user_control",
                        "id": "operator",
                        "session_id": None,
                    },
                },
                "completion_proposal": {
                    "availability": "unavailable",
                    "reason_code": "TASK_CANCELLED",
                },
                "attestations": {
                    "availability": "unavailable",
                    "reason_code": "TASK_CANCELLED",
                },
                "report": {
                    "availability": "unavailable",
                    "reason_code": "TASK_CANCELLED",
                },
            }
        )
        result["requirement_results"][0]["verdict"] = "UNVERIFIABLE"


__all__ = [
    "AuthorityHarness",
    "LEASE_ID",
    "NOW",
    "TASK_ID",
    "WORKER_ID",
    "policy",
]
