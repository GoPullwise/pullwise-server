from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types

from pullwise_server.agent_first_contract_bundle import build_bundle


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class VerificationDirectHarness:
    @classmethod
    def setUpClass(cls) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        cls.bundle = bundle.document
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle["families"]
            for schema in family["schemas"]
        }
        cls.python = types.ModuleType("_verification_direct_python")
        exec(bundle.python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = bundle.npm_wrapper

    def fixture_document(self, fixture_id: str) -> dict[str, object]:
        for family in self.bundle["families"]:
            for fixture in family["fixtures"]:
                if fixture["fixture_id"] == fixture_id:
                    return deepcopy(fixture["document"])
        raise KeyError(fixture_id)

    def reseal(self, schema_id: str, document: dict[str, object]) -> dict[str, object]:
        spec = self.schemas[schema_id]["x-pullwise-digest"]
        field = spec["field"]
        result = deepcopy(document)
        unsigned = {key: value for key, value in result.items() if key != field}
        result[field] = hashlib.sha256(
            spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return result

    def content_ref(
        self, artifact_id: str, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        raw = canonical_bytes(document)
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": artifact_id,
            "content_schema_id": schema_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    def bind_source_tree(
        self, tree: dict[str, object], policy: dict[str, object], artifact_id: str
    ) -> dict[str, object]:
        result = deepcopy(tree)
        result["selection_policy_ref"] = self.content_ref(
            artifact_id, "source-selection-policy/v1", policy
        )
        result["selection_policy_digest"] = policy["policy_digest"]
        return self.reseal("source-tree-manifest/v1", result)

    def build_graph(self) -> dict[str, object]:
        request = self.fixture_document("task_control_golden_task_request")
        policy = self.fixture_document("task_control_golden_effective_policy")
        ledger = self.fixture_document("requirements_golden_ledger")
        charter = self.fixture_document("requirements_golden_charter")
        attempt = self.fixture_document("task_control_golden_attempt_record")
        owner = self.fixture_document("task_control_golden_task_owner")
        profile = self.fixture_document("source_evidence_golden_execution_profile")
        selection = self.fixture_document("source_evidence_golden_selection_policy")
        original = self.bind_source_tree(
            self.fixture_document("source_evidence_golden_source_tree"),
            selection,
            "art_" + "1" * 32,
        )
        patch = self.fixture_document("source_evidence_golden_patch")
        change_set = self.fixture_document("source_evidence_golden_change_set")
        change_set["patch_ref"] = self.content_ref(
            "art_" + "2" * 32, "change-set-patch/v1", patch
        )
        change_set["original_source_state_id"] = original["source_state_id"]
        final = deepcopy(original)
        final["entries"] = deepcopy(
            self.fixture_document("source_evidence_negative_source_tree_changed_state")[
                "entries"
            ]
        )
        final["entry_count"] = 5
        final["total_bytes"] = 10
        final = self.bind_source_tree(final, selection, "art_" + "1" * 32)
        change_set["final_source_state_id"] = final["source_state_id"]
        change_set = self.reseal("change-set/v1", change_set)
        observation = self.fixture_document("task_observation_golden_observation")
        pre = self.fixture_document("task_observation_golden_pre_manifest")
        pre["entries"][0]["observation_ref"] = self.content_ref(
            "art_" + "4" * 32, "observation/v1", observation
        )
        pre["entries"][0]["source_state_before_id"] = original["source_state_id"]
        pre["entries"][0]["source_state_after_id"] = original["source_state_id"]
        pre = self.reseal("pre-verifier-observation-manifest/v1", pre)
        final_manifest = self.fixture_document("task_observation_golden_final_manifest")
        final_manifest["pre_verifier_observation_manifest_ref"] = self.content_ref(
            "art_" + "5" * 32, "pre-verifier-observation-manifest/v1", pre
        )
        final_manifest["entries"][0]["observation_ref"] = self.content_ref(
            "art_" + "4" * 32, "observation/v1", observation
        )
        for index in (0, 1):
            final_manifest["entries"][index]["source_state_before_id"] = original["source_state_id"]
            final_manifest["entries"][index]["source_state_after_id"] = original["source_state_id"]
        final_manifest = self.reseal("observation-manifest/v1", final_manifest)
        exec_state = self.fixture_document("source_evidence_golden_execution_state")
        exec_state["source_state_id"] = original["source_state_id"]
        exec_state["execution_profile_ref"] = self.content_ref(
            "art_" + "3" * 32, "execution-profile/v1", profile
        )
        exec_state["execution_profile_digest"] = profile["profile_digest"]
        exec_state = self.reseal("execution-state-manifest/v1", exec_state)
        record = self.fixture_document("task_control_golden_task_record")
        record["request_ref"] = self.content_ref("art_" + "9" * 32, "task-request/v1", request)
        record["request_digest"] = hashlib.sha256(canonical_bytes(request)).hexdigest()
        record["policy_ref"] = self.content_ref(
            "art_" + "8" * 32, "effective-execution-policy/v1", policy
        )
        record["policy_digest"] = policy["digest"]
        record["current_attempt_id"] = attempt["attempt_id"]
        record["native_epoch"] = attempt["native_epoch"]
        record["owner_epoch"] = owner["owner_epoch"]
        record["ledger_head_digest"] = ledger["ledger_digest"]
        record["charter_version"] = charter["charter_version"]
        record["charter_ref"] = self.content_ref(
            "art_" + "7" * 32, "task-charter/v1", charter
        )
        proposal = self.fixture_document("task_completion_golden_proposal")
        proposal["task_id"] = record["task_id"]
        proposal["attempt_id"] = attempt["attempt_id"]
        proposal["native_epoch"] = attempt["native_epoch"]
        proposal["owner_id"] = owner["owner_id"]
        proposal["owner_epoch"] = owner["owner_epoch"]
        proposal["proposed_from_task_version"] = record["task_version"]
        proposal["request_digest"] = record["request_digest"]
        proposal["requirement_ledger_digest"] = ledger["ledger_digest"]
        proposal["policy_digest"] = policy["digest"]
        proposal["charter_digest"] = charter["digest"]
        proposal["original_source_state_id"] = original["source_state_id"]
        proposal["final_source_state_id"] = final["source_state_id"]
        proposal["execution_state_ids"] = [exec_state["execution_state_id"]]
        proposal["change_set_ref"] = self.content_ref(
            "art_" + "2" * 32, "change-set/v1", change_set
        )
        proposal = self.reseal("completion-proposal/v1", proposal)
        plan = self.fixture_document("quality_policy_golden_q2_plan")
        plan["task_id"] = record["task_id"]
        plan["proposal_id"] = proposal["proposal_id"]
        plan["proposal_digest"] = proposal["proposal_digest"]
        plan["policy_digest"] = policy["digest"]
        plan["task_type"] = record["task_type"]
        plan["requirement_ledger_digest"] = ledger["ledger_digest"]
        plan = self.reseal("quality-policy-plan/v1", plan)
        rule_bytes = b"rule"
        engineering_rule = {
            "schema_id": "source-content/v1",
            "media_type": "text/plain",
            "encoding": "base64",
            "data_base64": base64.b64encode(rule_bytes).decode("ascii"),
            "byte_sha256": hashlib.sha256(rule_bytes).hexdigest(),
            "size_bytes": len(rule_bytes),
            "content_digest": "",
        }
        engineering_rule = self.reseal("source-content/v1", engineering_rule)
        input_manifest = self.fixture_document("task_verifier_input_golden_input")
        input_manifest["task_id"] = record["task_id"]
        input_manifest["proposal_id"] = proposal["proposal_id"]
        input_manifest["task_request_ref"] = self.content_ref(
            "art_" + "9" * 32, "task-request/v1", request
        )
        input_manifest["effective_policy_ref"] = self.content_ref(
            "art_" + "8" * 32, "effective-execution-policy/v1", policy
        )
        input_manifest["requirement_ledger_ref"] = self.content_ref(
            "art_" + "7" * 32, "requirement-ledger/v1", ledger
        )
        input_manifest["charter_ref"] = self.content_ref(
            "art_" + "6" * 32, "task-charter/v1", charter
        )
        input_manifest["completion_proposal_ref"] = self.content_ref(
            "art_" + "5" * 32, "completion-proposal/v1", proposal
        )
        input_manifest["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "d" * 32, "quality-policy-plan/v1", plan
        )
        input_manifest["quality_policy_plan_digest"] = plan["plan_digest"]
        input_manifest["original_source_ref"] = self.content_ref(
            "art_" + "1" * 32, "source-tree-manifest/v1", original
        )
        input_manifest["final_source_ref"] = self.content_ref(
            "art_" + "3" * 32, "source-tree-manifest/v1", final
        )
        input_manifest["change_set"] = {
            "availability": "available",
            "ref": self.content_ref("art_" + "2" * 32, "change-set/v1", change_set),
        }
        input_manifest["pre_verifier_observation_manifest_ref"] = self.content_ref(
            "art_" + "5" * 32, "pre-verifier-observation-manifest/v1", pre
        )
        input_manifest["engineering_rule_refs"] = [
            self.content_ref("art_" + "a" * 32, "source-content/v1", engineering_rule)
        ]
        input_manifest = self.reseal("verifier-input-manifest/v1", input_manifest)
        work = self.fixture_document("task_verifier_work_golden_work")
        work["task_id"] = record["task_id"]
        work["proposal_id"] = proposal["proposal_id"]
        work["verifier_input_manifest_ref"] = self.content_ref(
            "art_" + "b" * 32, "verifier-input-manifest/v1", input_manifest
        )
        work["verifier_input_manifest_digest"] = input_manifest["input_manifest_digest"]
        work = self.reseal("verifier-work-report/v1", work)
        attestation = self.fixture_document("task_attestation_golden_attestation")
        attestation["task_id"] = record["task_id"]
        attestation["proposal_id"] = proposal["proposal_id"]
        attestation["verifier_input_manifest_ref"] = self.content_ref(
            "art_" + "b" * 32, "verifier-input-manifest/v1", input_manifest
        )
        attestation["verifier_input_manifest_digest"] = input_manifest["input_manifest_digest"]
        attestation["verifier_work_report_ref"] = self.content_ref(
            "art_" + "c" * 32, "verifier-work-report/v1", work
        )
        attestation["verifier_work_report_digest"] = work["report_digest"]
        attestation["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "d" * 32, "quality-policy-plan/v1", plan
        )
        attestation["quality_policy_plan_digest"] = plan["plan_digest"]
        attestation["final_observation_manifest_ref"] = self.content_ref(
            "art_" + "e" * 32, "observation-manifest/v1", final_manifest
        )
        attestation["final_observation_manifest_digest"] = final_manifest["manifest_digest"]
        attestation["source_state_id"] = final["source_state_id"]
        attestation["execution_state_ids"] = [exec_state["execution_state_id"]]
        attestation = self.reseal("verification-attestation/v1", attestation)
        aggregate = self.fixture_document("task_verification_golden_attestation_manifest")
        aggregate["task_id"] = record["task_id"]
        aggregate["proposal_id"] = proposal["proposal_id"]
        aggregate["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "d" * 32, "quality-policy-plan/v1", plan
        )
        aggregate["quality_policy_plan_digest"] = plan["plan_digest"]
        aggregate["final_observation_manifest_ref"] = self.content_ref(
            "art_" + "e" * 32, "observation-manifest/v1", final_manifest
        )
        aggregate["final_observation_manifest_digest"] = final_manifest["manifest_digest"]
        aggregate["attestations"][0]["attestation_ref"] = self.content_ref(
            "art_" + "f" * 32, "verification-attestation/v1", attestation
        )
        aggregate = self.reseal("verification-attestation-manifest/v1", aggregate)
        return {
            "request": request,
            "policy": policy,
            "ledger": ledger,
            "charter": charter,
            "attempt": attempt,
            "owner": owner,
            "task_snapshot": record,
            "profile": profile,
            "original_source": original,
            "final_source": final,
            "patch": patch,
            "change_set": change_set,
            "pre_manifest": pre,
            "final_manifest": final_manifest,
            "execution_states": [exec_state],
            "proposal": proposal,
            "plan": plan,
            "engineering_rules": [engineering_rule],
            "input": input_manifest,
            "work": work,
            "attestation": attestation,
            "aggregate": aggregate,
        }

    def validate_document_pair(
        self, schema_id: str, document: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        fn = (
            self.python.verify_document_digest
            if "x-pullwise-digest" in self.schemas[schema_id]
            else self.python.validate_document
        )
        try:
            py = {"ok": True, "value": fn(schema_id, document)}
        except self.python.ContractValidationError as error:
            py = {"ok": False, "code": error.code, "detail": error.detail, "path": error.path}
        with tempfile.TemporaryDirectory(prefix="verification-direct-doc-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as f from {json.dumps(facade.as_uri())};",
                        f"const schemaId = {json.dumps(schema_id)};",
                        f"const document = {json.dumps(document, separators=(',', ':'))};",
                        "try {",
                        f"  const value = f.schema(schemaId)['x-pullwise-digest'] ? "
                        "await f.verifyDocumentDigest(schemaId, document) : f.validateDocument(schemaId, document);",
                        "  process.stdout.write(JSON.stringify({ok:true,value}));",
                        "} catch (error) {",
                        "  process.stdout.write(JSON.stringify({ok:false,code:error.code,detail:error.detail,path:error.path}));",
                        "}",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(["node", str(runner)], check=True, capture_output=True, encoding="utf-8").stdout
            )
        return py, node

    def python_helper_exports(self, names: list[str]) -> dict[str, bool]:
        return {name: hasattr(self.python, name) for name in names}

    def node_helper_exports(self, aliases: dict[str, str]) -> dict[str, dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="verification-direct-exports-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as f from {json.dumps(facade.as_uri())};",
                        f"const aliases = {json.dumps(aliases, separators=(',', ':'))};",
                        "const out = {};",
                        "for (const [snake, camel] of Object.entries(aliases)) {",
                        "  out[snake] = {snake: typeof f[snake] === 'function', camel: typeof f[camel] === 'function', same: f[snake] === f[camel]};",
                        "}",
                        "process.stdout.write(JSON.stringify(out));",
                    )
                ),
                encoding="utf-8",
            )
            return json.loads(
                subprocess.run(["node", str(runner)], check=True, capture_output=True, encoding="utf-8").stdout
            )
