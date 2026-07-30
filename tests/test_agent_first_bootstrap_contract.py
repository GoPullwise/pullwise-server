from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import types
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle
from tests.agent_first_bootstrap_support import golden_bootstrap, seal_adversarial


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

        self.assertIn("task-accept", families)
        self.assertIn("task-bootstrap", families)
        schemas = {
            schema["$id"]: schema
            for family_id in ("task-accept", "task-bootstrap")
            for schema in families[family_id]["schemas"]
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
        bootstrap = golden_bootstrap(self.contract)
        self.assertEqual(
            bootstrap,
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", bootstrap
            ),
        )

        mixed = deepcopy(bootstrap)
        mixed["construction_roots"]["owner"]["owner_epoch"] += 1
        mixed = seal_adversarial(
            self.contract,
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

    def test_accept_request_rejects_a_rebound_requirement_ledger(self) -> None:
        accept_request = deepcopy(golden_bootstrap(self.contract)["accept_request"])
        ledger = deepcopy(accept_request["requirement_ledger"])
        ledger.pop("ledger_digest")
        ledger["task_id"] = "task_ffffffffffffffffffffffffffffffff"
        accept_request["requirement_ledger"] = self.contract.seal_document(
            "requirement-ledger/v1", ledger
        )
        accept_request = seal_adversarial(
            self.contract,
            "accept_request_digest",
            "pullwise:agent-task-accept-request:v1",
            accept_request,
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.verify_document_digest(
                "agent-task-accept-request/v1", accept_request
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
        self.assertEqual("ACCEPT_REQUEST_TASK_BINDING_MISMATCH", raised.exception.detail)
        self.assertEqual("$.requirement_ledger.task_id", raised.exception.path)

    def test_runtime_bootstrap_rejects_each_cross_document_rebinding(self) -> None:
        cases: list[tuple[str, dict[str, object], str, str]] = []
        baseline = golden_bootstrap(self.contract)

        package_mix = deepcopy(baseline)
        response = deepcopy(package_mix["accept_response"])
        response.pop("response_digest")
        response["package"]["root_sha256"] = "f" * 64
        package_mix["accept_response"] = self.contract.seal_document(
            "agent-task-accept-response/v1", response
        )
        cases.append(
            (
                "package_mix",
                package_mix,
                "BOOTSTRAP_PACKAGE_MISMATCH",
                "$.package",
            )
        )

        task_mix = deepcopy(baseline)
        task_mix["construction_roots"]["task_record"]["task_id"] = (
            "task_ffffffffffffffffffffffffffffffff"
        )
        cases.append(
            (
                "task_mix",
                task_mix,
                "BOOTSTRAP_TASK_BINDING_MISMATCH",
                "$.construction_roots",
            )
        )

        version_mix = deepcopy(baseline)
        version_mix["construction_roots"]["task_record"]["task_version"] += 1
        cases.append(
            (
                "task_version_mix",
                version_mix,
                "BOOTSTRAP_TASK_VERSION_MISMATCH",
                "$.construction_roots.task_record.task_version",
            )
        )

        stale_authority = deepcopy(baseline)
        authority = stale_authority["authority"]
        grant = authority["grant"]
        grant.pop("grant_digest")
        grant["deletion_version"] += 1
        authority["grant"] = self.contract.seal_document(
            "agent-worker-grant/v1", grant
        )
        authority.pop("authority_digest")
        authority["deletion_version"] += 1
        stale_authority["authority"] = self.contract.seal_document(
            "server-authority-envelope/v1", authority
        )
        cases.append(
            (
                "stale_authority",
                stale_authority,
                "BOOTSTRAP_TASK_VERSION_MISMATCH",
                "$.construction_roots.task_record.task_version",
            )
        )

        transport_mix = deepcopy(baseline)
        transport_mix["transport_binding"]["outer_job_id"] = "job-other"
        cases.append(
            (
                "transport_mix",
                transport_mix,
                "BOOTSTRAP_TRANSPORT_BINDING_MISMATCH",
                "$.transport_binding",
            )
        )

        session_mix = deepcopy(baseline)
        session_mix["construction_roots"]["attempt"]["owner_session_id"] = (
            "sess_ffffffffffffffffffffffffffffffff"
        )
        cases.append(
            (
                "session_mix",
                session_mix,
                "BOOTSTRAP_AUTHORITY_BINDING_MISMATCH",
                "$.construction_roots",
            )
        )

        request_ref_mix = deepcopy(baseline)
        request_ref_mix["construction_roots"]["task_record"]["request_ref"][
            "sha256"
        ] = "f" * 64
        cases.append(
            (
                "request_ref_mix",
                request_ref_mix,
                "BOOTSTRAP_REQUEST_REF_MISMATCH",
                "$.construction_roots.task_record.request_ref",
            )
        )

        policy_ref_mix = deepcopy(baseline)
        policy_ref_mix["construction_roots"]["task_record"]["policy_ref"][
            "sha256"
        ] = "f" * 64
        cases.append(
            (
                "policy_ref_mix",
                policy_ref_mix,
                "BOOTSTRAP_POLICY_REF_MISMATCH",
                "$.construction_roots.task_record.policy_ref",
            )
        )

        ledger_mix = deepcopy(baseline)
        ledger_mix["construction_roots"]["task_record"][
            "ledger_head_digest"
        ] = "f" * 64
        cases.append(
            (
                "ledger_mix",
                ledger_mix,
                "BOOTSTRAP_LEDGER_BINDING_MISMATCH",
                "$.construction_roots.task_record.ledger_head_digest",
            )
        )

        deadline_mix = deepcopy(baseline)
        deadline_mix["construction_roots"]["task_record"][
            "absolute_deadline_at"
        ] = "2026-07-22T00:02:00.000Z"
        cases.append(
            (
                "deadline_mix",
                deadline_mix,
                "BOOTSTRAP_DEADLINE_BINDING_MISMATCH",
                "$.construction_roots.task_record.absolute_deadline_at",
            )
        )

        for name, document, detail, path in cases:
            with self.subTest(name=name):
                adversarial = seal_adversarial(
                    self.contract,
                    "bootstrap_digest",
                    "pullwise:agent-task-runtime-bootstrap:v1",
                    document,
                )
                with self.assertRaises(self.contract.ContractValidationError) as raised:
                    self.contract.verify_document_digest(
                        "agent-task-runtime-bootstrap/v1", adversarial
                    )
                self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
                self.assertEqual(detail, raised.exception.detail)
                self.assertEqual(path, raised.exception.path)

    def test_runtime_bootstrap_rejects_policy_deadline_derivation_drift(self) -> None:
        bootstrap = golden_bootstrap(self.contract)
        accept_request = deepcopy(bootstrap["accept_request"])
        request = accept_request["task_request"]
        request["requested_budgets"]["wall_ms"] = 120_000
        policy = accept_request["effective_policy"]
        policy.pop("digest")
        policy["budgets"]["wall_ms"] = 120_000
        policy = self.contract.seal_document(
            "effective-execution-policy/v1", policy
        )
        accept_request["effective_policy"] = policy
        accept_request.pop("accept_request_digest")
        accept_request = self.contract.seal_document(
            "agent-task-accept-request/v1", accept_request
        )
        bootstrap["accept_request"] = accept_request

        task = bootstrap["construction_roots"]["task_record"]
        request_bytes = self.contract.canonical_document_bytes(request)
        task["request_ref"]["sha256"] = self.contract.canonical_document_sha256(
            request
        )
        task["request_ref"]["size_bytes"] = len(request_bytes)
        task["request_digest"] = task["request_ref"]["sha256"]
        policy_bytes = self.contract.canonical_document_bytes(policy)
        task["policy_ref"]["sha256"] = self.contract.canonical_document_sha256(
            policy
        )
        task["policy_ref"]["size_bytes"] = len(policy_bytes)
        task["policy_digest"] = policy["digest"]

        authority = bootstrap["authority"]
        grant = authority["grant"]
        grant.pop("grant_digest")
        grant["policy_digest"] = policy["digest"]
        authority["grant"] = self.contract.seal_document(
            "agent-worker-grant/v1", grant
        )
        authority.pop("authority_digest")
        bootstrap["authority"] = self.contract.seal_document(
            "server-authority-envelope/v1", authority
        )
        bootstrap = seal_adversarial(
            self.contract,
            "bootstrap_digest",
            "pullwise:agent-task-runtime-bootstrap:v1",
            bootstrap,
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.verify_document_digest(
                "agent-task-runtime-bootstrap/v1", bootstrap
            )
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
        self.assertEqual(
            "BOOTSTRAP_DEADLINE_DERIVATION_MISMATCH", raised.exception.detail
        )
        self.assertEqual(
            "$.construction_roots.task_record.absolute_deadline_at",
            raised.exception.path,
        )

if __name__ == "__main__":
    unittest.main()
