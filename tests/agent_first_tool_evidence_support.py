from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    canonical_bytes,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/tool-evidence.json"
)
RECEIPT_FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/receipt-error.json"
)
SCHEMA_IDS = (
    "agent-tool-request/v1",
    "local-tool-receipt/v1",
    "r0-read-payload/v1",
    "r0-read-result/v1",
    "source-content/v1",
    "source-state/v1",
    "tool-catalog/v1",
    "tool-dispatch-capability/v1",
    "tool-dispatch-intent/v1",
    "tool-invocation/v1",
)
SEMANTICS = {
    "agent-tool-request/v1": (["agent_tool_request"], []),
    "local-tool-receipt/v1": (
        ["local_tool_receipt"],
        ["validate_tool_journal_settlement"],
    ),
    "r0-read-payload/v1": (["r0_read_payload"], []),
    "r0-read-result/v1": (["r0_read_result"], []),
    "source-content/v1": (["source_content"], []),
    "source-state/v1": (["source_state"], []),
    "tool-catalog/v1": (["tool_catalog"], []),
    "tool-dispatch-capability/v1": (
        ["tool_dispatch_capability"],
        ["validate_tool_capability_consumption"],
    ),
    "tool-dispatch-intent/v1": (
        ["tool_dispatch_intent"],
        ["validate_tool_journal_begin"],
    ),
    "tool-invocation/v1": (
        ["tool_invocation"],
        ["validate_tool_invocation_binding"],
    ),
}


def seal(
    schema: dict[str, object], document: dict[str, object]
) -> dict[str, object]:
    spec = schema["x-pullwise-digest"]
    field, domain = spec["field"], spec["domain"]
    unsigned = {key: value for key, value in document.items() if key != field}
    result = deepcopy(unsigned)
    result[field] = hashlib.sha256(
        domain.encode("utf-8") + b"\0" + canonical_bytes(unsigned)
    ).hexdigest()
    return result


class ToolEvidenceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        cls.schemas = {
            item["$id"]: item for item in cls.family["schemas"]
        }
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }

    def fixture(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def source_content(self) -> dict[str, object]:
        raw = b"hello"
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

    def source_state(self) -> dict[str, object]:
        invocation = self.fixture("tool_golden_invocation")
        return seal(
            self.schemas["source-state/v1"],
            {
                "schema_id": "source-state/v1",
                "task_id": invocation["task_id"],
                "attempt_id": invocation["attempt_id"],
                "native_epoch": invocation["native_epoch"],
                "repository_root_id": "f" * 64,
                "entry_count": 1,
                "manifest_sha256": "0" * 64,
            },
        )

    def request(self) -> dict[str, object]:
        invocation = self.fixture("tool_golden_invocation")
        return {
            "schema_id": "agent-tool-request/v1",
            "idempotency_key": invocation["idempotency_key"],
            "tool_key": invocation["tool_key"],
            "tool_input": deepcopy(invocation["tool_input"]),
        }

    def assert_content_ref(
        self,
        reference: dict[str, object],
        schema_id: str,
        document: dict[str, object],
    ) -> None:
        self.assertTrue(valid_content_ref(reference, {schema_id}))
        encoded = canonical_bytes(document)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), reference["sha256"])
        self.assertEqual(len(encoded), reference["size_bytes"])
        self.assertEqual("application/json", reference["media_type"])
        self.assertEqual("utf-8", reference["encoding"])
