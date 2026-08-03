from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import types
import unittest
from unittest.mock import patch

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server import agent_first_runtime_bootstrap
from pullwise_server._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    schema_ids,
    tool_catalog,
)
from tests.agent_first_authority_support import AuthorityHarness
from tests.agent_first_bootstrap_support import golden_bootstrap, seal_adversarial


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"
TRANSPORT_ATTEMPT_ID = (
    "transport_attempt_33333333333333333333333333333333"
)
TRANSPORT_ATTEMPT_PATTERN = "^transport_attempt_[0-9a-f]{32}$"


class AgentFirstS7TransportAttemptBindingTest(
    AuthorityHarness, unittest.TestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = build_bundle(SOURCE_ROOT)
        cls.contract = types.ModuleType("_agent_first_s7_transport_source_contract")
        exec(cls.bundle.python_wrapper, cls.contract.__dict__)
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle.document["families"]
            for schema in family["schemas"]
        }

    def test_source_requires_one_outer_transport_attempt_across_claim_and_roots(
        self,
    ) -> None:
        claim_request = self.schemas["agent-task-claim-request/v1"]
        bootstrap = self.schemas["agent-task-runtime-bootstrap/v1"]
        task_record = self.schemas["task-record/v1"]
        attempt_record = self.schemas["attempt-record/v1"]

        self.assertIn("transport_attempt_id", claim_request["required"])
        self.assertEqual(
            TRANSPORT_ATTEMPT_PATTERN,
            claim_request["properties"]["transport_attempt_id"]["pattern"],
        )

        bootstrap_binding = bootstrap["properties"]["transport_binding"]
        self.assertIn("transport_attempt_id", bootstrap_binding["required"])
        self.assertEqual(
            TRANSPORT_ATTEMPT_PATTERN,
            bootstrap_binding["properties"]["transport_attempt_id"]["pattern"],
        )

        self.assertIn("transport_attempt_id", task_record["required"])
        self.assertEqual(
            TRANSPORT_ATTEMPT_PATTERN,
            task_record["properties"]["transport_attempt_id"]["pattern"],
        )

        attempt_binding = attempt_record["properties"]["transport_binding"]
        self.assertIn("transport_attempt_id", attempt_binding["required"])
        self.assertEqual(
            TRANSPORT_ATTEMPT_PATTERN,
            attempt_binding["properties"]["transport_attempt_id"]["pattern"],
        )

    def test_golden_bootstrap_binds_outer_identity_distinct_from_native_attempt(
        self,
    ) -> None:
        bootstrap = golden_bootstrap(self.contract)
        binding = bootstrap["transport_binding"]
        task = bootstrap["construction_roots"]["task_record"]
        attempt = bootstrap["construction_roots"]["attempt"]

        self.assertEqual(TRANSPORT_ATTEMPT_ID, binding["transport_attempt_id"])
        self.assertEqual(
            binding["transport_attempt_id"], task["transport_attempt_id"]
        )
        self.assertEqual(
            binding["transport_attempt_id"],
            attempt["transport_binding"]["transport_attempt_id"],
        )
        self.assertNotEqual(
            binding["transport_attempt_id"], attempt["attempt_id"]
        )
        self.assertEqual(
            bootstrap,
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", bootstrap
            ),
        )

    def test_semantic_closure_rejects_outer_transport_attempt_rebinding(
        self,
    ) -> None:
        bootstrap = golden_bootstrap(self.contract)
        rebound = deepcopy(bootstrap)
        rebound["construction_roots"]["attempt"]["transport_binding"][
            "transport_attempt_id"
        ] = "transport_attempt_ffffffffffffffffffffffffffffffff"
        rebound = seal_adversarial(
            self.contract,
            "bootstrap_digest",
            "pullwise:agent-task-runtime-bootstrap:v1",
            rebound,
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", rebound
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
        self.assertEqual(
            "BOOTSTRAP_TRANSPORT_BINDING_MISMATCH", raised.exception.detail
        )
        self.assertEqual("$.transport_binding", raised.exception.path)

    def test_runtime_builder_carries_the_authenticated_claim_identity_exactly(
        self,
    ) -> None:
        self.register()
        self.accept()
        request = {
            **self.claim_request(),
            "transport_attempt_id": TRANSPORT_ATTEMPT_ID,
        }
        values = {
            **request,
            "package_tuple": PACKAGE_TUPLE,
            "required_schema_ids": tuple(schema_ids()),
            "expected_tool_catalog_digest": tool_catalog()["catalog_digest"],
        }
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            head = self.authority._claims._claimable_head(connection, values)

        source_functions = {
            name: getattr(self.contract, name)
            for name in (
                "ContractValidationError",
                "canonical_validated_bytes",
                "seal_document",
                "validate_claim_write_set",
                "verify_document_digest",
            )
        }
        with patch.multiple(agent_first_runtime_bootstrap, **source_functions):
            write = agent_first_runtime_bootstrap.build_runtime_bootstrap(
                head,
                request,
                claimed_at="2099-01-01T00:00:00.000Z",
            )

        bootstrap = self.contract.verify_document_digest(
            "agent-task-runtime-bootstrap/v1",
            json.loads(write["bootstrap_bytes"]),
        )
        binding = bootstrap["transport_binding"]
        self.assertEqual(TRANSPORT_ATTEMPT_ID, binding["transport_attempt_id"])
        self.assertEqual(
            TRANSPORT_ATTEMPT_ID,
            bootstrap["construction_roots"]["task_record"][
                "transport_attempt_id"
            ],
        )
        self.assertEqual(
            TRANSPORT_ATTEMPT_ID,
            bootstrap["construction_roots"]["attempt"]["transport_binding"][
                "transport_attempt_id"
            ],
        )
        self.assertNotEqual(
            TRANSPORT_ATTEMPT_ID,
            bootstrap["construction_roots"]["attempt"]["attempt_id"],
        )


if __name__ == "__main__":
    unittest.main()
