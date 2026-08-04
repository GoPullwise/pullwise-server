from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

from pullwise_server import db
from pullwise_server._generated_agent_task_contract import (
    canonical_document_bytes,
    canonical_validated_bytes,
    derive_release_gate_evaluation,
    fixture,
    package_tuple,
    seal_document,
)
from pullwise_server.agent_first_authority import AuthorityError
from pullwise_server.agent_first_release_evaluator import AgentFirstReleaseEvaluator
from pullwise_server.agent_first_release_evaluator_migrations import (
    CURRENT_RELEASE_EVALUATOR_TABLES,
    install_current_release_evaluator_tables,
)
from pullwise_server.agent_first_release_evaluator_store import (
    RELEASE_EVALUATOR_FAULT_POINTS,
)


def _release_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + canonical_document_bytes(value)
    ).hexdigest()


def _content_ref(
    original: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> dict[str, object]:
    encoded = canonical_validated_bytes(schema_id, document)
    return {
        **original,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _current_documents() -> tuple[dict[str, object], ...]:
    sample_set = deepcopy(
        fixture("release_gate_sample_set_golden_bootstrap_included")[
            "document"
        ]
    )
    benchmark = deepcopy(fixture("benchmark_bundle_golden_current")["document"])
    benchmark["package"] = package_tuple()
    benchmark["task_inventory_digest"] = sample_set[
        "task_inventory_digest"
    ]
    benchmark.pop("bundle_digest")
    benchmark = seal_document("benchmark-bundle/v1", benchmark)

    policy = deepcopy(fixture("release_gate_policy_golden_bootstrap")["document"])
    policy["package"] = package_tuple()
    policy["benchmark_digest"] = benchmark["bundle_digest"]
    policy["benchmark_ref"] = _content_ref(
        policy["benchmark_ref"],
        "benchmark-bundle/v1",
        benchmark,
    )
    for field in (
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
    ):
        policy[field] = benchmark[field]
    policy["candidate_digest"] = _release_digest(
        "pullwise:candidate-digest:v1",
        {
            field: policy[field]
            for field in (
                "package",
                "candidate_build_id",
                "control_plane_digest",
                "evaluation_runtime_digest",
                "benchmark_ref",
                "benchmark_digest",
                "threshold_table_digest",
                "profile_budget_digest",
                "canary_plan_digest",
            )
        },
    )
    policy.pop("policy_digest")
    policy = seal_document("release-gate-policy/v1", policy)

    sample_set["package"] = package_tuple()
    for field in (
        "candidate_build_id",
        "candidate_digest",
        "release_mode",
        "stable_package",
        "stable_candidate_digest",
        "stable_control_plane_digest",
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
        "organization_id",
    ):
        sample_set[field] = deepcopy(policy[field])
    sample_set["benchmark_ref"] = _content_ref(
        sample_set["benchmark_ref"],
        "benchmark-bundle/v1",
        benchmark,
    )
    sample_set["benchmark_digest"] = benchmark["bundle_digest"]
    sample_set["policy_ref"] = _content_ref(
        sample_set["policy_ref"],
        "release-gate-policy/v1",
        policy,
    )
    sample_set["policy_digest"] = policy["policy_digest"]
    sample_set.pop("sample_set_digest")
    sample_set = seal_document(
        "release-gate-sample-set/v1", sample_set
    )

    report = deepcopy(
        fixture("release_gate_report_golden_bootstrap_pass")["document"]
    )
    for field in (
        "package",
        "candidate_build_id",
        "candidate_digest",
        "release_mode",
        "stable_package",
        "stable_candidate_digest",
        "stable_control_plane_digest",
        "benchmark_digest",
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
        "threshold_table_digest",
        "profile_budget_digest",
        "canary_plan_digest",
    ):
        report[field] = deepcopy(policy[field])
    report["benchmark_ref"] = _content_ref(
        report["benchmark_ref"],
        "benchmark-bundle/v1",
        benchmark,
    )
    report["policy_digest"] = policy["policy_digest"]
    report["policy_ref"] = _content_ref(
        report["policy_ref"],
        "release-gate-policy/v1",
        policy,
    )
    report["sample_set_ref"] = _content_ref(
        report["sample_set_ref"],
        "release-gate-sample-set/v1",
        sample_set,
    )
    report["sample_set_digest"] = sample_set["sample_set_digest"]
    report.update(
        derive_release_gate_evaluation(benchmark, policy, sample_set)
    )
    report["completed_at"] = sample_set["completed_at"]
    report["signer_role"] = sample_set["producer_role"]
    report["signer_id"] = sample_set["producer_id"]
    report.pop("report_digest")
    report = seal_document("release-gate-report/v1", report)
    return benchmark, policy, sample_set, report


class AgentFirstReleaseEvaluatorStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "release-evaluator.sqlite3"
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

    def test_evaluate_and_store_persists_an_exact_verified_evaluation(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()

        self.evaluator.freeze_inputs(benchmark, policy)
        stored = self.evaluator.evaluate_and_store(
            benchmark,
            policy,
            sample_set,
            report,
        )

        self.assertEqual("INDETERMINATE", stored.verdict)
        self.assertEqual(2, stored.exit_code)
        self.assertEqual(
            canonical_validated_bytes("benchmark-bundle/v1", benchmark),
            stored.benchmark_bytes,
        )
        self.assertEqual(
            canonical_validated_bytes("release-gate-policy/v1", policy),
            stored.policy_bytes,
        )
        self.assertEqual(
            canonical_validated_bytes(
                "release-gate-sample-set/v1", sample_set
            ),
            stored.sample_set_bytes,
        )
        self.assertEqual(
            canonical_validated_bytes("release-gate-report/v1", report),
            stored.report_bytes,
        )
        self.assertEqual(
            stored,
            self.evaluator.load_evaluation(report["report_id"]),
        )
        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_EVALUATOR_TABLES
            )
        self.assertEqual((1, 1, 1, 1), counts)

    def test_verified_read_rejects_corrupt_domain_digest_metadata(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        self.evaluator.evaluate_and_store(
            benchmark, policy, sample_set, report
        )
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """
                DROP TRIGGER
                agent_current_release_gate_reports_immutable_update
                """
            )
            connection.execute(
                """
                UPDATE agent_current_release_gate_reports
                SET report_digest = ?
                WHERE report_id = ?
                """,
                ("f" * 64, report["report_id"]),
            )

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.load_evaluation(report["report_id"])

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)

    def test_exact_write_rejects_corrupt_existing_digest_row(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        self.evaluator.evaluate_and_store(
            benchmark, policy, sample_set, report
        )
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """
                DROP TRIGGER
                agent_current_release_gate_reports_immutable_update
                """
            )
            connection.execute(
                """
                UPDATE agent_current_release_gate_reports
                SET document_sha256 = ?
                WHERE report_id = ?
                """,
                ("e" * 64, report["report_id"]),
            )

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.evaluate_and_store(
                benchmark, policy, sample_set, report
            )

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)

    def test_verified_read_rejects_a_missing_linked_document(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        self.evaluator.evaluate_and_store(
            benchmark, policy, sample_set, report
        )
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DROP TRIGGER agent_current_release_gate_policies_immutable_delete"
            )
            connection.execute(
                "DELETE FROM agent_current_release_gate_policies WHERE policy_id = ?",
                (policy["policy_id"],),
            )

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.load_evaluation(report["report_id"])

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)

    def test_exact_replay_is_a_no_op_and_rows_are_immutable(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        first_inputs = self.evaluator.freeze_inputs(benchmark, policy)
        self.assertEqual(
            first_inputs,
            self.evaluator.freeze_inputs(deepcopy(benchmark), deepcopy(policy)),
        )
        first = self.evaluator.evaluate_and_store(
            benchmark, policy, sample_set, report
        )

        self.assertEqual(
            first,
            self.evaluator.evaluate_and_store(
                deepcopy(benchmark),
                deepcopy(policy),
                deepcopy(sample_set),
                deepcopy(report),
            ),
        )
        collision = deepcopy(report)
        collision["signature"] = "A" * 86
        collision.pop("report_digest")
        collision = seal_document("release-gate-report/v1", collision)
        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.evaluate_and_store(
                benchmark, policy, sample_set, collision
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", raised.exception.code)

        with closing(self.connect()) as connection:
            for table in CURRENT_RELEASE_EVALUATOR_TABLES:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"UPDATE {table} SET created_at = created_at")
                connection.rollback()
        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_EVALUATOR_TABLES
            )
        self.assertEqual((1, 1, 1, 1), counts)

    def test_invalid_or_noncurrent_chain_writes_nothing(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        mismatched = deepcopy(report)
        mismatched["benchmark_digest"] = "0" * 64
        mismatched.pop("report_digest")
        mismatched = seal_document("release-gate-report/v1", mismatched)

        with self.assertRaises(AuthorityError) as invalid:
            self.evaluator.evaluate_and_store(
                benchmark, policy, sample_set, mismatched
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", invalid.exception.code)

        fixture_benchmark = fixture("benchmark_bundle_golden_current")["document"]
        fixture_policy = fixture("release_gate_policy_golden_bootstrap")["document"]
        fixture_sample_set = fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        fixture_report = fixture(
            "release_gate_report_golden_bootstrap_pass"
        )["document"]
        with self.assertRaises(AuthorityError) as noncurrent:
            self.evaluator.evaluate_and_store(
                fixture_benchmark,
                fixture_policy,
                fixture_sample_set,
                fixture_report,
            )
        self.assertEqual("CURRENT_PACKAGE_PIN_MISMATCH", noncurrent.exception.code)

        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_EVALUATOR_TABLES
            )
        self.assertEqual((0, 0, 0, 0), counts)

    def test_input_and_report_faults_preserve_the_freeze_boundary(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        for fault_point in RELEASE_EVALUATOR_FAULT_POINTS:
            with self.subTest(fault_point=fault_point):
                def inject(point: str) -> None:
                    if point == fault_point:
                        raise RuntimeError(point)

                evaluator = AgentFirstReleaseEvaluator(
                    self.connect,
                    fault_injector=inject,
                )
                with self.assertRaisesRegex(RuntimeError, fault_point):
                    if fault_point.endswith(("benchmark", "policy")):
                        evaluator.freeze_inputs(benchmark, policy)
                    else:
                        evaluator.freeze_inputs(benchmark, policy)
                        evaluator.evaluate_and_store(
                            benchmark, policy, sample_set, report
                        )
                with closing(self.connect()) as connection:
                    counts = tuple(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in CURRENT_RELEASE_EVALUATOR_TABLES
                    )
                self.assertEqual(
                    (0, 0, 0, 0)
                    if fault_point.endswith(("benchmark", "policy"))
                    else (1, 1, 0, 0),
                    counts,
                )

    def test_report_projection_must_match_frozen_raw_samples(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)
        tampered = deepcopy(report)
        tampered["raw_sample_count"] += 1
        tampered["valid_sample_count"] += 1
        tampered.pop("report_digest")
        tampered = seal_document("release-gate-report/v1", tampered)

        with self.assertRaises(AuthorityError) as raised:
            self.evaluator.evaluate_and_store(
                benchmark, policy, sample_set, tampered
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)

        stored = self.evaluator.evaluate_and_store(
            benchmark, policy, sample_set, report
        )
        self.assertEqual("INDETERMINATE", stored.verdict)
        self.assertEqual(
            stored,
            self.evaluator.load_evaluation(report["report_id"]),
        )

    def test_concurrent_exact_writes_converge_and_conflicts_choose_one(self) -> None:
        benchmark, policy, sample_set, report = _current_documents()
        self.evaluator.freeze_inputs(benchmark, policy)

        def race(reports: tuple[dict[str, object], ...]) -> list[object]:
            barrier = threading.Barrier(len(reports))
            outcomes: list[object] = []

            def write(candidate: dict[str, object]) -> None:
                barrier.wait()
                try:
                    outcomes.append(
                        self.evaluator.evaluate_and_store(
                            benchmark,
                            policy,
                            sample_set,
                            candidate,
                        )
                    )
                except AuthorityError as error:
                    outcomes.append(error)

            threads = [threading.Thread(target=write, args=(item,)) for item in reports]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return outcomes

        exact = race((deepcopy(report), deepcopy(report)))
        self.assertEqual(2, len(exact))
        self.assertTrue(all(item == exact[0] for item in exact))

        first = deepcopy(report)
        first["report_id"] = "release_report_44444444444444444444444444444444"
        first.pop("report_digest")
        first = seal_document("release-gate-report/v1", first)
        second = deepcopy(first)
        second["signature"] = "B" * 86
        second.pop("report_digest")
        second = seal_document("release-gate-report/v1", second)
        conflict = race((first, second))

        errors = [item for item in conflict if isinstance(item, AuthorityError)]
        successes = [item for item in conflict if not isinstance(item, AuthorityError)]
        self.assertEqual(["IDEMPOTENCY_CONFLICT"], [error.code for error in errors])
        self.assertEqual(1, len(successes))
        self.assertIn(
            successes[0].report_bytes,
            {
                canonical_validated_bytes("release-gate-report/v1", first),
                canonical_validated_bytes("release-gate-report/v1", second),
            },
        )
        self.assertEqual(
            successes[0],
            self.evaluator.load_evaluation(first["report_id"]),
        )

    def test_main_database_initialization_installs_evaluator_tables(self) -> None:
        db_path = Path(self.temporary.name) / "main.sqlite3"
        with patch.dict(os.environ, {"PULLWISE_DB_PATH": str(db_path)}, clear=True):
            db.initialize()
            with closing(db.connect()) as connection:
                installed = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertTrue(set(CURRENT_RELEASE_EVALUATOR_TABLES).issubset(installed))

    def test_storage_schema_stays_inside_the_authorized_boundary(self) -> None:
        with closing(self.connect()) as connection, connection:
            install_current_release_evaluator_tables(connection)
            columns = {
                row[1]
                for table in CURRENT_RELEASE_EVALUATOR_TABLES
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            release_tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name LIKE 'agent_current_release_%'
                    """
                )
            }

        self.assertEqual(set(CURRENT_RELEASE_EVALUATOR_TABLES), release_tables)
        self.assertTrue(
            {
                "attestation_id",
                "baseline_id",
                "canary_state",
                "key_id",
                "organization_id",
                "principal_id",
                "signature",
                "trust_state",
            }.isdisjoint(columns)
        )
        for method in (
            "store_attestation",
            "activate_baseline",
            "advance_canary",
            "register_principal",
        ):
            self.assertFalse(hasattr(self.evaluator, method))


if __name__ == "__main__":
    unittest.main()
