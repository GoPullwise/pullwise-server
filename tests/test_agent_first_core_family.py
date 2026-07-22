from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"
FAMILY_PATH = SOURCE_ROOT / "families" / "core.json"
ERROR_FAMILY_PATH = SOURCE_ROOT / "families" / "receipt-error.json"
SCHEMA_IDS = (
    "canonical-document/v1",
    "canonical-json-profile/v1",
    "content-ref/v1",
    "current-package-ref/v1",
)
PAIR_IDS = {
    "canonical-document/v1": (
        "core_golden_canonical_document",
        "core_idempotency_canonical_document",
    ),
    "canonical-json-profile/v1": (
        "core_golden_canonical_profile",
        "core_idempotency_canonical_profile",
    ),
    "content-ref/v1": (
        "core_golden_content_ref",
        "core_idempotency_content_ref",
    ),
    "current-package-ref/v1": (
        "core_golden_current_package",
        "core_idempotency_current_package",
    ),
}


class AgentFirstCoreFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        cls.schemas = {item["$id"]: item for item in cls.family["schemas"]}
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }
        error_family = json.loads(ERROR_FAMILY_PATH.read_text(encoding="utf-8"))
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        minimal_bundle = {
            "root_manifest": {
                "schema_registry": [
                    {"schema_id": schema_id, "role": "public_document"}
                    for schema_id in SCHEMA_IDS
                ]
            },
            "families": [
                cls.family,
                {
                    "family_id": "receipt-error",
                    "schemas": [],
                    "fixtures": [error_fixture],
                },
            ],
        }
        canonical_bundle = json.dumps(
            minimal_bundle,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        content_sha256 = hashlib.sha256(canonical_bundle).hexdigest()
        root_sha256 = hashlib.sha256(b"core-family-test-root").hexdigest()
        wrapper_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            root_sha256,
            content_sha256,
            canonical_bundle,
        )
        cls.wrapper = ModuleType("_agent_first_core_test_wrapper")
        exec(wrapper_bytes, cls.wrapper.__dict__)

    def test_family_is_closed_and_fixture_matrix_is_executable(self) -> None:
        self.assertEqual("core", self.family["family_id"])
        self.assertEqual(list(SCHEMA_IDS), list(self.schemas))
        self.assertLessEqual(len(FAMILY_PATH.read_text().splitlines()), 600)
        self.assertEqual(
            sorted(self.fixtures),
            [item["fixture_id"] for item in self.family["fixtures"]],
        )

        classes: dict[str, set[str]] = {schema_id: set() for schema_id in SCHEMA_IDS}
        for fixture in self.family["fixtures"]:
            schema_id = fixture["schema_id"]
            fixture_class = fixture["fixture_class"]
            document = fixture["document"]
            classes[schema_id].add(fixture_class)
            self.assertEqual(schema_id, document["schema_id"])
            required = set(self.schemas[schema_id]["required"])
            self.assertLessEqual(required, set(document), fixture["fixture_id"])
            if fixture_class == "negative":
                with self.assertRaises(self.wrapper.ContractValidationError) as raised:
                    self.wrapper.validate_document(schema_id, document)
                self.assertEqual(fixture["expected_code"], raised.exception.code)
            else:
                self.assertIsNone(fixture["expected_code"])
                self.assertEqual(
                    document,
                    self.wrapper.validate_document(schema_id, document),
                )
        for schema_id, covered in classes.items():
            self.assertEqual(
                {"golden", "idempotency", "negative"}, covered, schema_id
            )

    def test_canonical_key_order_is_byte_exact_and_idempotent(self) -> None:
        for schema_id, (golden_id, idempotency_id) in PAIR_IDS.items():
            golden = self.fixtures[golden_id]["document"]
            idempotent = self.fixtures[idempotency_id]["document"]
            self.assertNotEqual(tuple(golden), tuple(idempotent), schema_id)
            expected = self.wrapper.canonical_validated_bytes(schema_id, golden)
            self.assertEqual(
                expected,
                self.wrapper.canonical_validated_bytes(schema_id, idempotent),
                schema_id,
            )
            self.assertEqual(
                expected,
                self.wrapper.canonical_document_bytes(
                    self.wrapper.validate_document(schema_id, golden)
                ),
            )

    def test_content_reference_is_closed_and_strict(self) -> None:
        schema = self.schemas["content-ref/v1"]
        properties = schema["properties"]
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(properties), set(schema["required"]))
        self.assertEqual("^art_[0-9a-f]{32}$", properties["artifact_id"]["pattern"])
        self.assertEqual(
            "^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$",
            properties["content_schema_id"]["pattern"],
        )
        self.assertEqual(["utf-8", "binary"], properties["encoding"]["enum"])
        self.assertLessEqual(
            {
                "core_negative_content_ref_artifact_prefix",
                "core_negative_content_ref_extra_field",
                "core_negative_content_ref_media_type",
            },
            set(self.fixtures),
        )

    def test_package_ref_is_structural_but_runtime_pin_is_exact(self) -> None:
        fixed = self.fixtures["core_golden_current_package"]["document"]
        self.assertEqual(
            fixed,
            self.wrapper.validate_document("current-package-ref/v1", fixed),
        )
        self.assertNotEqual(
            (fixed["content_sha256"], fixed["root_sha256"]),
            (self.wrapper.CONTENT_SHA256, self.wrapper.ROOT_SHA256),
        )
        with self.assertRaisesRegex(RuntimeError, "CURRENT_PACKAGE_PIN_MISMATCH"):
            self.wrapper.assert_pin(
                fixed["package_identity"],
                fixed["package_version"],
                fixed["content_sha256"],
                fixed["root_sha256"],
            )

        exact = self.wrapper.package_tuple()
        self.wrapper.validate_document("current-package-ref/v1", exact)
        self.wrapper.assert_pin(*self.wrapper.PACKAGE_TUPLE)
        for index in range(4):
            stale = list(self.wrapper.PACKAGE_TUPLE)
            stale[index] = (
                "@pullwise/wrong"
                if index == 0
                else "0.1.1"
                if index == 1
                else "0" * 64
            )
            with self.subTest(index=index), self.assertRaisesRegex(
                RuntimeError, "CURRENT_PACKAGE_PIN_MISMATCH"
            ):
                self.wrapper.assert_pin(*stale)


if __name__ == "__main__":
    unittest.main()
