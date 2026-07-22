from __future__ import annotations

from copy import deepcopy
import unittest

from tests.test_agent_first_budget_facades import (
    AgentFirstBudgetFacadesTest,
    canonical_bytes,
    seal,
)


class AgentFirstBudgetFacadesAdversarialTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        AgentFirstBudgetFacadesTest.setUpClass()
        cls.facade = AgentFirstBudgetFacadesTest(
            "test_golden_settlement_executes_through_both_public_facades"
        )

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.facade.fixtures[fixture_id]["document"])

    def test_every_source_fixture_executes_and_replays_are_exact(self) -> None:
        operations = [
            {
                "kind": "document",
                "schema_id": fixture["schema_id"],
                "args": [deepcopy(fixture["document"])],
            }
            for fixture in self.facade.family["fixtures"]
        ]

        results = self.facade.assert_parity(operations)

        for fixture, result in zip(self.facade.family["fixtures"], results):
            if fixture["fixture_id"] in {
                "budget_negative_ledger_call_limit",
                "budget_negative_ledger_elapsed_limit",
            }:
                self.assertEqual("BUDGET_EXHAUSTED", result["code"])
            else:
                self.assertTrue(result["ok"], fixture["fixture_id"])
        for golden, replay in (
            ("budget_golden_reservation", "budget_idempotency_reservation"),
            ("budget_golden_settlement", "budget_idempotency_settlement"),
            (
                "budget_golden_ledger_after_settlement",
                "budget_idempotency_ledger_after_settlement",
            ),
        ):
            self.assertEqual(
                canonical_bytes(self.document(golden)),
                canonical_bytes(self.document(replay)),
            )

    def test_contextual_negative_fixtures_fail_with_declared_codes(self) -> None:
        common = (
            self.document("budget_golden_ledger_reserved"),
            self.document("budget_golden_settlement"),
            self.document("budget_golden_ledger_after_settlement"),
        )
        operations = [
            {
                "kind": "transition",
                "args": [
                    self.document("budget_golden_ledger_elapsed_near_limit"),
                    self.document("budget_negative_reservation_elapsed_limit"),
                    *common,
                ],
            },
            {
                "kind": "transition",
                "args": [
                    self.document("budget_golden_ledger_call_limit_reached"),
                    self.document("budget_negative_reservation_call_limit"),
                    *common,
                ],
            },
            {
                "kind": "transition",
                "args": [
                    self.document("budget_golden_ledger_before"),
                    self.document("budget_golden_reservation"),
                    self.document("budget_golden_ledger_reserved"),
                    self.document("budget_negative_settlement_conservation"),
                    self.document("budget_golden_ledger_after_settlement"),
                ],
            },
        ]

        results = self.facade.assert_parity(operations)

        self.assertEqual(
            ["BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED", "CONTRACT_DOCUMENT_INVALID"],
            [item["code"] for item in results],
        )
        self.assertEqual(
            "BUDGET_ELAPSED_CONSERVATION_INVALID", results[2]["detail"]
        )

    def test_timestamp_and_context_drift_are_rejected_after_resealing(self) -> None:
        invalid_time = self.document("budget_golden_reservation")
        invalid_time["started_at"] = "2026-02-30T12:34:55.000Z"
        invalid_time = seal(
            self.facade.schemas["elapsed-budget-reservation/v1"], invalid_time
        )
        reserved_drift = self.document("budget_golden_ledger_reserved")
        reserved_drift["grant_digest"] = "9" * 64
        reserved_drift = seal(
            self.facade.schemas["elapsed-budget-ledger/v1"], reserved_drift
        )
        settlement_drift = self.document("budget_golden_settlement")
        settlement_drift["invocation_digest"] = "8" * 64
        settlement_drift = seal(
            self.facade.schemas["elapsed-budget-settlement/v1"], settlement_drift
        )
        before = self.document("budget_golden_ledger_before")
        reservation = self.document("budget_golden_reservation")
        reserved = self.document("budget_golden_ledger_reserved")
        settlement = self.document("budget_golden_settlement")
        after = self.document("budget_golden_ledger_after_settlement")

        results = self.facade.assert_parity(
            [
                {
                    "kind": "document",
                    "schema_id": "elapsed-budget-reservation/v1",
                    "args": [invalid_time],
                },
                {
                    "kind": "transition",
                    "args": [before, reservation, reserved_drift, settlement, after],
                },
                {
                    "kind": "transition",
                    "args": [before, reservation, reserved, settlement_drift, after],
                },
            ]
        )

        self.assertEqual(
            [
                "BUDGET_RESERVATION_TIME_INVALID",
                "BUDGET_RESERVED_LEDGER_MISMATCH",
                "BUDGET_SETTLEMENT_IDENTITY_MISMATCH",
            ],
            [item["detail"] for item in results],
        )


if __name__ == "__main__":
    unittest.main()
