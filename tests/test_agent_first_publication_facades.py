from __future__ import annotations

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


class AgentFirstPublicationFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(
            (FAMILY_ROOT / "task-publication.json").read_text(encoding="utf-8")
        )
        cls.core = json.loads(
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
            cls.core,
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
        cls.python = types.ModuleType("_publication_python_facade")
        exec(render_python_wrapper(*render_args), cls.python.__dict__)
        cls.npm = render_npm_wrapper(*render_args)
        cls.schemas = {item["$id"]: item for item in cls.family["schemas"]}
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def test_every_source_fixture_executes_and_exact_replay_is_stable(self) -> None:
        operations = [
            {
                "schema_id": fixture["schema_id"],
                "document": deepcopy(fixture["document"]),
            }
            for fixture in self.family["fixtures"]
        ]
        operations.extend(deepcopy(operations[:-1]))

        results = self.assert_parity(operations)

        for index, fixture in enumerate(self.family["fixtures"]):
            result = results[index]
            self.assertEqual(
                fixture["expected_code"],
                None if result["ok"] else result["code"],
                fixture["fixture_id"],
            )
        self.assertEqual(results[:4], results[5:])

    def test_registry_budget_and_report_semantics_reject_resealed_drift(self) -> None:
        registry = self.document("publication_golden_artifact_registry")
        registry["entries"][0]["content_schema_id"] = "change-set-patch/v1"
        registry = seal(self.schemas["artifact-content-registry/v1"], registry)

        elapsed = self.document("publication_golden_budget_summary")
        elapsed["consumed_ms"] = elapsed["elapsed_limit_ms"] + 1
        elapsed = seal(self.schemas["budget-summary/v1"], elapsed)
        calls = self.document("publication_golden_budget_summary")
        calls["calls_consumed"] = calls["tool_call_limit"] + 1
        calls = seal(self.schemas["budget-summary/v1"], calls)

        report_order = self.document("publication_golden_report")
        report_order["sections"].insert(
            0,
            {
                "section_id": "section_ffffffffffffffffffffffffffffffff",
                "title": "Later",
                "body": "Later section.",
                "evidence_refs": [],
            },
        )
        report_order = seal(self.schemas["task-report/v1"], report_order)
        utf8_report = self.document("publication_golden_report")
        utf8_report["title"] = "界" * 200
        utf8_report = seal(self.schemas["task-report/v1"], utf8_report)
        operations = [
            {"schema_id": "artifact-content-registry/v1", "document": registry},
            {"schema_id": "budget-summary/v1", "document": elapsed},
            {"schema_id": "budget-summary/v1", "document": calls},
            {"schema_id": "task-report/v1", "document": report_order},
            {"schema_id": "task-report/v1", "document": utf8_report},
        ]

        results = self.assert_parity(operations)

        self.assertEqual(
            [
                "ARTIFACT_CONTENT_REGISTRY_INVALID",
                "BUDGET_SUMMARY_ELAPSED_INVALID",
                "BUDGET_SUMMARY_CALLS_INVALID",
                "TASK_REPORT_SECTION_ORDER_INVALID",
                "TASK_REPORT_UTF8_LIMIT_INVALID",
            ],
            [item["detail"] for item in results],
        )
        self.assertEqual(
            ["CONTRACT_DOCUMENT_INVALID", "BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED"],
            [item["code"] for item in results[:3]],
        )

    def test_effect_ledger_rows_recount_and_order_with_runtime_parity(self) -> None:
        schema = self.schemas["effect-ledger-snapshot/v1"]

        def ledger(states: list[str]) -> dict[str, object]:
            value = self.document("publication_golden_effect_ledger")
            value["rows"] = [
                {
                    "effect_id": f"effect_{index:032x}",
                    "state": state,
                }
                for index, state in enumerate(states, start=1)
            ]
            value["watermark"] = len(value["rows"])
            value["state_counts"] = {
                state: states.count(state.upper())
                for state in (
                    "prepared",
                    "dispatched",
                    "committed",
                    "not_applied",
                    "rejected",
                    "unknown",
                )
            }
            return seal(schema, value)

        committed = ledger(["COMMITTED"])
        unknown = ledger(["UNKNOWN"])
        mixed = ledger(["COMMITTED", "NOT_APPLIED", "REJECTED", "UNKNOWN"])
        bad_count = deepcopy(mixed)
        bad_count["state_counts"]["committed"] = 0
        bad_count = seal(schema, bad_count)
        bad_order = deepcopy(mixed)
        bad_order["rows"].reverse()
        bad_order = seal(schema, bad_order)
        duplicate_effect_id = deepcopy(mixed)
        duplicate_effect_id["rows"][1]["effect_id"] = duplicate_effect_id["rows"][0]["effect_id"]
        duplicate_effect_id = seal(schema, duplicate_effect_id)
        bad_watermark = deepcopy(mixed)
        bad_watermark["watermark"] += 1
        bad_watermark = seal(schema, bad_watermark)
        results = self.assert_parity(
            [
                {"schema_id": "effect-ledger-snapshot/v1", "document": item}
                for item in (
                    committed,
                    unknown,
                    mixed,
                    bad_count,
                    bad_order,
                    duplicate_effect_id,
                    bad_watermark,
                )
            ]
        )

        self.assertTrue(all(item["ok"] for item in results[:3]), results)
        self.assertEqual(
            [
                ("EFFECT_LEDGER_STATE_COUNTS_INVALID", "$.state_counts"),
                ("EFFECT_LEDGER_ROW_ORDER_INVALID", "$.rows"),
                ("EFFECT_LEDGER_ROW_ORDER_INVALID", "$.rows"),
                ("EFFECT_LEDGER_WATERMARK_INVALID", "$.watermark"),
            ],
            [(item["detail"], item["path"]) for item in results[3:]],
        )

    def test_report_evidence_refs_are_ordered_and_globally_consistent(self) -> None:
        reference = deepcopy(
            next(
                item["document"]
                for item in self.core["fixtures"]
                if item["fixture_id"] == "core_golden_content_ref"
            )
        )
        later = deepcopy(reference)
        later["artifact_id"] = "art_ffffffffffffffffffffffffffffffff"
        conflict = deepcopy(reference)
        conflict["sha256"] = "9" * 64

        unordered = self.document("publication_golden_report")
        unordered["sections"][0]["evidence_refs"] = [later, reference]
        unordered = seal(self.schemas["task-report/v1"], unordered)

        conflicting = self.document("publication_golden_report")
        conflicting["sections"].append(
            {
                "section_id": "section_ffffffffffffffffffffffffffffffff",
                "title": "Evidence",
                "body": "Conflicting evidence identity.",
                "evidence_refs": [conflict],
            }
        )
        conflicting["sections"][0]["evidence_refs"] = [reference]
        conflicting = seal(self.schemas["task-report/v1"], conflicting)

        results = self.assert_parity(
            [
                {"schema_id": "task-report/v1", "document": unordered},
                {"schema_id": "task-report/v1", "document": conflicting},
            ]
        )

        self.assertEqual(
            ["TASK_REPORT_EVIDENCE_ORDER_INVALID", "CONTENT_REF_CONFLICT"],
            [item["detail"] for item in results],
        )

    def python_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        results = []
        for operation in operations:
            try:
                if "x-pullwise-digest" in self.schemas[operation["schema_id"]]:
                    value = self.python.verify_document_digest(
                        operation["schema_id"], operation["document"]
                    )
                else:
                    value = self.python.validate_document(
                        operation["schema_id"], operation["document"]
                    )
            except self.python.ContractValidationError as error:
                results.append(
                    {"ok": False, "code": error.code, "detail": error.detail, "path": error.path}
                )
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="publication-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as f from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = f.schema(operation.schema_id)['x-pullwise-digest']",
                        "      ? await f.verifyDocumentDigest(operation.schema_id, operation.document)",
                        "      : f.validateDocument(operation.schema_id, operation.document);",
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
