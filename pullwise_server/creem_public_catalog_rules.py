"""Verify configured Creem product facts before projecting public plan prices."""
from __future__ import annotations

import re
from decimal import Decimal



def _price(product: dict, interval: str) -> dict:
    cents = product["price"]
    amount = format((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")), "f")
    amount = amount.rstrip("0").rstrip(".") if "." in amount else amount
    return {"amount": amount, "currency": product["currency"].upper(),
        "interval": interval, "configured": True,
        "productId": product["id"], "billingPeriod": product["billing_period"]}


def _unconfigured(interval: str, currency: str) -> dict:
    return {"amount": None, "currency": currency, "interval": interval,
        "configured": False, "productId": None, "billingPeriod": None}


def verified_public_catalog(configured_ids: dict,
                            fetched_products: dict) -> dict:
    if (not isinstance(configured_ids, dict) or not isinstance(fetched_products, dict)
            or set(configured_ids) != {"pro", "max"}):
        raise ValueError("invalid configured Creem catalog")
    found = {"pro": {}, "max": {}}
    all_ids = set()
    currency = None
    for plan in ("pro", "max"):
        ids = configured_ids[plan]
        if (not isinstance(ids, dict) or set(ids) - {"month", "year"} or any(
                not isinstance(value, str) or not value for value in ids.values())):
            raise ValueError("invalid configured Creem product IDs")
        for configured_interval, product_id in ids.items():
            if product_id in all_ids:
                raise ValueError("duplicate Creem product binding")
            all_ids.add(product_id)
            product = fetched_products.get(product_id)
            if (not isinstance(product, dict) or product.get("id") != product_id
                    or product.get("status") != "active"
                    or product.get("billing_type") != "recurring"
                    or type(product.get("price")) is not int or product["price"] <= 0):
                raise ValueError("unverified Creem product")
            period = product.get("billing_period")
            interval = {"every-month": "month", "every-year": "year"}.get(period)
            product_currency = product.get("currency")
            if (interval is None or interval != configured_interval or interval in found[plan]
                    or not isinstance(product_currency, str)
                    or not re.fullmatch(r"[A-Za-z]{3}", product_currency)):
                raise ValueError("invalid Creem price period or currency")
            product = {**product, "currency": product_currency.upper()}
            if currency is not None and product["currency"] != currency:
                raise ValueError("conflicting Creem currencies")
            currency = product["currency"]
            found[plan][interval] = product
    currency = currency or "USD"
    plans = [{"id": "free", "name": "Free",
        "description": "Record project and shared expenses.",
        "currency": currency, "entitlements": None,
        "prices": {"month": {"amount": "0", "currency": currency,
                             "interval": "month", "configured": True}}}]
    for plan, title in (("pro", "Pullwise Pro"), ("max", "Pullwise Max")):
        products = found[plan]
        product = products.get("month") or products.get("year") or {}
        plans.append({"id": plan, "name": title,
            "description": "Pullwise project expense ledger subscription.",
            "currency": currency, "entitlements": None,
            "prices": {interval: (_price(products[interval], interval)
                                   if interval in products else _unconfigured(interval, currency))
                       for interval in ("month", "year")}})
    return {"provider": "creem" if all_ids else "disabled",
        "enabled": bool(all_ids), "currency": currency, "plans": plans,
        "name": plans[1]["name"], "description": plans[1]["description"],
        "interval": "month", "amount": plans[1]["prices"]["month"]["amount"]}
