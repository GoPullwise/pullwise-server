"""Resolve an omitted recurring category through the ordinary Jev save path.

Call only after current target authorization and creation replay checks. The
resolved category is persisted on the plan; later occurrences never invoke a
model or change submitted money, dates, purpose or target.
"""
from __future__ import annotations

from .account_cycle_rules import PAID_PLAN_IDS, effective_user_plan
from .cloudflare_jev_preferences import ensure_current_jev_authority
from .cloudflare_ledger_api import _error
from .cloudflare_ledger_expenses import _automatic_assistance


async def resolve_recurring_category(binding, headers, template, schedule, now,
                                     suggestion_gateway, user, proof):
    if template["category_id"] is not None:
        return template, None, None
    if effective_user_plan(user, timestamp=now) not in PAID_PLAN_IDS:
        return template, None, _error(422, "CATEGORY_REQUIRED")
    assistance = await _automatic_assistance(binding, headers,
        {**template, "occurred_on": schedule["startOn"]}, now,
        suggestion_gateway, None, user)
    await ensure_current_jev_authority(binding, headers, now, proof,
        scope="expenses:write", target_kind=template["target_kind"],
        project_id=template["project_id"])
    category_id = assistance["suggestions"].get("categoryId")
    if not category_id:
        return template, assistance, (422, {
            "error": {"code": "CATEGORY_REQUIRED"}, "assistance": assistance})
    # The suggestion workflow selects authorized active category IDs. Recheck
    # before the caller's atomic category guard to handle a concurrent archive.
    category = await binding.prepare("""SELECT id FROM expense_categories
        WHERE owner_id=? AND id=? AND archived_at IS NULL""").bind(
            user["id"], category_id).first()
    if category is None:
        return template, assistance, _error(422, "INVALID_CATEGORY")
    assistance["categorySource"] = "jev"
    return {**template, "category_id": category_id}, assistance, None
