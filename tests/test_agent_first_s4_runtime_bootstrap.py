from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

from pullwise_server._generated_agent_task_contract import (
    canonical_validated_bytes,
    verify_document_digest,
)
from tests.agent_first_authority_support import AuthorityHarness, TASK_ID


class AgentFirstS4RuntimeBootstrapTest(AuthorityHarness, unittest.TestCase):
    def _accept_request(self) -> dict[str, object]:
        return self.accept_request(TASK_ID)

    def test_claim_atomically_returns_and_persists_the_canonical_bootstrap(self) -> None:
        self.register()
        accept_request = self._accept_request()

        accepted_bytes = self.authority.accept_current_task(accept_request)
        claimed_bytes = self.authority.claim_and_issue_current_grant(
            self.claim_request()
        )

        accepted = verify_document_digest(
            "agent-task-accept-response/v1", json.loads(accepted_bytes)
        )
        bootstrap = verify_document_digest(
            "agent-task-runtime-bootstrap/v1", json.loads(claimed_bytes)
        )
        roots = bootstrap["construction_roots"]
        self.assertEqual(accept_request, bootstrap["accept_request"])
        self.assertEqual(accepted, bootstrap["accept_response"])
        self.assertEqual(bootstrap["authority"]["task_id"], roots["task_record"]["task_id"])
        self.assertEqual("LEASED", roots["attempt"]["state"])
        self.assertEqual("STARTING", roots["owner"]["state"])
        self.assertEqual(
            hashlib.sha256(
                canonical_validated_bytes(
                    "task-request/v1", accept_request["task_request"]
                )
            ).hexdigest(),
            roots["task_record"]["request_ref"]["sha256"],
        )
        self.assertEqual(
            hashlib.sha256(
                canonical_validated_bytes(
                    "effective-execution-policy/v1",
                    accept_request["effective_policy"],
                )
            ).hexdigest(),
            roots["task_record"]["policy_ref"]["sha256"],
        )

        with self.connect() as connection:
            acceptance = connection.execute(
                "SELECT accept_request_bytes, requirement_ledger_bytes, "
                "accept_response_bytes, outer_job_id, run_id "
                "FROM agent_current_task_acceptances"
            ).fetchone()
            persisted = connection.execute(
                "SELECT bootstrap_bytes, task_record_bytes, attempt_record_bytes, "
                "owner_record_bytes FROM agent_current_runtime_bootstraps"
            ).fetchone()
            attempt_state = connection.execute(
                "SELECT state FROM agent_current_attempts"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_runtime_bootstraps SET bootstrap_bytes=x'00'"
                )
        self.assertEqual(
            (
                canonical_validated_bytes(
                    "agent-task-accept-request/v1", accept_request
                ),
                canonical_validated_bytes(
                    "requirement-ledger/v1", accept_request["requirement_ledger"]
                ),
                accepted_bytes,
                "job-1",
                "run-1",
            ),
            tuple(acceptance),
        )
        self.assertEqual(claimed_bytes, persisted[0])
        self.assertEqual(
            roots,
            {
                "task_record": json.loads(persisted[1]),
                "attempt": json.loads(persisted[2]),
                "owner": json.loads(persisted[3]),
            },
        )
        self.assertEqual("LEASED", attempt_state)
        self.assertEqual(claimed_bytes, self.authority.claim_and_issue_current_grant(
            self.claim_request()
        ))

    def test_unversioned_acceptance_envelope_is_not_a_fallback(self) -> None:
        current = self.accept_request()
        unversioned = {
            field: current[field]
            for field in (
                "package",
                "idempotency_key",
                "task_request",
                "effective_policy",
            )
        }
        self.assert_error(
            "CONTRACT_DOCUMENT_INVALID",
            lambda: self.authority.accept_current_task(unversioned),
        )
        self.assertEqual((0,), self.counts("agent_current_task_requests"))


if __name__ == "__main__":
    unittest.main()
