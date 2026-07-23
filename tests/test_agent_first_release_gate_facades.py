from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
from pullwise_server.agent_first_contract_bundle_source import canonical_bytes


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"
FAMILY_IDS = (
    "benchmark-bundle",
    "release-gate-policy",
    "release-gate-report",
    "release-gate-attestation",
)


class AgentFirstReleaseGateFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.families = {
            family_id: cls.load_family(family_id) for family_id in FAMILY_IDS
        }
        cls.fixtures = {
            item["fixture_id"]: item
            for family in cls.families.values()
            for item in family["fixtures"]
        }
        error_family = cls.load_family("receipt-error")
        error_fixture = next(
            item for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical = canonical_bytes({
            "families": [
                cls.load_family("core"),
                *cls.families.values(),
                {
                    "family_id": "receipt-error",
                    "schemas": [],
                    "fixtures": [error_fixture],
                },
            ]
        })
        render_args = (
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"release-gate-facade-root").hexdigest(),
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        cls.python = ModuleType("_release_gate_parity_python")
        exec(render_python_wrapper(*render_args), cls.python.__dict__)
        cls.npm_bytes = render_npm_wrapper(*render_args)

    @staticmethod
    def load_family(family_id: str) -> dict[str, object]:
        return json.loads(
            (FAMILY_ROOT / f"{family_id}.json").read_text(encoding="utf-8")
        )

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def test_all_source_fixtures_have_python_node_parity(self) -> None:
        operations = [
            {
                "kind": "document",
                "schema_id": fixture["schema_id"],
                "documents": [fixture["document"]],
            }
            for family in self.families.values()
            for fixture in family["fixtures"]
        ]
        python_results = self.python_results(operations)
        node = self.node_results(operations)
        self.assertEqual(python_results, node["results"])
        for result, fixture in zip(
            python_results,
            (
                fixture
                for family in self.families.values()
                for fixture in family["fixtures"]
            ),
        ):
            with self.subTest(fixture_id=fixture["fixture_id"]):
                if fixture["fixture_class"] == "negative":
                    self.assertFalse(result["ok"])
                    self.assertEqual(fixture["expected_code"], result["code"])
                else:
                    self.assertEqual(
                        {"ok": True, "value": fixture["document"]}, result
                    )

    def test_context_helpers_exact_bind_the_supplied_evidence_chain(self) -> None:
        benchmark = self.document("benchmark_bundle_golden_current")
        policy = self.document("release_gate_policy_golden_bootstrap")
        report = self.document("release_gate_report_golden_bootstrap_pass")
        attestation = self.document(
            "release_gate_attestation_golden_bootstrap_pass"
        )

        wrong_benchmark = deepcopy(benchmark)
        wrong_benchmark["benchmark_version"] = "benchmark-2026-07-24"
        wrong_benchmark = self.reseal(
            "benchmark-bundle/v1", "bundle_digest", wrong_benchmark
        )

        wrong_candidate_report = deepcopy(report)
        wrong_candidate_report["candidate_build_id"] = (
            "candidate_44444444444444444444444444444444"
        )
        wrong_candidate_report = self.reseal(
            "release-gate-report/v1", "report_digest", wrong_candidate_report
        )

        failed_report = deepcopy(report)
        failed_report["absolute_results"][0]["status"] = "FAIL"
        failed_report["verdict"] = "FAIL"
        failed_report["exit_code"] = 1
        failed_report = self.reseal(
            "release-gate-report/v1", "report_digest", failed_report
        )
        nonpass_attestation = deepcopy(attestation)
        nonpass_attestation["report_digest"] = failed_report["report_digest"]
        nonpass_attestation["report_ref"] = self.content_ref(
            nonpass_attestation["report_ref"], failed_report
        )
        nonpass_attestation = self.reseal(
            "release-gate-attestation/v1",
            "attestation_digest",
            nonpass_attestation,
        )

        operations = [
            {"kind": "policy", "documents": [policy, benchmark]},
            {"kind": "report", "documents": [report, benchmark, policy]},
            {"kind": "attestation", "documents": [attestation, policy, report]},
            {"kind": "policy", "documents": [policy, wrong_benchmark]},
            {
                "kind": "report",
                "documents": [wrong_candidate_report, benchmark, policy],
            },
            {
                "kind": "attestation",
                "documents": [nonpass_attestation, policy, failed_report],
            },
            {"kind": "policy_snake", "documents": [policy, benchmark]},
            {"kind": "report_snake", "documents": [report, benchmark, policy]},
            {
                "kind": "attestation_snake",
                "documents": [attestation, policy, report],
            },
        ]
        python_results = self.python_results(operations)
        node = self.node_results(operations)
        self.assertEqual(python_results, node["results"])
        self.assertEqual(
            {
                "policy_camel": True,
                "policy_snake": True,
                "report_camel": True,
                "report_snake": True,
                "attestation_camel": True,
                "attestation_snake": True,
            },
            node["exports"],
        )
        self.assertTrue(all(item["ok"] for item in python_results[:3]))
        for result in python_results[3:6]:
            self.assertFalse(result["ok"])
            self.assertEqual("CONTRACT_DOCUMENT_INVALID", result["code"])
        self.assertEqual(python_results[:3], python_results[6:])

    @staticmethod
    def reseal(
        schema_id: str, digest_field: str, document: dict[str, object]
    ) -> dict[str, object]:
        unsigned = {
            key: value for key, value in document.items()
            if key != digest_field
        }
        document[digest_field] = hashlib.sha256(
            f"pullwise:{schema_id[:-3]}:v1\0".encode("ascii")
            + canonical_bytes(unsigned)
        ).hexdigest()
        return document

    @staticmethod
    def content_ref(
        original: dict[str, object], document: dict[str, object]
    ) -> dict[str, object]:
        encoded = canonical_bytes(document)
        return {
            **original,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }

    def capture(self, callback) -> dict[str, object]:
        try:
            return {"ok": True, "value": callback()}
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }

    def python_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        handlers = {
            "document": lambda operation: self.python.validate_document(
                operation["schema_id"], operation["documents"][0]
            ),
            "policy": lambda operation: self.python.verify_release_gate_policy_context(
                *operation["documents"]
            ),
            "report": lambda operation: self.python.verify_release_gate_report_context(
                *operation["documents"]
            ),
            "attestation": lambda operation: self.python.verify_release_gate_attestation_context(
                *operation["documents"]
            ),
            "policy_snake": lambda operation: self.python.verify_release_gate_policy_context(
                *operation["documents"]
            ),
            "report_snake": lambda operation: self.python.verify_release_gate_report_context(
                *operation["documents"]
            ),
            "attestation_snake": lambda operation: self.python.verify_release_gate_attestation_context(
                *operation["documents"]
            ),
        }
        return [
            self.capture(lambda operation=operation: handlers[operation["kind"]](operation))
            for operation in operations
        ]

    def node_results(
        self, operations: list[dict[str, object]]
    ) -> dict[str, object]:
        payload = json.dumps(operations, separators=(",", ":"))
        with tempfile.TemporaryDirectory(prefix="release-gate-facades-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_bytes)
            runner_path.write_text(
                "\n".join((
                    f"import * as facade from {json.dumps(facade_path.as_uri())};",
                    f"const operations = {payload};",
                    "const capture = async (callback) => {",
                    "  try { return {ok: true, value: await callback()}; }",
                    "  catch (error) { return {ok: false, code: error.code, detail: error.detail, path: error.path}; }",
                    "};",
                    "const handlers = {",
                    "  document: (item) => facade.validateDocument(item.schema_id, item.documents[0]),",
                    "  policy: (item) => facade.verifyReleaseGatePolicyContext(...item.documents),",
                    "  report: (item) => facade.verifyReleaseGateReportContext(...item.documents),",
                    "  attestation: (item) => facade.verifyReleaseGateAttestationContext(...item.documents),",
                    "  policy_snake: (item) => facade.verify_release_gate_policy_context(...item.documents),",
                    "  report_snake: (item) => facade.verify_release_gate_report_context(...item.documents),",
                    "  attestation_snake: (item) => facade.verify_release_gate_attestation_context(...item.documents),",
                    "};",
                    "const results = [];",
                    "for (const item of operations) results.push(await capture(() => handlers[item.kind](item)));",
                    "const exports = {",
                    "  policy_camel: typeof facade.verifyReleaseGatePolicyContext === 'function',",
                    "  policy_snake: typeof facade.verify_release_gate_policy_context === 'function',",
                    "  report_camel: typeof facade.verifyReleaseGateReportContext === 'function',",
                    "  report_snake: typeof facade.verify_release_gate_report_context === 'function',",
                    "  attestation_camel: typeof facade.verifyReleaseGateAttestationContext === 'function',",
                    "  attestation_snake: typeof facade.verify_release_gate_attestation_context === 'function',",
                    "};",
                    "process.stdout.write(JSON.stringify({results, exports}));",
                )),
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
