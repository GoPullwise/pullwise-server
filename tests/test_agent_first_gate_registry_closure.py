from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_registry_closure import (
    validate_gate_predicate_registry,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"


class GateRegistryClosureError(ValueError):
    pass


class AgentFirstGateRegistryClosureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(FAMILY_ROOT.glob("*.json"))
        ]
        cls.schemas = {
            schema["$id"]: schema
            for family in families
            for schema in family["schemas"]
        }
        cls.schema_owner = {
            schema["$id"]: family["family_id"]
            for family in families
            for schema in family["schemas"]
        }
        fixtures = {
            fixture["fixture_id"]: fixture["document"]
            for family in families
            for fixture in family["fixtures"]
        }
        cls.registry = fixtures["gate_golden_independent_registry"]
        cls.stable_codes = {
            entry["code"]
            for entry in fixtures["error_golden_current_registry"]["entries"]
        }

    def validate(self, registry: dict[str, object]) -> None:
        validate_gate_predicate_registry(
            self.schemas,
            registry,
            self.stable_codes,
            self.schema_owner,
            GateRegistryClosureError,
        )

    def test_explicit_predicate_order_and_many_to_many_codes_close(self) -> None:
        self.validate(self.registry)

    def test_registry_order_cannot_drift_from_schema_contract(self) -> None:
        changed = deepcopy(self.registry)
        changed["predicates"][0], changed["predicates"][1] = (
            changed["predicates"][1],
            changed["predicates"][0],
        )

        with self.assertRaisesRegex(
            GateRegistryClosureError,
            "gate_predicate_registry_bijection_invalid",
        ):
            self.validate(changed)

    def test_every_declared_failure_code_has_a_predicate_consumer(self) -> None:
        changed = deepcopy(self.registry)
        removed = changed["predicates"][0]["failure_codes"].pop()
        self.assertFalse(
            any(
                removed in entry["failure_codes"]
                for entry in changed["predicates"]
            )
        )

        with self.assertRaisesRegex(
            GateRegistryClosureError,
            "gate_failure_code_coverage_invalid",
        ):
            self.validate(changed)

    def test_all_failure_codes_and_input_schemas_are_registered(self) -> None:
        unknown_code = deepcopy(self.registry)
        unknown_code["predicates"][0]["failure_codes"].append("ZZZ_UNKNOWN")
        with self.assertRaisesRegex(
            GateRegistryClosureError,
            "gate_failure_code_unregistered",
        ):
            self.validate(unknown_code)

        unknown_schema = deepcopy(self.registry)
        unknown_schema["predicates"][0]["input_schema_ids"].append("unknown/v1")
        with self.assertRaisesRegex(
            GateRegistryClosureError,
            "gate_input_schema_unregistered",
        ):
            self.validate(unknown_schema)


if __name__ == "__main__":
    unittest.main()
