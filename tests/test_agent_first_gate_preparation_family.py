from __future__ import annotations

from pathlib import Path
import re
import unittest

from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)
from tests.agent_first_task_evidence_support import (
    ordered_unique,
    sealed,
    valid_actor,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/gate-preparation.json"
)
SCHEMA_IDS = (
    "debug-redaction-plan/v1",
    "publication-content-manifest/v1",
    "terminalization-fact/v1",
)
DEBUG_INPUT_SCHEMA_IDS = {
    "execution-state-manifest/v1",
    "local-tool-receipt/v1",
    "observation/v1",
    "stable-error/v1",
}
PUBLICATION_SOURCE_SCHEMA_IDS = {
    "change-set/v1",
    "completion-proposal/v1",
    "observation/v1",
    "source-content/v1",
    "task-report/v1",
    "task-request/v1",
    "verification-attestation/v1",
    "verifier-work-report/v1",
}
TERMINAL_EVIDENCE_SCHEMA_IDS = {
    "budget-summary/v1",
    "effective-execution-policy/v1",
    "execution-state-manifest/v1",
    "local-tool-receipt/v1",
    "requirement-ledger/v1",
    "source-tree-manifest/v1",
    "stable-error/v1",
    "task-request/v1",
}
AUTHORITATIVE_ACTOR_KINDS = {
    "server_control",
    "system_reconciler",
    "worker_control",
}
TERMINAL_REASON_CODES = {
    "BUDGET_EXHAUSTED",
    "CAPABILITY_UNAVAILABLE",
    "DEADLINE_REACHED",
    "INTERACTION_UNAVAILABLE",
    "POLICY_INVARIANT_BROKEN",
    "PROTOCOL_FAILURE",
    "RUNTIME_FAILURE",
    "STORAGE_FAILURE",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AgentFirstGatePreparationFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = load_family(FAMILY_PATH, "gate-preparation", {}, set())
        cls.schemas = {
            schema["$id"]: schema for schema in cls.family["schemas"]
        }
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for fixture in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return self.fixtures[fixture_id]["document"]

    @staticmethod
    def ref_key(ref: dict[str, object]) -> tuple[object, ...]:
        return (
            ref["content_schema_id"],
            ref["artifact_id"],
            ref["sha256"],
        )

    def valid_debug_plan(self, document: dict[str, object]) -> bool:
        return (
            set(document)
            == set(self.schemas["debug-redaction-plan/v1"]["required"])
            and sealed(document, self.schemas["debug-redaction-plan/v1"])
            and document["allowed_json_pointers"]
            == sorted(set(document["allowed_json_pointers"]))
            and document["rule_ids"] == sorted(set(document["rule_ids"]))
            and ordered_unique(document["debug_input_refs"], self.ref_key)
            and all(
                valid_content_ref(ref, DEBUG_INPUT_SCHEMA_IDS)
                for ref in document["debug_input_refs"]
            )
        )

    def valid_publication_manifest(self, document: dict[str, object]) -> bool:
        entries = document["entries"]
        if not (
            set(document)
            == set(
                self.schemas["publication-content-manifest/v1"]["required"]
            )
            and sealed(
                document,
                self.schemas["publication-content-manifest/v1"],
            )
            and document["entry_count"] == len(entries)
            and ordered_unique(entries, lambda item: item["json_pointer"])
        ):
            return False
        for entry in entries:
            if set(entry) != {
                "json_pointer",
                "content_kind",
                "source_ref",
                "inline_digest",
                "redaction_receipt",
            }:
                return False
            source_ref = entry["source_ref"]
            inline_digest = entry["inline_digest"]
            if entry["content_kind"] == "artifact_bytes":
                source_valid = (
                    inline_digest is None
                    and valid_content_ref(
                        source_ref,
                        PUBLICATION_SOURCE_SCHEMA_IDS,
                    )
                )
                original_sha256 = (
                    source_ref["sha256"] if source_valid else None
                )
            elif entry["content_kind"] in {
                "diagnostic_summary",
                "unstructured_string",
            }:
                source_valid = (
                    source_ref is None
                    and isinstance(inline_digest, str)
                    and HEX64.fullmatch(inline_digest) is not None
                )
                original_sha256 = inline_digest if source_valid else None
            else:
                return False
            receipt = entry["redaction_receipt"]
            if not (
                source_valid
                and set(receipt)
                == {
                    "policy_digest",
                    "original_sha256",
                    "redacted_sha256",
                    "status",
                    "receipt_digest",
                }
                and receipt["policy_digest"]
                == document["redaction_policy_digest"]
                and receipt["original_sha256"] == original_sha256
                and receipt["status"] == "passed"
                and HEX64.fullmatch(receipt["redacted_sha256"]) is not None
                and HEX64.fullmatch(receipt["receipt_digest"]) is not None
            ):
                return False
        return True

    def valid_terminalization_fact(self, document: dict[str, object]) -> bool:
        source = document["source"]
        evidence_refs = document["evidence_refs"]
        reason_code = document["reason_code"]
        expected_idempotency_key = (
            f"terminalize:{reason_code.lower()}:"
            f"{document['observed_task_version']}"
        )
        return (
            set(document)
            == set(self.schemas["terminalization-fact/v1"]["required"])
            and sealed(document, self.schemas["terminalization-fact/v1"])
            and reason_code in TERMINAL_REASON_CODES
            and document["idempotency_key"] == expected_idempotency_key
            and valid_actor(source)
            and source["kind"] in AUTHORITATIVE_ACTOR_KINDS
            and ordered_unique(evidence_refs, self.ref_key)
            and all(
                valid_content_ref(ref, TERMINAL_EVIDENCE_SCHEMA_IDS)
                for ref in evidence_refs
            )
            and (
                reason_code != "BUDGET_EXHAUSTED"
                or any(
                    ref["content_schema_id"] == "budget-summary/v1"
                    for ref in evidence_refs
                )
            )
        )

    def test_public_source_loader_accepts_the_bounded_family(self) -> None:
        self.assertEqual(
            list(SCHEMA_IDS),
            [schema["$id"] for schema in self.family["schemas"]],
        )

    def test_debug_plan_requires_a_typed_rfc6901_allowlist(self) -> None:
        properties = self.schemas["debug-redaction-plan/v1"]["properties"]
        pointers = properties["allowed_json_pointers"]
        inputs = properties["debug_input_refs"]

        self.assertEqual(1, pointers.get("minItems"))
        self.assertEqual(
            "^(?:/(?:[^~/]|~0|~1)*)+$",
            pointers["items"]["pattern"],
        )
        self.assertEqual(
            DEBUG_INPUT_SCHEMA_IDS,
            set(inputs["items"]["x-pullwise-content-schema-ids"]),
        )

    def test_publication_entries_discriminate_artifact_and_inline_content(self) -> None:
        entry_schema = self.schemas["publication-content-manifest/v1"][
            "properties"
        ]["entries"]["items"]
        artifact_branch, inline_branch = entry_schema["oneOf"]

        self.assertEqual(
            "^(?:/(?:[^~/]|~0|~1)*)+$",
            entry_schema["properties"]["json_pointer"]["pattern"],
        )
        self.assertEqual(
            ["object", "object"],
            [artifact_branch.get("type"), inline_branch.get("type")],
        )
        self.assertEqual(
            "artifact_bytes",
            artifact_branch["properties"]["content_kind"]["const"],
        )
        self.assertEqual(
            {"diagnostic_summary", "unstructured_string"},
            set(inline_branch["properties"]["content_kind"]["enum"]),
        )
        self.assertEqual(
            PUBLICATION_SOURCE_SCHEMA_IDS,
            set(
                artifact_branch["properties"]["source_ref"][
                    "x-pullwise-content-schema-ids"
                ]
            ),
        )
        self.assertIsNone(
            artifact_branch["properties"]["inline_digest"]["const"]
        )
        self.assertIsNone(
            inline_branch["properties"]["source_ref"]["const"]
        )

    def test_terminalization_schema_matches_authoritative_triggers_and_replay_keys(self) -> None:
        properties = self.schemas["terminalization-fact/v1"]["properties"]

        self.assertEqual(
            TERMINAL_REASON_CODES,
            set(properties["reason_code"]["enum"]),
        )
        self.assertEqual(
            "^terminalize:[a-z][a-z0-9_]{2,95}:[1-9][0-9]{0,15}$",
            properties["idempotency_key"]["pattern"],
        )

    def test_schemas_are_closed_and_keep_typed_reference_annotations(self) -> None:
        helpers = {
            "debug-redaction-plan/v1": [],
            "publication-content-manifest/v1": [],
            "terminalization-fact/v1": [
                "verify_terminalization_fact_context"
            ],
        }
        for schema_id, schema in self.schemas.items():
            with self.subTest(schema_id=schema_id):
                self.assertIs(False, schema["additionalProperties"])
                self.assertEqual(
                    set(schema["required"]),
                    set(schema["properties"]),
                )
                self.assertEqual(
                    {
                        "document_rules": [
                            schema_id.removesuffix("/v1").replace("-", "_")
                        ],
                        "contextual_helpers": helpers[schema_id],
                    },
                    schema["x-pullwise-semantics"],
                )

        publication_entry = self.schemas[
            "publication-content-manifest/v1"
        ]["properties"]["entries"]["items"]
        self.assertIs(False, publication_entry["additionalProperties"])
        self.assertIs(
            False,
            publication_entry["properties"]["redaction_receipt"][
                "additionalProperties"
            ],
        )
        self.assertEqual(
            PUBLICATION_SOURCE_SCHEMA_IDS,
            set(
                publication_entry["properties"]["source_ref"]["oneOf"][0][
                    "x-pullwise-content-schema-ids"
                ]
            ),
        )
        self.assertEqual(
            TERMINAL_EVIDENCE_SCHEMA_IDS,
            set(
                self.schemas["terminalization-fact/v1"]["properties"][
                    "evidence_refs"
                ]["items"]["x-pullwise-content-schema-ids"]
            ),
        )

    def test_fixture_matrix_is_full_and_documents_keep_the_closed_shape(self) -> None:
        classes = {schema_id: set() for schema_id in SCHEMA_IDS}
        for fixture in self.family["fixtures"]:
            schema_id = fixture["schema_id"]
            classes[schema_id].add(fixture["fixture_class"])
            self.assertEqual(
                set(self.schemas[schema_id]["required"]),
                set(fixture["document"]),
                fixture["fixture_id"],
            )
            expected_code = (
                "CONTRACT_DOCUMENT_INVALID"
                if fixture["fixture_class"] == "negative"
                else None
            )
            self.assertEqual(expected_code, fixture["expected_code"])
        self.assertEqual(
            {
                schema_id: {"golden", "idempotency", "negative"}
                for schema_id in SCHEMA_IDS
            },
            classes,
        )

    def test_debug_plan_golden_and_retry_are_sealed_and_output_free(self) -> None:
        golden = self.document("gate_preparation_golden_debug_plan")
        retry = self.document("gate_preparation_idempotency_debug_plan")

        self.assertTrue(self.valid_debug_plan(golden))
        self.assertNotEqual("0" * 64, golden["plan_digest"])
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
        self.assertNotIn(
            "worker-debug-fragment/v1",
            {ref["content_schema_id"] for ref in golden["debug_input_refs"]},
        )

    def test_publication_manifest_golden_and_retry_bind_scanned_content(self) -> None:
        golden = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        retry = self.document(
            "gate_preparation_idempotency_publication_manifest"
        )

        self.assertTrue(self.valid_publication_manifest(golden))
        self.assertNotEqual("0" * 64, golden["manifest_digest"])
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))

    def test_terminalization_fact_golden_and_retry_are_authoritative(self) -> None:
        golden = self.document(
            "gate_preparation_golden_terminalization_fact"
        )
        retry = self.document(
            "gate_preparation_idempotency_terminalization_fact"
        )

        self.assertTrue(self.valid_terminalization_fact(golden))
        self.assertNotEqual("0" * 64, golden["fact_digest"])
        self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
        self.assertEqual(
            {"budget-summary/v1"},
            {ref["content_schema_id"] for ref in golden["evidence_refs"]},
        )

    def test_negative_fixtures_are_sealed_but_break_family_semantics(self) -> None:
        cases = (
            (
                "gate_preparation_negative_debug_output_ref",
                "debug-redaction-plan/v1",
                self.valid_debug_plan,
            ),
            (
                "gate_preparation_negative_publication_dual_source",
                "publication-content-manifest/v1",
                self.valid_publication_manifest,
            ),
            (
                "gate_preparation_negative_terminalization_actor",
                "terminalization-fact/v1",
                self.valid_terminalization_fact,
            ),
        )
        for fixture_id, schema_id, validator in cases:
            with self.subTest(fixture_id=fixture_id):
                fixture = self.fixtures[fixture_id]
                document = fixture["document"]
                self.assertEqual(
                    "CONTRACT_DOCUMENT_INVALID",
                    fixture["expected_code"],
                )
                self.assertTrue(sealed(document, self.schemas[schema_id]))
                self.assertFalse(validator(document))


if __name__ == "__main__":
    unittest.main()
