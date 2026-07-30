from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from pullwise_server._generated_agent_task_contract import (
    canonical_document_bytes,
    package_tuple,
    verify_document_digest,
)
from pullwise_server.agent_first_checkpoint_watermark import (
    CHECKPOINT_WATERMARK_FAULT_POINTS,
    verify_checkpoint_watermark_ack,
)
from tests.agent_first_authority_support import AuthorityHarness


def _seal_request(unsigned: dict[str, object]) -> dict[str, object]:
    digest = hashlib.sha256(
        b"pullwise:checkpoint-watermark-request:internal-v1\0"
        + canonical_document_bytes(unsigned)
    ).hexdigest()
    return {**unsigned, "request_digest": digest}


class AgentFirstCheckpointWatermarkTest(AuthorityHarness, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register()
        self.accept()
        response = self.authority.claim_and_issue_current_grant(self.claim_request())
        bootstrap = verify_document_digest(
            "agent-task-runtime-bootstrap/v1", json.loads(response)
        )
        self.envelope = bootstrap["authority"]

    def request(
        self,
        generation: int = 1,
        *,
        previous_manifest_hash: str | None = None,
        manifest_char: str = "a",
    ) -> dict[str, object]:
        envelope = self.envelope
        from_version = envelope["task_version"] + generation - 1
        return _seal_request(
            {
                "schema_id": "checkpoint-watermark-request/internal-v1",
                "package": package_tuple(),
                "task_id": envelope["task_id"],
                "generation": generation,
                "previous_manifest_hash": previous_manifest_hash,
                "manifest_hash": manifest_char * 64,
                "committed_from_task_version": from_version,
                "committed_task_version": from_version + 1,
                "attempt_id": envelope["attempt_id"],
                "owner_id": envelope["owner_id"],
                "owner_epoch": envelope["owner_epoch"],
                "lease_id": envelope["lease_id"],
                "authority_digest": envelope["authority_digest"],
                "grant_id": envelope["grant"]["grant_id"],
                "grant_digest": envelope["grant"]["grant_digest"],
                "deletion_version": envelope["deletion_version"],
                "native_epoch": envelope["native_epoch"],
                "transport_epoch": envelope["transport_epoch"],
                "same_run_resume_selected": True,
            }
        )

    def test_watermark_ack_is_sequential_atomic_and_exactly_idempotent(self) -> None:
        first_request = self.request()
        first = self.authority.acknowledge_current_checkpoint_watermark(first_request)
        self.assertEqual(
            first,
            self.authority.acknowledge_current_checkpoint_watermark(
                deepcopy(first_request)
            ),
        )
        first_ack = verify_checkpoint_watermark_ack(first)
        second_request = self.request(
            2,
            previous_manifest_hash=first_ack["manifest_hash"],
            manifest_char="b",
        )
        second = verify_checkpoint_watermark_ack(
            self.authority.acknowledge_current_checkpoint_watermark(second_request)
        )
        self.assertEqual(2, second["generation"])
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT generation, manifest_hash, previous_manifest_hash "
                "FROM agent_current_checkpoint_watermarks ORDER BY generation"
            ).fetchall()
            head = connection.execute(
                "SELECT generation, manifest_hash, committed_task_version "
                "FROM agent_current_checkpoint_watermark_heads"
            ).fetchone()
            task_version = connection.execute(
                "SELECT task_version FROM agent_current_task_heads"
            ).fetchone()[0]
        self.assertEqual([(1, "a" * 64, None), (2, "b" * 64, "a" * 64)], rows)
        self.assertEqual((2, "b" * 64, 4), head)
        self.assertEqual(self.envelope["task_version"], task_version)

    def test_fork_skip_stale_fence_and_capability_off_are_rejected(self) -> None:
        first = self.request()
        self.authority.acknowledge_current_checkpoint_watermark(first)
        before = self.counts(
            "agent_current_checkpoint_watermarks",
            "agent_current_checkpoint_watermark_heads",
        )
        variants = (
            self.request(3, previous_manifest_hash="a" * 64, manifest_char="c"),
            self.request(2, previous_manifest_hash="f" * 64, manifest_char="b"),
            {**self.request(2, previous_manifest_hash="a" * 64),
             "native_epoch": self.envelope["native_epoch"] + 1},
            {**self.request(2, previous_manifest_hash="a" * 64),
             "same_run_resume_selected": False},
        )
        for raw in variants:
            request = _seal_request(
                {key: value for key, value in raw.items() if key != "request_digest"}
            )
            with self.subTest(request=request):
                self.assert_error(
                    "AUTHORITY_FENCED",
                    lambda request=request: self.authority.
                    acknowledge_current_checkpoint_watermark(request),
                )
                self.assertEqual(before, self.counts(
                    "agent_current_checkpoint_watermarks",
                    "agent_current_checkpoint_watermark_heads",
                ))

    def test_every_watermark_write_stage_rolls_back(self) -> None:
        request = self.request()
        self.assert_fault_rolls_back(
            CHECKPOINT_WATERMARK_FAULT_POINTS,
            lambda authority: authority.acknowledge_current_checkpoint_watermark(
                request
            ),
            (
                "agent_current_checkpoint_watermarks",
                "agent_current_checkpoint_watermark_heads",
            ),
        )


if __name__ == "__main__":
    unittest.main()
