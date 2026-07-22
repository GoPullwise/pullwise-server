from __future__ import annotations

from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    canonical_bytes,
    sealed,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/budget.json"
SCHEMAS = (
    "elapsed-budget-ledger/v1",
    "elapsed-budget-reservation/v1",
    "elapsed-budget-settlement/v1",
)
HELPERS = {schema_id: ["verify_budget_transition"] for schema_id in SCHEMAS}


class BudgetFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return self.fixtures[fixture_id]["document"]

    def valid_ledger(self, value: dict[str, object]) -> bool:
        return (
            sealed(value, self.schemas["elapsed-budget-ledger/v1"])
            and value["consumed_ms"] + value["reserved_ms"]
            <= value["elapsed_limit_ms"]
            and value["calls_consumed"] + value["calls_reserved"]
            <= value["tool_call_limit"]
        )

    def valid_transition(
        self,
        before: dict[str, object],
        reservation: dict[str, object],
        reserved: dict[str, object],
        settlement: dict[str, object],
        after: dict[str, object],
    ) -> bool:
        if not (
            self.valid_ledger(before)
            and sealed(
                reservation,
                self.schemas["elapsed-budget-reservation/v1"],
            )
            and self.valid_ledger(reserved)
            and sealed(
                settlement,
                self.schemas["elapsed-budget-settlement/v1"],
            )
            and self.valid_ledger(after)
        ):
            return False
        previous = (
            ("previous_consumed_ms", "consumed_ms"),
            ("previous_reserved_ms", "reserved_ms"),
            ("previous_calls_consumed", "calls_consumed"),
            ("previous_calls_reserved", "calls_reserved"),
        )
        if reservation["task_id"] != before["task_id"] or any(
            reservation[left] != before[right] for left, right in previous
        ):
            return False
        if (
            before["consumed_ms"]
            + before["reserved_ms"]
            + reservation["reserved_ms"]
            > before["elapsed_limit_ms"]
            or before["calls_consumed"]
            + before["calls_reserved"]
            + reservation["reserved_calls"]
            > before["tool_call_limit"]
        ):
            return False
        expected_reserved = {
            "consumed_ms": before["consumed_ms"],
            "reserved_ms": before["reserved_ms"]
            + reservation["reserved_ms"],
            "calls_consumed": before["calls_consumed"],
            "calls_reserved": before["calls_reserved"]
            + reservation["reserved_calls"],
        }
        if any(reserved[key] != value for key, value in expected_reserved.items()):
            return False
        if (
            settlement["reservation_id"] != reservation["reservation_id"]
            or settlement["invocation_digest"]
            != reservation["invocation_digest"]
            or settlement["consumed_ms"] + settlement["released_ms"]
            != reservation["reserved_ms"]
            or settlement["consumed_calls"] + settlement["released_calls"]
            != reservation["reserved_calls"]
        ):
            return False
        if settlement["outcome"] == "settled":
            outcome_valid = (
                settlement["consumed_calls"] == 1
                and settlement["released_calls"] == 0
                and settlement["consumed_ms"] == settlement["elapsed_ms"]
                and settlement["released_ms"]
                == reservation["reserved_ms"] - settlement["elapsed_ms"]
            )
        else:
            outcome_valid = (
                settlement["consumed_calls"] == 0
                and settlement["released_calls"] == 1
                and settlement["consumed_ms"] == 0
                and settlement["released_ms"] == reservation["reserved_ms"]
            )
        expected_after = {
            "consumed_ms": before["consumed_ms"] + settlement["consumed_ms"],
            "reserved_ms": before["reserved_ms"],
            "calls_consumed": before["calls_consumed"]
            + settlement["consumed_calls"],
            "calls_reserved": before["calls_reserved"],
        }
        return outcome_valid and all(
            settlement[f"resulting_{key}"] == value and after[key] == value
            for key, value in expected_after.items()
        )

    def test_closed_schemas_register_document_and_context_semantics(self) -> None:
        self.assert_family_contract("budget", SCHEMAS, HELPERS)

    def test_every_fixture_is_full_sealed_and_classes_are_complete(self) -> None:
        classes = {schema_id: set() for schema_id in SCHEMAS}
        self.assertEqual(
            sorted(self.fixtures),
            [item["fixture_id"] for item in self.family["fixtures"]],
        )
        for fixture in self.family["fixtures"]:
            schema_id = fixture["schema_id"]
            classes[schema_id].add(fixture["fixture_class"])
            self.assertEqual(
                set(self.schemas[schema_id]["required"]),
                set(fixture["document"]),
                fixture["fixture_id"],
            )
            self.assertTrue(
                sealed(fixture["document"], self.schemas[schema_id]),
                fixture["fixture_id"],
            )
        for schema_id in SCHEMAS:
            self.assertTrue(
                {"golden", "idempotency", "negative"}.issubset(
                    classes[schema_id]
                )
            )
        self.assertEqual(
            {"elapsed-budget-ledger/v1", "elapsed-budget-reservation/v1"},
            {
                fixture["schema_id"]
                for fixture in self.family["fixtures"]
                if fixture["fixture_class"] == "crash"
            },
        )

    def test_begin_settle_and_exact_replay_are_executable(self) -> None:
        before = self.document("budget_golden_ledger_before")
        held = self.document("budget_golden_reservation")
        reserved = self.document("budget_golden_ledger_reserved")
        settlement = self.document("budget_golden_settlement")
        after = self.document("budget_golden_ledger_after_settlement")
        self.assertTrue(
            self.valid_transition(before, held, reserved, settlement, after)
        )
        pairs = (
            ("budget_golden_reservation", "budget_idempotency_reservation"),
            ("budget_golden_settlement", "budget_idempotency_settlement"),
            (
                "budget_golden_ledger_after_settlement",
                "budget_idempotency_ledger_after_settlement",
            ),
        )
        for golden, retry in pairs:
            self.assertEqual(
                canonical_bytes(self.document(golden)),
                canonical_bytes(self.document(retry)),
            )

    def test_abandon_and_fenced_recovery_release_both_reservations(self) -> None:
        before = self.document("budget_golden_ledger_before")
        held = self.document("budget_golden_reservation")
        reserved = self.document("budget_golden_ledger_reserved")
        abandonment = self.document("budget_golden_fenced_recovery_abandonment")
        after = self.document("budget_golden_ledger_after_abandonment")
        self.assertTrue(
            self.valid_transition(before, held, reserved, abandonment, after)
        )
        self.assertEqual(
            (0, 100, 0, 1, "abandoned"),
            (
                abandonment["consumed_ms"],
                abandonment["released_ms"],
                abandonment["consumed_calls"],
                abandonment["released_calls"],
                abandonment["outcome"],
            ),
        )

    def test_elapsed_and_call_limits_fail_independently(self) -> None:
        self.assertFalse(
            self.valid_ledger(self.document("budget_negative_ledger_elapsed_limit"))
        )
        self.assertFalse(
            self.valid_ledger(self.document("budget_negative_ledger_call_limit"))
        )
        reserved = self.document("budget_golden_ledger_reserved")
        settlement = self.document("budget_golden_settlement")
        after = self.document("budget_golden_ledger_after_settlement")
        cases = (
            (
                "budget_golden_ledger_elapsed_near_limit",
                "budget_negative_reservation_elapsed_limit",
            ),
            (
                "budget_golden_ledger_call_limit_reached",
                "budget_negative_reservation_call_limit",
            ),
        )
        for ledger_id, reservation_id in cases:
            self.assertFalse(
                self.valid_transition(
                    self.document(ledger_id),
                    self.document(reservation_id),
                    reserved,
                    settlement,
                    after,
                )
            )

    def test_crash_keeps_only_pending_reservation_state_for_cleanup(self) -> None:
        crash_reservation = self.fixtures["budget_crash_reservation"]
        crash_ledger = self.fixtures["budget_crash_reserved_ledger"]
        self.assertEqual("INVOCATION_PENDING", crash_reservation["expected_code"])
        self.assertEqual("INVOCATION_PENDING", crash_ledger["expected_code"])
        self.assertEqual(
            canonical_bytes(self.document("budget_golden_reservation")),
            canonical_bytes(crash_reservation["document"]),
        )
        self.assertEqual(
            canonical_bytes(self.document("budget_golden_ledger_reserved")),
            canonical_bytes(crash_ledger["document"]),
        )
        self.assertFalse(
            any(
                fixture["fixture_class"] == "crash"
                and fixture["schema_id"] == "elapsed-budget-settlement/v1"
                for fixture in self.family["fixtures"]
            )
        )

    def test_negative_settlement_breaks_conservation(self) -> None:
        self.assertFalse(
            self.valid_transition(
                self.document("budget_golden_ledger_before"),
                self.document("budget_golden_reservation"),
                self.document("budget_golden_ledger_reserved"),
                self.document("budget_negative_settlement_conservation"),
                self.document("budget_golden_ledger_after_settlement"),
            )
        )


if __name__ == "__main__":
    unittest.main()
