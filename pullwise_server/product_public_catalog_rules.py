"""Pure validation/projection of a trusted public Billing plan catalog."""
from __future__ import annotations

import json

from .product_entitlement_rules import PLAN_ENTITLEMENTS


def catalog_payload(rows: list[dict], now: int) -> dict | None:
    if len(rows) != 1 or int(rows[0]["expires_at"]) <= now:
        return None
    try:
        saved = json.loads(rows[0]["payload_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("plans"), list):
        return None
    plans = []
    for record in saved["plans"]:
        if not isinstance(record, dict) or record.get("id") not in PLAN_ENTITLEMENTS:
            return None
        plan = {key: value for key, value in record.items()
                if key not in {"reviewLimit", "repositoryLimits", "agentConfig"}}
        plan["entitlements"] = dict(PLAN_ENTITLEMENTS[plan["id"]])
        plans.append(plan)
    if {plan["id"] for plan in plans} != set(PLAN_ENTITLEMENTS) or len(plans) != 3:
        return None
    payload = {key: value for key, value in saved.items()
               if key not in {"account", "agentConfigs"}}
    payload["plans"] = plans
    payload["page"] = {"id": "pricing",
        "checkoutAction": {"method": "POST", "href": "/billing/checkout-sessions"},
        "billingRoute": "/billing"}
    return payload
