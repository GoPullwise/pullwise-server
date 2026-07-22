"""Dispatch declared document semantics in generated Python wrappers."""

from __future__ import annotations


PYTHON_DISPATCH = r'''
_DOCUMENT_RULE_HANDLERS = {
    "acceptance_source_ids_unique": _rule_task_request_acceptance_sources,
    "actor": _rule_actor,
    "capability_and_delivery_sets_sorted_unique": _rule_task_request_sets,
    "utf8_nfc_byte_limits": _utf8_fields,
}


def _validate_semantics(
    schema_id: str, value: dict[str, object]
) -> None:
    semantics = schema(schema_id).get("x-pullwise-semantics")
    if semantics is None:
        return
    if not isinstance(semantics, dict) or set(semantics) != {
        "document_rules",
        "contextual_helpers",
    }:
        _fail("CONTRACT_SEMANTICS_INVALID", schema_id)
    rules = semantics["document_rules"]
    helpers = semantics["contextual_helpers"]
    if (
        not isinstance(rules, list)
        or not rules
        or not all(isinstance(item, str) for item in rules)
        or len(rules) != len(set(rules))
        or rules != sorted(rules)
        or not isinstance(helpers, list)
        or not all(isinstance(item, str) for item in helpers)
        or len(helpers) != len(set(helpers))
        or helpers != sorted(helpers)
    ):
        _fail("CONTRACT_SEMANTICS_INVALID", schema_id)
    for rule_id in rules:
        handler = _DOCUMENT_RULE_HANDLERS.get(rule_id)
        if handler is None:
            _fail("CONTRACT_SEMANTIC_RULE_UNIMPLEMENTED", rule_id)
        handler(value)
'''


__all__ = ["PYTHON_DISPATCH"]
