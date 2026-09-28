"""Operator-configurable ledger allowances; provider prices are separate."""
from __future__ import annotations
import copy
import json
from decimal import Decimal, InvalidOperation

from .account_cycle_rules import effective_user_plan


_DEFAULT = {
    "free": {"projects": 3, "records": 500, "writesPerMinute": 10,
             "writesPerMonth": 1000, "jevMonthlyBudgetUsd": "0.00"},
    "pro": {"projects": 100, "records": 20000, "writesPerMinute": 60,
            "writesPerMonth": 10000, "jevMonthlyBudgetUsd": "0.00"},
    "max": {"projects": 100, "records": 20000, "writesPerMinute": 60,
            "writesPerMonth": 10000, "jevMonthlyBudgetUsd": "5.00"},
}
JEV_MODEL = "jev-1.13.0"
JEV_INPUT_MICROUSD_PER_MILLION = 42000
JEV_MAX_INPUT_TOKENS = 65536  # Conservative interpretation of documented 64k.
JEV_RESERVATION_MICROUSD = (JEV_INPUT_MICROUSD_PER_MILLION * JEV_MAX_INPUT_TOKENS + 999999) // 1000000


def default_policy():
    return copy.deepcopy(_DEFAULT)


def usd_micros(value):
    if not isinstance(value, str):
        raise ValueError("USD budget must be a decimal string")
    try:
        amount = Decimal(value)
        micros = amount * 1000000
        if not amount.is_finite() or amount < 0 or amount > 100 or micros != micros.to_integral_value():
            raise ValueError("invalid USD budget")
        return int(micros)
    except InvalidOperation:
        raise ValueError("invalid USD budget") from None


def parse_policy(raw=None):
    policy = default_policy()
    if raw is not None and raw != "":
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate policy key")
                result[key] = value
            return result
        overrides = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(overrides, dict) or set(overrides) - set(policy):
            raise ValueError("unknown plan")
        for plan, values in overrides.items():
            if not isinstance(values, dict) or set(values) - set(policy[plan]):
                raise ValueError("unknown allowance")
            policy[plan].update(values)
    for plan, values in policy.items():
        for key in ("projects", "records", "writesPerMinute", "writesPerMonth"):
            if type(values[key]) is not int or not 1 <= values[key] <= 1000000:
                raise ValueError("allowance must be a positive bounded integer")
        budget = usd_micros(values["jevMonthlyBudgetUsd"])
        if plan != "max" and budget:
            raise ValueError("Jev is Max-only")
    if policy["pro"]["records"] != policy["max"]["records"]:
        raise ValueError("Pro and Max record allowances must match")
    return policy


def entitlements(user, *, now, policy=None, jev_available=False):
    plan = effective_user_plan(user, timestamp=now)
    return plan_entitlements(plan, policy=policy, jev_available=jev_available)


def plan_entitlements(plan, *, policy=None, jev_available=False):
    values = (policy or default_policy())[plan]
    return {"plan": plan, "limits": {"projects": values["projects"],
        "expenseRecords": values["records"], "writesPerMinute": values["writesPerMinute"],
        "writesPerMonth": values["writesPerMonth"]},
        "jev": {"eligible": plan == "max", "available": plan == "max" and jev_available,
                "monthlyBudgetUsd": values["jevMonthlyBudgetUsd"],
                "period": "utc-calendar-month", "rollover": False}}
