from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    reference_dag,
    schema_edges,
    sha256,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"
OWNER_SCHEMA_ID = "\ud7ff/v1"
BMP_SCHEMA_ID = "\ue000/v1"
NON_BMP_SCHEMA_ID = "\U0001f600/v1"


class AgentFirstContractBundleNpmOrderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        published = build_bundle(SOURCE_ROOT)
        document = deepcopy(published.document)
        family = document["families"][-1]
        added_schemas = [
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
        family["schemas"].extend(added_schemas)
        for schema in added_schemas:
            edges = schema_edges(schema)
            family["schema_registry"].append(
                {
                    "schema_id": schema["$id"],
                    "family_id": family["family_id"],
                    "role": "public_document",
                    "references": sorted(
                        {edge["target_schema_id"] for edge in edges}
                    ),
                    "edges": edges,
                    "sha256": sha256(canonical_bytes(schema)),
                }
            )

        schema_owner = {
            schema["$id"]: item["family_id"]
            for item in document["families"]
            for schema in item["schemas"]
        }
        root = document["root_manifest"]
        root["schema_registry"] = [
            entry
            for item in document["families"]
            for entry in item["schema_registry"]
        ]
        root["fixture_registry"] = [
            entry
            for item in document["families"]
            for entry in item["fixture_registry"]
        ]
        root["reference_dag"] = reference_dag(document["families"], schema_owner)
        root["families"] = [
            {
                "family_id": item["family_id"],
                "schema_ids": [schema["$id"] for schema in item["schemas"]],
                "fixture_ids": [fixture["fixture_id"] for fixture in item["fixtures"]],
                "sha256": sha256(canonical_bytes(item)),
            }
            for item in document["families"]
        ]
        root.pop("root_sha256", None)
        root_sha256 = sha256(canonical_bytes(root))
        root["root_sha256"] = root_sha256
        payload = canonical_bytes(document)
        content_sha256 = sha256(payload)

        python_wrapper = render_python_wrapper(
            published.package_identity,
            published.package_version,
            root_sha256,
            content_sha256,
            payload,
        )
        cls.python_facade = types.ModuleType("_contract_bundle_ordering_python_facade")
        exec(python_wrapper, cls.python_facade.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            published.package_identity,
            published.package_version,
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
        self.assertTrue(self.python_facade.verify_bundle())

        result = self._node_result()

        self.assertEqual([NON_BMP_SCHEMA_ID, BMP_SCHEMA_ID], result["native_order"])
        self.assertEqual({"ok": True, "value": True}, result["verification"])

    def _node_result(self) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="contract-bundle-ordering-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        "let verification;",
                        "try {",
                        "  verification = {ok: true, value: await facade.verifyBundle()};",
                        "} catch (error) {",
                        "  verification = {",
                        "    ok: false, code: error.code, detail: error.detail, path: error.path,",
                        "  };",
                        "}",
                        "process.stdout.write(JSON.stringify({",
                        f"  native_order: [{json.dumps(BMP_SCHEMA_ID)}, {json.dumps(NON_BMP_SCHEMA_ID)}].sort(),",
                        "  verification,",
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
