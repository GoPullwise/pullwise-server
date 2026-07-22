"""Node facade result/debug/transport semantic fragments."""

from __future__ import annotations

from .agent_first_contract_bundle_npm_result_context import NPM_RESULT_CONTEXT
from .agent_first_contract_bundle_npm_result_rules import NPM_RESULT_RULES


NPM_RESULT = "\n".join((NPM_RESULT_RULES, NPM_RESULT_CONTEXT))


__all__ = ["NPM_RESULT"]
