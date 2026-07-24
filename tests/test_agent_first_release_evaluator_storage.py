from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import sqlite3
import tempfile
from pathlib import Path
import unittest

from pullwise_server._generated_agent_task_contract import (
    canonical_document_bytes,
    canonical_validated_bytes,
    fixture,
    package_tuple,
    seal_document,
)
from pullwise_server.agent_first_release_evaluator import AgentFirstReleaseEvaluator
from pullwise_server.agent_first_release_evaluator_migrations import (
    CURRENT_RELEASE_EVALUATOR_TABLES,
    install_current_release_evaluator_tables,
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
    benchmark = deepcopy(fixture("benchmark_bundle_golden_current")["document"])
    benchmark["package"] = package_tuple()
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
    report.pop("report_digest")
    report = seal_document("release-gate-report/v1", report)
    return benchmark, policy, report


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
        benchmark, policy, report = _current_documents()

        stored = self.evaluator.evaluate_and_store(benchmark, policy, report)

        self.assertEqual("PASS", stored.verdict)
        self.assertEqual(0, stored.exit_code)
        self.assertEqual(
            canonical_validated_bytes("benchmark-bundle/v1", benchmark),
            stored.benchmark_bytes,
        )
        self.assertEqual(
            canonical_validated_bytes("release-gate-policy/v1", policy),
            stored.policy_bytes,
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
        self.assertEqual((1, 1, 1), counts)


if __name__ == "__main__":
    unittest.main()
