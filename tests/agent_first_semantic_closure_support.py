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
from tests.agent_first_verification_direct_graph_builder import (
    VerificationDirectGraphBuilderMixin,
    canonical_bytes,
)
from tests.agent_first_task_result_selector_support import (
    bind_task_result_to_terminal_decision,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source"


def snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SemanticClosureHarness(VerificationDirectGraphBuilderMixin):
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.bundle = built.document
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle["families"]
            for schema in family["schemas"]
        }
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for family in cls.bundle["families"]
            for fixture in family["fixtures"]
        }
        cls.stable_error_codes = {
            entry["code"]
            for entry in cls.fixture_document("error_golden_current_registry")[
                "entries"
            ]
        }
        cls.python = types.ModuleType("_semantic_closure_python_facade")
        exec(built.python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = built.npm_wrapper
        cls.rule_inventory = cls._semantic_inventory("document_rules")
        cls.helper_inventory = cls._semantic_inventory("contextual_helpers")

    @classmethod
    def _semantic_inventory(
        cls, key: str
    ) -> dict[str, tuple[str, ...]]:
        owners: dict[str, set[str]] = {}
        for schema_id, schema in cls.schemas.items():
            semantics = schema.get("x-pullwise-semantics")
            if not isinstance(semantics, dict):
                continue
            for item in semantics.get(key, []):
                owners.setdefault(item, set()).add(schema_id)
        return {
            item: tuple(sorted(schema_ids))
            for item, schema_ids in sorted(owners.items())
        }

    @classmethod
    def fixture_document(cls, fixture_id: str) -> dict[str, object]:
        return deepcopy(cls.fixtures[fixture_id]["document"])

    @classmethod
    def schema_rules(cls, schema_id: str) -> tuple[str, ...]:
        semantics = cls.schemas[schema_id].get("x-pullwise-semantics", {})
        return tuple(semantics.get("document_rules", []))

    @classmethod
    def schema_helpers(cls, schema_id: str) -> tuple[str, ...]:
        semantics = cls.schemas[schema_id].get("x-pullwise-semantics", {})
        return tuple(semantics.get("contextual_helpers", []))

    @classmethod
    def reseal(cls, schema_id: str, document: dict[str, object]) -> dict[str, object]:
        spec = cls.schemas[schema_id]["x-pullwise-digest"]
        field = spec["field"]
        result = deepcopy(document)
        unsigned = {key: value for key, value in result.items() if key != field}
        result[field] = hashlib.sha256(
            spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return result

    @classmethod
    def reseal_source_tree(cls, document: dict[str, object]) -> dict[str, object]:
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
        return cls.reseal("source-tree-manifest/v1", result)

    @classmethod
    def reseal_execution_state(
        cls, document: dict[str, object]
    ) -> dict[str, object]:
        result = deepcopy(document)
        unsigned = {
            key: value
            for key, value in result.items()
            if key not in {"execution_state_id", "manifest_digest"}
        }
        result["execution_state_id"] = hashlib.sha256(
            canonical_bytes(unsigned)
        ).hexdigest()
        return cls.reseal("execution-state-manifest/v1", result)

    @classmethod
    def content_ref(
        cls, artifact_id: str, schema_id: str, document: dict[str, object]
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

    @classmethod
    def helper_aliases(cls) -> dict[str, str]:
        return {
            helper_id: snake_to_camel(helper_id)
            for helper_id in cls.helper_inventory
        }

    @classmethod
    def _positive_fixture_cases(cls) -> list[dict[str, object]]:
        preferred: dict[str, dict[str, object]] = {}
        for fixture in cls.fixtures.values():
            if fixture["expected_code"] is not None:
                continue
            if not cls.schema_rules(fixture["schema_id"]):
                continue
            schema_id = fixture["schema_id"]
            current = preferred.get(schema_id)
            if current is not None and current["fixture_class"] == "golden":
                continue
            if current is None or fixture["fixture_class"] == "golden":
                preferred[schema_id] = fixture
        return [
            {
                "fixture_id": fixture["fixture_id"],
                "schema_id": fixture["schema_id"],
                "document": deepcopy(fixture["document"]),
            }
            for schema_id, fixture in sorted(preferred.items())
        ]

    @classmethod
    def synthetic_agent_tool_request(cls) -> dict[str, object]:
        invocation = cls.fixture_document("tool_golden_invocation")
        return {
            "schema_id": "agent-tool-request/v1",
            "idempotency_key": invocation["idempotency_key"],
            "tool_key": invocation["tool_key"],
            "tool_input": deepcopy(invocation["tool_input"]),
        }

    @classmethod
    def synthetic_source_content(cls, raw: bytes = b"hello") -> dict[str, object]:
        return cls.reseal(
            "source-content/v1",
            {
                "schema_id": "source-content/v1",
                "media_type": "application/octet-stream",
                "encoding": "base64",
                "data_base64": base64.b64encode(raw).decode("ascii"),
                "byte_sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
        )

    @classmethod
    def synthetic_source_state(cls, manifest_seed: str = "0") -> dict[str, object]:
        invocation = cls.fixture_document("tool_golden_invocation")
        return cls.reseal(
            "source-state/v1",
            {
                "schema_id": "source-state/v1",
                "task_id": invocation["task_id"],
                "attempt_id": invocation["attempt_id"],
                "native_epoch": invocation["native_epoch"],
                "repository_root_id": "f" * 64,
                "entry_count": 1,
                "manifest_sha256": manifest_seed * 64,
            },
        )

    @classmethod
    def synthetic_artifact_content_ref(cls) -> dict[str, object]:
        document = cls.fixture_document("source_evidence_golden_change_set")
        return {
            "schema_id": "artifact-content-ref/v1",
            "artifact_kind": "change_set",
            "ref": cls.content_ref(
                "art_" + "3" * 32, "change-set/v1", document
            ),
        }

    @classmethod
    def synthetic_availability_ref(cls) -> dict[str, object]:
        document = cls.fixture_document("source_evidence_golden_source_tree")
        return {
            "availability": "available",
            "ref": cls.content_ref(
                "art_" + "4" * 32, "source-tree-manifest/v1", document
            ),
        }

    @classmethod
    def synthetic_requirement_entry(cls) -> dict[str, object]:
        entry = cls.fixture_document(
            "requirements_negative_derived_mandatory_without_rationale"
        )
        entry["rationale"] = "Required to preserve the accepted objective."
        return entry

    @classmethod
    def synthetic_waiver_event(cls) -> dict[str, object]:
        return cls.fixture_document("requirements_negative_waiver_empty_issuer_profile")

    @classmethod
    def synthetic_tool_dispatch_intent(cls) -> dict[str, object]:
        return cls.fixture_document("tool_crash_after_intent")

    @classmethod
    def positive_document_cases(cls) -> list[dict[str, object]]:
        cases = cls._positive_fixture_cases()
        covered = {
            rule_id
            for case in cases
            for rule_id in cls.schema_rules(case["schema_id"])
        }
        synthetic_builders = {
            "agent-tool-request/v1": cls.synthetic_agent_tool_request,
            "artifact-content-ref/v1": cls.synthetic_artifact_content_ref,
            "availability-ref/v1": cls.synthetic_availability_ref,
            "requirement-entry/v1": cls.synthetic_requirement_entry,
            "source-content/v1": cls.synthetic_source_content,
            "source-state/v1": cls.synthetic_source_state,
            "tool-dispatch-intent/v1": cls.synthetic_tool_dispatch_intent,
            "waiver-event/v1": cls.synthetic_waiver_event,
        }
        for schema_id, builder in synthetic_builders.items():
            schema_rules = set(cls.schema_rules(schema_id))
            if not schema_rules.difference(covered):
                continue
            cases.append(
                {
                    "fixture_id": f"synthetic::{schema_id}",
                    "schema_id": schema_id,
                    "document": builder(),
                }
            )
            covered.update(schema_rules)
        return cases

    def _python_capture(self, callback) -> dict[str, object]:
        try:
            return {"ok": True, "value": callback()}
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        except Exception as error:  # pragma: no cover - fail-closed sentinel
            return {
                "ok": False,
                "code": f"__python_exception__:{type(error).__name__}",
                "detail": str(error),
                "path": "$",
            }

    def python_document_rule_results(
        self, cases: list[dict[str, object]]
    ) -> dict[str, object]:
        handlers = self.python._DOCUMENT_RULE_HANDLERS
        original = dict(handlers)
        original_validate_semantics = self.python._validate_semantics
        hits: list[str] = []
        case_hits: list[list[str]] = []
        active_hits: list[str] = []
        events: list[dict[str, str]] = []
        case_events: list[list[dict[str, str]]] = []
        active_events: list[dict[str, str]] = []
        schema_stack: list[str] = []
        failures: list[str] = []
        case_failures: list[list[str]] = []
        active_failures: list[str] = []
        try:
            def tracked_validate_semantics(
                schema_id: str, value: dict[str, object]
            ) -> None:
                schema_stack.append(schema_id)
                try:
                    original_validate_semantics(schema_id, value)
                finally:
                    schema_stack.pop()

            self.python._validate_semantics = tracked_validate_semantics
            for rule_id, handler in original.items():
                def wrapped(
                    value: dict[str, object],
                    *,
                    _handler=handler,
                    _rule_id=rule_id,
                ) -> object:
                    hits.append(_rule_id)
                    active_hits.append(_rule_id)
                    event = {"schemaId": schema_stack[-1], "ruleId": _rule_id}
                    events.append(event)
                    active_events.append(event)
                    try:
                        return _handler(value)
                    except BaseException:
                        failures.append(_rule_id)
                        active_failures.append(_rule_id)
                        raise

                handlers[rule_id] = wrapped
            results = []
            for case in cases:
                active_hits = []
                active_events = []
                active_failures = []
                schema_id = case["schema_id"]
                validator = (
                    self.python.verify_document_digest
                    if "x-pullwise-digest" in self.schemas[schema_id]
                    else self.python.validate_document
                )
                results.append(
                    self._python_capture(
                        lambda validator=validator, case=case: validator(
                            schema_id, case["document"]
                        )
                    )
                )
                case_hits.append(active_hits)
                case_events.append(active_events)
                case_failures.append(active_failures)
        finally:
            self.python._validate_semantics = original_validate_semantics
            handlers.clear()
            handlers.update(original)
        return {
            "results": results,
            "hits": hits,
            "case_hits": case_hits,
            "events": events,
            "case_events": case_events,
            "failures": failures,
            "case_failures": case_failures,
        }

    def node_document_rule_results(
        self, cases: list[dict[str, object]]
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="semantic-closure-rules-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const hits = [];",
                        "const caseHits = [];",
                        "let activeHits = [];",
                        "const failures = [];",
                        "const caseFailures = [];",
                        "let activeFailures = [];",
                        "globalThis.__PULLWISE_DOCUMENT_RULE_PROBE__ = (event) => {",
                        "  hits.push(event);",
                        "  activeHits.push(event);",
                        "  if (event.rejected === true) {",
                        "    failures.push(event.ruleId);",
                        "    activeFailures.push(event.ruleId);",
                        "  }",
                        "};",
                        "const results = [];",
                        "for (const item of cases) {",
                        "  activeHits = [];",
                        "  activeFailures = [];",
                        "  try {",
                        "    const digest = facade.schema(item.schema_id)['x-pullwise-digest'];",
                        "    const value = digest",
                        "      ? await facade.verifyDocumentDigest(item.schema_id, item.document)",
                        "      : facade.validateDocument(item.schema_id, item.document);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) {",
                        "    results.push({ok: false, code: error.code ?? `__node_exception__:${error.name}`,",
                        "      detail: error.detail ?? String(error.message ?? error), path: error.path ?? '$'});",
                        "  }",
                        "  caseHits.push(activeHits);",
                        "  caseFailures.push(activeFailures);",
                        "}",
                        "delete globalThis.__PULLWISE_DOCUMENT_RULE_PROBE__;",
                        "process.stdout.write(JSON.stringify({results, hits, case_hits: caseHits, failures, case_failures: caseFailures}));",
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

    def python_document_rule_handler_results(
        self, cases: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results = []
        for case in cases:
            rule_id = case["rule_id"]
            handler = self.python._DOCUMENT_RULE_HANDLERS.get(rule_id)
            if handler is None:
                results.append(
                    {
                        "ok": False,
                        "code": "CONTRACT_SEMANTIC_RULE_UNIMPLEMENTED",
                        "detail": rule_id,
                        "path": "$",
                    }
                )
                continue
            results.append(
                self._python_capture(
                    lambda handler=handler, case=case: handler(
                        case["semantic_document"]
                    )
                )
            )
        return results

    def node_document_rule_handler_results(
        self, cases: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        operations = [
            {
                "rule_id": case["rule_id"],
                "semantic_document": case["semantic_document"],
            }
            for case in cases
        ]
        with tempfile.TemporaryDirectory(prefix="semantic-closure-rule-handlers-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(
                self.npm_wrapper
                + b'''\nexport function __semanticClosureInvokeDocumentRule(ruleId, value) {
  const handler = DOCUMENT_RULE_HANDLERS[ruleId];
  if (!handler) fail("CONTRACT_SEMANTIC_RULE_UNIMPLEMENTED", ruleId);
  return handler(value);
}
'''
            )
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = await facade.__semanticClosureInvokeDocumentRule(",
                        "      operation.rule_id, operation.semantic_document);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) {",
                        "    results.push({ok: false, code: error.code ?? `__node_exception__:${error.name}`,",
                        "      detail: error.detail ?? String(error.message ?? error), path: error.path ?? '$'});",
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
        return json.loads(completed.stdout)

    def python_helper_exports(self) -> dict[str, dict[str, bool]]:
        exported = set(getattr(self.python, "__all__", []))
        return {
            helper_id: {
                "present": callable(getattr(self.python, helper_id, None)),
                "exported": helper_id in exported,
            }
            for helper_id in self.helper_inventory
        }

    def node_helper_exports(self) -> dict[str, dict[str, bool]]:
        aliases = self.helper_aliases()
        with tempfile.TemporaryDirectory(prefix="semantic-closure-exports-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const aliases = {json.dumps(aliases, separators=(',', ':'))};",
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
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        return json.loads(completed.stdout)

    @staticmethod
    def helper_operation(
        helper_id: str,
        args: list[object],
        *,
        kwargs: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "python": helper_id,
            "node": snake_to_camel(helper_id),
            "args": args,
            "kwargs": kwargs or {},
        }

    @staticmethod
    def normalize_runtime_value(value: object) -> object:
        def normalize(item: object) -> object:
            if isinstance(item, (bytes, bytearray, memoryview)):
                return {"__bytes_hex__": bytes(item).hex()}
            if isinstance(item, list):
                return [normalize(child) for child in item]
            if isinstance(item, dict):
                return {key: normalize(child) for key, child in item.items()}
            return item

        return normalize(value)

    def python_helper_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results = []
        for operation in operations:
            helper = getattr(self.python, operation["python"], None)
            if not callable(helper):
                results.append(
                    {
                        "ok": False,
                        "code": "__python_exception__:MissingExport",
                        "detail": operation["python"],
                        "path": "$",
                    }
                )
                continue
            result = self._python_capture(
                lambda helper=helper, operation=operation: helper(
                    *operation["args"], **operation["kwargs"]
                )
            )
            if result["ok"]:
                result["value"] = self.normalize_runtime_value(result["value"])
            results.append(result)
        return results

    def node_helper_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="semantic-closure-helpers-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        "const normalize = (value) => {",
                        "  if (value instanceof Uint8Array) {",
                        "    return {__bytes_hex__: Buffer.from(value).toString('hex')};",
                        "  }",
                        "  if (Array.isArray(value)) return value.map(normalize);",
                        "  if (value !== null && typeof value === 'object') {",
                        "    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalize(item)]));",
                        "  }",
                        "  return value;",
                        "};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  const snake = facade[operation.python];",
                        "  const camel = facade[operation.node];",
                        "  if (typeof snake !== 'function' || typeof camel !== 'function' || snake !== camel) {",
                        "    results.push({ok: false, code: '__node_exception__:MissingExport', detail: operation.python, path: '$'});",
                        "    continue;",
                        "  }",
                        "  try {",
                        "    const extra = operation.kwargs && Object.keys(operation.kwargs).length",
                        "      ? [operation.kwargs] : [];",
                        "    const value = await camel(...operation.args, ...extra);",
                        "    results.push({ok: true, value: normalize(value)});",
                        "  } catch (error) {",
                        "    results.push({ok: false, code: error.code ?? `__node_exception__:${error.name}`,",
                        "      detail: error.detail ?? String(error.message ?? error), path: error.path ?? '$'});",
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
        return json.loads(completed.stdout)

    def make_pre_gate_manifest(
        self,
        root_set: dict[str, object],
        *,
        extra_refs: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        root_ref = self.content_ref(
            "art_" + "a" * 32, "pre-gate-root-set/v1", root_set
        )
        entries = []
        for field, value in root_set.items():
            if field in {
                "schema_id",
                "task_id",
                "outcome_candidate",
                "root_set_digest",
            }:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict) and item.get("availability") == "available":
                    entries.append(deepcopy(item["ref"]))
        entries.append(root_ref)
        entries.extend(extra_refs or [])
        unique = {
            canonical_bytes(ref): ref
            for ref in entries
        }
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item["content_schema_id"],
                item["artifact_id"],
                item["sha256"],
            ),
        )
        return self.reseal(
            "pre-gate-evidence-closure-manifest/v1",
            {
                "schema_id": "pre-gate-evidence-closure-manifest/v1",
                "task_id": root_set["task_id"],
                "pre_gate_root_set_ref": root_ref,
                "entries": ordered,
                "entry_count": len(ordered),
                "pre_gate_closure_digest": hashlib.sha256(
                    canonical_bytes(ordered)
                ).hexdigest(),
            },
        )

    def make_success_context(self) -> list[object]:
        root_set = self.fixture_document("pre_gate_golden_root_set")
        snapshot = self.fixture_document("gate_input_golden_success_snapshot")
        quality_ref = deepcopy(snapshot["quality_policy_plan_ref"])
        quality_ref["artifact_id"] = "art_deadbeefdeadbeefdeadbeefdeadbeef"
        manifest = self.make_pre_gate_manifest(root_set, extra_refs=[quality_ref])
        mapping = {
            "request_ref": "request",
            "policy_ref": "policy",
            "requirement_ledger_ref": "ledger",
            "completion_proposal_ref": "proposal",
            "original_source_ref": "original_source",
            "final_source_ref": "final_source",
            "pre_observation_manifest_ref": "pre_observation_manifest",
            "final_observation_manifest_ref": "final_observation_manifest",
            "verification_attestation_manifest_ref": "attestations",
            "effect_ledger_ref": "effect_ledger",
            "budget_summary_ref": "budget_summary",
            "publication_content_manifest_ref": "publication_content_manifest",
            "debug_redaction_plan_ref": "debug_redaction_plan",
        }
        for target, source in mapping.items():
            snapshot[target] = deepcopy(root_set[source]["ref"])
        snapshot["execution_state_refs"] = [
            deepcopy(item["ref"]) for item in root_set["execution_states"]
        ]
        snapshot["change_set"] = deepcopy(root_set["change_set"])
        snapshot["requested_outcome"] = root_set["outcome_candidate"]
        snapshot["pre_gate_root_set_ref"] = self.content_ref(
            "art_" + "a" * 32, "pre-gate-root-set/v1", root_set
        )
        snapshot["pre_gate_evidence_closure_ref"] = self.content_ref(
            "art_" + "b" * 32, "pre-gate-evidence-closure-manifest/v1", manifest
        )
        snapshot["pre_gate_closure_digest"] = manifest["pre_gate_closure_digest"]
        snapshot["quality_policy_plan_ref"] = deepcopy(quality_ref)
        snapshot = self.reseal("gate-input-snapshot/v1", snapshot)
        return [snapshot, root_set, manifest]

    def make_terminal_context(self) -> list[object]:
        root_set = self.fixture_document("pre_gate_golden_terminal_root_set")
        snapshot = self.fixture_document(
            "gate_input_golden_terminalization_snapshot"
        )
        fact = self.fixture_document("gate_preparation_golden_terminalization_fact")
        fact_ref = self.content_ref(
            "art_" + "d" * 32, "terminalization-fact/v1", fact
        )
        root_set["termination_facts"] = [
            {"availability": "available", "ref": fact_ref}
        ]
        root_set = self.reseal("pre-gate-root-set/v1", root_set)
        manifest = self.make_pre_gate_manifest(root_set)
        mapping = {
            "request_ref": "request",
            "policy_ref": "policy",
            "requirement_ledger_ref": "ledger",
            "original_source": "original_source",
            "final_source": "final_source",
            "final_observation_manifest": "final_observation_manifest",
            "effect_ledger_ref": "effect_ledger",
            "budget_summary_ref": "budget_summary",
            "publication_content_manifest_ref": "publication_content_manifest",
            "debug_redaction_plan_ref": "debug_redaction_plan",
        }
        for target, source in mapping.items():
            value = root_set[source]
            snapshot[target] = deepcopy(
                value["ref"] if target.endswith("_ref") else value
            )
        snapshot["terminalization_fact_refs"] = [fact_ref]
        snapshot["pre_gate_root_set_ref"] = self.content_ref(
            "art_" + "a" * 32, "pre-gate-root-set/v1", root_set
        )
        snapshot["pre_gate_evidence_closure_ref"] = self.content_ref(
            "art_" + "b" * 32, "pre-gate-evidence-closure-manifest/v1", manifest
        )
        snapshot["pre_gate_closure_digest"] = manifest["pre_gate_closure_digest"]
        snapshot = self.reseal("terminalization-input-snapshot/v1", snapshot)
        return [snapshot, root_set, manifest, [fact]]

    def gate_success_inputs(self) -> tuple[dict[str, object], dict[str, object]]:
        registry = self.fixture_document("gate_golden_independent_registry")
        snapshot = self.fixture_document("gate_input_golden_success_snapshot")
        decision = self.fixture_document("gate_decision_golden_success")
        snapshot["predicate_registry_digest"] = registry["registry_digest"]
        snapshot = self.reseal("gate-input-snapshot/v1", snapshot)
        context = {
            "input_snapshot_ref": self.content_ref(
                "art_" + "1" * 32, "gate-input-snapshot/v1", snapshot
            ),
            "predicate_results": deepcopy(decision["predicate_results"]),
        }
        return snapshot, context

    def gate_terminal_inputs(
        self,
    ) -> tuple[dict[str, object], dict[str, object]]:
        registry = self.fixture_document("gate_golden_independent_registry")
        snapshot = self.fixture_document(
            "gate_input_golden_terminalization_snapshot"
        )
        decision = self.fixture_document("gate_decision_golden_terminalization")
        snapshot["predicate_registry_digest"] = registry["registry_digest"]
        snapshot = self.reseal("terminalization-input-snapshot/v1", snapshot)
        context = {
            "input_snapshot_ref": self.content_ref(
                "art_" + "2" * 32,
                "terminalization-input-snapshot/v1",
                snapshot,
            ),
            "profile": decision["profile"],
            "gate_mode": decision["gate_mode"],
            "cancel_state": decision["cancel_state"],
            "effect_state": decision["effect_state"],
            "cause_family": decision["cause_family"],
            "delivery_state": decision["delivery_state"],
            "source_availability": deepcopy(snapshot["final_source"]),
            "evidence_availability": deepcopy(decision["evidence_availability"]),
            "effect_availability": {
                "availability": "available",
                "ref": deepcopy(snapshot["effect_ledger_ref"]),
            },
            "predicate_results": deepcopy(decision["predicate_results"]),
        }
        return snapshot, context

    def quality_policy_context(self, plan: dict[str, object]) -> list[dict[str, object]]:
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

    def build_uploaded_documents(self) -> dict[str, dict[str, object]]:
        envelope_fixture = self.fixture_document(
            "task_result_transport_crash_uploaded_replay"
        )
        fragment = self.fixture_document(
            "worker_debug_transport_fragment_golden_terminal"
        )
        file_manifest = self.fixture_document(
            "worker_debug_content_golden_file_manifest"
        )
        redaction_report = self.fixture_document(
            "worker_debug_content_golden_redaction_report"
        )
        task_result, terminal_gate_decision = bind_task_result_to_terminal_decision(
            self,
            envelope_fixture["task_result"],
        )
        task_result_core = deepcopy(task_result)
        task_result_core["schema_id"] = "task-result-core/v1"
        task_result_core["diagnostics"] = {}
        task_result_core_ref = self.content_ref(
            "art_" + "9" * 31 + "2", "task-result-core/v1", task_result_core
        )
        fragment["task_result_core"]["ref"] = deepcopy(task_result_core_ref)

        fragment_ref = self.content_ref(
            "art_" + "9" * 31 + "1",
            "worker-debug-fragment/v1",
            fragment,
        )
        server_fragment_ref = self.content_ref(
            "art_" + "9" * 31 + "7",
            "worker-debug-fragment/v1",
            fragment,
        )
        transport_receipt = self.reseal(
            "server-transport-receipt/v1",
            {
                "schema_id": "server-transport-receipt/v1",
                "receipt_kind": "server_transport",
                "package": deepcopy(envelope_fixture["package"]),
                "receipt_id": "receipt_" + "5" * 32,
                "task_id": envelope_fixture["authority"]["task_id"],
                "attempt_id": envelope_fixture["authority"]["attempt_id"],
                "session_id": envelope_fixture["authority"]["session_id"],
                "owner_id": envelope_fixture["authority"]["owner_id"],
                "lease_id": envelope_fixture["authority"]["lease_id"],
                "authority_digest": envelope_fixture["authority"][
                    "authority_digest"
                ],
                "task_version": envelope_fixture["authority"]["task_version"],
                "deletion_version": envelope_fixture["authority"][
                    "deletion_version"
                ],
                "owner_epoch": envelope_fixture["authority"]["owner_epoch"],
                "native_epoch": envelope_fixture["authority"]["native_epoch"],
                "transport_epoch": envelope_fixture["authority"][
                    "transport_epoch"
                ],
                "grant_digest": envelope_fixture["authority"]["grant"][
                    "grant_digest"
                ],
                "content_ref": deepcopy(fragment_ref),
                "accepted_at": "2026-07-22T00:01:05Z",
            },
        )
        transport_receipt_ref = self.content_ref(
            "art_" + "9" * 31 + "3",
            "server-transport-receipt/v1",
            transport_receipt,
        )
        worker_debug_descriptor = {
            "schema_id": "worker-debug-fragment-descriptor/v1",
            "state": "uploaded",
            "fragment_ref": deepcopy(fragment_ref),
            "sealed": True,
            "snapshot_seq": fragment["snapshot_seq"],
            "source_sha256": fragment_ref["sha256"],
            "transport_kind": "server_transport",
            "server_fragment_ref": deepcopy(server_fragment_ref),
            "server_receipt_ref": deepcopy(transport_receipt_ref),
            "reason_code": None,
        }
        worker_debug_descriptor_ref = self.content_ref(
            "art_" + "9" * 31 + "4",
            "worker-debug-fragment-descriptor/v1",
            worker_debug_descriptor,
        )
        task_result["diagnostics"] = {
            "worker_debug_fragment": {
                "availability": "available",
                "ref": deepcopy(worker_debug_descriptor_ref),
            }
        }
        transport_envelope = {
            "schema_id": "task-result-transport-envelope/v1",
            "package": deepcopy(envelope_fixture["package"]),
            "authority": deepcopy(envelope_fixture["authority"]),
            "full_fence": deepcopy(envelope_fixture["full_fence"]),
            "task_result": deepcopy(task_result),
            "task_result_digest": hashlib.sha256(
                canonical_bytes(task_result)
            ).hexdigest(),
            "task_result_core_ref": deepcopy(task_result_core_ref),
            "task_result_core_digest": hashlib.sha256(
                canonical_bytes(task_result_core)
            ).hexdigest(),
            "worker_debug_descriptor": deepcopy(worker_debug_descriptor),
            "transport_receipt": {
                "availability": "available",
                "ref": deepcopy(transport_receipt_ref),
            },
        }
        transport_ack = self.reseal(
            "task-result-transport-ack/v1",
            {
                "schema_id": "task-result-transport-ack/v1",
                "package": deepcopy(envelope_fixture["package"]),
                "result_id": task_result["result_id"],
                "task_id": task_result["task_id"],
                "outcome": task_result["outcome"],
                "published_from_version": task_result["published_from_version"],
                "terminal_task_version": task_result["terminal_task_version"],
                "transport_envelope_digest": hashlib.sha256(
                    canonical_bytes(transport_envelope)
                ).hexdigest(),
                "receipt_binding_state": "bound",
                "receipt_digest": transport_receipt["receipt_digest"],
                "accepted_at": "2026-07-22T00:01:10Z",
            },
        )
        return {
            "task_result": task_result,
            "task_result_core": task_result_core,
            "task_result_transport_envelope": transport_envelope,
            "task_result_transport_ack": transport_ack,
            "terminal_gate_decision": terminal_gate_decision,
            "transport_receipt": transport_receipt,
            "worker_debug_descriptor": worker_debug_descriptor,
            "worker_debug_fragment": fragment,
            "worker_debug_file_manifest": file_manifest,
            "worker_debug_redaction_report": redaction_report,
        }

    def task_control_probe_operations(self) -> dict[str, dict[str, object]]:
        request = self.fixture_document("task_control_golden_task_request")
        policy = self.fixture_document("task_control_golden_effective_policy")
        bounded = deepcopy(request)
        bounded["requested_budgets"]["wall_ms"] = policy["budgets"]["wall_ms"] - 1
        waiver = self.fixture_document("requirements_negative_waiver_empty_issuer_profile")
        ledger = self.fixture_document("requirements_golden_ledger")
        charter = self.fixture_document("requirements_golden_charter")
        entry = self.fixture_document(
            "requirements_negative_derived_mandatory_without_rationale"
        )
        valid_entry = deepcopy(entry)
        valid_entry["rationale"] = "Required to preserve the accepted objective."
        candidate_unsigned = deepcopy(ledger)
        candidate_unsigned.pop("ledger_digest")
        candidate_unsigned["ledger_version"] = 2
        candidate_unsigned["entries"].append(valid_entry)
        candidate_unsigned["active_requirement_ids"] = sorted(
            candidate_unsigned["active_requirement_ids"]
            + [valid_entry["requirement_id"]]
        )
        candidate = self.reseal("requirement-ledger/v1", candidate_unsigned)
        mutated = deepcopy(candidate)
        mutated.pop("ledger_digest")
        mutated["entries"][0]["statement"] = "Mutated history."
        mutated = self.reseal("requirement-ledger/v1", mutated)

        charter_v2 = deepcopy(charter)
        previous_bytes = canonical_bytes(charter)
        charter_v2.pop("digest")
        charter_v2["charter_version"] = 2
        charter_v2["previous_charter_ref"] = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + "9" * 32,
            "content_schema_id": "task-charter/v1",
            "sha256": hashlib.sha256(previous_bytes).hexdigest(),
            "size_bytes": len(previous_bytes),
            "media_type": "application/json",
            "encoding": "utf-8",
        }
        charter_v2["created_at"] = "2026-07-22T00:01:00.000Z"
        charter_v2 = self.reseal("task-charter/v1", charter_v2)
        bad_charter_v2 = deepcopy(charter_v2)
        bad_charter_v2["previous_charter_ref"]["sha256"] = "0" * 64
        bad_charter_v2 = self.reseal("task-charter/v1", bad_charter_v2)

        queued = self.fixture_document("task_control_golden_task_record")
        attempt = self.fixture_document("task_control_golden_attempt_record")
        owner = self.fixture_document("task_control_golden_task_owner")
        claimed = deepcopy(queued)
        claimed.update(
            lifecycle="ACTIVE",
            task_version=2,
            native_epoch=1,
            current_attempt_id=attempt["attempt_id"],
            owner_epoch=1,
            updated_at="2026-07-22T00:00:01.000Z",
        )
        invalid_attempt = deepcopy(attempt)
        invalid_attempt["state"] = "SUSPENDED"
        invalid_attempt["state_version"] = 2
        invalid_owner = deepcopy(owner)
        invalid_owner["state"] = "ACTIVE"
        invalid_owner["state_version"] = 2
        invalid_owner["session_id"] = "sess_" + "f" * 32
        version_jump = deepcopy(claimed)
        version_jump["task_version"] = 3
        orphan_owner = deepcopy(owner)
        orphan_owner["attempt_id"] = "attempt_" + "f" * 32

        finalizing = deepcopy(claimed)
        finalizing.update(
            lifecycle="FINALIZING",
            task_version=3,
            updated_at="2026-07-22T00:00:02.000Z",
        )
        result = {
            "schema_id": "task-result/v1",
            "task_id": finalizing["task_id"],
            "task_type": finalizing["task_type"],
            "outcome": "COMPLETED",
            "published_from_version": 3,
            "terminal_task_version": 4,
            "request_ref": finalizing["request_ref"],
            "policy_ref": finalizing["policy_ref"],
            "attempt_identity": {
                "kind": "started",
                "attempt_id": attempt["attempt_id"],
                "native_epoch": 1,
            },
            "owner_identity": {
                "kind": "started",
                "owner_id": finalizing["owner_id"],
                "owner_epoch": 1,
            },
            "terminal_at": "2026-07-22T00:01:00Z",
        }
        result_bytes = canonical_bytes(result)
        result_digest = hashlib.sha256(result_bytes).hexdigest()
        terminal = deepcopy(finalizing)
        terminal.update(
            lifecycle="TERMINAL",
            task_version=4,
            terminal_kind="task_result",
            result_digest=result_digest,
            result_ref={
                "schema_id": "content-ref/v1",
                "artifact_id": "art_" + "8" * 32,
                "content_schema_id": "task-result/v1",
                "sha256": result_digest,
                "size_bytes": len(result_bytes),
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            outcome="COMPLETED",
            updated_at="2026-07-22T00:01:00.000Z",
            terminal_at="2026-07-22T00:01:00.000Z",
        )
        wrong_result = deepcopy(result)
        wrong_result["task_id"] = "task_" + "f" * 32

        return {
            "validate_task_request_acceptance": self.helper_operation(
                "validate_task_request_acceptance",
                [request, "task_" + "2" * 32, None],
            ),
            "validate_effective_policy_derivation": self.helper_operation(
                "validate_effective_policy_derivation",
                [bounded, policy],
            ),
            "verify_waiver_event_authority": self.helper_operation(
                "verify_waiver_event_authority",
                [waiver, policy, "2026-07-22T02:00:00.000Z"],
            ),
            "validate_requirement_entry_ingest": self.helper_operation(
                "validate_requirement_entry_ingest",
                [entry, ledger],
            ),
            "validate_requirement_ledger_transition": self.helper_operation(
                "validate_requirement_ledger_transition",
                [ledger, mutated],
            ),
            "validate_task_charter_transition": self.helper_operation(
                "validate_task_charter_transition",
                [charter, bad_charter_v2, candidate],
            ),
            "validate_attempt_transition": self.helper_operation(
                "validate_attempt_transition",
                [attempt, invalid_attempt],
            ),
            "validate_task_owner_transition": self.helper_operation(
                "validate_task_owner_transition",
                [owner, invalid_owner],
            ),
            "validate_task_record_transition": self.helper_operation(
                "validate_task_record_transition",
                [queued, version_jump],
            ),
            "validate_claim_write_set": self.helper_operation(
                "validate_claim_write_set",
                [queued, claimed, attempt, orphan_owner],
            ),
            "validate_task_result_publication": self.helper_operation(
                "validate_task_result_publication",
                [finalizing, terminal, wrong_result],
            ),
        }

    def pre_gate_probe_operations(self) -> dict[str, dict[str, object]]:
        success = self.make_success_context()
        terminal = self.make_terminal_context()
        root = success[1]
        stale_root = deepcopy(success)
        stale_root[0]["request_ref"]["sha256"] = "0" * 64
        stale_root[0] = self.reseal("gate-input-snapshot/v1", stale_root[0])

        missing_root = deepcopy(success)
        direct_ref = missing_root[1]["request"]["ref"]
        missing_root[2]["entries"].remove(direct_ref)
        missing_root[2]["entry_count"] -= 1
        missing_root[2]["pre_gate_closure_digest"] = hashlib.sha256(
            canonical_bytes(missing_root[2]["entries"])
        ).hexdigest()
        missing_root[2] = self.reseal(
            "pre-gate-evidence-closure-manifest/v1", missing_root[2]
        )

        bad_fact = deepcopy(terminal)
        bad_fact[3][0]["observed_task_version"] = terminal[0]["task_version"]
        bad_fact[3][0]["idempotency_key"] = (
            f"terminalize:budget_exhausted:{terminal[0]['task_version']}"
        )
        bad_fact[3][0] = self.reseal("terminalization-fact/v1", bad_fact[3][0])

        return {
            "verify_pre_gate_root_set_context": self.helper_operation(
                "verify_pre_gate_root_set_context",
                [root, "task_" + "2" * 32, root["outcome_candidate"]],
            ),
            "verify_pre_gate_evidence_closure_context": self.helper_operation(
                "verify_pre_gate_evidence_closure_context",
                [missing_root[2], missing_root[1]],
            ),
            "verify_gate_input_snapshot_context": self.helper_operation(
                "verify_gate_input_snapshot_context",
                stale_root,
            ),
            "verify_terminalization_input_snapshot_context": self.helper_operation(
                "verify_terminalization_input_snapshot_context",
                bad_fact,
            ),
        }

    def gate_probe_operations(self) -> dict[str, dict[str, object]]:
        success_snapshot, success_context = self.gate_success_inputs()
        bad_success_context = deepcopy(success_context)
        bad_success_context["input_snapshot_ref"]["sha256"] = "0" * 64
        terminal_snapshot, terminal_context = self.gate_terminal_inputs()
        bad_terminal_context = deepcopy(terminal_context)
        bad_terminal_context["input_snapshot_ref"]["sha256"] = "0" * 64
        return {
            "evaluate_success_gate": self.helper_operation(
                "evaluate_success_gate",
                [success_snapshot, bad_success_context],
            ),
            "evaluate_terminalization_gate": self.helper_operation(
                "evaluate_terminalization_gate",
                [terminal_snapshot, bad_terminal_context],
            ),
        }

    def source_probe_operations(self) -> dict[str, dict[str, object]]:
        graph = self.build_graph()
        bad_selection = deepcopy(graph["selection_policy"])
        bad_selection["root_identity"] = "root_" + "2" * 32
        bad_selection = self.reseal("source-selection-policy/v1", bad_selection)
        bad_profile = deepcopy(graph["profile"])
        bad_profile["sandbox_identity"] = "bwrap:2.0"
        bad_profile = self.reseal("execution-profile/v1", bad_profile)
        bad_change = deepcopy(graph["change_set"])
        bad_change["modified"][0]["after"]["executable"] = True
        bad_change = self.reseal("change-set/v1", bad_change)
        bad_manifest = deepcopy(graph["final_manifest"])
        bad_manifest["proposal_id"] = "proposal_" + "2" * 32
        bad_manifest = self.reseal("observation-manifest/v1", bad_manifest)
        return {
            "verify_source_tree_context": self.helper_operation(
                "verify_source_tree_context",
                [graph["original_source"], bad_selection],
            ),
            "verify_execution_state_context": self.helper_operation(
                "verify_execution_state_context",
                [graph["execution_states"][0], graph["final_source"], bad_profile],
            ),
            "verify_change_set_context": self.helper_operation(
                "verify_change_set_context",
                [bad_change, graph["original_source"], graph["final_source"], graph["patch"]],
            ),
            "verify_observation_manifest_extension": self.helper_operation(
                "verify_observation_manifest_extension",
                [bad_manifest, graph["pre_manifest"]],
            ),
        }

    def verification_probe_operations(self) -> dict[str, dict[str, object]]:
        graph = self.build_graph()
        bad_owner = deepcopy(graph["owner"])
        bad_owner["owner_id"] = "owner_" + "f" * 32
        bad_plan = deepcopy(graph["plan"])
        bad_plan["proposal_digest"] = "0" * 64
        bad_plan = self.reseal("quality-policy-plan/v1", bad_plan)
        bad_final_manifest = deepcopy(graph["final_manifest"])
        bad_final_manifest["proposal_id"] = "proposal_" + "f" * 32
        bad_final_manifest = self.reseal("observation-manifest/v1", bad_final_manifest)
        bad_aggregate = deepcopy(graph["aggregate"])
        bad_aggregate["attestation_count"] = 0
        bad_aggregate = self.reseal(
            "verification-attestation-manifest/v1", bad_aggregate
        )
        return {
            "verify_completion_proposal_context": self.helper_operation(
                "verify_completion_proposal_context",
                [
                    graph["proposal"],
                    graph["task_snapshot"],
                    graph["attempt"],
                    bad_owner,
                    graph["request"],
                    graph["policy"],
                    graph["ledger"],
                    graph["charter"],
                    graph["original_source"],
                    graph["final_source"],
                    graph["execution_states"],
                    graph["change_set"],
                    graph["pre_manifest"],
                ],
            ),
            "verify_verifier_input_context": self.helper_operation(
                "verify_verifier_input_context",
                [
                    graph["input"],
                    graph["proposal"],
                    bad_plan,
                    graph["request"],
                    graph["policy"],
                    graph["ledger"],
                    graph["charter"],
                    graph["original_source"],
                    graph["final_source"],
                    graph["change_set"],
                    graph["pre_manifest"],
                    graph["engineering_rules"],
                ],
            ),
            "verify_verifier_work_context": self.helper_operation(
                "verify_verifier_work_context",
                [graph["work"], graph["input"], graph["proposal"], bad_final_manifest],
            ),
            "verify_attestation_context": self.helper_operation(
                "verify_attestation_context",
                [
                    graph["attestation"],
                    graph["input"],
                    graph["work"],
                    graph["proposal"],
                    graph["plan"],
                    graph["final_source"],
                    graph["execution_states"],
                    bad_final_manifest,
                ],
            ),
            "verify_attestation_manifest_context": self.helper_operation(
                "verify_attestation_manifest_context",
                [bad_aggregate, graph["plan"], graph["final_manifest"], [graph["attestation"]]],
            ),
        }

    def quality_policy_probe_operations(self) -> dict[str, dict[str, object]]:
        plan = self.fixture_document("quality_policy_golden_q2_plan")
        context = self.quality_policy_context(plan)
        binding_mismatch = deepcopy(context)
        binding_mismatch[1]["proposal_id"] = "proposal_" + "2" * 32
        return {
            "verify_quality_policy_plan_context": self.helper_operation(
                "verify_quality_policy_plan_context",
                binding_mismatch,
            )
        }

    def task_evidence_probe_operations(self) -> dict[str, dict[str, object]]:
        manifest = self.fixture_document("task_evidence_golden_manifest")
        pre_gate = self.fixture_document("pre_gate_golden_evidence_closure")
        bad_pre_gate = deepcopy(pre_gate)
        bad_pre_gate["task_id"] = "task_" + "2" * 32
        bad_pre_gate = self.reseal(
            "pre-gate-evidence-closure-manifest/v1", bad_pre_gate
        )
        return {
            "verify_evidence_closure_context": self.helper_operation(
                "verify_evidence_closure_context",
                [manifest, bad_pre_gate],
            )
        }

    def budget_probe_operations(self) -> dict[str, dict[str, object]]:
        before = self.fixture_document("budget_golden_ledger_before")
        reservation = self.fixture_document("budget_golden_reservation")
        reserved = self.fixture_document("budget_golden_ledger_reserved")
        settlement = self.fixture_document("budget_golden_settlement")
        after = self.fixture_document("budget_golden_ledger_after_settlement")
        bad_after = deepcopy(after)
        bad_after["consumed_ms"] += 1
        bad_after = self.reseal("elapsed-budget-ledger/v1", bad_after)
        return {
            "verify_budget_transition": self.helper_operation(
                "verify_budget_transition",
                [before, reservation, reserved, settlement, bad_after],
            )
        }

    def gate_preparation_probe_operations(self) -> dict[str, dict[str, object]]:
        fact = self.fixture_document("gate_preparation_golden_terminalization_fact")
        return {
            "verify_terminalization_fact_context": self.helper_operation(
                "verify_terminalization_fact_context",
                [
                    fact,
                    "task_" + "2" * 32,
                    7,
                    "FINALIZING",
                    None,
                ],
            )
        }

    def result_debug_probe_operations(self) -> dict[str, dict[str, object]]:
        documents = self.build_uploaded_documents()
        task_result = documents["task_result"]
        task_result_core = documents["task_result_core"]
        transport_envelope = documents["task_result_transport_envelope"]
        transport_ack = documents["task_result_transport_ack"]
        transport_receipt = documents["transport_receipt"]
        worker_debug_descriptor = documents["worker_debug_descriptor"]
        worker_debug_fragment = documents["worker_debug_fragment"]
        worker_debug_file_manifest = documents["worker_debug_file_manifest"]
        worker_debug_redaction_report = documents["worker_debug_redaction_report"]

        invalid_core = deepcopy(task_result_core)
        invalid_core["summary"] = "Different but schema-valid core summary."
        invalid_task_result = deepcopy(task_result)
        invalid_task_result["diagnostics"]["worker_debug_fragment"]["ref"][
            "sha256"
        ] = "0" * 64
        invalid_envelope = deepcopy(transport_envelope)
        invalid_envelope["task_result_core_digest"] = "0" * 64
        invalid_ack = deepcopy(transport_ack)
        invalid_ack["receipt_digest"] = "0" * 64
        invalid_ack = self.reseal("task-result-transport-ack/v1", invalid_ack)
        invalid_descriptor = deepcopy(worker_debug_descriptor)
        invalid_descriptor["fragment_ref"]["sha256"] = "0" * 64
        invalid_descriptor["server_fragment_ref"]["sha256"] = "0" * 64
        invalid_descriptor["source_sha256"] = "0" * 64
        invalid_fragment = deepcopy(worker_debug_fragment)
        invalid_fragment["last_server_acked_event_seq"] = (
            invalid_fragment["local_event_seq"] + 1
        )

        return {
            "verify_task_result_core": self.helper_operation(
                "verify_task_result_core",
                [task_result, invalid_core],
            ),
            "verify_task_result_context": self.helper_operation(
                "verify_task_result_context",
                [invalid_task_result],
                kwargs={
                    "terminal_gate_decision": documents["terminal_gate_decision"],
                    "worker_debug_descriptor": worker_debug_descriptor,
                },
            ),
            "verify_task_result_transport_envelope": self.helper_operation(
                "verify_task_result_transport_envelope",
                [invalid_envelope, task_result_core],
                kwargs={
                    "transport_receipt": transport_receipt,
                    "worker_debug_descriptor": worker_debug_descriptor,
                },
            ),
            "verify_task_result_transport_ack": self.helper_operation(
                "verify_task_result_transport_ack",
                [invalid_ack, transport_envelope],
                kwargs={"transport_receipt": transport_receipt},
            ),
            "verify_worker_debug_descriptor_content": self.helper_operation(
                "verify_worker_debug_descriptor_content",
                [invalid_descriptor, worker_debug_fragment],
                kwargs={"transport_receipt": transport_receipt},
            ),
            "verify_worker_debug_fragment_content": self.helper_operation(
                "verify_worker_debug_fragment_content",
                [
                    invalid_fragment,
                    task_result_core,
                    worker_debug_file_manifest,
                    worker_debug_redaction_report,
                ],
            ),
        }

    def tool_probe_operations(self) -> dict[str, dict[str, object]]:
        request = self.synthetic_agent_tool_request()
        catalog = self.fixture_document("tool_golden_current_catalog")
        invocation = self.fixture_document("tool_golden_invocation")
        invalid_invocation = deepcopy(invocation)
        invalid_invocation["idempotency_key"] = "invoke:other"
        invalid_invocation = self.reseal("tool-invocation/v1", invalid_invocation)
        intent = self.fixture_document("tool_crash_after_intent")
        capability = self.fixture_document("tool_golden_dispatch_capability")
        mismatched_intent = deepcopy(intent)
        mismatched_intent["idempotency_key"] = "invoke:other"
        mismatched_intent = self.reseal(
            "tool-dispatch-intent/v1", mismatched_intent
        )
        wrong_receipt = self.fixture_document("tool_golden_local_receipt")
        wrong_receipt["elapsed_ms"] += 1
        wrong_receipt = self.reseal("local-tool-receipt/v1", wrong_receipt)
        source = self.synthetic_source_state()
        changed = self.synthetic_source_state("9")
        return {
            "validate_tool_invocation_binding": self.helper_operation(
                "validate_tool_invocation_binding",
                [request, invalid_invocation, catalog],
            ),
            "validate_tool_journal_begin": self.helper_operation(
                "validate_tool_journal_begin",
                [invocation, mismatched_intent, capability],
            ),
            "validate_tool_capability_consumption": self.helper_operation(
                "validate_tool_capability_consumption",
                [intent, capability, [capability["capability_digest"]]],
            ),
            "validate_tool_journal_settlement": self.helper_operation(
                "validate_tool_journal_settlement",
                [
                    invocation,
                    intent,
                    wrong_receipt,
                    self.fixture_document("tool_golden_r0_payload"),
                    self.fixture_document("tool_golden_r0_result"),
                    source,
                    changed,
                ],
            ),
        }

    def positive_helper_operations(self) -> dict[str, dict[str, object]]:
        success_snapshot, success_context = self.gate_success_inputs()
        terminal_snapshot, terminal_context = self.gate_terminal_inputs()

        request = self.fixture_document("task_control_golden_task_request")
        policy = self.fixture_document("task_control_golden_effective_policy")
        waiver = self.fixture_document("requirements_negative_waiver_empty_issuer_profile")
        ledger = self.fixture_document("requirements_golden_ledger")
        charter = self.fixture_document("requirements_golden_charter")
        valid_entry = self.fixture_document(
            "requirements_negative_derived_mandatory_without_rationale"
        )
        valid_entry["rationale"] = "Required to preserve the accepted objective."
        candidate_unsigned = deepcopy(ledger)
        candidate_unsigned.pop("ledger_digest")
        candidate_unsigned["ledger_version"] = 2
        candidate_unsigned["entries"].append(valid_entry)
        candidate_unsigned["active_requirement_ids"] = sorted(
            candidate_unsigned["active_requirement_ids"]
            + [valid_entry["requirement_id"]]
        )
        candidate = self.reseal("requirement-ledger/v1", candidate_unsigned)
        charter_v2 = deepcopy(charter)
        previous_bytes = canonical_bytes(charter)
        charter_v2.pop("digest")
        charter_v2["charter_version"] = 2
        charter_v2["previous_charter_ref"] = {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + "9" * 32,
            "content_schema_id": "task-charter/v1",
            "sha256": hashlib.sha256(previous_bytes).hexdigest(),
            "size_bytes": len(previous_bytes),
            "media_type": "application/json",
            "encoding": "utf-8",
        }
        charter_v2["created_at"] = "2026-07-22T00:01:00.000Z"
        charter_v2 = self.reseal("task-charter/v1", charter_v2)
        queued = self.fixture_document("task_control_golden_task_record")
        attempt = self.fixture_document("task_control_golden_attempt_record")
        owner = self.fixture_document("task_control_golden_task_owner")
        claimed = deepcopy(queued)
        claimed.update(
            lifecycle="ACTIVE",
            task_version=2,
            native_epoch=1,
            current_attempt_id=attempt["attempt_id"],
            owner_epoch=1,
            updated_at="2026-07-22T00:00:01.000Z",
        )
        preparing = deepcopy(attempt)
        preparing.update(state="PREPARING", state_version=2)
        active_owner = deepcopy(owner)
        active_owner.update(state="ACTIVE", state_version=2)
        finalizing = deepcopy(claimed)
        finalizing.update(
            lifecycle="FINALIZING",
            task_version=3,
            updated_at="2026-07-22T00:00:02.000Z",
        )
        result = {
            "schema_id": "task-result/v1",
            "task_id": finalizing["task_id"],
            "task_type": finalizing["task_type"],
            "outcome": "COMPLETED",
            "published_from_version": 3,
            "terminal_task_version": 4,
            "request_ref": finalizing["request_ref"],
            "policy_ref": finalizing["policy_ref"],
            "attempt_identity": {
                "kind": "started",
                "attempt_id": attempt["attempt_id"],
                "native_epoch": 1,
            },
            "owner_identity": {
                "kind": "started",
                "owner_id": finalizing["owner_id"],
                "owner_epoch": 1,
            },
            "terminal_at": "2026-07-22T00:01:00Z",
        }
        result_bytes = canonical_bytes(result)
        result_digest = hashlib.sha256(result_bytes).hexdigest()
        terminal = deepcopy(finalizing)
        terminal.update(
            lifecycle="TERMINAL",
            task_version=4,
            terminal_kind="task_result",
            result_digest=result_digest,
            result_ref={
                "schema_id": "content-ref/v1",
                "artifact_id": "art_" + "8" * 32,
                "content_schema_id": "task-result/v1",
                "sha256": result_digest,
                "size_bytes": len(result_bytes),
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            outcome="COMPLETED",
            updated_at="2026-07-22T00:01:00.000Z",
            terminal_at="2026-07-22T00:01:00.000Z",
        )

        pre_gate_success = self.make_success_context()
        pre_gate_terminal = self.make_terminal_context()
        root = pre_gate_success[1]

        graph = self.build_graph()
        plan = self.fixture_document("quality_policy_golden_q2_plan")
        quality_context = self.quality_policy_context(plan)
        evidence_manifest = self.fixture_document("task_evidence_golden_manifest")
        pre_gate_manifest = self.fixture_document("pre_gate_golden_evidence_closure")
        budget_before = self.fixture_document("budget_golden_ledger_before")
        budget_reservation = self.fixture_document("budget_golden_reservation")
        budget_reserved = self.fixture_document("budget_golden_ledger_reserved")
        budget_settlement = self.fixture_document("budget_golden_settlement")
        budget_after = self.fixture_document("budget_golden_ledger_after_settlement")
        fact = self.fixture_document("gate_preparation_golden_terminalization_fact")
        uploaded = self.build_uploaded_documents()
        tool_invocation = self.fixture_document("tool_golden_invocation")
        tool_intent = self.fixture_document("tool_crash_after_intent")
        tool_capability = self.fixture_document("tool_golden_dispatch_capability")
        tool_request = self.synthetic_agent_tool_request()
        tool_catalog = self.fixture_document("tool_golden_current_catalog")
        tool_source = self.synthetic_source_state()

        return {
            "evaluate_success_gate": self.helper_operation(
                "evaluate_success_gate", [success_snapshot, success_context]
            ),
            "evaluate_terminalization_gate": self.helper_operation(
                "evaluate_terminalization_gate",
                [terminal_snapshot, terminal_context],
            ),
            "validate_attempt_transition": self.helper_operation(
                "validate_attempt_transition", [attempt, preparing]
            ),
            "validate_claim_write_set": self.helper_operation(
                "validate_claim_write_set", [queued, claimed, attempt, owner]
            ),
            "validate_effective_policy_derivation": self.helper_operation(
                "validate_effective_policy_derivation", [request, policy]
            ),
            "validate_requirement_entry_ingest": self.helper_operation(
                "validate_requirement_entry_ingest", [valid_entry, ledger]
            ),
            "validate_requirement_ledger_transition": self.helper_operation(
                "validate_requirement_ledger_transition", [ledger, candidate]
            ),
            "validate_task_charter_transition": self.helper_operation(
                "validate_task_charter_transition", [charter, charter_v2, candidate]
            ),
            "validate_task_owner_transition": self.helper_operation(
                "validate_task_owner_transition", [owner, active_owner]
            ),
            "validate_task_record_transition": self.helper_operation(
                "validate_task_record_transition", [queued, claimed]
            ),
            "validate_task_request_acceptance": self.helper_operation(
                "validate_task_request_acceptance", [request]
            ),
            "validate_task_result_publication": self.helper_operation(
                "validate_task_result_publication", [finalizing, terminal, result]
            ),
            "validate_tool_capability_consumption": self.helper_operation(
                "validate_tool_capability_consumption", [tool_intent, tool_capability, []]
            ),
            "validate_tool_invocation_binding": self.helper_operation(
                "validate_tool_invocation_binding",
                [tool_request, tool_invocation, tool_catalog],
            ),
            "validate_tool_journal_begin": self.helper_operation(
                "validate_tool_journal_begin",
                [tool_invocation, tool_intent, tool_capability],
            ),
            "validate_tool_journal_settlement": self.helper_operation(
                "validate_tool_journal_settlement",
                [
                    tool_invocation,
                    tool_intent,
                    self.fixture_document("tool_golden_local_receipt"),
                    self.fixture_document("tool_golden_r0_payload"),
                    self.fixture_document("tool_golden_r0_result"),
                    tool_source,
                    tool_source,
                ],
            ),
            "verify_attestation_context": self.helper_operation(
                "verify_attestation_context",
                [
                    graph["attestation"],
                    graph["input"],
                    graph["work"],
                    graph["proposal"],
                    graph["plan"],
                    graph["final_source"],
                    graph["execution_states"],
                    graph["final_manifest"],
                ],
            ),
            "verify_attestation_manifest_context": self.helper_operation(
                "verify_attestation_manifest_context",
                [
                    graph["aggregate"],
                    graph["plan"],
                    graph["final_manifest"],
                    [graph["attestation"]],
                ],
            ),
            "verify_budget_transition": self.helper_operation(
                "verify_budget_transition",
                [
                    budget_before,
                    budget_reservation,
                    budget_reserved,
                    budget_settlement,
                    budget_after,
                ],
            ),
            "verify_change_set_context": self.helper_operation(
                "verify_change_set_context",
                [graph["change_set"], graph["original_source"], graph["final_source"], graph["patch"]],
            ),
            "verify_completion_proposal_context": self.helper_operation(
                "verify_completion_proposal_context",
                [
                    graph["proposal"],
                    graph["task_snapshot"],
                    graph["attempt"],
                    graph["owner"],
                    graph["request"],
                    graph["policy"],
                    graph["ledger"],
                    graph["charter"],
                    graph["original_source"],
                    graph["final_source"],
                    graph["execution_states"],
                    graph["change_set"],
                    graph["pre_manifest"],
                ],
            ),
            "verify_evidence_closure_context": self.helper_operation(
                "verify_evidence_closure_context", [evidence_manifest, pre_gate_manifest]
            ),
            "verify_execution_state_context": self.helper_operation(
                "verify_execution_state_context",
                [graph["execution_states"][0], graph["final_source"], graph["profile"]],
            ),
            "verify_gate_input_snapshot_context": self.helper_operation(
                "verify_gate_input_snapshot_context", pre_gate_success
            ),
            "verify_observation_manifest_extension": self.helper_operation(
                "verify_observation_manifest_extension",
                [graph["final_manifest"], graph["pre_manifest"]],
            ),
            "verify_pre_gate_evidence_closure_context": self.helper_operation(
                "verify_pre_gate_evidence_closure_context",
                [pre_gate_success[2], pre_gate_success[1]],
            ),
            "verify_pre_gate_root_set_context": self.helper_operation(
                "verify_pre_gate_root_set_context",
                [root, root["task_id"], root["outcome_candidate"]],
            ),
            "verify_quality_policy_plan_context": self.helper_operation(
                "verify_quality_policy_plan_context", quality_context
            ),
            "verify_source_tree_context": self.helper_operation(
                "verify_source_tree_context",
                [graph["original_source"], graph["selection_policy"]],
            ),
            "verify_task_result_context": self.helper_operation(
                "verify_task_result_context",
                [uploaded["task_result"]],
                kwargs={
                    "terminal_gate_decision": uploaded["terminal_gate_decision"],
                    "worker_debug_descriptor": uploaded["worker_debug_descriptor"],
                },
            ),
            "verify_task_result_core": self.helper_operation(
                "verify_task_result_core",
                [uploaded["task_result"], uploaded["task_result_core"]],
            ),
            "verify_task_result_transport_ack": self.helper_operation(
                "verify_task_result_transport_ack",
                [uploaded["task_result_transport_ack"], uploaded["task_result_transport_envelope"]],
                kwargs={"transport_receipt": uploaded["transport_receipt"]},
            ),
            "verify_task_result_transport_envelope": self.helper_operation(
                "verify_task_result_transport_envelope",
                [uploaded["task_result_transport_envelope"], uploaded["task_result_core"]],
                kwargs={
                    "transport_receipt": uploaded["transport_receipt"],
                    "worker_debug_descriptor": uploaded["worker_debug_descriptor"],
                },
            ),
            "verify_terminalization_fact_context": self.helper_operation(
                "verify_terminalization_fact_context",
                [fact, "task_11111111111111111111111111111111", 7, "FINALIZING", fact],
            ),
            "verify_terminalization_input_snapshot_context": self.helper_operation(
                "verify_terminalization_input_snapshot_context", pre_gate_terminal
            ),
            "verify_verifier_input_context": self.helper_operation(
                "verify_verifier_input_context",
                [
                    graph["input"],
                    graph["proposal"],
                    graph["plan"],
                    graph["request"],
                    graph["policy"],
                    graph["ledger"],
                    graph["charter"],
                    graph["original_source"],
                    graph["final_source"],
                    graph["change_set"],
                    graph["pre_manifest"],
                    graph["engineering_rules"],
                ],
            ),
            "verify_verifier_work_context": self.helper_operation(
                "verify_verifier_work_context",
                [graph["work"], graph["input"], graph["proposal"], graph["final_manifest"]],
            ),
            "verify_waiver_event_authority": self.helper_operation(
                "verify_waiver_event_authority", [waiver, policy, "2026-07-22T00:30:00.000Z"]
            ),
            "verify_worker_debug_descriptor_content": self.helper_operation(
                "verify_worker_debug_descriptor_content",
                [uploaded["worker_debug_descriptor"], uploaded["worker_debug_fragment"]],
                kwargs={"transport_receipt": uploaded["transport_receipt"]},
            ),
            "verify_worker_debug_fragment_content": self.helper_operation(
                "verify_worker_debug_fragment_content",
                [
                    uploaded["worker_debug_fragment"],
                    uploaded["task_result_core"],
                    uploaded["worker_debug_file_manifest"],
                    uploaded["worker_debug_redaction_report"],
                ],
            ),
        }

    def helper_probe_operations(self) -> dict[str, dict[str, object]]:
        operations: dict[str, dict[str, object]] = {}
        for group in (
            self.budget_probe_operations(),
            self.source_probe_operations(),
            self.task_control_probe_operations(),
            self.pre_gate_probe_operations(),
            self.gate_probe_operations(),
            self.quality_policy_probe_operations(),
            self.task_evidence_probe_operations(),
            self.verification_probe_operations(),
            self.result_debug_probe_operations(),
            self.tool_probe_operations(),
            self.gate_preparation_probe_operations(),
        ):
            overlap = set(operations).intersection(group)
            if overlap:
                raise AssertionError(f"duplicate helper probes: {sorted(overlap)}")
            operations.update(group)
        return operations

