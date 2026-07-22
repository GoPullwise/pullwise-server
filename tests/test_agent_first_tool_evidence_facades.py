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
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def seal(schema: dict[str, object], value: dict[str, object]) -> dict[str, object]:
    result = deepcopy(value)
    spec = schema["x-pullwise-digest"]
    field = spec["field"]
    unsigned = {key: item for key, item in result.items() if key != field}
    result[field] = hashlib.sha256(
        spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
    ).hexdigest()
    return result


class AgentFirstToolEvidenceFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(
            (FAMILY_ROOT / "tool-evidence.json").read_text(encoding="utf-8")
        )
        core = json.loads(
            (FAMILY_ROOT / "core.json").read_text(encoding="utf-8")
        )
        errors = json.loads(
            (FAMILY_ROOT / "receipt-error.json").read_text(encoding="utf-8")
        )
        error_fixture = next(
            item
            for item in errors["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        families = [
            core,
            cls.family,
            {
                "family_id": "receipt-error",
                "schemas": [],
                "fixtures": [error_fixture],
            },
        ]
        payload = canonical_bytes(
            {"root_manifest": {"schema_registry": []}, "families": families}
        )
        render_args = (
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(payload).hexdigest(),
            payload,
        )
        cls.python = types.ModuleType("_tool_evidence_python_facade")
        exec(render_python_wrapper(*render_args), cls.python.__dict__)
        cls.npm = render_npm_wrapper(*render_args)
        cls.schemas = {item["$id"]: item for item in cls.family["schemas"]}
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def request(self) -> dict[str, object]:
        invocation = self.document("tool_golden_invocation")
        return {
            "schema_id": "agent-tool-request/v1",
            "idempotency_key": invocation["idempotency_key"],
            "tool_key": invocation["tool_key"],
            "tool_input": deepcopy(invocation["tool_input"]),
        }

    def source_state(self, manifest: str = "0") -> dict[str, object]:
        invocation = self.document("tool_golden_invocation")
        return seal(
            self.schemas["source-state/v1"],
            {
                "schema_id": "source-state/v1",
                "task_id": invocation["task_id"],
                "attempt_id": invocation["attempt_id"],
                "native_epoch": invocation["native_epoch"],
                "repository_root_id": "f" * 64,
                "entry_count": 1,
                "manifest_sha256": manifest * 64,
            },
        )

    def source_content(self, raw: bytes = b"hello") -> dict[str, object]:
        return seal(
            self.schemas["source-content/v1"],
            {
                "schema_id": "source-content/v1",
                "media_type": "application/octet-stream",
                "encoding": "base64",
                "data_base64": base64.b64encode(raw).decode("ascii"),
                "byte_sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
        )

    def positive_context_operations(self) -> list[dict[str, object]]:
        invocation = self.document("tool_golden_invocation")
        intent = self.document("tool_crash_after_intent")
        capability = self.document("tool_golden_dispatch_capability")
        source = self.source_state()
        return [
            {
                "kind": "invocation",
                "args": [
                    self.request(),
                    invocation,
                    self.document("tool_golden_current_catalog"),
                ],
            },
            {"kind": "begin", "args": [invocation, intent, capability]},
            {"kind": "consume", "args": [intent, capability, []]},
            {
                "kind": "settlement",
                "args": [
                    invocation,
                    intent,
                    self.document("tool_golden_local_receipt"),
                    self.document("tool_golden_r0_payload"),
                    self.document("tool_golden_r0_result"),
                    source,
                    source,
                ],
            },
        ]

    def test_all_rule_ids_and_context_helpers_execute_with_exact_parity(self) -> None:
        operations: list[dict[str, object]] = []
        for fixture in self.family["fixtures"]:
            operations.append(
                {
                    "kind": "document",
                    "schema_id": fixture["schema_id"],
                    "args": [deepcopy(fixture["document"])],
                }
            )
        operations.extend(
            [
                {
                    "kind": "document",
                    "schema_id": "agent-tool-request/v1",
                    "args": [self.request()],
                },
                {
                    "kind": "document",
                    "schema_id": "source-content/v1",
                    "args": [self.source_content()],
                },
                {
                    "kind": "document",
                    "schema_id": "source-state/v1",
                    "args": [self.source_state()],
                },
            ]
        )
        operations.extend(self.positive_context_operations())

        results = self.assert_parity(operations)

        negative_index = next(
            index
            for index, fixture in enumerate(self.family["fixtures"])
            if fixture["fixture_id"] == "tool_negative_agent_selected_authority"
        )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", results[negative_index]["code"])
        for index, result in enumerate(results):
            if index != negative_index:
                self.assertTrue(result["ok"], (index, result))
        self.assertEqual(
            [{"ok": True, "value": True}] * 4,
            results[-4:],
        )

    def test_source_content_and_path_adversaries_are_rejected(self) -> None:
        missing_padding = self.source_content(b"a")
        missing_padding["data_base64"] = "YQ"
        missing_padding = seal(self.schemas["source-content/v1"], missing_padding)
        noncanonical = self.source_content(b"\0")
        noncanonical["data_base64"] = "AB=="
        noncanonical = seal(self.schemas["source-content/v1"], noncanonical)
        wrong_size = self.source_content()
        wrong_size["size_bytes"] += 1
        wrong_size = seal(self.schemas["source-content/v1"], wrong_size)
        wrong_hash = self.source_content()
        wrong_hash["byte_sha256"] = "9" * 64
        wrong_hash = seal(self.schemas["source-content/v1"], wrong_hash)
        operations = [
            {
                "kind": "document",
                "schema_id": "source-content/v1",
                "args": [document],
            }
            for document in (missing_padding, noncanonical, wrong_size, wrong_hash)
        ]
        for path in ("/etc/passwd", "../secret", "a/../secret", "a\\b"):
            request = self.request()
            request["tool_input"]["relative_path"] = path
            operations.append(
                {
                    "kind": "document",
                    "schema_id": "agent-tool-request/v1",
                    "args": [request],
                }
            )

        results = self.assert_parity(operations)

        self.assertEqual(
            [
                "SOURCE_CONTENT_BASE64_INVALID",
                "SOURCE_CONTENT_BASE64_NONCANONICAL",
                "SOURCE_CONTENT_SIZE_MISMATCH",
                "SOURCE_CONTENT_SHA256_MISMATCH",
            ]
            + ["TOOL_SOURCE_PATH_INVALID"] * 4,
            [item["detail"] for item in results],
        )

    def test_context_drift_and_one_shot_reuse_fail_closed(self) -> None:
        invocation = self.document("tool_golden_invocation")
        intent = self.document("tool_crash_after_intent")
        capability = self.document("tool_golden_dispatch_capability")
        mismatched_intent = deepcopy(intent)
        mismatched_intent["idempotency_key"] = "invoke:other"
        mismatched_intent = seal(
            self.schemas["tool-dispatch-intent/v1"], mismatched_intent
        )
        wrong_receipt = self.document("tool_golden_local_receipt")
        wrong_receipt["elapsed_ms"] += 1
        wrong_receipt = seal(self.schemas["local-tool-receipt/v1"], wrong_receipt)
        source = self.source_state()
        changed = self.source_state("9")
        operations = [
            {"kind": "begin", "args": [invocation, mismatched_intent, capability]},
            {
                "kind": "consume",
                "args": [intent, capability, [capability["capability_digest"]]],
            },
            {"kind": "consume", "args": [intent, capability, ["f" * 64, "e" * 64]]},
            {
                "kind": "settlement",
                "args": [
                    invocation,
                    intent,
                    wrong_receipt,
                    self.document("tool_golden_r0_payload"),
                    self.document("tool_golden_r0_result"),
                    source,
                    source,
                ],
            },
            {
                "kind": "settlement",
                "args": [
                    invocation,
                    intent,
                    self.document("tool_golden_local_receipt"),
                    self.document("tool_golden_r0_payload"),
                    self.document("tool_golden_r0_result"),
                    source,
                    changed,
                ],
            },
        ]

        results = self.assert_parity(operations)

        self.assertEqual(
            [
                "TOOL_INTENT_BINDING_INVALID",
                "CAPABILITY_ALREADY_CONSUMED",
                "TOOL_CAPABILITY_CONSUMPTION_INVALID",
                "LOCAL_RECEIPT_TIMING_INVALID",
                "SOURCE_STATE_CHANGED",
            ],
            [item["detail"] for item in results],
        )

    def python_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        results = []
        helpers = {
            "invocation": self.python.validate_tool_invocation_binding,
            "begin": self.python.validate_tool_journal_begin,
            "consume": self.python.validate_tool_capability_consumption,
            "settlement": self.python.validate_tool_journal_settlement,
        }
        for operation in operations:
            try:
                if operation["kind"] == "document":
                    schema_id = operation["schema_id"]
                    if "x-pullwise-digest" in self.schemas[schema_id]:
                        value = self.python.verify_document_digest(
                            schema_id, *operation["args"]
                        )
                    else:
                        value = self.python.validate_document(
                            schema_id, *operation["args"]
                        )
                else:
                    value = helpers[operation["kind"]](*operation["args"])
            except self.python.ContractValidationError as error:
                results.append(
                    {"ok": False, "code": error.code, "detail": error.detail, "path": error.path}
                )
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="tool-evidence-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as f from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const aliases = [",
                        "  ['validate_tool_invocation_binding', 'validateToolInvocationBinding'],",
                        "  ['validate_tool_journal_begin', 'validateToolJournalBegin'],",
                        "  ['validate_tool_capability_consumption', 'validateToolCapabilityConsumption'],",
                        "  ['validate_tool_journal_settlement', 'validateToolJournalSettlement'],",
                        "];",
                        "if (aliases.some(([snake, camel]) => f[snake] !== f[camel]))",
                        "  throw new Error('tool helper alias mismatch');",
                        "const helpers = {invocation: f.validateToolInvocationBinding,",
                        "  begin: f.validateToolJournalBegin,",
                        "  consume: f.validateToolCapabilityConsumption,",
                        "  settlement: f.validateToolJournalSettlement};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    let value;",
                        "    if (operation.kind === 'document') {",
                        "      value = f.schema(operation.schema_id)['x-pullwise-digest']",
                        "        ? await f.verifyDocumentDigest(operation.schema_id, ...operation.args)",
                        "        : f.validateDocument(operation.schema_id, ...operation.args);",
                        "    } else value = await helpers[operation.kind](...operation.args);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) { results.push({ok: false, code: error.code,",
                        "      detail: error.detail, path: error.path}); }",
                        "}",
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

    def assert_parity(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        python = self.python_results(operations)
        self.assertEqual(python, self.node_results(operations))
        return python


if __name__ == "__main__":
    unittest.main()
