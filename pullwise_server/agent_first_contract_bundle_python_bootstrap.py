"""Python facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


PYTHON_BOOTSTRAP = r'''
def _rule_agent_task_accept_request(value: dict[str, object]) -> None:
    request = value["task_request"]
    validate_effective_policy_derivation(request, value["effective_policy"])
    ledger = verify_document_digest(
        "requirement-ledger/v1", value["requirement_ledger"]
    )
    _require(
        ledger["task_id"] == request["task_id"],
        "ACCEPT_REQUEST_TASK_BINDING_MISMATCH",
        "$.requirement_ledger.task_id",
    )


def _rule_agent_task_runtime_bootstrap(value: dict[str, object]) -> None:
    authority = value["authority"]
    roots = value["construction_roots"]
    task = roots["task_record"]
    attempt = roots["attempt"]
    owner = roots["owner"]
    same_generation = (
        authority["owner_epoch"] == task["owner_epoch"] == owner["owner_epoch"]
        and authority["native_epoch"]
        == task["native_epoch"]
        == attempt["native_epoch"]
        == owner["native_epoch"]
    )
    _require(
        same_generation,
        "BOOTSTRAP_GENERATION_MISMATCH",
        "$.construction_roots",
    )
'''


__all__ = ["PYTHON_BOOTSTRAP"]
