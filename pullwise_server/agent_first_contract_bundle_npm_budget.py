"""Executable npm facade semantics for elapsed dispatch budgets."""

from __future__ import annotations


NPM_BUDGET = r'''
function budgetError(detail, path = "$", code = null) {
  throw new ContractValidationError(
    publicErrorCode(detail, code), detail, path,
  );
}

function validBudgetTimestamp(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$/.exec(value);
  if (!match) return false;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) {
    return false;
  }
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= days[month - 1];
}

function ruleElapsedBudgetLedger(value) {
  verifyEmbeddedDigestSync("elapsed-budget-ledger/v1", value);
  if (value.consumed_ms + value.reserved_ms > value.elapsed_limit_ms) {
    budgetError("BUDGET_ELAPSED_LIMIT_EXCEEDED", "$", "BUDGET_EXHAUSTED");
  }
  if (value.calls_consumed + value.calls_reserved > value.tool_call_limit) {
    budgetError("BUDGET_CALL_LIMIT_EXCEEDED", "$", "BUDGET_EXHAUSTED");
  }
}

function ruleElapsedBudgetReservation(value) {
  verifyEmbeddedDigestSync("elapsed-budget-reservation/v1", value);
  if (!validBudgetTimestamp(value.started_at)) {
    budgetError("BUDGET_RESERVATION_TIME_INVALID", "$.started_at");
  }
  const elapsedTotal = value.previous_consumed_ms +
    value.previous_reserved_ms + value.reserved_ms;
  const callTotal = value.previous_calls_consumed +
    value.previous_calls_reserved + value.reserved_calls;
  if (!Number.isSafeInteger(elapsedTotal) || !Number.isSafeInteger(callTotal)) {
    budgetError("BUDGET_RESERVATION_TOTAL_UNSAFE");
  }
}

function ruleElapsedBudgetSettlement(value) {
  verifyEmbeddedDigestSync("elapsed-budget-settlement/v1", value);
  if (value.consumed_calls + value.released_calls !== 1) {
    budgetError("BUDGET_CALL_CONSERVATION_INVALID");
  }
  if (value.outcome === "settled") {
    if (value.consumed_calls !== 1 || value.released_calls !== 0) {
      budgetError("BUDGET_SETTLED_CALL_INVALID");
    }
    if (value.consumed_ms !== value.elapsed_ms) {
      budgetError("BUDGET_SETTLED_ELAPSED_INVALID");
    }
  } else if (value.elapsed_ms !== 0 || value.consumed_ms !== 0 ||
             value.consumed_calls !== 0 || value.released_calls !== 1) {
    budgetError("BUDGET_ABANDONMENT_RELEASE_INVALID");
  }
  if (!Number.isSafeInteger(value.consumed_ms + value.released_ms)) {
    budgetError("BUDGET_SETTLEMENT_TOTAL_UNSAFE");
  }
}
'''


__all__ = ["NPM_BUDGET"]
