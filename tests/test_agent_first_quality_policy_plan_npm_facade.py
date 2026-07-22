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
FAMILY_DIR = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"
SCHEMA_ID = "quality-policy-plan/v1"


class QualityPolicyPlanNpmFacadeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        family = json.loads(
            (FAMILY_DIR / "quality-policy-plan.json").read_text(encoding="utf-8")
        )
        error_family = json.loads(
            (FAMILY_DIR / "receipt-error.json").read_text(encoding="utf-8")
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical = canonical_bytes(
            {
                "families": [
                    family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ]
            }
        )
        render_args = (
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"quality-policy-npm-root").hexdigest(),
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        python_bytes = render_python_wrapper(*render_args)
        cls.python = ModuleType("_quality_policy_npm_parity_python")
        exec(python_bytes, cls.python.__dict__)
        cls.npm_bytes = render_npm_wrapper(*render_args)
        cls.fixtures = {item["fixture_id"]: item for item in family["fixtures"]}

    def test_node_api_matches_python_for_rules_context_and_digest_helpers(self) -> None:
        golden = deepcopy(
            self.fixtures["quality_policy_golden_q2_plan"]["document"]
        )
        wrong_input = deepcopy(golden)
        wrong_input["input_digest"] = "0" * 64
        wrong_input = self._reseal(wrong_input)
        zero_digest = deepcopy(golden)
        zero_digest["plan_digest"] = "0" * 64
        documents = [golden, wrong_input, zero_digest]

        valid_context = self._context(golden)
        binding_mismatch = deepcopy(valid_context)
        binding_mismatch[1]["proposal_id"] = "proposal_" + "2" * 32
        below_floor = deepcopy(valid_context)
        below_floor[2]["quality_risk_floor"] = "Q3"
        missing_coverage = deepcopy(valid_context)
        missing_coverage[4]["entries"][1]["mandatory"] = True
        inactive_requirement = deepcopy(valid_context)
        inactive_requirement[0]["slots"][0]["requirement_ids"].append(
            "req_user_objective_" + "3" * 64
        )
        inactive_requirement[0] = self._reseal(inactive_requirement[0])
        contexts = [
            valid_context,
            binding_mismatch,
            below_floor,
            missing_coverage,
            inactive_requirement,
        ]
        unsigned = {
            key: value for key, value in golden.items() if key != "plan_digest"
        }

        expected = {
            "documents": [
                self._capture(
                    lambda document=document: self.python.validate_document(
                        SCHEMA_ID, document
                    )
                )
                for document in documents
            ],
            "contexts": [
                self._capture(
                    lambda context=context: self.python.verify_quality_policy_plan_context(
                        *context
                    )
                )
                for context in contexts
            ],
            "digest": self._capture(
                lambda: self.python.document_digest(SCHEMA_ID, unsigned)
            ),
            "sealed": self._capture(
                lambda: self.python.seal_document(SCHEMA_ID, unsigned)
            ),
        }
        actual = self._node_results(documents, contexts, unsigned)

        self.assertTrue(actual.pop("camel_export"))
        self.assertTrue(actual.pop("snake_export"))
        self.assertEqual(actual.pop("snake_context"), expected["contexts"][0])
        self.assertEqual(expected, actual)
        self.assertEqual({"ok": True, "value": golden}, actual["documents"][0])
        for result in actual["documents"][1:] + actual["contexts"][1:]:
            self.assertFalse(result["ok"])
            self.assertEqual("CONTRACT_DOCUMENT_INVALID", result["code"])

    @staticmethod
    def _context(plan: dict[str, object]) -> list[dict[str, object]]:
        mandatory_id = "req_user_objective_" + "1" * 64
        optional_id = "req_user_objective_" + "2" * 64
        return [
            deepcopy(plan),
            {
                "task_id": plan["task_id"],
                "proposal_id": plan["proposal_id"],
                "proposal_digest": plan["proposal_digest"],
                "policy_digest": plan["policy_digest"],
                "requirement_ledger_digest": plan["requirement_ledger_digest"],
            },
            {
                "digest": plan["policy_digest"],
                "task_type": plan["task_type"],
                "quality_risk_floor": plan["quality_risk"],
            },
            {"task_id": plan["task_id"], "task_type": plan["task_type"]},
            {
                "task_id": plan["task_id"],
                "ledger_digest": plan["requirement_ledger_digest"],
                "active_requirement_ids": [mandatory_id, optional_id],
                "entries": [
                    {"requirement_id": mandatory_id, "mandatory": True},
                    {"requirement_id": optional_id, "mandatory": False},
                ],
            },
            {
                "change_set_classification_digest": plan[
                    "change_set_classification_digest"
                ],
                "capability_usage_digest": plan["capability_usage_digest"],
            },
        ]

    @staticmethod
    def _reseal(document: dict[str, object]) -> dict[str, object]:
        unsigned = {
            key: value for key, value in document.items() if key != "plan_digest"
        }
        document["plan_digest"] = hashlib.sha256(
            b"pullwise:quality-policy-plan:v1\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return document

    def _capture(self, callback) -> dict[str, object]:
        try:
            return {"ok": True, "value": callback()}
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }

    def _node_results(
        self,
        documents: list[dict[str, object]],
        contexts: list[list[dict[str, object]]],
        unsigned: dict[str, object],
    ) -> dict[str, object]:
        payload = json.dumps(
            {"documents": documents, "contexts": contexts, "unsigned": unsigned},
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory(prefix="quality-policy-npm-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_bytes)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const payload = {payload};",
                        "const capture = async (callback) => {",
                        "  try { return {ok: true, value: await callback()}; }",
                        "  catch (error) { return {ok: false, code: error.code,",
                        "    detail: error.detail, path: error.path}; }",
                        "};",
                        "const documents = [];",
                        "for (const item of payload.documents) documents.push(",
                        "  await capture(() => facade.validateDocument(",
                        f"    {json.dumps(SCHEMA_ID)}, item)),",
                        ");",
                        "const contexts = [];",
                        "for (const item of payload.contexts) contexts.push(",
                        "  await capture(() => facade.verifyQualityPolicyPlanContext(...item)),",
                        ");",
                        "const result = {",
                        "  camel_export: typeof facade.verifyQualityPolicyPlanContext === 'function',",
                        "  snake_export: typeof facade.verify_quality_policy_plan_context === 'function',",
                        "  documents, contexts,",
                        "  snake_context: await capture(() =>",
                        "    facade.verify_quality_policy_plan_context(...payload.contexts[0])),",
                        "  digest: await capture(() => facade.documentDigest(",
                        f"    {json.dumps(SCHEMA_ID)}, payload.unsigned)),",
                        "  sealed: await capture(() => facade.sealDocument(",
                        f"    {json.dumps(SCHEMA_ID)}, payload.unsigned)),",
                        "};",
                        "process.stdout.write(JSON.stringify(result));",
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
