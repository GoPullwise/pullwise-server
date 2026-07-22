"""Executable Python facade semantics for elapsed dispatch budgets."""

from __future__ import annotations


PYTHON_BUDGET = r'''
def _budget_exhausted(detail: str, path: str = "$") -> None:
    _fail(detail, path, code="BUDGET_EXHAUSTED")


def _rule_elapsed_budget_ledger(value: dict[str, object]) -> None:
    _verify_embedded_digest("elapsed-budget-ledger/v1", value)
    if value["consumed_ms"] + value["reserved_ms"] > value["elapsed_limit_ms"]:
        _budget_exhausted("BUDGET_ELAPSED_LIMIT_EXCEEDED")
    if (
        value["calls_consumed"] + value["calls_reserved"]
        > value["tool_call_limit"]
    ):
        _budget_exhausted("BUDGET_CALL_LIMIT_EXCEEDED")


def _rule_elapsed_budget_reservation(value: dict[str, object]) -> None:
    _verify_embedded_digest("elapsed-budget-reservation/v1", value)
    _require(
        _timestamp_millis(value["started_at"]) is not None,
        "BUDGET_RESERVATION_TIME_INVALID",
        "$.started_at",
    )
    totals = (
        value["previous_consumed_ms"]
        + value["previous_reserved_ms"]
        + value["reserved_ms"],
        value["previous_calls_consumed"]
        + value["previous_calls_reserved"]
        + value["reserved_calls"],
    )
    _require(
        all(item <= SAFE_INTEGER for item in totals),
        "BUDGET_RESERVATION_TOTAL_UNSAFE",
    )


def _rule_elapsed_budget_settlement(value: dict[str, object]) -> None:
    _verify_embedded_digest("elapsed-budget-settlement/v1", value)
    _require(
        value["consumed_calls"] + value["released_calls"] == 1,
        "BUDGET_CALL_CONSERVATION_INVALID",
    )
    if value["outcome"] == "settled":
        _require(
            value["consumed_calls"] == 1 and value["released_calls"] == 0,
            "BUDGET_SETTLED_CALL_INVALID",
        )
        _require(
            value["consumed_ms"] == value["elapsed_ms"],
            "BUDGET_SETTLED_ELAPSED_INVALID",
        )
    else:
        _require(
            value["elapsed_ms"] == 0
            and value["consumed_ms"] == 0
            and value["consumed_calls"] == 0
            and value["released_calls"] == 1,
            "BUDGET_ABANDONMENT_RELEASE_INVALID",
        )
    _require(
        value["consumed_ms"] + value["released_ms"] <= SAFE_INTEGER,
        "BUDGET_SETTLEMENT_TOTAL_UNSAFE",
    )
'''


__all__ = ["PYTHON_BUDGET"]
