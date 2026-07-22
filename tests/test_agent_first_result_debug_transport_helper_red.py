from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.agent_first_result_debug_transport_facade_support import (
    ResultDebugTransportFacadeHarness,
    canonical_bytes,
)


HELPER_ALIASES = {
    "verify_task_result_context": "verifyTaskResultContext",
    "verify_task_result_core": "verifyTaskResultCore",
    "verify_task_result_transport_ack": "verifyTaskResultTransportAck",
    "verify_task_result_transport_envelope": "verifyTaskResultTransportEnvelope",
    "verify_worker_debug_descriptor_content": "verifyWorkerDebugDescriptorContent",
    "verify_worker_debug_fragment_content": "verifyWorkerDebugFragmentContent",
    "derive_task_result_core": "deriveTaskResultCore",
}


class AgentFirstResultDebugTransportHelperRedTest(
    ResultDebugTransportFacadeHarness, unittest.TestCase
):
    @staticmethod
    def normalize(value: object) -> object:
        if isinstance(value, bytes):
            return {"__bytes_hex__": value.hex()}
        if isinstance(value, list):
            return [AgentFirstResultDebugTransportHelperRedTest.normalize(item) for item in value]
        if isinstance(value, dict):
            return {
                key: AgentFirstResultDebugTransportHelperRedTest.normalize(item)
                for key, item in value.items()
            }
        return value

    def content_ref(self, artifact_id: str, schema_id: str, document: dict[str, object]) -> dict[str, object]:
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

    def derive_core_expected(self, task_result: dict[str, object]) -> dict[str, object]:
        core = deepcopy(task_result)
        core["schema_id"] = "task-result-core/v1"
        core["diagnostics"] = {}
        return core

    def build_uploaded_documents(self) -> dict[str, dict[str, object]]:
        envelope_fixture = self.fixture_document("task_result_transport_crash_uploaded_replay")
        fragment = self.fixture_document("worker_debug_transport_fragment_golden_terminal")
        file_manifest = self.fixture_document("worker_debug_content_golden_file_manifest")
        redaction_report = self.fixture_document("worker_debug_content_golden_redaction_report")

        fragment_ref = self.content_ref(
            "art_99999999999999999999999999999991",
            "worker-debug-fragment/v1",
            fragment,
        )
        server_fragment_ref = self.content_ref(
            "art_99999999999999999999999999999997",
            "worker-debug-fragment/v1",
            fragment,
        )
        transport_receipt = self.python.seal_document(
            "server-transport-receipt/v1",
            {
                "schema_id": "server-transport-receipt/v1",
                "receipt_kind": "server_transport",
                "package": deepcopy(envelope_fixture["package"]),
                "receipt_id": "receipt_55555555555555555555555555555555",
                "task_id": envelope_fixture["authority"]["task_id"],
                "attempt_id": envelope_fixture["authority"]["attempt_id"],
                "session_id": envelope_fixture["authority"]["session_id"],
                "owner_id": envelope_fixture["authority"]["owner_id"],
                "lease_id": envelope_fixture["authority"]["lease_id"],
                "authority_digest": envelope_fixture["authority"]["authority_digest"],
                "task_version": envelope_fixture["authority"]["task_version"],
                "deletion_version": envelope_fixture["authority"]["deletion_version"],
                "owner_epoch": envelope_fixture["authority"]["owner_epoch"],
                "native_epoch": envelope_fixture["authority"]["native_epoch"],
                "transport_epoch": envelope_fixture["authority"]["transport_epoch"],
                "grant_digest": envelope_fixture["authority"]["grant"]["grant_digest"],
                "content_ref": deepcopy(fragment_ref),
                "accepted_at": "2026-07-22T00:01:05Z",
            },
        )
        transport_receipt_ref = self.content_ref(
            "art_99999999999999999999999999999993",
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
            "art_99999999999999999999999999999994",
            "worker-debug-fragment-descriptor/v1",
            worker_debug_descriptor,
        )
        task_result = deepcopy(envelope_fixture["task_result"])
        task_result["diagnostics"] = {
            "worker_debug_fragment": {
                "availability": "available",
                "ref": deepcopy(worker_debug_descriptor_ref),
            }
        }
        task_result_core = self.derive_core_expected(task_result)
        task_result_core_ref = self.content_ref(
            "art_99999999999999999999999999999992",
            "task-result-core/v1",
            task_result_core,
        )
        transport_envelope = {
            "schema_id": "task-result-transport-envelope/v1",
            "package": deepcopy(envelope_fixture["package"]),
            "authority": deepcopy(envelope_fixture["authority"]),
            "full_fence": deepcopy(envelope_fixture["full_fence"]),
            "task_result": deepcopy(task_result),
            "task_result_digest": hashlib.sha256(canonical_bytes(task_result)).hexdigest(),
            "task_result_core_ref": deepcopy(task_result_core_ref),
            "task_result_core_digest": hashlib.sha256(canonical_bytes(task_result_core)).hexdigest(),
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
                "transport_envelope_digest": hashlib.sha256(canonical_bytes(transport_envelope)).hexdigest(),
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
            "transport_receipt": transport_receipt,
            "worker_debug_descriptor": worker_debug_descriptor,
            "worker_debug_fragment": fragment,
            "worker_debug_file_manifest": file_manifest,
            "worker_debug_redaction_report": redaction_report,
        }

    def python_helper_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for operation in operations:
            helper = operation["python"]
            args = operation.get("args", [])
            kwargs = operation.get("kwargs", {})
            if not hasattr(self.python, helper):
                results.append({"ok": False, "code": "HELPER_EXPORT_MISSING", "detail": helper, "path": "$"})
                continue
            try:
                value = getattr(self.python, helper)(*args, **kwargs)
            except self.python.ContractValidationError as error:
                results.append({"ok": False, "code": error.code, "detail": error.detail, "path": error.path})
            except Exception as error:
                results.append({"ok": False, "code": type(error).__name__, "detail": str(error), "path": "$"})
            else:
                results.append({"ok": True, "value": self.normalize(value)})
        return results

    def node_helper_results(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="result-debug-transport-helpers-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        "function normalize(value) {",
                        "  if (value instanceof Uint8Array) return {__bytes_hex__: Buffer.from(value).toString('hex')};",
                        "  if (Array.isArray(value)) return value.map(normalize);",
                        "  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalize(item)]));",
                        "  return value;",
                        "}",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  const helper = facade[operation.node] ?? facade[operation.python];",
                        "  if (typeof helper !== 'function') {",
                        "    results.push({ok: false, code: 'HELPER_EXPORT_MISSING', detail: operation.python, path: '$'});",
                        "    continue;",
                        "  }",
                        "  try {",
                        "    const value = await helper(...(operation.args ?? []), ...(operation.kwargs ? [operation.kwargs] : []));",
                        "    results.push({ok: true, value: normalize(value)});",
                        "  } catch (error) {",
                        "    results.push({",
                        "      ok: false,",
                        "      code: error.code ?? error.name,",
                        "      detail: error.detail ?? String(error.message ?? error),",
                        "      path: error.path ?? '$',",
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
        return json.loads(completed.stdout)

    def assert_helper_parity(self, operations: list[dict[str, object]]) -> list[dict[str, object]]:
        python = self.python_helper_results(operations)
        node = self.node_helper_results(operations)
        self.assertEqual(python, node)
        return python

    def test_synthesized_documents_are_internally_consistent(self) -> None:
        documents = self.build_uploaded_documents()
        task_result = documents["task_result"]
        transport_ack = documents["transport_ack"]
        task_result_core = documents["task_result_core"]
        transport_envelope = documents["task_result_transport_envelope"]
        transport_ack = documents["task_result_transport_ack"]
        transport_receipt = documents["transport_receipt"]
        worker_debug_descriptor = documents["worker_debug_descriptor"]
        worker_debug_fragment = documents["worker_debug_fragment"]
        worker_debug_file_manifest = documents["worker_debug_file_manifest"]
        worker_debug_redaction_report = documents["worker_debug_redaction_report"]

        descriptor_ref = self.content_ref(
            "art_99999999999999999999999999999994",
            "worker-debug-fragment-descriptor/v1",
            worker_debug_descriptor,
        )
        receipt_ref = self.content_ref(
            "art_99999999999999999999999999999993",
            "server-transport-receipt/v1",
            transport_receipt,
        )
        core_ref = self.content_ref(
            "art_99999999999999999999999999999992",
            "task-result-core/v1",
            task_result_core,
        )
        self.assertEqual(self.derive_core_expected(task_result), task_result_core)
        self.assertEqual(task_result["diagnostics"]["worker_debug_fragment"]["ref"], descriptor_ref)
        self.assertEqual(worker_debug_descriptor["server_receipt_ref"], receipt_ref)
        self.assertEqual(transport_envelope["transport_receipt"]["ref"], receipt_ref)
        self.assertEqual(transport_envelope["task_result_core_ref"], core_ref)
        self.assertEqual(
            transport_envelope["task_result_digest"],
            hashlib.sha256(canonical_bytes(task_result)).hexdigest(),
        )
        self.assertEqual(
            transport_envelope["task_result_core_digest"],
            hashlib.sha256(canonical_bytes(task_result_core)).hexdigest(),
        )
        self.assertEqual(
            transport_ack["transport_envelope_digest"],
            hashlib.sha256(canonical_bytes(transport_envelope)).hexdigest(),
        )
        self.assertEqual(transport_ack["receipt_digest"], transport_receipt["receipt_digest"])
        self.assertEqual(worker_debug_fragment["task_result_core"]["ref"]["sha256"], core_ref["sha256"])
        self.assertEqual(
            worker_debug_fragment["file_manifest_ref"]["sha256"],
            self.content_ref(
                "art_99999999999999999999999999999996",
                "worker-debug-file-manifest/v1",
                worker_debug_file_manifest,
            )["sha256"],
        )
        self.assertEqual(
            worker_debug_fragment["redaction_report_ref"]["sha256"],
            self.content_ref(
                "art_99999999999999999999999999999995",
                "worker-debug-redaction-report/v1",
                worker_debug_redaction_report,
            )["sha256"],
        )

    def test_helper_exports_are_present_in_python_and_node(self) -> None:
        self.assertEqual(
            {name: {"present": True, "exported": True} for name in HELPER_ALIASES},
            self.python_exports(list(HELPER_ALIASES)),
        )
        self.assertEqual(
            {name: {"snake": True, "camel": True, "same": True} for name in HELPER_ALIASES},
            self.node_exports(HELPER_ALIASES),
        )

    def test_positive_helper_bindings_have_exact_parity(self) -> None:
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

        operations = [
            {"python": "derive_task_result_core", "node": "deriveTaskResultCore", "args": [task_result]},
            {"python": "verify_task_result_core", "node": "verifyTaskResultCore", "args": [task_result, task_result_core]},
            {"python": "verify_task_result_context", "node": "verifyTaskResultContext", "args": [task_result], "kwargs": {"worker_debug_descriptor": worker_debug_descriptor}},
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [transport_envelope, task_result_core], "kwargs": {"transport_receipt": transport_receipt, "worker_debug_descriptor": worker_debug_descriptor}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [transport_ack, transport_envelope], "kwargs": {"transport_receipt": transport_receipt}},
            {"python": "verify_worker_debug_descriptor_content", "node": "verifyWorkerDebugDescriptorContent", "args": [worker_debug_descriptor, worker_debug_fragment], "kwargs": {"transport_receipt": transport_receipt}},
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [worker_debug_fragment, task_result_core, worker_debug_file_manifest, worker_debug_redaction_report]},
        ]
        self.assertEqual(
            [
                {"ok": True, "value": task_result_core},
                {"ok": True, "value": task_result_core},
                {"ok": True, "value": task_result},
                {
                    "ok": True,
                    "value": {
                        "document": transport_envelope,
                        "canonical_bytes": {"__bytes_hex__": canonical_bytes(transport_envelope).hex()},
                        "transport_envelope_digest": hashlib.sha256(canonical_bytes(transport_envelope)).hexdigest(),
                    },
                },
                {"ok": True, "value": transport_ack},
                {"ok": True, "value": worker_debug_descriptor},
                {"ok": True, "value": worker_debug_fragment},
            ],
            self.assert_helper_parity(operations),
        )

    def test_negative_helper_bindings_have_exact_parity(self) -> None:
        documents = self.build_uploaded_documents()
        task_result = documents["task_result"]
        task_result_core = documents["task_result_core"]
        transport_envelope = documents["task_result_transport_envelope"]
        transport_receipt = documents["transport_receipt"]
        worker_debug_descriptor = documents["worker_debug_descriptor"]
        worker_debug_fragment = documents["worker_debug_fragment"]
        worker_debug_file_manifest = documents["worker_debug_file_manifest"]
        worker_debug_redaction_report = documents["worker_debug_redaction_report"]

        invalid_core = deepcopy(task_result_core)
        invalid_core["summary"] = "Different but schema-valid core summary."
        invalid_task_result = deepcopy(task_result)
        invalid_task_result["diagnostics"]["worker_debug_fragment"]["ref"]["sha256"] = "0" * 64
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
        invalid_fragment["last_server_acked_event_seq"] = invalid_fragment["local_event_seq"] + 1

        operations = [
            {"python": "verify_task_result_core", "node": "verifyTaskResultCore", "args": [task_result, invalid_core]},
            {"python": "verify_task_result_context", "node": "verifyTaskResultContext", "args": [invalid_task_result], "kwargs": {"worker_debug_descriptor": worker_debug_descriptor}},
            {"python": "verify_task_result_transport_envelope", "node": "verifyTaskResultTransportEnvelope", "args": [invalid_envelope, task_result_core], "kwargs": {"transport_receipt": transport_receipt, "worker_debug_descriptor": worker_debug_descriptor}},
            {"python": "verify_task_result_transport_ack", "node": "verifyTaskResultTransportAck", "args": [invalid_ack, transport_envelope], "kwargs": {"transport_receipt": transport_receipt}},
            {"python": "verify_worker_debug_descriptor_content", "node": "verifyWorkerDebugDescriptorContent", "args": [invalid_descriptor, worker_debug_fragment], "kwargs": {"transport_receipt": transport_receipt}},
            {"python": "verify_worker_debug_fragment_content", "node": "verifyWorkerDebugFragmentContent", "args": [invalid_fragment, task_result_core, worker_debug_file_manifest, worker_debug_redaction_report]},
        ]
        self.assertEqual(
            [
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "TASK_RESULT_CORE_PROJECTION_INVALID", "path": "$"},
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "TASK_RESULT_CONTEXT_INVALID", "path": "$.diagnostics.worker_debug_fragment.ref"},
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "TRANSPORT_CORE_DIGEST_INVALID", "path": "$"},
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "TRANSPORT_ACK_RECEIPT_MATRIX_INVALID", "path": "$.receipt_binding_state"},
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "CAS_CORRUPT", "path": "$.fragment_ref"},
                {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "DEBUG_EVENT_SEQUENCE_INVALID", "path": "$"},
            ],
            self.assert_helper_parity(operations),
        )


if __name__ == "__main__":
    unittest.main()

