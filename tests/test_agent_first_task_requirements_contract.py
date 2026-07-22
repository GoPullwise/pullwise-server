from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest

from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    REPO_ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "task-requirements.json"
)


class AgentFirstTaskRequirementsContractTest(unittest.TestCase):
    def test_exact_normative_requirement_ledger_charter_and_waiver_contracts(
        self,
    ) -> None:
        family = load_family(FAMILY_PATH, "task-requirements", {}, set())
        schemas = {item["$id"]: item for item in family["schemas"]}
        fixtures = {item["fixture_id"]: item for item in family["fixtures"]}

        self.assertEqual(
            [
                "requirement-entry/v1",
                "requirement-ledger/v1",
                "task-charter/v1",
                "waiver-event/v1",
            ],
            list(schemas),
        )
        expected_fields = {
            "requirement-entry/v1": {
                "schema_id",
                "requirement_id",
                "source_kind",
                "source_id",
                "statement",
                "mandatory",
                "necessity",
                "parent_requirement_ids",
                "introduced_by",
                "introduced_at",
                "ledger_version",
                "rationale",
                "supersedes",
            },
            "requirement-ledger/v1": {
                "schema_id",
                "task_id",
                "ledger_version",
                "entries",
                "active_requirement_ids",
                "ledger_digest",
            },
            "task-charter/v1": {
                "schema_id",
                "task_id",
                "charter_version",
                "previous_charter_ref",
                "objective_restated",
                "scope_in",
                "scope_out",
                "assumptions",
                "plan",
                "requirement_ids",
                "unresolved_questions",
                "delivery_plan",
                "created_by",
                "created_at",
                "digest",
            },
            "waiver-event/v1": {
                "schema_id",
                "waiver_id",
                "task_id",
                "requirement_id",
                "waived_ledger_version",
                "ledger_digest",
                "policy_version",
                "scope",
                "issuer",
                "key_id",
                "reason",
                "issued_at",
                "expires_at",
                "revokes_waiver_id",
                "signature",
            },
        }
        for schema_id, fields in expected_fields.items():
            with self.subTest(schema_id=schema_id):
                schema = schemas[schema_id]
                self.assertEqual(fields, set(schema["required"]))
                self.assertEqual(fields, set(schema["properties"]))
                self.assertFalse(schema["additionalProperties"])

        self.assertEqual(
            [
                "canonical_source_tuple_identity",
                "derived_requirement_authority",
                "requirement_id_source_kind_match",
                "sorted_unique_requirement_links",
                "utf8_nfc_byte_limits",
            ],
            schemas["requirement-entry/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            [
                "active_set_exact",
                "derived_graph_acyclic_same_task_lower_version",
                "entries_source_tuple_ordered",
                "entry_ledger_versions_valid",
                "ledger_digest_exact",
            ],
            schemas["requirement-ledger/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            [
                "charter_digest_exact",
                "previous_charter_exact_predecessor",
                "sorted_unique_charter_sets",
                "utf8_nfc_byte_limits",
            ],
            schemas["task-charter/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            [
                "ed25519_signature_binding",
                "issuer_policy_authority",
                "revocation_target_rules",
                "waiver_time_window",
            ],
            schemas["waiver-event/v1"]["x-pullwise-semantics"],
        )

        ledger = fixtures["requirements_golden_ledger"]["document"]
        charter = fixtures["requirements_golden_charter"]["document"]
        self._assert_digest(
            ledger,
            schemas["requirement-ledger/v1"]["x-pullwise-digest"],
        )
        self._assert_digest(
            charter,
            schemas["task-charter/v1"]["x-pullwise-digest"],
        )
        self.assertEqual(
            sorted(
                ledger["entries"],
                key=lambda item: (
                    item["source_kind"],
                    item["source_id"],
                    item["requirement_id"],
                ),
            ),
            ledger["entries"],
        )
        self.assertEqual(
            sorted(ledger["active_requirement_ids"]),
            ledger["active_requirement_ids"],
        )

        no_rationale = fixtures[
            "requirements_negative_derived_mandatory_without_rationale"
        ]["document"]
        self.assertEqual("derived", no_rationale["source_kind"])
        self.assertTrue(no_rationale["mandatory"])
        self.assertEqual("", no_rationale["rationale"])

        cycle = fixtures["requirements_negative_derived_cycle"]["document"]
        parents = {
            entry["requirement_id"]: set(entry["parent_requirement_ids"])
            for entry in cycle["entries"]
        }
        left, right = sorted(parents)
        self.assertIn(right, parents[left])
        self.assertIn(left, parents[right])

        invalid_waiver = fixtures[
            "requirements_negative_waiver_signature"
        ]["document"]
        signature_pattern = schemas["waiver-event/v1"]["properties"][
            "signature"
        ]["pattern"]
        self.assertIsNone(re.fullmatch(signature_pattern, invalid_waiver["signature"]))

    def _assert_digest(
        self,
        document: dict[str, object],
        specification: dict[str, str],
    ) -> None:
        field = specification["field"]
        unsigned = {key: value for key, value in document.items() if key != field}
        expected = hashlib.sha256(
            specification["domain"].encode("utf-8")
            + b"\0"
            + canonical_bytes(unsigned)
        ).hexdigest()
        self.assertEqual(expected, document[field])


if __name__ == "__main__":
    unittest.main()
