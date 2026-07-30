"""Python facade semantics for the atomic task runtime bootstrap."""

from __future__ import annotations


PYTHON_BOOTSTRAP = r'''
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
