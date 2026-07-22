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
            {
                "document_rules": [
                    "derived_requirement_shape",
                    "requirement_id_source_kind_match",
                    "sorted_unique_requirement_links",
                    "utf8_nfc_byte_limits",
                ],
                "contextual_helpers": ["validate_requirement_entry_ingest"],
            },
            schemas["requirement-entry/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            {
                "document_rules": [
                    "entries_normative_ingest_then_append_order",
                    "ledger_digest_exact",
                    "sorted_unique_active_requirement_ids",
                ],
                "contextual_helpers": ["validate_requirement_ledger_transition"],
            },
            schemas["requirement-ledger/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            {
                "document_rules": [
                    "charter_digest_exact",
                    "sorted_unique_charter_sets",
                    "utf8_nfc_byte_limits",
                ],
                "contextual_helpers": ["validate_task_charter_transition"],
            },
            schemas["task-charter/v1"]["x-pullwise-semantics"],
        )
        self.assertEqual(
            {
                "document_rules": ["utf8_nfc_byte_limits", "waiver_time_order"],
                "contextual_helpers": ["verify_waiver_event_authority"],
                "signature_contract": {
                    "algorithm": "Ed25519",
                    "domain": "pullwise-waiver-event/v1",
                    "domain_separator": "NUL",
                    "encoding": "base64url_no_padding",
                    "signed_projection": "event_without_signature",
                },
            },
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
        self._validate_initial_ledger(ledger)

        no_rationale = fixtures[
            "requirements_negative_derived_mandatory_without_rationale"
        ]["document"]
        self.assertEqual("derived", no_rationale["source_kind"])
        self.assertTrue(no_rationale["mandatory"])
        self.assertEqual("", no_rationale["rationale"])

        cycle = fixtures["requirements_negative_derived_cycle"]["document"]
        with self.assertRaisesRegex(ValueError, "CONTRACT_DOCUMENT_INVALID"):
            self._validate_ledger_transition(ledger, cycle)

        predecessor = fixtures["requirements_negative_charter_predecessor"][
            "document"
        ]
        with self.assertRaisesRegex(ValueError, "CONTRACT_DOCUMENT_INVALID"):
            self._validate_charter_transition(
                {
                    "schema_id": "content-ref/v1",
                    "artifact_id": "art_00000000000000000000000000000001",
                    "content_schema_id": "task-charter/v1",
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                    "media_type": "application/json",
                    "encoding": "utf-8",
                },
                predecessor,
            )

        invalid_waiver = fixtures[
            "requirements_negative_waiver_empty_issuer_profile"
        ]["document"]
        signature_pattern = schemas["waiver-event/v1"]["properties"][
            "signature"
        ]["pattern"]
        self.assertIsNotNone(
            re.fullmatch(signature_pattern, invalid_waiver["signature"])
        )
        with self.assertRaisesRegex(ValueError, "WAIVER_INVALID"):
            self._verify_empty_issuer_profile(invalid_waiver, {})

    def _normative_ingest_key(self, entry: dict[str, object]) -> tuple[object, ...]:
        rank = {
            "user_objective": 0,
            "user_acceptance": 1,
            "user_constraint": 2,
            "delivery": 3,
            "policy": 4,
            "interaction": 5,
            "derived": 5,
        }
        return (
            rank[entry["source_kind"]],
            entry["ledger_version"] if rank[entry["source_kind"]] >= 5 else 0,
            entry["source_id"],
            entry["requirement_id"],
        )

    def _validate_initial_ledger(self, ledger: dict[str, object]) -> None:
        if ledger["ledger_version"] != 1:
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        if ledger["entries"] != sorted(
            ledger["entries"], key=self._normative_ingest_key
        ):
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        if any(entry["ledger_version"] != 1 for entry in ledger["entries"]):
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        self._assert_active_set_exact(ledger)

    def _validate_ledger_transition(
        self,
        previous: dict[str, object],
        candidate: dict[str, object],
    ) -> None:
        entries = candidate["entries"]
        if entries != sorted(entries, key=self._normative_ingest_key):
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        parents = {
            entry["requirement_id"]: set(entry["parent_requirement_ids"])
            for entry in entries
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(requirement_id: str) -> None:
            if requirement_id in visiting:
                raise ValueError("CONTRACT_DOCUMENT_INVALID")
            if requirement_id in visited:
                return
            visiting.add(requirement_id)
            for parent_id in parents[requirement_id]:
                if parent_id in parents:
                    visit(parent_id)
            visiting.remove(requirement_id)
            visited.add(requirement_id)

        for requirement_id in parents:
            visit(requirement_id)
        if candidate["task_id"] != previous["task_id"]:
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        if candidate["ledger_version"] != previous["ledger_version"] + 1:
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        previous_entries = {
            entry["requirement_id"]: entry for entry in previous["entries"]
        }
        candidate_entries = {
            entry["requirement_id"]: entry for entry in candidate["entries"]
        }
        if any(
            candidate_entries.get(key) != value
            for key, value in previous_entries.items()
        ):
            raise ValueError("CONTRACT_DOCUMENT_INVALID")
        self._assert_active_set_exact(candidate)

    def _assert_active_set_exact(self, ledger: dict[str, object]) -> None:
        superseded = {
            requirement_id
            for entry in ledger["entries"]
            for requirement_id in entry["supersedes"]
        }
        expected = sorted(
            entry["requirement_id"]
            for entry in ledger["entries"]
            if entry["requirement_id"] not in superseded
        )
        if ledger["active_requirement_ids"] != expected:
            raise ValueError("CONTRACT_DOCUMENT_INVALID")

    def _validate_charter_transition(
        self,
        expected_previous_ref: dict[str, object] | None,
        candidate: dict[str, object],
    ) -> None:
        if candidate["charter_version"] == 1:
            if candidate["previous_charter_ref"] is not None:
                raise ValueError("CONTRACT_DOCUMENT_INVALID")
            return
        if candidate["previous_charter_ref"] != expected_previous_ref:
            raise ValueError("CONTRACT_DOCUMENT_INVALID")

    def _verify_empty_issuer_profile(
        self,
        event: dict[str, object],
        keyring: dict[str, object],
    ) -> None:
        self.assertEqual({}, keyring)
        self.assertTrue(event["issuer"])
        raise ValueError("WAIVER_INVALID")

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
