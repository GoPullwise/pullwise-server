from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
from pullwise_server.agent_first_contract_bundle_source import (
    SEMANTIC_CYCLE_EXCEPTIONS,
    canonical_bytes,
    reference_dag,
    schema_edges,
    sha256,
)


OWNER_SCHEMA_ID = "\ud7ff/v1"
BMP_SCHEMA_ID = "\ue000/v1"
NON_BMP_SCHEMA_ID = "\U0001f600/v1"
MISSING_FIXTURE_ID = "core_golden_canonical_profile"


class AgentFirstContractBundleNpmOrderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        family_id = "unicode-code-point-order-probe"
        schemas = [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": OWNER_SCHEMA_ID,
                "type": "object",
                "properties": {},
                "additionalProperties": False,
                "x-pullwise-content-schema-ids": [
                    NON_BMP_SCHEMA_ID,
                    BMP_SCHEMA_ID,
                ],
            },
            cls._empty_schema(BMP_SCHEMA_ID),
            cls._empty_schema(NON_BMP_SCHEMA_ID),
        ]
        registry = []
        for schema in schemas:
            edges = schema_edges(schema)
            registry.append(
                {
                    "schema_id": schema["$id"],
                    "family_id": family_id,
                    "role": "public_document",
                    "references": sorted(
                        {edge["target_schema_id"] for edge in edges}
                    ),
                    "edges": edges,
                    "sha256": sha256(canonical_bytes(schema)),
                }
            )
        family = {
            "family_id": family_id,
            "schemas": schemas,
            "schema_registry": registry,
            "fixtures": [],
            "fixture_registry": [],
        }
        root_body = {
            "required_families": [family_id],
            "fixture_classes": [],
            "semantic_cycle_exceptions": list(SEMANTIC_CYCLE_EXCEPTIONS),
            "semantic_cycle_exceptions_sha256": sha256(
                canonical_bytes(list(SEMANTIC_CYCLE_EXCEPTIONS))
            ),
            "families": [
                {
                    "family_id": family_id,
                    "schema_ids": [schema["$id"] for schema in schemas],
                    "fixture_ids": [],
                    "sha256": sha256(canonical_bytes(family)),
                }
            ],
            "schema_registry": registry,
            "fixture_registry": [],
            "reference_dag": reference_dag(
                [family], {schema["$id"]: family_id for schema in schemas}
            ),
        }
        root_sha256 = sha256(canonical_bytes(root_body))
        document = {
            "package_identity": "@pullwise/unicode-order-probe",
            "package_version": "0.0.0",
            "root_manifest": {**root_body, "root_sha256": root_sha256},
            "families": [family],
        }
        # Preserve registry member insertion order so the public verifier reaches
        # its reference/DAG checks; all integrity digests above remain canonical.
        payload = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        content_sha256 = sha256(payload)

        python_wrapper = render_python_wrapper(
            document["package_identity"],
            document["package_version"],
            root_sha256,
            content_sha256,
            payload,
        )
        cls.python_facade = types.ModuleType("_contract_bundle_ordering_python_facade")
        exec(python_wrapper, cls.python_facade.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            document["package_identity"],
            document["package_version"],
            root_sha256,
            content_sha256,
            payload,
        )

    @staticmethod
    def _empty_schema(schema_id: str) -> dict[str, object]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": schema_id,
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def test_public_verify_bundle_matches_python_code_point_order(self) -> None:
        python_result = self._python_result()
        node_result = self._node_result()

        expected_python_order = [BMP_SCHEMA_ID, NON_BMP_SCHEMA_ID]
        self.assertEqual(expected_python_order, python_result["reference_order"])
        self.assertEqual(expected_python_order, python_result["edge_order"])
        self.assertEqual(
            [OWNER_SCHEMA_ID, BMP_SCHEMA_ID, NON_BMP_SCHEMA_ID],
            python_result["dag_order"],
        )
        self.assertEqual(
            [NON_BMP_SCHEMA_ID, BMP_SCHEMA_ID], node_result["native_utf16_order"]
        )
        self.assertEqual("post_reference_dag", python_result["stage"])
        self.assertEqual(python_result["stage"], node_result["stage"])

    def _python_result(self) -> dict[str, object]:
        root = self.python_facade.root_manifest()
        owner = next(
            item
            for item in root["reference_dag"]
            if item["schema_id"] == OWNER_SCHEMA_ID
        )
        try:
            self.python_facade.verify_bundle()
        except KeyError as error:
            if error.args == (MISSING_FIXTURE_ID,):
                return {
                    "stage": "post_reference_dag",
                    "reference_order": owner["references"],
                    "edge_order": [
                        edge["target_schema_id"] for edge in owner["edges"]
                    ],
                    "dag_order": [
                        item["schema_id"] for item in root["reference_dag"]
                    ],
                }
            raise
        self.fail("minimal ordering probe unexpectedly passed full bundle verification")

    def _node_result(self) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="contract-bundle-ordering-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            missing_message = json.dumps(
                f"UNKNOWN_CONTRACT_DOCUMENT: {MISSING_FIXTURE_ID}"
            )
            bmp_literal = json.dumps(BMP_SCHEMA_ID)
            non_bmp_literal = json.dumps(NON_BMP_SCHEMA_ID)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        "let stage;",
                        "try {",
                        "  await facade.verifyBundle();",
                        "  stage = 'verified';",
                        "} catch (error) {",
                        f"  if (error.message === {missing_message}) {{",
                        "    stage = 'post_reference_dag';",
                        "  } else {",
                        "    stage = error.detail ?? error.message;",
                        "  }",
                        "}",
                        "process.stdout.write(JSON.stringify({",
                        "  stage,",
                        "  native_utf16_order: "
                        f"[{bmp_literal}, {non_bmp_literal}].sort(),",
                        "}));",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
