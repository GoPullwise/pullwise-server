from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types

from pullwise_server.agent_first_contract_bundle import build_bundle


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"
ALIASES = {
    "verify_completion_proposal_context": "verifyCompletionProposalContext",
    "verify_verifier_input_context": "verifyVerifierInputContext",
    "verify_verifier_work_context": "verifyVerifierWorkContext",
    "verify_attestation_context": "verifyAttestationContext",
    "verify_attestation_manifest_context": "verifyAttestationManifestContext",
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class VerificationDirectGraphHarness:
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.bundle = built.document
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle["families"]
            for schema in family["schemas"]
        }
        cls.python = types.ModuleType("_verification_direct_public_python")
        exec(built.python_wrapper, cls.python.__dict__)
        cls.python_helpers = cls.python
        cls.npm_wrapper = built.npm_wrapper
        cls.npm_helper_wrapper = cls.npm_wrapper

    def fixture_document(self, fixture_id: str) -> dict[str, object]:
        for family in self.bundle["families"]:
            for fixture in family["fixtures"]:
                if fixture["fixture_id"] == fixture_id:
                    return deepcopy(fixture["document"])
        raise KeyError(fixture_id)

    def reseal(self, schema_id: str, document: dict[str, object]) -> dict[str, object]:
        spec = self.schemas[schema_id]["x-pullwise-digest"]
        field = spec["field"]
        unsigned = {key: value for key, value in document.items() if key != field}
        result = deepcopy(document)
        result[field] = hashlib.sha256(
            spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return result

    def reseal_source_tree(self, document: dict[str, object]) -> dict[str, object]:
        result = deepcopy(document)
        result["entries"] = sorted(
            result["entries"], key=lambda item: item["path"].encode("utf-8")
        )
        result["entry_count"] = len(result["entries"])
        result["total_bytes"] = sum(
            item.get("size_bytes", 0)
            for item in result["entries"]
            if item["type"] == "file"
        )
        identity = {
            "base_revision": result["base_revision"],
            "selection_policy_digest": result["selection_policy_digest"],
            "entries": result["entries"],
        }
        result["source_state_id"] = hashlib.sha256(
            canonical_bytes(identity)
        ).hexdigest()
        return self.reseal("source-tree-manifest/v1", result)

    def reseal_execution_state(self, document: dict[str, object]) -> dict[str, object]:
        result = deepcopy(document)
        unsigned = {
            key: value
            for key, value in result.items()
            if key not in {"execution_state_id", "manifest_digest"}
        }
        result["execution_state_id"] = hashlib.sha256(
            canonical_bytes(unsigned)
        ).hexdigest()
        return self.reseal("execution-state-manifest/v1", result)

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

    def _capture_python(self, module, callback) -> dict[str, object]:
        try:
            return {"ok": True, "value": callback()}
        except module.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }

    def document_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        python = []
        for schema_id, document in cases:
            fn = (
                self.python.verify_document_digest
                if "x-pullwise-digest" in self.schemas[schema_id]
                else self.python.validate_document
            )
            python.append(
                self._capture_python(
                    self.python, lambda fn=fn, schema_id=schema_id, document=document: fn(schema_id, document)
                )
            )
        with tempfile.TemporaryDirectory(prefix="verification-direct-docs-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const capture = async (schemaId, document) => {",
                        "  try {",
                        "    const digest = facade.schema(schemaId)['x-pullwise-digest'];",
                        "    const value = digest ?",
                        "      await facade.verifyDocumentDigest(schemaId, document) :",
                        "      facade.validateDocument(schemaId, document);",
                        "    return {ok: true, value};",
                        "  } catch (error) {",
                        "    return {ok: false, code: error.code, detail: error.detail, path: error.path};",
                        "  }",
                        "};",
                        "const results = [];",
                        "for (const [schemaId, document] of cases) results.push(await capture(schemaId, document));",
                        "process.stdout.write(JSON.stringify(results));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node

    def public_helper_exports(self) -> tuple[dict[str, bool], dict[str, dict[str, object]]]:
        python = {name: hasattr(self.python, name) for name in ALIASES}
        with tempfile.TemporaryDirectory(prefix="verification-direct-exports-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const aliases = {json.dumps(ALIASES, separators=(',', ':'))};",
                        "const out = {};",
                        "for (const [snake, camel] of Object.entries(aliases)) {",
                        "  out[snake] = {",
                        "    snake: typeof facade[snake] === 'function',",
                        "    camel: typeof facade[camel] === 'function',",
                        "    same: facade[snake] === facade[camel],",
                        "  };",
                        "}",
                        "process.stdout.write(JSON.stringify(out));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node

    def helper_results(
        self, operations: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        python = [
            self._capture_python(
                self.python_helpers,
                lambda op=operation: getattr(self.python_helpers, op["python"])(*op["args"]),
            )
            for operation in operations
        ]
        payload = [
            {"snake": item["python"], "camel": item["node"], "args": item["args"]}
            for item in operations
        ]
        with tempfile.TemporaryDirectory(prefix="verification-direct-helpers-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_helper_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const ops = {json.dumps(payload, separators=(',', ':'))};",
                        "const capture = async (fn, args) => {",
                        "  try { return {ok: true, value: await fn(...args)}; }",
                        "  catch (error) {",
                        "    return {ok: false, code: error.code, detail: error.detail, path: error.path};",
                        "  }",
                        "};",
                        "const out = [];",
                        "for (const op of ops) {",
                        "  const snake = facade[op.snake];",
                        "  const camel = facade[op.camel];",
                        "  const result = await capture(camel, op.args);",
                        "  out.push({",
                        "    same: snake === camel,",
                        "    snake: result,",
                        "    camel: result,",
                        "  });",
                        "}",
                        "process.stdout.write(JSON.stringify(out));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node

    def build_graph(self) -> dict[str, object]:
        request = self.fixture_document("task_control_golden_task_request")
        policy = self.fixture_document("task_control_golden_effective_policy")
        ledger = self.fixture_document("requirements_golden_ledger")
        charter = self.fixture_document("requirements_golden_charter")
        attempt = self.fixture_document("task_control_golden_attempt_record")
        owner = self.fixture_document("task_control_golden_task_owner")
        task_snapshot = self.fixture_document("task_control_golden_task_record")
        profile = self.fixture_document("source_evidence_golden_execution_profile")
        selection = self.fixture_document("source_evidence_golden_selection_policy")
        proposal_id = "proposal_" + "0" * 32
        original_source = self.fixture_document("source_evidence_golden_source_tree")
        original_source["selection_policy_ref"] = self.content_ref(
            "art_" + "1" * 32, "source-selection-policy/v1", selection
        )
        original_source["selection_policy_digest"] = selection["policy_digest"]
        original_source = self.reseal_source_tree(original_source)
        patch = self.fixture_document("source_evidence_golden_patch")
        change_set = self.fixture_document("source_evidence_golden_change_set")
        change_set["patch_ref"] = self.content_ref(
            "art_" + "2" * 32, "change-set-patch/v1", patch
        )
        change_set["original_source_state_id"] = original_source["source_state_id"]
        entries = {item["path"]: deepcopy(item) for item in original_source["entries"]}
        for group in ("added", "modified", "type_changed"):
            for item in change_set[group]:
                entries[item["after"]["path"]] = deepcopy(item["after"])
        for item in change_set["deleted"]:
            entries.pop(item["before"]["path"])
        final_source = deepcopy(original_source)
        final_source["entries"] = list(entries.values())
        final_source = self.reseal_source_tree(final_source)
        change_set["final_source_state_id"] = final_source["source_state_id"]
        change_set = self.reseal("change-set/v1", change_set)
        owner_observation = self.fixture_document("task_observation_golden_observation")
        owner_observation["task_id"] = request["task_id"]
        owner_observation["attempt_id"] = attempt["attempt_id"]
        owner_observation["native_epoch"] = attempt["native_epoch"]
        owner_observation["actor"]["id"] = owner["owner_id"]
        owner_observation["actor"]["session_id"] = owner["session_id"]
        owner_observation["source_state_before_id"] = original_source["source_state_id"]
        owner_observation["source_state_after_id"] = original_source["source_state_id"]
        owner_observation["started_at"] = "2026-07-22T00:00:01.100Z"
        owner_observation["completed_at"] = "2026-07-22T00:00:01.105Z"
        owner_observation["duration_ms"] = 5
        owner_observation = self.reseal("observation/v1", owner_observation)
        verifier_observation = deepcopy(owner_observation)
        verifier_observation["observation_id"] = "obs_" + "2" * 32
        verifier_observation["observation_seq"] = 2
        verifier_observation["actor"] = {
            "schema_id": "actor/v1",
            "kind": "quality_verifier",
            "id": "verifier_" + "2" * 32,
            "session_id": "sess_" + "2" * 32,
        }
        verifier_observation["tool_invocation_id"] = "toolinv_" + "2" * 32
        verifier_observation["idempotency_key"] = "verify:reqs:slot1"
        verifier_observation["source_state_before_id"] = final_source["source_state_id"]
        verifier_observation["source_state_after_id"] = final_source["source_state_id"]
        verifier_observation["started_at"] = "2026-07-22T00:00:03.050Z"
        verifier_observation["completed_at"] = "2026-07-22T00:00:03.060Z"
        verifier_observation["duration_ms"] = 10
        verifier_observation = self.reseal("observation/v1", verifier_observation)
        pre_manifest = self.fixture_document("task_observation_golden_pre_manifest")
        pre_manifest["task_id"] = request["task_id"]
        pre_manifest["proposal_id"] = proposal_id
        pre_manifest["attempt_id"] = attempt["attempt_id"]
        pre_manifest["native_epoch"] = attempt["native_epoch"]
        pre_manifest["entries"][0]["actor"] = deepcopy(owner_observation["actor"])
        pre_manifest["entries"][0]["observation_ref"] = self.content_ref(
            "art_" + "4" * 32, "observation/v1", owner_observation
        )
        pre_manifest["entries"][0]["observation_id"] = owner_observation["observation_id"]
        pre_manifest["entries"][0]["observation_seq"] = owner_observation["observation_seq"]
        pre_manifest["entries"][0]["source_state_before_id"] = original_source["source_state_id"]
        pre_manifest["entries"][0]["source_state_after_id"] = original_source["source_state_id"]
        pre_manifest = self.reseal("pre-verifier-observation-manifest/v1", pre_manifest)
        final_manifest = self.fixture_document("task_observation_golden_final_manifest")
        final_manifest["task_id"] = request["task_id"]
        final_manifest["proposal_id"] = proposal_id
        final_manifest["attempt_id"] = attempt["attempt_id"]
        final_manifest["native_epoch"] = attempt["native_epoch"]
        final_manifest["pre_verifier_observation_manifest_ref"] = self.content_ref(
            "art_" + "5" * 32, "pre-verifier-observation-manifest/v1", pre_manifest
        )
        final_manifest["entries"][0] = deepcopy(pre_manifest["entries"][0])
        final_manifest["entries"][1]["actor"] = deepcopy(verifier_observation["actor"])
        final_manifest["entries"][1]["observation_ref"] = self.content_ref(
            "art_" + "6" * 32, "observation/v1", verifier_observation
        )
        final_manifest["entries"][1]["observation_id"] = verifier_observation["observation_id"]
        final_manifest["entries"][1]["observation_seq"] = verifier_observation["observation_seq"]
        final_manifest["entries"][1]["source_state_before_id"] = final_source["source_state_id"]
        final_manifest["entries"][1]["source_state_after_id"] = final_source["source_state_id"]
        final_manifest = self.reseal("observation-manifest/v1", final_manifest)
        execution_state = self.fixture_document("source_evidence_golden_execution_state")
        execution_state["source_state_id"] = final_source["source_state_id"]
        execution_state["execution_profile_ref"] = self.content_ref(
            "art_" + "3" * 32, "execution-profile/v1", profile
        )
        execution_state["execution_profile_digest"] = profile["profile_digest"]
        execution_state = self.reseal_execution_state(execution_state)
        task_snapshot["current_attempt_id"] = attempt["attempt_id"]
        task_snapshot["native_epoch"] = attempt["native_epoch"]
        task_snapshot["owner_epoch"] = owner["owner_epoch"]
        task_snapshot["lifecycle"] = "ACTIVE"
        task_snapshot["charter_version"] = charter["charter_version"]
        task_snapshot["charter_ref"] = self.content_ref(
            "art_" + "7" * 32, "task-charter/v1", charter
        )
        task_snapshot["ledger_head_digest"] = ledger["ledger_digest"]
        task_snapshot["request_ref"] = self.content_ref(
            "art_" + "8" * 32, "task-request/v1", request
        )
        task_snapshot["request_digest"] = hashlib.sha256(
            canonical_bytes(request)
        ).hexdigest()
        task_snapshot["policy_ref"] = self.content_ref(
            "art_" + "9" * 32, "effective-execution-policy/v1", policy
        )
        task_snapshot["policy_digest"] = policy["digest"]
        task_snapshot["updated_at"] = "2026-07-22T00:00:01.050Z"
        proposal = self.fixture_document("task_completion_golden_proposal")
        proposal["task_id"] = request["task_id"]
        proposal["proposal_id"] = proposal_id
        proposal["attempt_id"] = attempt["attempt_id"]
        proposal["native_epoch"] = attempt["native_epoch"]
        proposal["owner_id"] = owner["owner_id"]
        proposal["owner_epoch"] = owner["owner_epoch"]
        proposal["proposed_from_task_version"] = task_snapshot["task_version"]
        proposal["request_digest"] = task_snapshot["request_digest"]
        proposal["policy_digest"] = policy["digest"]
        proposal["requirement_ledger_digest"] = ledger["ledger_digest"]
        proposal["charter_digest"] = charter["digest"]
        proposal["outcome_requested"] = "COMPLETED"
        proposal["original_source_state_id"] = original_source["source_state_id"]
        proposal["final_source_state_id"] = final_source["source_state_id"]
        proposal["execution_state_ids"] = [execution_state["execution_state_id"]]
        proposal["change_set_ref"] = self.content_ref(
            "art_" + "a" * 32, "change-set/v1", change_set
        )
        proposal["requirement_claims"] = [
            {
                "requirement_id": requirement_id,
                "claimed_status": "PASS",
                "evidence_ids": [owner_observation["observation_id"]],
            }
            for requirement_id in ledger["active_requirement_ids"]
        ]
        proposal["created_at"] = "2026-07-22T00:00:02.000Z"
        proposal = self.reseal("completion-proposal/v1", proposal)
        task_snapshot["completion_proposal_ref"] = self.content_ref(
            "art_" + "b" * 32, "completion-proposal/v1", proposal
        )
        plan = self.fixture_document("quality_policy_golden_q2_plan")
        plan["task_id"] = request["task_id"]
        plan["proposal_id"] = proposal_id
        plan["proposal_digest"] = proposal["proposal_digest"]
        plan["policy_digest"] = policy["digest"]
        plan["task_type"] = request["task_type"]
        plan["requirement_ledger_digest"] = ledger["ledger_digest"]
        for slot in plan["slots"]:
            slot["requirement_ids"] = list(ledger["active_requirement_ids"])
        plan["input_digest"] = hashlib.sha256(
            canonical_bytes(
                {
                    field: plan[field]
                    for field in (
                        "proposal_digest",
                        "policy_digest",
                        "task_type",
                        "requirement_ledger_digest",
                        "change_set_classification_digest",
                        "capability_usage_digest",
                    )
                }
            )
        ).hexdigest()
        plan = self.reseal("quality-policy-plan/v1", plan)
        rule = {
            "schema_id": "source-content/v1",
            "media_type": "text/plain",
            "encoding": "base64",
            "data_base64": "cnVsZQ==",
            "byte_sha256": hashlib.sha256(b"rule").hexdigest(),
            "size_bytes": 4,
            "content_digest": "",
        }
        rule = self.reseal("source-content/v1", rule)
        verifier_input = self.fixture_document("task_verifier_input_golden_input")
        verifier_input["task_id"] = request["task_id"]
        verifier_input["proposal_id"] = proposal_id
        verifier_input["task_request_ref"] = self.content_ref(
            "art_" + "8" * 32, "task-request/v1", request
        )
        verifier_input["effective_policy_ref"] = self.content_ref(
            "art_" + "9" * 32, "effective-execution-policy/v1", policy
        )
        verifier_input["requirement_ledger_ref"] = self.content_ref(
            "art_" + "c" * 32, "requirement-ledger/v1", ledger
        )
        verifier_input["charter_ref"] = self.content_ref(
            "art_" + "d" * 32, "task-charter/v1", charter
        )
        verifier_input["completion_proposal_ref"] = self.content_ref(
            "art_" + "e" * 32, "completion-proposal/v1", proposal
        )
        verifier_input["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "f" * 32, "quality-policy-plan/v1", plan
        )
        verifier_input["quality_policy_plan_digest"] = plan["plan_digest"]
        verifier_input["original_source_ref"] = self.content_ref(
            "art_" + "1" * 32, "source-tree-manifest/v1", original_source
        )
        verifier_input["final_source_ref"] = self.content_ref(
            "art_" + "0" * 32, "source-tree-manifest/v1", final_source
        )
        verifier_input["change_set"] = {
            "availability": "available",
            "ref": self.content_ref("art_" + "a" * 32, "change-set/v1", change_set),
        }
        verifier_input["pre_verifier_observation_manifest_ref"] = self.content_ref(
            "art_" + "5" * 32, "pre-verifier-observation-manifest/v1", pre_manifest
        )
        verifier_input["engineering_rule_refs"] = [
            self.content_ref("art_" + "9" * 32, "source-content/v1", rule)
        ]
        verifier_input["slot_id"] = plan["slots"][0]["slot_id"]
        verifier_input["slot_concern"] = plan["slots"][0]["concern"]
        verifier_input["requirement_ids"] = list(ledger["active_requirement_ids"])
        verifier_input["created_at"] = "2026-07-22T00:00:02.500Z"
        verifier_input = self.reseal("verifier-input-manifest/v1", verifier_input)
        verifier_work = self.fixture_document("task_verifier_work_golden_work")
        verifier_work["task_id"] = request["task_id"]
        verifier_work["proposal_id"] = proposal_id
        verifier_work["verifier_input_manifest_ref"] = self.content_ref(
            "art_" + "1" * 31 + "a", "verifier-input-manifest/v1", verifier_input
        )
        verifier_work["verifier_input_manifest_digest"] = verifier_input["input_manifest_digest"]
        verifier_work["slot_id"] = verifier_input["slot_id"]
        verifier_work["own_observation_ids"] = [verifier_observation["observation_id"]]
        verifier_work["provisional_requirement_assessments"] = [
            {
                "requirement_id": requirement_id,
                "verdict": "PASS",
                "limitations": [],
                "evidence_ids": [verifier_observation["observation_id"]],
            }
            for requirement_id in verifier_input["requirement_ids"]
        ]
        verifier_work["created_at"] = "2026-07-22T00:00:03.000Z"
        verifier_work = self.reseal("verifier-work-report/v1", verifier_work)
        attestation = self.fixture_document("task_attestation_golden_attestation")
        attestation["task_id"] = request["task_id"]
        attestation["proposal_id"] = proposal_id
        attestation["verifier_input_manifest_ref"] = self.content_ref(
            "art_" + "1" * 31 + "a", "verifier-input-manifest/v1", verifier_input
        )
        attestation["verifier_input_manifest_digest"] = verifier_input["input_manifest_digest"]
        attestation["verifier_work_report_ref"] = self.content_ref(
            "art_" + "2" * 31 + "b", "verifier-work-report/v1", verifier_work
        )
        attestation["verifier_work_report_digest"] = verifier_work["report_digest"]
        attestation["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "f" * 32, "quality-policy-plan/v1", plan
        )
        attestation["quality_policy_plan_digest"] = plan["plan_digest"]
        attestation["final_observation_manifest_ref"] = self.content_ref(
            "art_" + "3" * 31 + "c", "observation-manifest/v1", final_manifest
        )
        attestation["final_observation_manifest_digest"] = final_manifest["manifest_digest"]
        attestation["source_state_id"] = final_source["source_state_id"]
        attestation["execution_state_ids"] = [execution_state["execution_state_id"]]
        attestation["slot_id"] = verifier_input["slot_id"]
        attestation["own_observation_ids"] = [verifier_observation["observation_id"]]
        attestation["requirement_verdicts"] = deepcopy(
            verifier_work["provisional_requirement_assessments"]
        )
        attestation["created_at"] = "2026-07-22T00:00:03.500Z"
        attestation = self.reseal("verification-attestation/v1", attestation)
        aggregate = self.fixture_document("task_verification_golden_attestation_manifest")
        aggregate["task_id"] = request["task_id"]
        aggregate["proposal_id"] = proposal_id
        aggregate["quality_policy_plan_ref"] = self.content_ref(
            "art_" + "f" * 32, "quality-policy-plan/v1", plan
        )
        aggregate["quality_policy_plan_digest"] = plan["plan_digest"]
        aggregate["final_observation_manifest_ref"] = self.content_ref(
            "art_" + "3" * 31 + "c", "observation-manifest/v1", final_manifest
        )
        aggregate["final_observation_manifest_digest"] = final_manifest["manifest_digest"]
        aggregate["attestation_count"] = 1
        aggregate["attestations"] = [
            {
                "slot_id": plan["slots"][0]["slot_id"],
                "attestation_id": attestation["attestation_id"],
                "run_status": attestation["run_status"],
                "attestation_ref": self.content_ref(
                    "art_" + "4" * 31 + "d", "verification-attestation/v1", attestation
                ),
            }
        ]
        aggregate["requirement_aggregates"] = [
            {
                "requirement_id": requirement_id,
                "required_slot_ids": [slot["slot_id"] for slot in plan["slots"]],
                "attestation_ids": [attestation["attestation_id"]],
                "verdict": "UNVERIFIABLE",
            }
            for requirement_id in verifier_input["requirement_ids"]
        ]
        aggregate["created_at"] = "2026-07-22T00:00:04.000Z"
        aggregate = self.reseal("verification-attestation-manifest/v1", aggregate)
        return {
            "request": request,
            "policy": policy,
            "ledger": ledger,
            "charter": charter,
            "attempt": attempt,
            "owner": owner,
            "task_snapshot": task_snapshot,
            "profile": profile,
            "selection_policy": selection,
            "owner_observation": owner_observation,
            "verifier_observation": verifier_observation,
            "original_source": original_source,
            "final_source": final_source,
            "patch": patch,
            "change_set": change_set,
            "pre_manifest": pre_manifest,
            "final_manifest": final_manifest,
            "execution_states": [execution_state],
            "proposal": proposal,
            "plan": plan,
            "engineering_rules": [rule],
            "input": verifier_input,
            "work": verifier_work,
            "attestation": attestation,
            "aggregate": aggregate,
        }
