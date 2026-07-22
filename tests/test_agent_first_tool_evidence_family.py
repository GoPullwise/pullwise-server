from __future__ import annotations

import json

from pullwise_server.agent_first_contract_bundle_registry import (
    CONTEXTUAL_HELPER_IDS,
    DOCUMENT_RULE_IDS,
)
from tests.agent_first_task_evidence_support import sealed
from tests.agent_first_tool_evidence_support import (
    RECEIPT_FAMILY_PATH,
    SCHEMA_IDS,
    SEMANTICS,
    ToolEvidenceCase,
)


class AgentFirstToolEvidenceFamilyTest(ToolEvidenceCase):
    def test_exact_closed_schema_and_semantic_registry(self) -> None:
        self.assertEqual("tool-evidence", self.family["family_id"])
        self.assertEqual(
            list(SCHEMA_IDS),
            [item["$id"] for item in self.family["schemas"]],
        )
        rules: set[str] = set()
        helpers: set[str] = set()
        for schema_id in SCHEMA_IDS:
            schema = self.schemas[schema_id]
            expected_rules, expected_helpers = SEMANTICS[schema_id]
            self.assertEqual(
                {
                    "document_rules": expected_rules,
                    "contextual_helpers": expected_helpers,
                },
                schema["x-pullwise-semantics"],
            )
            self.assertIs(False, schema["additionalProperties"])
            self.assertEqual(
                set(schema["required"]), set(schema["properties"])
            )
            rules.update(expected_rules)
            helpers.update(expected_helpers)
        self.assertTrue(rules.issubset(DOCUMENT_RULE_IDS))
        self.assertTrue(helpers.issubset(CONTEXTUAL_HELPER_IDS))

    def test_catalog_intent_capability_and_receipts_are_exact_types(self) -> None:
        catalog = self.schemas["tool-catalog/v1"]["properties"]["tools"]
        descriptor = catalog["items"]["properties"]
        self.assertEqual((1, 1), (catalog["minItems"], catalog["maxItems"]))
        self.assertEqual("internal.read_source", descriptor["tool_key"]["const"])
        self.assertEqual("source.read", descriptor["capability_id"]["const"])
        for field in (
            "uses_command",
            "uses_network",
            "uses_secret",
            "requests_approval",
        ):
            self.assertIs(False, descriptor[field]["const"])

        intent = self.schemas["tool-dispatch-intent/v1"]["properties"]
        self.assertEqual("INTENT", intent["state"]["const"])
        self.assertTrue(
            {"task_id", "idempotency_key", "tool_key", "tool_input"}
            .issubset(intent)
        )
        capability = self.schemas[
            "tool-dispatch-capability/v1"
        ]["properties"]
        self.assertEqual(
            "opaque_dispatch", capability["capability_kind"]["const"]
        )
        self.assertEqual(1, capability["max_uses"]["const"])
        self.assertNotIn("secret", capability)

        local = self.schemas["local-tool-receipt/v1"]["properties"]
        receipt_family = json.loads(
            RECEIPT_FAMILY_PATH.read_text(encoding="utf-8")
        )
        transport = next(
            item
            for item in receipt_family["schemas"]
            if item["$id"] == "server-transport-receipt/v1"
        )["properties"]
        self.assertEqual("local_tool", local["receipt_kind"]["const"])
        self.assertEqual(
            "server_transport", transport["receipt_kind"]["const"]
        )
        self.assertEqual("succeeded", local["status"]["const"])
        self.assertFalse(
            {"package", "receipt_id", "transport_epoch", "content_ref"}
            .intersection(local)
        )

    def test_all_fixtures_are_complete_and_digest_executable(self) -> None:
        self.assertEqual(
            sorted(self.fixtures),
            [item["fixture_id"] for item in self.family["fixtures"]],
        )
        for fixture_id, fixture in self.fixtures.items():
            with self.subTest(fixture_id=fixture_id):
                schema = self.schemas[fixture["schema_id"]]
                document = fixture["document"]
                self.assertTrue(
                    set(schema["required"]).issubset(document),
                    fixture_id,
                )
                if fixture["fixture_class"] != "negative":
                    self.assertEqual(
                        set(schema["properties"]), set(document), fixture_id
                    )
                if "x-pullwise-digest" in schema:
                    self.assertTrue(sealed(document, schema), fixture_id)
        crash = self.fixtures["tool_crash_after_intent"]
        self.assertEqual("crash", crash["fixture_class"])
        self.assertEqual("INVOCATION_PENDING", crash["expected_code"])
        negative = self.fixture("tool_negative_agent_selected_authority")
        self.assertEqual(
            set(self.schemas["agent-tool-request/v1"]["properties"])
            | {"owner_epoch"},
            set(negative),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
