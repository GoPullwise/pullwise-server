from __future__ import annotations

import datetime as dt
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
AUTHORITY_PATH = FAMILY_ROOT / "authority-control.json"
DEADLINE_FIELDS = ("absolute_deadline_at", "terminalization_reserve_ms")


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


def _deadline_at(accepted_at: str, wall_ms: int) -> str:
    accepted = dt.datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
    return (
        (accepted + dt.timedelta(milliseconds=wall_ms))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class AgentFirstDeadlineWireTest(AuthorityHarness, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = _source_families()
        cls.authority_family = next(
            family for family in families if family["family_id"] == "authority-control"
        )
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
        cls.live_contract = types.ModuleType("_deadline_wire_live_contract")
        exec(wrapper, cls.live_contract.__dict__)

    def test_source_contract_requires_deadline_wire_and_negative_drift_fixture(self) -> None:
        grant_schema = self.schemas["agent-worker-grant/v1"]
        envelope_schema = self.schemas["server-authority-envelope/v1"]
        for schema in (grant_schema, envelope_schema):
            self.assertTrue(set(DEADLINE_FIELDS).issubset(schema["required"]))
            self.assertEqual(
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z$",
                schema["properties"]["absolute_deadline_at"]["pattern"],
            )
            self.assertEqual(
                0,
                schema["properties"]["terminalization_reserve_ms"]["minimum"],
            )

        grant = self.fixtures["authority_idempotency_exact_grant"]["document"]
        self.assertEqual("2026-07-22T12:01:00.000Z", grant["absolute_deadline_at"])
        self.assertEqual(1_000, grant["terminalization_reserve_ms"])
        drift = self.fixtures["authority_negative_deadline_grant_drift"]
        self.assertEqual("AUTHORITY_GRANT_BINDING_MISMATCH", drift["expected_code"])
        self.assertNotEqual(
            drift["document"]["absolute_deadline_at"],
            drift["document"]["grant"]["absolute_deadline_at"],
        )

    def test_acceptance_persists_deadline_once_and_claim_copies_exact_values(self) -> None:
        self.register()
        contract_patch = patch.multiple(
            authority_module,
            ContractValidationError=self.live_contract.ContractValidationError,
            canonical_validated_bytes=self.live_contract.canonical_validated_bytes,
            package_tuple=self.live_contract.package_tuple,
            seal_document=self.live_contract.seal_document,
            verify_document_digest=self.live_contract.verify_document_digest,
        )
        with contract_patch, patch.object(authority_module, "_now", return_value=NOW):
            accepted_bytes = self.accept()
        accepted = self.live_contract.verify_document_digest(
            "agent-task-accept-response/v1", json.loads(accepted_bytes)
        )

        with self.connect() as connection:
            stored = connection.execute(
                "SELECT accepted_at, absolute_deadline_at, "
                "terminalization_reserve_ms, policy_bytes "
                "FROM agent_current_task_requests"
            ).fetchone()
        policy = self.live_contract.verify_document_digest(
            "effective-execution-policy/v1", json.loads(stored[3])
        )
        expected_deadline = _deadline_at(NOW, policy["budgets"]["wall_ms"])
        self.assertEqual(NOW, accepted["accepted_at"])
        self.assertEqual(
            (NOW, expected_deadline, policy["terminalization_reserve_ms"]),
            stored[:3],
        )

        later = "2026-07-22T13:00:00.000Z"
        with contract_patch, patch.object(authority_module, "_now", return_value=later):
            self.assertEqual(accepted_bytes, self.accept())
            envelope_bytes = self.authority.claim_and_issue_current_grant(
                self.claim_request()
            )
        envelope = self.live_contract.verify_document_digest(
            "server-authority-envelope/v1", json.loads(envelope_bytes)
        )
        grant = self.live_contract.verify_document_digest(
            "agent-worker-grant/v1", envelope["grant"]
        )
        for document in (grant, envelope):
            self.assertEqual(expected_deadline, document["absolute_deadline_at"])
            self.assertEqual(
                policy["terminalization_reserve_ms"],
                document["terminalization_reserve_ms"],
            )

        with self.connect() as connection:
            replayed = connection.execute(
                "SELECT accepted_at, absolute_deadline_at, "
                "terminalization_reserve_ms FROM agent_current_task_requests"
            ).fetchone()
        self.assertEqual(stored[:3], replayed)


if __name__ == "__main__":
    unittest.main()
