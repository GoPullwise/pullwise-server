from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.agent_first_verification_facade_support import VerificationFacadeHarness


HELPER_ALIASES = {
    "verify_completion_proposal_context": "verifyCompletionProposalContext",
    "verify_verifier_input_context": "verifyVerifierInputContext",
    "verify_verifier_work_context": "verifyVerifierWorkContext",
    "verify_attestation_context": "verifyAttestationContext",
    "verify_attestation_manifest_context": "verifyAttestationManifestContext",
}


class AgentFirstVerificationContextFacadesTest(
    VerificationFacadeHarness, unittest.TestCase
):
    def test_all_declared_verification_helpers_export_in_python_and_node(self) -> None:
        self.assertEqual(
            {name: True for name in HELPER_ALIASES},
            self.python_helper_exports(list(HELPER_ALIASES)),
        )
        self.assertEqual(
            {
                name: {"snake": True, "camel": True, "same": True}
                for name in HELPER_ALIASES
            },
            self.node_helper_exports(HELPER_ALIASES),
        )

    def test_direct_document_contexts_have_python_node_parity(self) -> None:
        self.assertEqual(
            {name: True for name in HELPER_ALIASES},
            self.python_helper_exports(list(HELPER_ALIASES)),
        )
        results = self.assert_helper_parity(self.helper_operations())
        self.assertTrue(all(item["ok"] for item in results[:5]))
        self.assertTrue(all(not item["ok"] for item in results[5:]))

    def helper_operations(self) -> list[dict[str, object]]:
        proposal = self.fixture_document("task_completion_golden_proposal")
        input_manifest = self.fixture_document("task_verifier_input_golden_input")
        work = self.fixture_document("task_verifier_work_golden_work")
        attestation = self.fixture_document("task_attestation_golden_attestation")
        manifest = self.fixture_document("task_verification_golden_attestation_manifest")
        policy = self.fixture_document("quality_policy_golden_q2_plan")
        pre = self.fixture_document("task_observation_golden_pre_manifest")
        final = self.fixture_document("task_observation_golden_final_manifest")
        observation = self.fixture_document("task_observation_golden_observation")
        proposal_ctx = {
            "task_id": proposal["task_id"],
            "attempt_id": proposal["attempt_id"],
            "native_epoch": proposal["native_epoch"],
            "owner_id": proposal["owner_id"],
            "owner_epoch": proposal["owner_epoch"],
            "task_version": proposal["proposed_from_task_version"],
            "request_digest": proposal["request_digest"],
            "requirement_ledger_digest": proposal["requirement_ledger_digest"],
            "policy_digest": proposal["policy_digest"],
            "charter_digest": proposal["charter_digest"],
            "original_source_state_id": proposal["original_source_state_id"],
            "final_source_state_id": proposal["final_source_state_id"],
            "observation_ids": proposal["requirement_claims"][0]["evidence_ids"],
            "created_at": proposal["created_at"],
        }
        work_ctx = {
            "task_id": work["task_id"],
            "proposal_id": work["proposal_id"],
            "slot_id": work["slot_id"],
            "observation_ids": work["own_observation_ids"],
            "requirement_ids": input_manifest["requirement_ids"],
            "created_at": work["created_at"],
            "latest_completed_at": observation["completed_at"],
        }
        positives = [
            ("verify_completion_proposal_context", "verifyCompletionProposalContext", [proposal, proposal_ctx]),
            ("verify_verifier_input_context", "verifyVerifierInputContext", [input_manifest, proposal, policy, pre]),
            ("verify_verifier_work_context", "verifyVerifierWorkContext", [work, input_manifest, work_ctx]),
            ("verify_attestation_context", "verifyAttestationContext", [attestation, input_manifest, work, policy, final]),
            ("verify_attestation_manifest_context", "verifyAttestationManifestContext", [manifest, [attestation], policy, final]),
        ]
        stale_ctx = deepcopy(proposal_ctx)
        stale_ctx["owner_epoch"] += 1
        stale_input = deepcopy(policy)
        stale_input["proposal_id"] = "proposal_" + "2" * 32
        stale_work = deepcopy(work_ctx)
        stale_work["observation_ids"] = []
        stale_work["latest_completed_at"] = "2026-01-01T00:00:02.000Z"
        stale_attestation = deepcopy(final)
        stale_attestation["task_id"] = "task_" + "2" * 32
        stale_manifest = deepcopy(attestation)
        stale_manifest["requirement_verdicts"].append(
            {
                "requirement_id": "req_user_objective_" + "2" * 64,
                "verdict": "PASS",
                "evidence_ids": ["obs_22222222222222222222222222222222"],
                "limitations": [],
            }
        )
        stale_manifest = self.reseal("verification-attestation/v1", stale_manifest)
        negatives = [
            ("verify_completion_proposal_context", "verifyCompletionProposalContext", [proposal, stale_ctx]),
            ("verify_verifier_input_context", "verifyVerifierInputContext", [input_manifest, proposal, stale_input, pre]),
            ("verify_verifier_work_context", "verifyVerifierWorkContext", [work, input_manifest, stale_work]),
            ("verify_attestation_context", "verifyAttestationContext", [attestation, input_manifest, work, policy, stale_attestation]),
            ("verify_attestation_manifest_context", "verifyAttestationManifestContext", [manifest, [stale_manifest], policy, final]),
        ]
        return [
            {"python": py, "node": node, "args": args}
            for py, node, args in positives + negatives
        ]

    def assert_helper_parity(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python = []
        for item in operations:
            try:
                value = getattr(self.python, item["python"])(*item["args"])
            except Exception as error:
                python.append(
                    {
                        "ok": False,
                        "code": getattr(error, "code", type(error).__name__),
                        "detail": getattr(error, "detail", str(error)),
                        "path": getattr(error, "path", item["python"]),
                    }
                )
            else:
                python.append({"ok": True, "value": value})
        with tempfile.TemporaryDirectory(prefix="verification-context-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const item of operations) {",
                        "  try {",
                        "    if (facade[item.python] !== facade[item.node]) throw new Error(item.python);",
                        "    results.push({ok: true, value: await facade[item.node](...item.args)});",
                        "  } catch (error) {",
                        "    results.push({",
                        "      ok: false,",
                        "      code: error.code ?? error.name,",
                        "      detail: error.detail ?? String(error.message ?? error),",
                        "      path: error.path ?? item.python,",
                        "    });",
                        "  }",
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
        node = json.loads(completed.stdout)
        self.assertEqual(python, node)
        return python


if __name__ == "__main__":
    unittest.main()
