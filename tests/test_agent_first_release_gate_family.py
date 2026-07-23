from __future__ import annotations

import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle import REQUIRED_FAMILIES
from pullwise_server.agent_first_contract_bundle_source import load_family


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

    def test_benchmark_bundle_freezes_reproducible_input_shape(self) -> None:
        path = SOURCE_ROOT / families/benchmark-bundle.json
        family = json.loads(path.read_text(encoding=utf-8))
        loaded = load_family(path, benchmark-bundle, {}, set())
        self.assertEqual(
            [benchmark-bundle/v1],
            [schema[$id] for schema in loaded[schemas]],
        )
        schema = loaded[schemas][0]
        self.assertIs(False, schema[additionalProperties])
        self.assertEqual(set(schema[required]), set(schema[properties]))
        self.assertEqual(
            {
                document_rules: [benchmark_bundle],
                contextual_helpers: [],
            },
            schema[x-pullwise-semantics],
        )
        self.assertEqual(
            {
                field: bundle_digest,
                domain: pullwise:benchmark-bundle:v1,
            },
            schema[x-pullwise-digest],
        )
        properties = schema[properties]
        self.assertEqual(current-package-ref/v1, properties[package][$ref])
        self.assertEqual(
            (3, 3),
            (properties[seeds][minItems], properties[seeds][maxItems]),
        )
        self.assertEqual(3, properties[repeats_per_task][const])
        self.assertEqual(120, properties[known_gold_task_count][minimum])
        self.assertEqual(
            15,
            properties[unknown_families][items][properties][task_count][
                minimum
            ],
        )
        self.assertEqual(50, properties[oracle_positive_finding_count][minimum])
        coverage = properties[cluster_coverage][items][properties]
        for field in (
            real_fix_tasks,
            bad_or_incomplete_patch_tasks,
            fake_success_or_zero_test_tasks,
            environment_or_capability_failure_tasks,
            adversarial_input_tasks,
        ):
            self.assertEqual(3, coverage[field][minimum])
        self.assertEqual(Ed25519, properties[signature_algorithm][const])
        self.assertEqual(benchmark_owner, properties[signer_role][const])

        fixtures = {
            fixture[fixture_id]: fixture for fixture in family[fixtures]
        }
        self.assertEqual(
            {
                benchmark_bundle_golden_current,
                benchmark_bundle_idempotency_current,
                benchmark_bundle_negative_unsorted_seeds,
            },
            set(fixtures),
        )
        self.assertEqual(
            fixtures[benchmark_bundle_golden_current][document],
            fixtures[benchmark_bundle_idempotency_current][document],
        )
        self.assertEqual(
            CONTRACT_DOCUMENT_INVALID,
            fixtures[benchmark_bundle_negative_unsorted_seeds][expected_code],
        )


if __name__ == "__main__":
    unittest.main()
