"""Python facade aggregation for TaskResult and debug transport."""

from __future__ import annotations

from .agent_first_contract_bundle_python_result_context import (
    PYTHON_RESULT_CONTEXT,
)
from .agent_first_contract_bundle_python_result_rules import (
    PYTHON_RESULT_RULES,
)


PYTHON_RESULT = "\n".join((PYTHON_RESULT_RULES, PYTHON_RESULT_CONTEXT))


__all__ = ["PYTHON_RESULT"]
