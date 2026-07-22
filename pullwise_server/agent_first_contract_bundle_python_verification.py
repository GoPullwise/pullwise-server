"""Combined Python verification rules and contextual checks."""

from __future__ import annotations

from .agent_first_contract_bundle_python_verification_context import (
    PYTHON_VERIFICATION_CONTEXT,
)
from .agent_first_contract_bundle_python_verification_rules import (
    PYTHON_VERIFICATION_RULES,
)


PYTHON_VERIFICATION = PYTHON_VERIFICATION_RULES + "\n" + PYTHON_VERIFICATION_CONTEXT


__all__ = ["PYTHON_VERIFICATION"]
