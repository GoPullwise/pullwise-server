from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from pullwise_server._generated_agent_task_contract import fixture, seal_document
from pullwise_server.agent_first_authority import AuthorityError
from pullwise_server.agent_first_release_evaluator import AgentFirstReleaseEvaluator
from pullwise_server.agent_first_release_evaluator_migrations import (
    CURRENT_RELEASE_EVALUATOR_TABLES,
    install_current_release_evaluator_tables,
)
from tests.test_agent_first_release_evaluator_storage import (
    _current_documents,
    _release_digest,
)


class AgentFirstReleaseInputFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "release-input-freeze.sqlite3"
        with closing(self.connect()) as connection:
            install_current_release_evaluator_tables(connection)
        self.evaluator = AgentFirstReleaseEvaluator(self.connect)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            check_same_thread=False,
        )
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _counts(self) -> tuple[int, ...]:
        with closing(self.connect()) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_EVALUATOR_TABLES
            )

    def _delete_policy_row(self, policy_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            with connection:
                connection.execute(
                    "DROP TRIGGER agent_current_release_gate_policies_immutable_delete"
                )
                connection.execute(
                    "DELETE FROM agent_current_release_gate_policies WHERE policy_id = ?",
                    (policy_id,),
                )

    def test_invalid_or_noncurrent_input_freeze_writes_nothing(self) -> None:
        benchmark, policy, _ = _current_documents()
        mismatched = deepcopy(policy)
        mismatched["benchmark_digest"] = "0" * 64
        mismatched["candidate_digest"] = _release_digest(
            "pullwise:candidate-digest:v1",
            {
                field: mismatched[field]
                for field in (
                    "package", "candidate_build_id", "control_plane_digest",
                    "evaluation_runtime_digest", "benchmark_ref",
                    "benchmark_digest", "threshold_table_digest",
                    "profile_budget_digest", "canary_plan_digest",
                )
            },
        )
        mismatched.pop("policy_digest")
        mismatched = seal_document("release-gate-policy/v1", mismatched)

        with self.assertRaises(AuthorityError) as invalid:
            self.evaluator.freeze_inputs(benchmark, mismatched)
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", invalid.exception.code)

        with self.assertRaises(AuthorityError) as noncurrent:
            self.evaluator.freeze_inputs(
                fixture("benchmark_bundle_golden_current")["document"],
                fixture("release_gate_policy_golden_bootstrap")["document"],
            )
        self.assertEqual("CURRENT_PACKAGE_PIN_MISMATCH", noncurrent.exception.code)
        self.assertEqual((0, 0, 0), self._counts())

    def test_input_freeze_rejects_a_stable_policy_id_conflict(self) -> None:
        benchmark, policy, _ = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        collision = deepcopy(policy)
        collision["expires_at"] = "2026-07-29T00:00:00.000Z"
        collision.pop("policy_digest")
        collision = seal_document("release-gate-policy/v1", collision)

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.freeze_inputs(benchmark, collision)

        self.assertEqual("IDEMPOTENCY_CONFLICT", raised.exception.code)
        self.assertEqual((1, 1, 0), self._counts())

    def test_input_freeze_rejects_corrupt_existing_bytes_metadata(self) -> None:
        benchmark, policy, _ = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "DROP TRIGGER agent_current_release_gate_policies_immutable_update"
            )
            connection.execute(
                """
                UPDATE agent_current_release_gate_policies
                SET document_sha256 = ?
                WHERE policy_id = ?
                """,
                ("e" * 64, policy["policy_id"]),
            )

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.freeze_inputs(benchmark, policy)

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)

    def test_input_freeze_does_not_backfill_a_partially_missing_pair(self) -> None:
        benchmark, policy, _ = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        self._delete_policy_row(str(policy["policy_id"]))

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.freeze_inputs(benchmark, policy)

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)
        self.assertEqual((1, 0, 0), self._counts())

    def test_report_rejects_a_partially_missing_frozen_pair_as_corrupt(self) -> None:
        benchmark, policy, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        self._delete_policy_row(str(policy["policy_id"]))

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.evaluate_and_store(benchmark, policy, report)

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)
        self.assertEqual((1, 0, 0), self._counts())

    def test_concurrent_exact_input_freezes_converge(self) -> None:
        benchmark, policy, _ = _current_documents()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def freeze() -> None:
            barrier.wait()
            try:
                outcomes.append(
                    self.evaluator.freeze_inputs(
                        deepcopy(benchmark),
                        deepcopy(policy),
                    )
                )
            except AuthorityError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=freeze) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(2, len(outcomes))
        self.assertTrue(all(item == outcomes[0] for item in outcomes))
        self.assertEqual((1, 1, 0), self._counts())

    def test_locked_input_freeze_preserves_the_sqlite_lock_error(self) -> None:
        benchmark, policy, _ = _current_documents()

        def quick_connect() -> sqlite3.Connection:
            connection = sqlite3.connect(self.db_path, timeout=0.01)
            connection.execute("PRAGMA busy_timeout=10")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        evaluator = AgentFirstReleaseEvaluator(quick_connect)
        with closing(self.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                evaluator.freeze_inputs(benchmark, policy)
            blocker.rollback()

        self.assertEqual((0, 0, 0), self._counts())


if __name__ == "__main__":
    unittest.main()
