from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle import REQUIRED_FAMILIES, build_bundle
from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"
D22_FAMILIES = (
    "benchmark-bundle",
    "release-gate-policy",
    "release-gate-report",
    "release-gate-attestation",
)


class AgentFirstReleaseGateFamilyTest(unittest.TestCase):
    def test_source_package_requires_the_complete_d22_evidence_chain(self) -> None:
        package = json.loads(
            (SOURCE_ROOT / "package.json").read_text(encoding="utf-8")
        )
        observed = {
            "python_inventory": [
                family_id
                for family_id in REQUIRED_FAMILIES
                if family_id in D22_FAMILIES
            ],
            "source_inventory": [
                family_id
                for family_id in package["required_families"]
                if family_id in D22_FAMILIES
            ],
            "source_files": [
                family_id
                for family_id in D22_FAMILIES
                if (SOURCE_ROOT / "families" / f"{family_id}.json").is_file()
            ],
        }
        self.assertEqual(
            {key: list(D22_FAMILIES) for key in observed},
            observed,
        )

    def test_d22_typed_reference_dag_is_exact_and_files_stay_reviewable(self) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        dag = {
            item["schema_id"]: item
            for item in bundle.document["root_manifest"]["reference_dag"]
        }
        expected_targets = {
            "benchmark-bundle/v1": set(),
            "release-gate-policy/v1": {"benchmark-bundle/v1"},
            "release-gate-report/v1": {
                "benchmark-bundle/v1",
                "release-gate-policy/v1",
            },
            "release-gate-attestation/v1": {
                "release-gate-policy/v1",
                "release-gate-report/v1",
            },
        }
        for schema_id, targets in expected_targets.items():
            with self.subTest(schema_id=schema_id):
                typed_targets = {
                    edge["target_schema_id"]
                    for edge in dag[schema_id]["edges"]
                    if edge["kind"] == "content_ref_target"
                }
                self.assertEqual(targets, typed_targets)

        for family_id in D22_FAMILIES:
            lines = (SOURCE_ROOT / "families" / f"{family_id}.json").read_text(
                encoding="utf-8"
            ).splitlines()
            with self.subTest(family_id=family_id):
                self.assertLessEqual(len(lines), 600)
                self.assertLessEqual(max(map(len, lines)), 200)

    def test_benchmark_bundle_freezes_reproducible_input_shape(self) -> None:
        path = SOURCE_ROOT / "families/benchmark-bundle.json"
        family = json.loads(path.read_text(encoding="utf-8"))
        loaded = load_family(path, "benchmark-bundle", {}, set())
        self.assertEqual(
            ["benchmark-bundle/v1"],
            [schema["$id"] for schema in loaded["schemas"]],
        )
        schema = loaded["schemas"][0]
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            {
                "document_rules": ["benchmark_bundle"],
                "contextual_helpers": [],
            },
            schema["x-pullwise-semantics"],
        )
        self.assertEqual(
            {
                "field": "bundle_digest",
                "domain": "pullwise:benchmark-bundle:v1",
            },
            schema["x-pullwise-digest"],
        )
        properties = schema["properties"]
        self.assertEqual("current-package-ref/v1", properties["package"]["$ref"])
        self.assertEqual(
            (3, 3),
            (properties["seeds"]["minItems"], properties["seeds"]["maxItems"]),
        )
        self.assertEqual(3, properties["repeats_per_task"]["const"])
        self.assertEqual(120, properties["known_gold_task_count"]["minimum"])
        self.assertEqual(
            15,
            properties["unknown_families"]["items"]["properties"]["task_count"][
                "minimum"
            ],
        )
        self.assertEqual(50, properties["oracle_positive_finding_count"]["minimum"])
        coverage = properties["cluster_coverage"]["items"]["properties"]
        for field in (
            "real_fix_tasks",
            "bad_or_incomplete_patch_tasks",
            "fake_success_or_zero_test_tasks",
            "environment_or_capability_failure_tasks",
            "adversarial_input_tasks",
        ):
            self.assertEqual(3, coverage[field]["minimum"])
        self.assertEqual("Ed25519", properties["signature_algorithm"]["const"])
        self.assertEqual("benchmark_owner", properties["signer_role"]["const"])

        fixtures = {
            fixture["fixture_id"]: fixture for fixture in family["fixtures"]
        }
        self.assertEqual(
            {
                "benchmark_bundle_golden_current",
                "benchmark_bundle_idempotency_current",
                "benchmark_bundle_negative_unsorted_seeds",
            },
            set(fixtures),
        )
        self.assertEqual(
            fixtures["benchmark_bundle_golden_current"]["document"],
            fixtures["benchmark_bundle_idempotency_current"]["document"],
        )
        self.assertEqual(
            "CONTRACT_DOCUMENT_INVALID",
            fixtures["benchmark_bundle_negative_unsorted_seeds"]["expected_code"],
        )
        for fixture_id in (
            "benchmark_bundle_golden_current",
            "benchmark_bundle_idempotency_current",
            "benchmark_bundle_negative_unsorted_seeds",
        ):
            document = fixtures[fixture_id]["document"]
            unsigned = {
                key: value for key, value in document.items()
                if key != "bundle_digest"
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:benchmark-bundle:v1\0"
                    + canonical_bytes(unsigned)
                ).hexdigest(),
                document["bundle_digest"],
                fixture_id,
            )

        golden = fixtures["benchmark_bundle_golden_current"]["document"]
        self.assertEqual(sorted(set(golden["seeds"])), golden["seeds"])
        family_ids = [item["family_id"] for item in golden["unknown_families"]]
        self.assertEqual(sorted(set(family_ids)), family_ids)
        cluster_ids = golden["core_cluster_ids"]
        coverage_ids = [item["cluster_id"] for item in golden["cluster_coverage"]]
        self.assertEqual(sorted(set(cluster_ids)), cluster_ids)
        self.assertEqual(cluster_ids, coverage_ids)
        self.assertLess(golden["issued_at"], golden["expires_at"])
        negative = fixtures[
            "benchmark_bundle_negative_unsorted_seeds"
        ]["document"]
        self.assertNotEqual(sorted(negative["seeds"]), negative["seeds"])

    def test_release_gate_policy_freezes_candidate_and_rollout_inputs(self) -> None:
        path = SOURCE_ROOT / "families/release-gate-policy.json"
        family = json.loads(path.read_text(encoding="utf-8"))
        loaded = load_family(path, "release-gate-policy", {}, set())
        schema = loaded["schemas"][0]
        self.assertEqual("release-gate-policy/v1", schema["$id"])
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            {
                "document_rules": ["release_gate_policy"],
                "contextual_helpers": ["verify_release_gate_policy_context"],
            },
            schema["x-pullwise-semantics"],
        )
        properties = schema["properties"]
        self.assertEqual(
            "benchmark-bundle/v1",
            properties["benchmark_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            ["BOOTSTRAP", "STABLE"],
            properties["release_mode"]["enum"],
        )
        self.assertEqual(
            ["NOT_APPLICABLE", "REQUIRED"],
            properties["relative_gates"]["items"]["properties"][
                "applicability"
            ]["enum"],
        )
        budgets = properties["profile_budgets"]["items"]["properties"]
        for field in ("wall_ms", "token_limit", "cost_microusd"):
            self.assertEqual(1, budgets[field]["minimum"])
        self.assertEqual(
            ["CAPACITY_5", "CAPACITY_25", "FULL_CAPACITY"],
            properties["canary_stages"]["items"]["properties"]["stage_id"][
                "enum"
            ],
        )
        self.assertEqual("release_operator", properties["signer_role"]["const"])
        self.assertEqual("Ed25519", properties["signature_algorithm"]["const"])

        fixtures = {
            fixture["fixture_id"]: fixture for fixture in family["fixtures"]
        }
        self.assertEqual(
            {
                "release_gate_policy_golden_bootstrap",
                "release_gate_policy_idempotency_bootstrap",
                "release_gate_policy_negative_bootstrap_relative_required",
            },
            set(fixtures),
        )
        self.assertEqual(
            fixtures["release_gate_policy_golden_bootstrap"]["document"],
            fixtures["release_gate_policy_idempotency_bootstrap"]["document"],
        )
        for fixture_id, fixture in fixtures.items():
            document = fixture["document"]
            unsigned = {
                key: value for key, value in document.items()
                if key != "policy_digest"
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-gate-policy:v1\0"
                    + canonical_bytes(unsigned)
                ).hexdigest(),
                document["policy_digest"],
                fixture_id,
            )
            candidate_projection = {
                key: document[key]
                for key in (
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
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:candidate-digest:v1\0"
                    + canonical_bytes(candidate_projection)
                ).hexdigest(),
                document["candidate_digest"],
                fixture_id,
            )
            threshold_projection = {
                key: document[key]
                for key in (
                    "absolute_gates",
                    "relative_gates",
                    "infrastructure_reason_codes",
                )
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-threshold-table:v1\0"
                    + canonical_bytes(threshold_projection)
                ).hexdigest(),
                document["threshold_table_digest"],
                fixture_id,
            )
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-profile-budgets:v1\0"
                    + canonical_bytes(document["profile_budgets"])
                ).hexdigest(),
                document["profile_budget_digest"],
                fixture_id,
            )
            canary_projection = {
                key: document[key]
                for key in (
                    "canary_stages",
                    "canary_platform_failure_rate_max_bps",
                    "canary_relative_platform_failure_increase_max_bps",
                    "canary_p95_wall_time_increase_max_bps",
                    "canary_p95_cost_increase_max_bps",
                )
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-canary-plan:v1\0"
                    + canonical_bytes(canary_projection)
                ).hexdigest(),
                document["canary_plan_digest"],
                fixture_id,
            )

        golden = fixtures["release_gate_policy_golden_bootstrap"]["document"]
        for field in ("absolute_gates", "relative_gates", "profile_budgets"):
            identity = "profile_id" if field == "profile_budgets" else "gate_id"
            values = [item[identity] for item in golden[field]]
            self.assertEqual(sorted(set(values)), values)
        self.assertEqual(
            sorted(set(golden["infrastructure_reason_codes"])),
            golden["infrastructure_reason_codes"],
        )
        self.assertEqual(
            ["CAPACITY_5", "CAPACITY_25", "FULL_CAPACITY"],
            [item["stage_id"] for item in golden["canary_stages"]],
        )
        self.assertEqual("BOOTSTRAP", golden["release_mode"])
        self.assertIsNone(golden["stable_package"])
        self.assertIsNone(golden["stable_candidate_digest"])
        self.assertIsNone(golden["stable_control_plane_digest"])
        self.assertTrue(all(
            item["applicability"] == "NOT_APPLICABLE"
            for item in golden["relative_gates"]
        ))
        self.assertLess(golden["issued_at"], golden["expires_at"])
        negative = fixtures[
            "release_gate_policy_negative_bootstrap_relative_required"
        ]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertIn(
            "REQUIRED",
            [item["applicability"] for item in negative["document"]["relative_gates"]],
        )

    def test_release_gate_report_freezes_reproducible_three_state_result(self) -> None:
        path = SOURCE_ROOT / "families/release-gate-report.json"
        family = json.loads(path.read_text(encoding="utf-8"))
        loaded = load_family(path, "release-gate-report", {}, set())
        schema = loaded["schemas"][0]
        self.assertEqual("release-gate-report/v1", schema["$id"])
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            {
                "document_rules": ["release_gate_report"],
                "contextual_helpers": [
                    "evaluate_release_gate",
                    "verify_release_gate_report_context",
                ],
            },
            schema["x-pullwise-semantics"],
        )
        properties = schema["properties"]
        self.assertEqual(
            "benchmark-bundle/v1",
            properties["benchmark_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "release-gate-policy/v1",
            properties["policy_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(["PASS", "FAIL", "INDETERMINATE"], properties["verdict"]["enum"])
        self.assertEqual([0, 1, 2], properties["exit_code"]["enum"])
        self.assertEqual(
            [
                "BASELINE_INCOMPARABLE",
                "EVALUATOR_FAILURE",
                "EVIDENCE_MISSING",
                "EVIDENCE_STALE",
                "ORACLE_RUBRIC_CONFLICT",
                "SAMPLE_INSUFFICIENT",
                "TIMEOUT",
                "ZERO_DENOMINATOR",
            ],
            properties["indeterminate_reason_codes"]["items"]["enum"],
        )
        self.assertEqual(0, properties["valid_sample_count"]["minimum"])
        self.assertEqual(
            [{"type": "null"}, {"type": "integer", "minimum": 0,
             "maximum": 9007199254740991}],
            properties["absolute_results"]["items"]["properties"][
                "observed_value"
            ]["oneOf"],
        )
        profile_properties = properties["profile_results"]["items"][
            "properties"
        ]
        for field in ("wall_ms", "token_count", "cost_microusd"):
            self.assertEqual(
                [
                    {"type": "null"},
                    {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 9007199254740991,
                    },
                ],
                profile_properties[field]["oneOf"],
            )

        fixtures = {
            fixture["fixture_id"]: fixture for fixture in family["fixtures"]
        }
        self.assertEqual(
            {
                "release_gate_report_golden_bootstrap_pass",
                "release_gate_report_idempotency_bootstrap_pass",
                "release_gate_report_negative_exit_verdict_mismatch",
            },
            set(fixtures),
        )
        self.assertEqual(
            fixtures["release_gate_report_golden_bootstrap_pass"]["document"],
            fixtures["release_gate_report_idempotency_bootstrap_pass"]["document"],
        )
        for fixture_id, fixture in fixtures.items():
            document = fixture["document"]
            unsigned = {
                key: value for key, value in document.items()
                if key != "report_digest"
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-gate-report:v1\0"
                    + canonical_bytes(unsigned)
                ).hexdigest(),
                document["report_digest"],
                fixture_id,
            )

        golden = fixtures["release_gate_report_golden_bootstrap_pass"]["document"]
        self.assertEqual(("PASS", 0), (golden["verdict"], golden["exit_code"]))
        self.assertEqual(
            golden["raw_sample_count"],
            golden["valid_sample_count"] + golden["excluded_sample_count"],
        )
        self.assertEqual(
            golden["excluded_sample_count"],
            sum(item["count"] for item in golden["excluded_reason_counts"]),
        )
        for field in ("absolute_results", "relative_results", "profile_results"):
            identity = "profile_id" if field == "profile_results" else "gate_id"
            values = [item[identity] for item in golden[field]]
            self.assertEqual(sorted(set(values)), values)
        self.assertTrue(all(
            item["applicability"] == "NOT_APPLICABLE"
            and item["status"] == "NOT_APPLICABLE"
            for item in golden["relative_results"]
        ))
        negative = fixtures["release_gate_report_negative_exit_verdict_mismatch"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertEqual("PASS", negative["document"]["verdict"])
        self.assertEqual(1, negative["document"]["exit_code"])

    def test_release_gate_attestation_binds_only_an_exact_pass_report(self) -> None:
        path = SOURCE_ROOT / "families/release-gate-attestation.json"
        family = json.loads(path.read_text(encoding="utf-8"))
        loaded = load_family(path, "release-gate-attestation", {}, set())
        schema = loaded["schemas"][0]
        self.assertEqual("release-gate-attestation/v1", schema["$id"])
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            {
                "document_rules": ["release_gate_attestation"],
                "contextual_helpers": ["verify_release_gate_attestation_context"],
            },
            schema["x-pullwise-semantics"],
        )
        properties = schema["properties"]
        self.assertEqual(
            "release-gate-policy/v1",
            properties["policy_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "release-gate-report/v1",
            properties["report_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual("PASS", properties["attested_verdict"]["const"])
        self.assertEqual(0, properties["attested_exit_code"]["const"])
        self.assertEqual("release_operator", properties["signer_role"]["const"])
        self.assertEqual("Ed25519", properties["signature_algorithm"]["const"])
        self.assertNotIn("signature_contract", schema["x-pullwise-semantics"])

        fixtures = {
            fixture["fixture_id"]: fixture for fixture in family["fixtures"]
        }
        self.assertEqual(
            {
                "release_gate_attestation_golden_bootstrap_pass",
                "release_gate_attestation_idempotency_bootstrap_pass",
                "release_gate_attestation_negative_validity_window",
            },
            set(fixtures),
        )
        self.assertEqual(
            fixtures["release_gate_attestation_golden_bootstrap_pass"]["document"],
            fixtures["release_gate_attestation_idempotency_bootstrap_pass"]["document"],
        )
        for fixture_id, fixture in fixtures.items():
            document = fixture["document"]
            unsigned = {
                key: value for key, value in document.items()
                if key != "attestation_digest"
            }
            self.assertEqual(
                hashlib.sha256(
                    b"pullwise:release-gate-attestation:v1\0"
                    + canonical_bytes(unsigned)
                ).hexdigest(),
                document["attestation_digest"],
                fixture_id,
            )

        golden = fixtures["release_gate_attestation_golden_bootstrap_pass"]["document"]
        self.assertEqual(("PASS", 0), (
            golden["attested_verdict"], golden["attested_exit_code"]
        ))
        self.assertLess(golden["issued_at"], golden["expires_at"])
        negative = fixtures["release_gate_attestation_negative_validity_window"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertEqual("2026-07-31T01:00:00.000Z", negative["document"]["expires_at"])


if __name__ == "__main__":
    unittest.main()
