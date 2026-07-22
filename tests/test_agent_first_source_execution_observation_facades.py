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
FAMILY_FILES = (
    "core.json",
    "change-set-patch.json",
    "change-set.json",
    "execution-profile.json",
    "execution-state.json",
    "source-state.json",
    "task-result-identities.json",
    "task-observation-manifests.json",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstSourceExecutionObservationFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.families = [
            json.loads((FAMILY_ROOT / name).read_text(encoding="utf-8"))
            for name in FAMILY_FILES
        ]
        cls.family_by_id = {
            family["family_id"]: family for family in cls.families
        }
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.families
            for schema in family["schemas"]
        }
        canonical = canonical_bytes({"families": cls.families})
        python_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_source_execution_observation_python")
        exec(python_bytes, cls.python.__dict__)
        cls.npm = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def python_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for schema_id, document in cases:
            try:
                value = self.python.validate_document(schema_id, document)
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
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="source-state-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const results = cases.map(([schemaId, document]) => {",
                        "  try {",
                        "    return {ok: true, value: facade.validateDocument(",
                        "      schemaId, document",
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

    def assert_parity(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        python = self.python_results(cases)
        self.assertEqual(python, self.node_results(cases))
        return python

    def family_fixture_cases(
        self, family_id: str
    ) -> tuple[list[dict[str, object]], list[tuple[str, dict[str, object]]]]:
        fixtures = self.family_by_id[family_id]["fixtures"]
        cases = [
            (fixture["schema_id"], deepcopy(fixture["document"]))
            for fixture in fixtures
        ]
        return fixtures, cases

    def fixture_document(self, family_id: str, fixture_id: str) -> dict[str, object]:
        fixture = next(
            item
            for item in self.family_by_id[family_id]["fixtures"]
            if item["fixture_id"] == fixture_id
        )
        return deepcopy(fixture["document"])

    def reseal(self, schema_id: str, value: dict[str, object]) -> dict[str, object]:
        document = deepcopy(value)
        spec = self.schemas[schema_id]["x-pullwise-digest"]
        field, domain = spec["field"], spec["domain"]
        unsigned = {key: item for key, item in document.items() if key != field}
        document[field] = hashlib.sha256(
            domain.encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return document

    def reseal_source_tree(self, value: dict[str, object]) -> dict[str, object]:
        document = deepcopy(value)
        entries = sorted(document["entries"], key=lambda item: item["path"].encode("utf-8"))
        document["entries"] = entries
        document["entry_count"] = len(entries)
        document["total_bytes"] = sum(
            item["size_bytes"] for item in entries if item["type"] == "file"
        )
        identity = {
            "base_revision": document["base_revision"],
            "selection_policy_digest": document["selection_policy_digest"],
            "entries": entries,
        }
        document["source_state_id"] = hashlib.sha256(
            canonical_bytes(identity)
        ).hexdigest()
        return self.reseal("source-tree-manifest/v1", document)

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

    def final_source_tree(
        self, original: dict[str, object], change_set: dict[str, object]
    ) -> dict[str, object]:
        entries = {item["path"]: deepcopy(item) for item in original["entries"]}
        for item in change_set["added"]:
            entries[item["after"]["path"]] = deepcopy(item["after"])
        for item in change_set["modified"] + change_set["type_changed"]:
            entries[item["after"]["path"]] = deepcopy(item["after"])
        for item in change_set["deleted"]:
            del entries[item["before"]["path"]]
        final = deepcopy(original)
        final["entries"] = list(entries.values())
        return self.reseal_source_tree(final)

    def bind_observation_prefix(
        self,
        observations: dict[str, object],
        pre: dict[str, object],
    ) -> dict[str, object]:
        document = deepcopy(observations)
        artifact_id = document["pre_verifier_observation_manifest_ref"]["artifact_id"]
        document["pre_verifier_observation_manifest_ref"] = self.content_ref(
            artifact_id, "pre-verifier-observation-manifest/v1", pre
        )
        return self.reseal("observation-manifest/v1", document)

    def python_helper_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results = []
        for operation in operations:
            try:
                value = getattr(self.python, operation["python"])(*operation["args"])
            except self.python.ContractValidationError as error:
                results.append({
                    "ok": False,
                    "code": error.code,
                    "detail": error.detail,
                    "path": error.path,
                })
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_helper_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        payload = [
            {"helper": item["node"], "args": item["args"]}
            for item in operations
        ]
        with tempfile.TemporaryDirectory(prefix="source-context-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join((
                    f"import * as facade from {json.dumps(facade_path.as_uri())};",
                    f"const operations = {json.dumps(payload, separators=(',', ':'))};",
                    "const results = [];",
                    "for (const operation of operations) {",
                    "  try {",
                    "    results.push({ok: true, value:",
                    "      await facade[operation.helper](...operation.args)});",
                    "  } catch (error) {",
                    "    results.push({ok: false, code: error.code,",
                    "      detail: error.detail, path: error.path});",
                    "  }",
                    "}",
                    "process.stdout.write(JSON.stringify(results));",
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

    def assert_helper_parity(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python = self.python_helper_results(operations)
        self.assertEqual(python, self.node_helper_results(operations))
        return python

    def test_change_set_fixtures_execute_through_both_facades(self) -> None:
        fixtures, cases = self.family_fixture_cases("change-set")

        results = self.assert_parity(cases)

        for fixture, (_, document), result in zip(fixtures, cases, results):
            expected_code = fixture["expected_code"]
            self.assertEqual(
                expected_code,
                None if result["ok"] else result["code"],
                fixture["fixture_id"],
            )
            if expected_code is None:
                self.assertEqual(document, result["value"])
        self.assertEqual(
            canonical_bytes(results[0]["value"]),
            canonical_bytes(results[1]["value"]),
        )

    def test_other_owned_fixtures_execute_through_both_facades(self) -> None:
        for family_id in (
            "execution-state",
            "source-state",
            "task-observation-manifests",
        ):
            with self.subTest(family_id=family_id):
                fixtures, cases = self.family_fixture_cases(family_id)
                results = self.assert_parity(cases)
                for fixture, (_, document), result in zip(fixtures, cases, results):
                    expected_code = fixture["expected_code"]
                    self.assertEqual(
                        expected_code,
                        None if result["ok"] else result["code"],
                        fixture["fixture_id"],
                    )
                    if expected_code is None:
                        self.assertEqual(document, result["value"])

    def test_context_helpers_bind_exact_documents_with_parity(self) -> None:
        policy = self.fixture_document(
            "source-state", "source_evidence_golden_selection_policy"
        )
        original = self.fixture_document(
            "source-state", "source_evidence_golden_source_tree"
        )
        profile = self.fixture_document(
            "execution-profile", "source_evidence_golden_execution_profile"
        )
        execution = self.fixture_document(
            "execution-state", "source_evidence_golden_execution_state"
        )
        patch = self.fixture_document(
            "change-set-patch", "source_evidence_golden_patch"
        )
        change = self.fixture_document(
            "change-set", "source_evidence_golden_change_set"
        )
        final = self.final_source_tree(original, change)
        pre = self.fixture_document(
            "task-observation-manifests", "task_observation_golden_pre_manifest"
        )
        observations = self.fixture_document(
            "task-observation-manifests", "task_observation_golden_final_manifest"
        )
        observations = self.bind_observation_prefix(observations, pre)
        self.assertEqual(change["final_source_state_id"], final["source_state_id"])
        operations = [
            {"python": "verify_source_tree_context", "node": "verifySourceTreeContext", "args": [original, policy]},
            {"python": "verify_execution_state_context", "node": "verifyExecutionStateContext", "args": [execution, original, profile]},
            {"python": "verify_change_set_context", "node": "verifyChangeSetContext", "args": [change, original, final, patch]},
            {"python": "verify_observation_manifest_extension", "node": "verifyObservationManifestExtension", "args": [observations, pre]},
        ]
        results = self.assert_helper_parity(operations)
        self.assertEqual(
            [original, execution, change, observations],
            [item["value"] for item in results],
        )

    def test_context_and_extension_drift_have_exact_parity(self) -> None:
        policy = self.fixture_document(
            "source-state", "source_evidence_golden_selection_policy"
        )
        original = self.fixture_document(
            "source-state", "source_evidence_golden_source_tree"
        )
        profile = self.fixture_document(
            "execution-profile", "source_evidence_golden_execution_profile"
        )
        execution = self.fixture_document(
            "execution-state", "source_evidence_golden_execution_state"
        )
        patch = self.fixture_document(
            "change-set-patch", "source_evidence_golden_patch"
        )
        change = self.fixture_document(
            "change-set", "source_evidence_golden_change_set"
        )
        final = self.final_source_tree(original, change)
        pre = self.fixture_document(
            "task-observation-manifests", "task_observation_golden_pre_manifest"
        )
        observations = self.fixture_document(
            "task-observation-manifests", "task_observation_golden_final_manifest"
        )
        observations = self.bind_observation_prefix(observations, pre)

        changed_policy = deepcopy(policy)
        changed_policy["root_identity"] = "root_22222222222222222222222222222222"
        changed_policy = self.reseal("source-selection-policy/v1", changed_policy)
        changed_profile = deepcopy(profile)
        changed_profile["sandbox_identity"] = "bwrap:2.0"
        changed_profile = self.reseal("execution-profile/v1", changed_profile)
        changed_change = deepcopy(change)
        changed_change["modified"][0]["after"]["executable"] = True
        changed_change = self.reseal("change-set/v1", changed_change)
        changed_observations = deepcopy(observations)
        changed_observations["proposal_id"] = (
            "proposal_22222222222222222222222222222222"
        )
        changed_observations = self.reseal(
            "observation-manifest/v1", changed_observations
        )
        changed_pre = deepcopy(pre)
        changed_pre["entries"][0]["source_state_after_id"] = "f" * 64
        changed_pre = self.reseal(
            "pre-verifier-observation-manifest/v1", changed_pre
        )
        changed_prefix = self.bind_observation_prefix(observations, changed_pre)
        operations = [
            {"python": "verify_source_tree_context", "node": "verifySourceTreeContext", "args": [original, changed_policy]},
            {"python": "verify_execution_state_context", "node": "verifyExecutionStateContext", "args": [execution, original, changed_profile]},
            {"python": "verify_change_set_context", "node": "verifyChangeSetContext", "args": [changed_change, original, final, patch]},
            {"python": "verify_observation_manifest_extension", "node": "verifyObservationManifestExtension", "args": [changed_observations, pre]},
            {"python": "verify_observation_manifest_extension", "node": "verifyObservationManifestExtension", "args": [changed_prefix, changed_pre]},
        ]
        results = self.assert_helper_parity(operations)
        self.assertEqual(
            [
                ("SOURCE_TREE_CONTEXT_INVALID", "$.selection_policy_digest"),
                ("EXECUTION_STATE_CONTEXT_INVALID", "$.execution_profile_digest"),
                ("CHANGE_SET_CONTEXT_INVALID", "$.modified"),
                ("OBSERVATION_MANIFEST_IDENTITY_INVALID", "$.proposal_id"),
                ("OBSERVATION_MANIFEST_EXTENSION_INVALID", "$.entries"),
            ],
            [(item["detail"], item["path"]) for item in results],
        )

    def test_embedded_digest_and_semantic_drift_have_exact_parity(self) -> None:
        documents = [
            ("change-set/v1", self.fixture_document("change-set", "source_evidence_golden_change_set")),
            ("execution-state-manifest/v1", self.fixture_document("execution-state", "source_evidence_golden_execution_state")),
            ("source-selection-policy/v1", self.fixture_document("source-state", "source_evidence_golden_selection_policy")),
            ("source-tree-manifest/v1", self.fixture_document("source-state", "source_evidence_golden_source_tree")),
            ("observation-manifest/v1", self.fixture_document("task-observation-manifests", "task_observation_golden_final_manifest")),
            ("pre-verifier-observation-manifest/v1", self.fixture_document("task-observation-manifests", "task_observation_golden_pre_manifest")),
        ]
        cases = []
        expected_paths = []
        for schema_id, document in documents:
            field = self.schemas[schema_id]["x-pullwise-digest"]["field"]
            document[field] = (
                ("0" if document[field][0] != "0" else "1") + document[field][1:]
            )
            cases.append((schema_id, document))
            expected_paths.append("$." + field)
        results = self.assert_parity(cases)
        self.assertEqual(
            [("CONTRACT_DIGEST_MISMATCH", path) for path in expected_paths],
            [(item["detail"], item["path"]) for item in results],
        )

    def test_current_domain_reviewer_is_accepted_and_legacy_alias_rejected(self) -> None:
        current = self.fixture_document(
            "task-observation-manifests", "task_observation_golden_pre_manifest"
        )
        current["entries"][0]["actor"] = {
            "schema_id": "actor/v1",
            "kind": "domain_reviewer",
            "id": "reviewer_11111111111111111111111111111111",
            "session_id": "sess_11111111111111111111111111111111",
        }
        current = self.reseal("pre-verifier-observation-manifest/v1", current)
        legacy = deepcopy(current)
        legacy["entries"][0]["actor"]["kind"] = "legacy_domain_reviewer"
        legacy = self.reseal("pre-verifier-observation-manifest/v1", legacy)
        results = self.assert_parity([
            ("pre-verifier-observation-manifest/v1", current),
            ("pre-verifier-observation-manifest/v1", legacy),
        ])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])


if __name__ == "__main__":
    unittest.main()
