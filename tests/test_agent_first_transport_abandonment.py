from __future__ import annotations

import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from pullwise_server import agent_first_authority as authority_module
from pullwise_server._generated_agent_task_contract import PACKAGE_TUPLE
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
from tests.agent_first_authority_support import AuthorityHarness, NOW


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"
SCHEMA_ID = "transport-abandonment-record/v1"


def _source_families() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FAMILY_ROOT.glob("*.json"))
    ]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstTransportAbandonmentTest(AuthorityHarness, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = _source_families()
        cls.family_ids = {family["family_id"] for family in families}
        cls.schemas = {
            schema["$id"]: schema
            for family in families
            for schema in family["schemas"]
        }
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for family in families
            for fixture in family["fixtures"]
        }
        identity, version, content_sha256, root_sha256 = PACKAGE_TUPLE
        wrapper = render_python_wrapper(
            identity,
            version,
            root_sha256,
            content_sha256,
            _canonical({"families": families}),
        )
        cls.live_contract = types.ModuleType("_transport_abandonment_live_contract")
        exec(wrapper, cls.live_contract.__dict__)

    def _contract_patch(self):
        return patch.multiple(
            authority_module,
            ContractValidationError=self.live_contract.ContractValidationError,
            canonical_validated_bytes=self.live_contract.canonical_validated_bytes,
            package_tuple=self.live_contract.package_tuple,
            seal_document=self.live_contract.seal_document,
            verify_document_digest=self.live_contract.verify_document_digest,
        )

    @staticmethod
    def _abandon_request(envelope: dict[str, object]) -> dict[str, object]:
        return {
            "schema_id": "agent-claim-abandon-request/v1",
            "package": {
                "schema_id": "current-package-ref/v1",
                "package_identity": PACKAGE_TUPLE[0],
                "package_version": PACKAGE_TUPLE[1],
                "content_sha256": PACKAGE_TUPLE[2],
                "root_sha256": PACKAGE_TUPLE[3],
            },
            **{
                key: envelope[key]
                for key in (
                    "task_id",
                    "attempt_id",
                    "session_id",
                    "owner_id",
                    "lease_id",
                    "deletion_version",
                    "owner_epoch",
                    "native_epoch",
                    "transport_epoch",
                )
            },
            "grant_id": envelope["grant"]["grant_id"],
            "expected_task_version": envelope["task_version"],
            "reason": "outer_lease_lost",
            "idempotency_key": "abandon:independent-record",
        }

    def test_source_contract_defines_independent_non_result_record(self) -> None:
        self.assertIn("transport-abandonment", self.family_ids)
        self.assertIn(SCHEMA_ID, self.schemas)
        schema = self.schemas[SCHEMA_ID]
        self.assertEqual(
            {
                "schema_id",
                "package",
                "abandonment_id",
                "task_id",
                "attempt_id",
                "session_id",
                "owner_id",
                "grant_id",
                "lease_id",
                "previous_task_version",
                "abandoned_task_version",
                "deletion_version",
                "owner_epoch",
                "native_epoch",
                "transport_epoch",
                "grant_digest",
                "superseded_authority_digest",
                "reason",
                "abandoned_at",
                "abandonment_digest",
            },
            set(schema["required"]),
        )
        self.assertEqual(
            {
                "document_rules": ["transport_abandonment_record"],
                "contextual_helpers": [],
            },
            schema["x-pullwise-semantics"],
        )
        self.assertNotIn("result_ref", schema["properties"])
        self.assertNotIn("receipt_digest", schema["properties"])

        golden = self.fixtures["transport_abandonment_golden_record"]
        replay = self.fixtures[
            "transport_abandonment_idempotency_exact_record"
        ]
        negative = self.fixtures[
            "transport_abandonment_negative_successor_version"
        ]
        self.assertEqual(golden["document"], replay["document"])
        self.assertEqual("AUTHORITY_INPUT_UNTRUSTED", negative["expected_code"])
        self.assertNotEqual(
            negative["document"]["previous_task_version"] + 1,
            negative["document"]["abandoned_task_version"],
        )

    def test_abandonment_record_and_successor_authority_are_distinct_and_atomic(
        self,
    ) -> None:
        self.register()
        with self._contract_patch(), patch.object(
            authority_module, "_now", return_value=NOW
        ):
            self.accept()
            envelope = json.loads(
                self.authority.claim_and_issue_current_grant(self.claim_request())
            )
            stale_receipt = self.receipt(envelope)
            self.authority.store_transport_receipt(stale_receipt)
            request = self._abandon_request(envelope)
            first = self.authority.abandon_current_claim(request)
            replay = self.authority.abandon_current_claim(dict(request))

        self.assertEqual(first, replay)
        response = self.live_contract.verify_document_digest(
            "agent-claim-abandon-response/v1", json.loads(first)
        )
        with self.connect() as connection:
            stored = connection.execute(
                "SELECT abandonment_id, abandonment_digest, abandonment_bytes, "
                "previous_task_version, abandoned_task_version, grant_digest, "
                "superseded_authority_digest "
                "FROM agent_current_abandonments"
            ).fetchone()
            head = connection.execute(
                "SELECT current_authority_schema_id, current_authority_digest "
                "FROM agent_current_task_heads"
            ).fetchone()
            binding = connection.execute(
                "SELECT transport_envelope_digest "
                "FROM agent_current_transport_receipt_bindings"
            ).fetchone()[0]

        record_bytes = bytes(stored[2])
        record = self.live_contract.verify_document_digest(
            SCHEMA_ID, json.loads(record_bytes)
        )
        self.assertEqual(stored[0], record["abandonment_id"])
        self.assertEqual(stored[1], record["abandonment_digest"])
        self.assertEqual(stored[3], record["previous_task_version"])
        self.assertEqual(stored[4], record["abandoned_task_version"])
        self.assertEqual(stored[5], record["grant_digest"])
        self.assertEqual(stored[6], record["superseded_authority_digest"])
        self.assertEqual(response["previous_task_version"], record["previous_task_version"])
        self.assertEqual(response["task_version"], record["abandoned_task_version"])
        self.assertEqual(response["grant"]["grant_digest"], record["grant_digest"])
        self.assertEqual(
            response["superseded_authority_digest"],
            record["superseded_authority_digest"],
        )
        self.assertEqual(response["reason"], record["reason"])
        self.assertEqual(response["abandoned_at"], record["abandoned_at"])
        self.assertNotEqual(first, record_bytes)
        self.assertNotEqual(response["response_digest"], record["abandonment_digest"])
        self.assertEqual(
            ("agent-claim-abandon-response/v1", response["response_digest"]),
            tuple(head),
        )
        self.assertIsNone(binding)
        self.assertEqual(
            (1, 4),
            self.counts("agent_current_abandonments", "agent_current_fences"),
        )


if __name__ == "__main__":
    unittest.main()
