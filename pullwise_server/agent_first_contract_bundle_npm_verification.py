"""Combined Node verification rules and contextual checks."""

from __future__ import annotations

from .agent_first_contract_bundle_npm_verification_context import NPM_VERIFICATION_CONTEXT
from .agent_first_contract_bundle_npm_verification_rules import NPM_VERIFICATION_RULES


NPM_VERIFICATION = NPM_VERIFICATION_RULES + "\n" + NPM_VERIFICATION_CONTEXT


__all__ = ["NPM_VERIFICATION"]
