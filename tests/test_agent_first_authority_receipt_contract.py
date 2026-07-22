from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"
AUTHORITY_PATH = FAMILY_ROOT / "authority-control.json"
RECEIPT_PATH = FAMILY_ROOT / "receipt-error.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _schema_map() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for family in (_load(AUTHORITY_PATH), _load(RECEIPT_PATH)):
        result.update({item["$id"]: item for item in family["schemas"]})
    return result


def _raw_digest(schema_id: str, document: dict[str, object]) -> str | None:
    spec = _schema_map()[schema_id].get("x-pullwise-digest")
    if not isinstance(spec, dict):
        return None
    field, domain = spec["field"], spec["domain"]
    unsigned = {key: value for key, value in document.items() if key != field}
    return hashlib.sha256(
        domain.encode("utf-8") + b"\0" + _canonical(unsigned)
    ).hexdigest()


def _fixture(family: dict[str, object], fixture_id: str) -> dict[str, object]:
    return next(item for item in family["fixtures"] if item["fixture_id"] == fixture_id)


def _package_refs(value: object):
    if isinstance(value, dict):
        if value.get("schema_id") == "current-package-ref/v1":
            yield value
        for item in value.values():
            yield from _package_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _package_refs(item)


class AgentFirstAuthorityReceiptContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.authority_family = _load(AUTHORITY_PATH)
        cls.receipt_family = _load(RECEIPT_PATH)

    def test_declared_fixture_documents_are_complete_and_digest_exact(self) -> None:
        from pullwise_server import _generated_agent_task_contract as contract

        local_type_negative = "receipt_negative_transport_as_local"
        for family in (self.authority_family, self.receipt_family):
            for item in family["fixtures"]:
                with self.subTest(fixture_id=item["fixture_id"]):
                    document = item["document"]
                    schema_id = item["schema_id"]
                    schema = contract.schema(schema_id)
                    self.assertEqual(set(schema["required"]), set(document))
                    expected = _raw_digest(schema_id, document)
                    if expected is not None:
                        self.assertEqual(expected, document[schema["x-pullwise-digest"]["field"]])
                    if item["fixture_id"] == local_type_negative:
                        with self.assertRaises(contract.ContractValidationError) as raised:
                            contract.validate_document(schema_id, document)
                        self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)
                    elif expected is None:
                        contract.validate_document(schema_id, document)
                    else:
                        contract.verify_document_digest(schema_id, document)

    def test_raw_fixture_digest_literals_are_exact(self) -> None:
        for family in (self.authority_family, self.receipt_family):
            for item in family["fixtures"]:
                expected = _raw_digest(item["schema_id"], item["document"])
                if expected is None:
                    continue
                field = _schema_map()[item["schema_id"]]["x-pullwise-digest"]["field"]
                with self.subTest(fixture_id=item["fixture_id"]):
                    self.assertEqual(expected, item["document"][field])

    def test_family_sources_pass_closed_loader(self) -> None:
        from pullwise_server.agent_first_contract_bundle_source import load_family

        owners: dict[str, str] = {}
        fixture_ids: set[str] = set()
        for path, family_id in (
            (AUTHORITY_PATH, "authority-control"),
            (RECEIPT_PATH, "receipt-error"),
        ):
            loaded = load_family(path, family_id, owners, fixture_ids)
            self.assertEqual(family_id, loaded["family_id"])

    def test_authority_fixtures_execute_full_fence_and_successor_semantics(self) -> None:
        crash = _fixture(self.authority_family, "authority_crash_after_claim")
        stale = _fixture(self.authority_family, "authority_fence_stale_deletion_version")
        abandon = _fixture(self.authority_family, "authority_golden_abandon_response")
        grant = _fixture(self.authority_family, "authority_idempotency_exact_grant")
        untrusted = _fixture(self.authority_family, "authority_negative_agent_selected_fence")

        grant_document = grant["document"]
        abandon_document = abandon["document"]
        self.assertEqual(grant_document, abandon_document["grant"])
        self.assertEqual(
            abandon_document["previous_task_version"] + 1,
            abandon_document["task_version"],
        )
        fence_fields = (
            "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
            "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
        )
        self.assertTrue(
            all(abandon_document[key] == grant_document[key] for key in fence_fields)
        )
        stale_document = stale["document"]
        stable_fields = tuple(key for key in fence_fields if key != "deletion_version")
        self.assertTrue(all(stale_document[key] == grant_document[key] for key in stable_fields))
        self.assertLess(stale_document["deletion_version"], grant_document["deletion_version"])
        self.assertEqual("AUTHORITY_FENCED", stale["expected_code"])

        untrusted_document = untrusted["document"]
        self.assertEqual(grant_document, untrusted_document["grant"])
        self.assertEqual(9, untrusted_document["owner_epoch"])
        self.assertEqual(1, grant_document["owner_epoch"])
        self.assertEqual("AUTHORITY_INPUT_UNTRUSTED", untrusted["expected_code"])
        self.assertEqual("agent-task-claim-request/v1", crash["document"]["schema_id"])

    def test_receipt_fixtures_keep_immutable_receipt_and_binding_separate(self) -> None:
        crash = _fixture(self.receipt_family, "receipt_crash_binding_rollback")
        receipt = _fixture(self.receipt_family, "receipt_golden_immutable_transport")
        exact = _fixture(self.receipt_family, "receipt_idempotency_exact_binding")
        conflict = _fixture(self.receipt_family, "receipt_negative_rebinding")
        wrong_type = _fixture(self.receipt_family, "receipt_negative_transport_as_local")

        receipt_document = receipt["document"]
        self.assertEqual("server_transport", receipt_document["receipt_kind"])
        self.assertNotIn("bound_transport_envelope_digest", receipt_document)
        for binding in (crash["document"], exact["document"], conflict["document"]):
            self.assertEqual(receipt_document["receipt_id"], binding["receipt_id"])
            self.assertEqual(receipt_document["receipt_digest"], binding["receipt_digest"])
        self.assertEqual(("unbound", None), (
            crash["document"]["state"],
            crash["document"]["bound_transport_envelope_digest"],
        ))
        self.assertEqual("bound", exact["document"]["state"])
        self.assertNotEqual(
            exact["document"]["bound_transport_envelope_digest"],
            conflict["document"]["bound_transport_envelope_digest"],
        )
        self.assertEqual("TRANSPORT_RECEIPT_ALREADY_BOUND", conflict["expected_code"])
        differences = {
            key for key in receipt_document if receipt_document[key] != wrong_type["document"][key]
        }
        self.assertEqual({"receipt_kind", "receipt_digest"}, differences)
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", wrong_type["expected_code"])

    def test_fixture_package_pin_is_structural_and_current_pin_is_contextual(self) -> None:
        from pullwise_server import _generated_agent_task_contract as contract

        references = [
            reference
            for family in (self.authority_family, self.receipt_family)
            for item in family["fixtures"]
            for reference in _package_refs(item["document"])
        ]
        self.assertTrue(references)
        self.assertTrue(all(reference == references[0] for reference in references))
        self.assertNotEqual(contract.package_tuple(), references[0])
        contextual = copy.deepcopy(
            _fixture(self.authority_family, "authority_crash_after_claim")["document"]
        )
        contextual["package"] = contract.package_tuple()
        contract.validate_document("agent-task-claim-request/v1", contextual)

    def test_claim_crash_retry_conflict_and_abandon_are_atomic(self) -> None:
        from pullwise_server._generated_agent_task_contract import package_tuple
        from pullwise_server.agent_first_authority import AgentFirstAuthority
        from tests.agent_first_authority_support import AuthorityHarness

        class Harness(AuthorityHarness, unittest.TestCase):
            pass

        harness = Harness()
        harness.setUp()
        try:
            harness.register()
            harness.accept()
            request = copy.deepcopy(
                _fixture(self.authority_family, "authority_crash_after_claim")["document"]
            )
            request["package"] = package_tuple()
            tables = (
                "agent_current_attempts", "agent_current_owner_incarnations",
                "agent_current_grants", "agent_current_claims",
            )
            before = harness.counts(*tables)

            def inject(point: str) -> None:
                if point == "claim.after_claim":
                    raise RuntimeError("injected:claim.after_claim")

            with self.assertRaisesRegex(RuntimeError, "injected:claim.after_claim"):
                AgentFirstAuthority(harness.connect, fault_injector=inject).claim_and_issue_current_grant(request)
            self.assertEqual(before, harness.counts(*tables))
            self.assertEqual(
                "AUTHORITY_RELOAD_REQUIRED",
                _fixture(self.authority_family, "authority_crash_after_claim")["expected_code"],
            )

            first = harness.authority.claim_and_issue_current_grant(request)
            self.assertEqual(first, harness.authority.claim_and_issue_current_grant(request))
            conflict = copy.deepcopy(request)
            conflict["tool_call_limit"] = 6
            harness.assert_error(
                "IDEMPOTENCY_CONFLICT",
                lambda: harness.authority.claim_and_issue_current_grant(conflict),
            )
            envelope = json.loads(first)
            abandon = {
                "schema_id": "agent-claim-abandon-request/v1",
                "package": package_tuple(),
                **{key: envelope[key] for key in (
                    "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
                    "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
                )},
                "grant_id": envelope["grant"]["grant_id"],
                "expected_task_version": envelope["task_version"],
                "reason": "outer_lease_lost",
                "idempotency_key": "abandon:fixture:one",
            }
            abandoned = harness.authority.abandon_current_claim(abandon)
            self.assertEqual(abandoned, harness.authority.abandon_current_claim(abandon))
            abandon_conflict = {**abandon, "reason": "worker_shutdown"}
            harness.assert_error(
                "IDEMPOTENCY_CONFLICT",
                lambda: harness.authority.abandon_current_claim(abandon_conflict),
            )
            self.assertEqual("FENCED", json.loads(abandoned)["state"])
        finally:
            harness.tearDown()

    def test_transport_receipt_retry_collision_and_type_are_exact(self) -> None:
        from pullwise_server._generated_agent_task_contract import seal_document
        from tests.agent_first_authority_support import AuthorityHarness

        class Harness(AuthorityHarness, unittest.TestCase):
            pass

        harness = Harness()
        harness.setUp()
        try:
            _, envelope = harness.prepare_claim()
            receipt = harness.receipt(envelope)
            first = harness.authority.store_transport_receipt(receipt)
            self.assertEqual(first, harness.authority.store_transport_receipt(receipt))
            collision = copy.deepcopy(receipt)
            collision["content_ref"]["sha256"] = "e" * 64
            collision.pop("receipt_digest")
            collision = seal_document("server-transport-receipt/v1", collision)
            harness.assert_error(
                "TRANSPORT_RECEIPT_BINDING_CONFLICT",
                lambda: harness.authority.store_transport_receipt(collision),
            )
            wrong = _fixture(
                self.receipt_family, "receipt_negative_transport_as_local"
            )["document"]
            harness.assert_error(
                "TRANSPORT_RECEIPT_TYPE_INVALID",
                lambda: harness.authority.store_transport_receipt(wrong),
            )
            with harness.connect() as connection:
                stored = connection.execute(
                    "SELECT receipt_bytes FROM agent_current_transport_receipts"
                ).fetchone()[0]
                binding = connection.execute(
                    "SELECT transport_envelope_digest FROM "
                    "agent_current_transport_receipt_bindings"
                ).fetchone()[0]
            self.assertEqual(first, bytes(stored))
            self.assertIsNone(binding)
        finally:
            harness.tearDown()

    def test_binding_crash_rolls_back_to_fixture_unbound_state(self) -> None:
        from pullwise_server.agent_first_authority import AgentFirstAuthority
        from tests.agent_first_authority_support import AuthorityHarness
        from tests.agent_first_transport_support import TransportEnvelopeHarness

        class Harness(TransportEnvelopeHarness, AuthorityHarness, unittest.TestCase):
            pass

        harness = Harness()
        harness.setUp()
        try:
            _, authority = harness.prepare_claim()
            envelope, receipt = harness.transport_envelope(
                authority, diagnostics_state="uploaded"
            )
            assert receipt is not None
            stored_receipt = harness.authority.store_transport_receipt(receipt)

            def inject(point: str) -> None:
                if point == "terminal.after_binding":
                    raise RuntimeError("injected:terminal.after_binding")

            with self.assertRaisesRegex(RuntimeError, "injected:terminal.after_binding"):
                AgentFirstAuthority(
                    harness.connect, fault_injector=inject
                ).commit_current_transport_envelope(envelope)
            expected = _fixture(
                self.receipt_family, "receipt_crash_binding_rollback"
            )["document"]
            with harness.connect() as connection:
                binding = connection.execute(
                    "SELECT transport_envelope_digest FROM "
                    "agent_current_transport_receipt_bindings"
                ).fetchone()[0]
                receipt_bytes = bytes(connection.execute(
                    "SELECT receipt_bytes FROM agent_current_transport_receipts"
                ).fetchone()[0])
                terminal_count = connection.execute(
                    "SELECT COUNT(*) FROM agent_current_terminal_results"
                ).fetchone()[0]
            self.assertEqual((expected["state"], expected["bound_transport_envelope_digest"]),
                             ("unbound", binding))
            self.assertEqual(stored_receipt, receipt_bytes)
            self.assertEqual(0, terminal_count)
        finally:
            harness.tearDown()

    def test_waiver_invalid_is_registered_with_frozen_policy(self) -> None:
        registry = _fixture(
            self.receipt_family, "error_golden_current_registry"
        )["document"]
        entry = next(item for item in registry["entries"] if item["code"] == "WAIVER_INVALID")
        self.assertEqual(
            {"code": "WAIVER_INVALID", "retryable": False,
             "retry_scope": "none", "outcome": "rejected"},
            entry,
        )


if __name__ == "__main__":
    unittest.main()
