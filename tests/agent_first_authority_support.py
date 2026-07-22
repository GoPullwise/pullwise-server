from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from pullwise_server._generated_agent_task_contract import (
    package_tuple,
    schema_ids,
    seal_document,
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
    return seal_document(
        "agent-task-policy/v1",
        {
            "schema_id": "agent-task-policy/v1",
            "policy_id": "policy_33333333333333333333333333333333",
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "elapsed_limit_ms": 60_000,
            "tool_call_limit": 7,
        },
    )


class AuthorityHarness:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "authority.sqlite3"
        with self.connect() as connection:
            install_current_authority_tables(connection)
        self.authority = AgentFirstAuthority(self.connect)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
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
        return {
            "schema_id": "agent-task-request/v1",
            "package": package_tuple(),
            "task_id": task_id,
            "task_type": "repo_review.full_scan",
            "idempotency_key": f"accept:{task_id}",
            "policy": policy(),
            "request": {"repository": "octo/example", "commit": "a" * 40},
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

    def receipt(self, envelope: dict[str, object]) -> dict[str, object]:
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
                "content_ref": {
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


__all__ = [
    "AuthorityHarness",
    "LEASE_ID",
    "NOW",
    "TASK_ID",
    "WORKER_ID",
    "policy",
]
