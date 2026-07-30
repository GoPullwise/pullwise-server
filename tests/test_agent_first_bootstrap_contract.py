from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import types
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"


class AgentFirstBootstrapContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = build_bundle(SOURCE_ROOT)
        cls.contract = types.ModuleType("_agent_first_bootstrap_source_contract")
        exec(cls.bundle.python_wrapper, cls.contract.__dict__)

    def test_current_bundle_publishes_versioned_atomic_bootstrap_shapes(self) -> None:
        families = {
            family["family_id"]: family
            for family in self.bundle.document["families"]
        }

        self.assertIn("task-bootstrap", families)
        schemas = {
            schema["$id"]: schema for schema in families["task-bootstrap"]["schemas"]
        }
        self.assertEqual(
            ["agent-task-accept-request/v1", "agent-task-runtime-bootstrap/v1"],
            list(schemas),
        )

        accept_request = schemas["agent-task-accept-request/v1"]
        self.assertEqual(
            {
                "schema_id",
                "package",
                "idempotency_key",
                "outer_job_id",
                "run_id",
                "task_request",
                "effective_policy",
                "requirement_ledger",
                "accept_request_digest",
            },
            set(accept_request["required"]),
        )
        self.assertEqual(set(accept_request["required"]), set(accept_request["properties"]))
        self.assertFalse(accept_request["additionalProperties"])
        self.assertEqual(
            "task-request/v1", accept_request["properties"]["task_request"]["$ref"]
        )
        self.assertEqual(
            "effective-execution-policy/v1",
            accept_request["properties"]["effective_policy"]["$ref"],
        )
        self.assertEqual(
            "requirement-ledger/v1",
            accept_request["properties"]["requirement_ledger"]["$ref"],
        )

        bootstrap = schemas["agent-task-runtime-bootstrap/v1"]
        self.assertEqual(
            {
                "schema_id",
                "package",
                "accept_request",
                "accept_response",
                "authority",
                "transport_binding",
                "construction_roots",
                "bootstrap_digest",
            },
            set(bootstrap["required"]),
        )
        self.assertEqual(set(bootstrap["required"]), set(bootstrap["properties"]))
        self.assertFalse(bootstrap["additionalProperties"])
        self.assertEqual(
            "agent-task-accept-request/v1",
            bootstrap["properties"]["accept_request"]["$ref"],
        )
        self.assertEqual(
            "agent-task-accept-response/v1",
            bootstrap["properties"]["accept_response"]["$ref"],
        )
        self.assertEqual(
            "server-authority-envelope/v1",
            bootstrap["properties"]["authority"]["$ref"],
        )
        roots = bootstrap["properties"]["construction_roots"]
        self.assertEqual({"task_record", "attempt", "owner"}, set(roots["required"]))
        self.assertEqual(set(roots["required"]), set(roots["properties"]))
        self.assertFalse(roots["additionalProperties"])
        self.assertEqual("task-record/v1", roots["properties"]["task_record"]["$ref"])
        self.assertEqual("attempt-record/v1", roots["properties"]["attempt"]["$ref"])
        self.assertEqual("task-owner/v1", roots["properties"]["owner"]["$ref"])

    def test_runtime_bootstrap_rejects_mixed_generation_roots(self) -> None:
        bootstrap = self._golden_bootstrap()
        self.assertEqual(
            bootstrap,
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", bootstrap
            ),
        )

        mixed = deepcopy(bootstrap)
        mixed["construction_roots"]["owner"]["owner_epoch"] += 1
        mixed = self._seal_adversarial(
            "agent-task-runtime-bootstrap/v1",
            "bootstrap_digest",
            "pullwise:agent-task-runtime-bootstrap:v1",
            mixed,
        )
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", mixed
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
        self.assertEqual("BOOTSTRAP_GENERATION_MISMATCH", raised.exception.detail)
        self.assertEqual("$.construction_roots", raised.exception.path)

    def _golden_bootstrap(self) -> dict[str, object]:
        contract = self.contract
        package = contract.package_tuple()
        request = deepcopy(
            contract.fixture("task_control_golden_task_request")["document"]
        )
        policy = deepcopy(
            contract.fixture("task_control_golden_effective_policy")["document"]
        )
        ledger = deepcopy(contract.fixture("requirements_golden_ledger")["document"])
        accept_request = contract.seal_document(
            "agent-task-accept-request/v1",
            {
                "schema_id": "agent-task-accept-request/v1",
                "package": package,
                "idempotency_key": "accept:bootstrap:one",
                "outer_job_id": "job-1",
                "run_id": "run-1",
                "task_request": request,
                "effective_policy": policy,
                "requirement_ledger": ledger,
            },
        )
        accept_response = contract.seal_document(
            "agent-task-accept-response/v1",
            {
                "schema_id": "agent-task-accept-response/v1",
                "package": package,
                "task_id": request["task_id"],
                "task_version": 1,
                "deletion_version": 0,
                "lifecycle": "QUEUED",
                "desired_state": "RUN",
                "accepted_at": "2026-07-22T00:00:00.000Z",
            },
        )

        task_record = deepcopy(
            contract.fixture("task_control_golden_task_record")["document"]
        )
        attempt = deepcopy(
            contract.fixture("task_control_golden_attempt_record")["document"]
        )
        owner = deepcopy(
            contract.fixture("task_control_golden_task_owner")["document"]
        )
        lease_id = "lease_22222222222222222222222222222222"
        task_record.update(
            {
                "lifecycle": "ACTIVE",
                "task_version": 2,
                "lease_id": lease_id,
                "native_epoch": 1,
                "current_attempt_id": attempt["attempt_id"],
                "owner_epoch": 1,
                "ledger_head_digest": ledger["ledger_digest"],
                "updated_at": "2026-07-22T00:00:01.000Z",
            }
        )
        attempt["transport_binding"]["lease_id"] = lease_id

        grant = deepcopy(
            contract.fixture("authority_golden_server_authority_envelope")[
                "document"
            ]["grant"]
        )
        grant.update(
            {
                "package": package,
                "task_id": task_record["task_id"],
                "attempt_id": attempt["attempt_id"],
                "session_id": owner["session_id"],
                "owner_id": owner["owner_id"],
                "lease_id": lease_id,
                "task_version": task_record["task_version"],
                "deletion_version": task_record["deletion_version"],
                "owner_epoch": owner["owner_epoch"],
                "native_epoch": attempt["native_epoch"],
                "transport_epoch": task_record["transport_epoch"],
                "policy_digest": policy["digest"],
                "absolute_deadline_at": task_record["absolute_deadline_at"],
                "terminalization_reserve_ms": task_record[
                    "terminalization_reserve_ms"
                ],
            }
        )
        grant.pop("grant_digest")
        grant = contract.seal_document("agent-worker-grant/v1", grant)
        authority = contract.seal_document(
            "server-authority-envelope/v1",
            {
                "schema_id": "server-authority-envelope/v1",
                "package": package,
                "task_id": task_record["task_id"],
                "attempt_id": attempt["attempt_id"],
                "session_id": owner["session_id"],
                "owner_id": owner["owner_id"],
                "lease_id": lease_id,
                "task_version": task_record["task_version"],
                "deletion_version": task_record["deletion_version"],
                "owner_epoch": owner["owner_epoch"],
                "native_epoch": attempt["native_epoch"],
                "transport_epoch": task_record["transport_epoch"],
                "absolute_deadline_at": task_record["absolute_deadline_at"],
                "terminalization_reserve_ms": task_record[
                    "terminalization_reserve_ms"
                ],
                "lifecycle": "ACTIVE",
                "desired_state": "RUN",
                "grant": grant,
            },
        )
        return contract.seal_document(
            "agent-task-runtime-bootstrap/v1",
            {
                "schema_id": "agent-task-runtime-bootstrap/v1",
                "package": package,
                "accept_request": accept_request,
                "accept_response": accept_response,
                "authority": authority,
                "transport_binding": {
                    "outer_job_id": task_record["outer_job_id"],
                    "run_id": task_record["run_id"],
                    "lease_id": lease_id,
                    "transport_epoch": task_record["transport_epoch"],
                },
                "construction_roots": {
                    "task_record": task_record,
                    "attempt": attempt,
                    "owner": owner,
                },
            },
        )

    def _seal_adversarial(
        self,
        schema_id: str,
        digest_field: str,
        domain: str,
        document: dict[str, object],
    ) -> dict[str, object]:
        unsigned = {key: value for key, value in document.items() if key != digest_field}
        digest = hashlib.sha256(
            domain.encode("utf-8")
            + b"\0"
            + self.contract.canonical_document_bytes(unsigned)
        ).hexdigest()
        return {**unsigned, digest_field: digest}


if __name__ == "__main__":
    unittest.main()
