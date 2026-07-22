"""Generated Python facade semantic validators."""

from __future__ import annotations


PYTHON_SEMANTICS = r'''
def _validate_semantics(schema_id: str, value: dict[str, object]) -> None:
    if schema_id == "source-content/v1":
        try:
            raw = base64.b64decode(value["data_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractValidationError("SOURCE_CONTENT_BASE64_INVALID: $.data_base64") from exc
        if base64.b64encode(raw).decode("ascii") != value["data_base64"]:
            _fail("SOURCE_CONTENT_BASE64_NONCANONICAL", "$.data_base64")
        if len(raw) != value["size_bytes"]:
            _fail("SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes")
        if hashlib.sha256(raw).hexdigest() != value["byte_sha256"]:
            _fail("SOURCE_CONTENT_SHA256_MISMATCH", "$.byte_sha256")
    elif schema_id == "elapsed-budget-ledger/v1":
        if value["consumed_ms"] + value["reserved_ms"] > value["elapsed_limit_ms"]:
            _fail("BUDGET_ELAPSED_LIMIT_EXCEEDED")
        if value["calls_consumed"] + value["calls_reserved"] > value["tool_call_limit"]:
            _fail("BUDGET_CALL_LIMIT_EXCEEDED")
    elif schema_id == "elapsed-budget-settlement/v1":
        if value["consumed_calls"] + value["released_calls"] != 1:
            _fail("BUDGET_CALL_CONSERVATION_INVALID")
        if value["consumed_ms"] != value["elapsed_ms"]:
            _fail("BUDGET_ELAPSED_CONSUMPTION_INVALID")


def verify_budget_transition(
    previous_ledger: object,
    reservation: object,
    settlement: object,
    resulting_ledger: object,
) -> bool:
    before = verify_document_digest("elapsed-budget-ledger/v1", previous_ledger)
    held = verify_document_digest("elapsed-budget-reservation/v1", reservation)
    settled = verify_document_digest("elapsed-budget-settlement/v1", settlement)
    after = verify_document_digest("elapsed-budget-ledger/v1", resulting_ledger)
    if held["task_id"] != before["task_id"]:
        _fail("BUDGET_TASK_MISMATCH")
    previous_fields = (
        ("previous_consumed_ms", "consumed_ms"),
        ("previous_reserved_ms", "reserved_ms"),
        ("previous_calls_consumed", "calls_consumed"),
        ("previous_calls_reserved", "calls_reserved"),
    )
    if any(held[left] != before[right] for left, right in previous_fields):
        _fail("BUDGET_PREVIOUS_STATE_MISMATCH")
    if before["consumed_ms"] + before["reserved_ms"] + held["reserved_ms"] > before["elapsed_limit_ms"]:
        _fail("BUDGET_ELAPSED_LIMIT_EXCEEDED")
    if before["calls_consumed"] + before["calls_reserved"] + held["reserved_calls"] > before["tool_call_limit"]:
        _fail("BUDGET_CALL_LIMIT_EXCEEDED")
    if settled["reservation_id"] != held["reservation_id"] or settled["invocation_digest"] != held["invocation_digest"]:
        _fail("BUDGET_SETTLEMENT_IDENTITY_MISMATCH")
    if settled["consumed_ms"] + settled["released_ms"] != held["reserved_ms"]:
        _fail("BUDGET_ELAPSED_CONSERVATION_INVALID")
    if settled["consumed_calls"] + settled["released_calls"] != held["reserved_calls"]:
        _fail("BUDGET_CALL_CONSERVATION_INVALID")
    expected = {
        "resulting_consumed_ms": before["consumed_ms"] + settled["consumed_ms"],
        "resulting_reserved_ms": before["reserved_ms"],
        "resulting_calls_consumed": before["calls_consumed"] + settled["consumed_calls"],
        "resulting_calls_reserved": before["calls_reserved"],
    }
    if any(settled[key] != item for key, item in expected.items()):
        _fail("BUDGET_RESULTING_STATE_MISMATCH")
    after_expected = {
        "task_id": before["task_id"],
        "grant_digest": before["grant_digest"],
        "elapsed_limit_ms": before["elapsed_limit_ms"],
        "tool_call_limit": before["tool_call_limit"],
        "consumed_ms": expected["resulting_consumed_ms"],
        "reserved_ms": expected["resulting_reserved_ms"],
        "calls_consumed": expected["resulting_calls_consumed"],
        "calls_reserved": expected["resulting_calls_reserved"],
    }
    if any(after[key] != item for key, item in after_expected.items()):
        _fail("BUDGET_RESULTING_LEDGER_MISMATCH")
    return True
'''


__all__ = ["PYTHON_SEMANTICS"]
