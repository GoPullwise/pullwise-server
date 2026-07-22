from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "change-set-patch.json"
)
SCHEMA_ID = "change-set-patch/v1"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstChangeSetPatchFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        cls.fixtures = {
            fixture["fixture_id"]: fixture for fixture in cls.family["fixtures"]
        }
        cls.golden = deepcopy(
            cls.fixtures["source_evidence_golden_patch"]["document"]
        )
        canonical = canonical_bytes({"families": [cls.family]})
        python_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_change_set_patch_python_facade")
        exec(python_bytes, cls.python.__dict__)
        cls.npm = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    @classmethod
    def reseal(
        cls,
        value: dict[str, object],
        *,
        domain: str = "pullwise:change-set-patch/v1",
        separator: bytes = b"\0",
    ) -> dict[str, object]:
        document = deepcopy(value)
        unsigned = {
            key: item for key, item in document.items() if key != "patch_digest"
        }
        document["patch_digest"] = hashlib.sha256(
            domain.encode("utf-8") + separator + canonical_bytes(unsigned)
        ).hexdigest()
        return document

    @classmethod
    def with_patch_bytes(cls, raw: bytes) -> dict[str, object]:
        document = deepcopy(cls.golden)
        document["data_base64"] = base64.b64encode(raw).decode("ascii")
        document["byte_sha256"] = hashlib.sha256(raw).hexdigest()
        document["size_bytes"] = len(raw)
        return cls.reseal(document)

    def python_results(
        self, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for document in documents:
            try:
                value = self.python.validate_document(SCHEMA_ID, document)
            except self.python.ContractValidationError as error:
                results.append(
                    {
                        "ok": False,
                        "code": error.code,
                        "detail": error.detail,
                        "path": error.path,
                    }
                )
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_results(
        self, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="change-set-patch-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const documents = {json.dumps(documents, separators=(',', ':'))};",
                        "const results = documents.map((document) => {",
                        "  try {",
                        "    return {ok: true, value: facade.validateDocument(",
                        f"      {json.dumps(SCHEMA_ID)}, document",
                        "    )};",
                        "  } catch (error) {",
                        "    return {ok: false, code: error.code,",
                        "      detail: error.detail, path: error.path};",
                        "  }",
                        "});",
                        "process.stdout.write(JSON.stringify(results));",
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

    def assert_facade_parity(
        self, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python_results = self.python_results(documents)
        self.assertEqual(python_results, self.node_results(documents))
        return python_results

    def test_source_fixtures_execute_with_stable_facade_parity(self) -> None:
        fixture_ids = [fixture["fixture_id"] for fixture in self.family["fixtures"]]
        documents = [
            deepcopy(self.fixtures[fixture_id]["document"])
            for fixture_id in fixture_ids
        ]

        results = self.assert_facade_parity(documents)

        self.assertEqual({"ok": True, "value": documents[0]}, results[0])
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "SOURCE_CONTENT_SHA256_MISMATCH",
                "path": "$.byte_sha256",
            },
            results[2],
        )
        for fixture_id, result in zip(fixture_ids, results):
            expected_code = self.fixtures[fixture_id]["expected_code"]
            self.assertEqual(expected_code, None if result["ok"] else result["code"])

    def test_adversarial_content_and_digest_cases_have_exact_parity(self) -> None:
        missing_padding = self.with_patch_bytes(b"a")
        missing_padding["data_base64"] = "YQ"
        missing_padding = self.reseal(missing_padding)

        noncanonical = self.with_patch_bytes(b"\0")
        noncanonical["data_base64"] = "AB=="
        noncanonical = self.reseal(noncanonical)

        wrong_size = deepcopy(self.golden)
        wrong_size["size_bytes"] += 1
        wrong_size = self.reseal(wrong_size)

        wrong_byte_digest = deepcopy(self.golden)
        wrong_byte_digest["byte_sha256"] = "0" * 64
        wrong_byte_digest = self.reseal(wrong_byte_digest)

        wrong_patch_digest = deepcopy(self.golden)
        wrong_patch_digest["patch_digest"] = "f" * 64

        wrong_domain = self.reseal(
            self.golden,
            domain="pullwise:source-content/v1",
        )
        missing_separator = self.reseal(self.golden, separator=b"")

        cases = [
            (
                missing_padding,
                "SOURCE_CONTENT_BASE64_INVALID",
                "$.data_base64",
            ),
            (
                noncanonical,
                "SOURCE_CONTENT_BASE64_NONCANONICAL",
                "$.data_base64",
            ),
            (wrong_size, "SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes"),
            (
                wrong_byte_digest,
                "SOURCE_CONTENT_SHA256_MISMATCH",
                "$.byte_sha256",
            ),
            (wrong_patch_digest, "CONTRACT_DIGEST_MISMATCH", "$.patch_digest"),
            (wrong_domain, "CONTRACT_DIGEST_MISMATCH", "$.patch_digest"),
            (missing_separator, "CONTRACT_DIGEST_MISMATCH", "$.patch_digest"),
        ]

        results = self.assert_facade_parity([document for document, _, _ in cases])

        for result, (_, detail, path) in zip(results, cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": path,
                },
                result,
            )


if __name__ == "__main__":
    unittest.main()
