"""Session-only Creem mutations; webhook facts remain authoritative and separate."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .cloudflare_github_identity_http import _session_user, _redirect, _write_user, _user
from .cloudflare_creem_gateway import CreemRequestRejected
from .cloudflare_principal import _header

PAID = {"pro": 1, "max": 2}
PATHS = {"/billing/checkout-sessions", "/billing/change-interval",
         "/billing/cancel-subscription", "/billing/resume-subscription"}


def _product(products: dict, plan: str, interval: str) -> str:
    group = products.get(plan)
    value = group.get(interval) if isinstance(group, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{3,128}", value) else ""


def _provider_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid Creem redirect")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"checkout.creem.io", "test-checkout.creem.io"}:
        raise ValueError("invalid Creem redirect")
    return value


def _subscription_result(result: dict, billing: dict, *, plan: str, interval: str) -> tuple[dict, dict]:
    if not isinstance(result, dict) or result.get("id") != billing.get("subscriptionId"):
        raise ValueError("Creem subscription identity mismatch")
    raw_status = result.get("status")
    status = "canceling" if raw_status == "scheduled_cancel" else raw_status
    if status not in {"active", "trialing", "canceling"}:
        raise ValueError("Creem subscription status invalid")
    next_billing = {**billing, "provider": "creem", "plan": plan, "interval": interval,
                    "status": status, "cancelAtPeriodEnd": status == "canceling"}
    return next_billing, {"provider": "creem", "plan": plan, "interval": interval,
                          "subscriptionId": billing["subscriptionId"], "status": status,
                          "cancelAtPeriodEnd": status == "canceling"}


async def handle_billing_mutation(*, binding: Any, gateway: Any, now: int,
                                  method: str, path: str, headers: Mapping[str, object],
                                  body: object, app_url: str, trusted_origins: set[str],
                                  products: dict):
    if path not in PATHS:
        return None
    if method != "POST":
        return 405, {"error": {"code": "METHOD_NOT_ALLOWED"}}
    session, user = await _session_user(binding, headers, now)
    if not session:
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    origin = _header(headers, "Origin") or _header(headers, "Referer")
    parsed = urlsplit(origin)
    if f"{parsed.scheme}://{parsed.netloc}" not in trusted_origins:
        return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}
    if not isinstance(body, dict) or not isinstance(products, dict):
        return 422, {"error": {"code": "INVALID_REQUEST"}}
    if any(name in body and not isinstance(body[name], str)
           for name in ("plan", "interval", "mode")):
        return 422, {"error": {"code": "INVALID_REQUEST"}}
    billing = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    plan = billing.get("plan") or "free"
    interval = billing.get("interval") or "month"

    if path == "/billing/checkout-sessions":
        if any(name in body and not isinstance(body[name], str)
               for name in ("successUrl", "cancelUrl")):
            return 422, {"error": {"code": "INVALID_REQUEST"}}
        target_plan = body.get("plan") or "pro"
        target_interval = body.get("interval") or "month"
        if target_plan not in PAID or target_interval not in {"month", "year"}:
            return 422, {"error": {"code": "INVALID_PLAN"}}
        if plan in PAID and billing.get("status") in {"active", "trialing", "canceling"}:
            return 409, {"error": {"code": "PAID_SUBSCRIPTION_EXISTS"}}
        product_id = _product(products, target_plan, target_interval)
        if not product_id:
            return 503, {"error": {"code": "BILLING_NOT_CONFIGURED"}}
        success_url = _redirect(body.get("successUrl", ""), app_url, "/settings?billing=success")
        cancel_url = _redirect(body.get("cancelUrl", ""), app_url, "/settings?billing=cancel")
        source = f"{user['id']}\0{product_id}\0{now // 600}"
        request_id = "pw_checkout_" + hashlib.sha256(source.encode()).hexdigest()[:32]
        payload = {"product_id": product_id, "request_id": request_id, "units": 1,
                   "success_url": success_url, "metadata": {"userId": user["id"],
                   "plan": target_plan, "interval": target_interval}}
        # Creem does not accept a cancel_url field; the return target stays Web-side.
        if isinstance(user.get("email"), str) and user["email"]:
            payload["customer"] = {"email": user["email"]}
        checkout = await gateway.post("v1/checkouts", payload)
        if not isinstance(checkout, dict) or not isinstance(checkout.get("id"), str):
            raise ValueError("invalid Creem checkout")
        url = _provider_url(checkout.get("checkout_url"))
        next_user = {**user, "billingCheckout": {"provider": "creem", "id": checkout["id"],
                      "requestId": request_id, "plan": target_plan,
                      "interval": target_interval, "createdAt": now}}
        await _write_user(binding, next_user, now, expected_user=user)
        return 200, {"provider": "creem", "plan": target_plan,
                     "interval": target_interval, "id": checkout["id"],
                     "requestId": request_id, "url": url, "cancelUrl": cancel_url}

    subscription_id = billing.get("subscriptionId")
    if (not isinstance(subscription_id, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", subscription_id) or plan not in PAID):
        return 409, {"error": {"code": "NO_PAID_SUBSCRIPTION"}}
    status = billing.get("status")
    change = user.get("billingChange") if isinstance(user.get("billingChange"), dict) else {}
    if change and change.get("subscriptionId") == subscription_id:
        if (path == "/billing/change-interval" and
                (body.get("plan") or plan) == change.get("plan") and
                (body.get("interval") or "year") == change.get("interval")):
            return 200, {"provider": "creem", "plan": change["plan"],
                         "interval": change["interval"], "pending": True,
                         "subscriptionId": subscription_id}
        return 409, {"error": {"code": "SUBSCRIPTION_CHANGE_PENDING"}}
    if path == "/billing/cancel-subscription":
        if body.get("mode", "scheduled") != "scheduled":
            return 422, {"error": {"code": "INVALID_CANCEL_MODE"}}
        if status == "canceling":
            return 200, {"provider": "creem", "alreadyScheduled": True, "status": "canceling"}
        if status not in {"active", "trialing"}:
            return 409, {"error": {"code": "SUBSCRIPTION_NOT_ACTIVE"}}
        result = await gateway.post(f"v1/subscriptions/{subscription_id}/cancel", {"mode": "scheduled"})
    elif path == "/billing/resume-subscription":
        if status in {"active", "trialing"}:
            return 200, {"provider": "creem", "alreadyActive": True, "status": status}
        if status != "canceling":
            return 409, {"error": {"code": "SUBSCRIPTION_NOT_CANCELING"}}
        result = await gateway.post(f"v1/subscriptions/{subscription_id}/resume", {})
    else:
        target_plan = body.get("plan") or plan
        target_interval = body.get("interval") or "year"
        if target_plan not in PAID or target_interval not in {"month", "year"}:
            return 422, {"error": {"code": "INVALID_PLAN"}}
        if target_plan == plan and target_interval == interval:
            return 200, {"provider": "creem", "alreadyActive": True,
                         "plan": plan, "interval": interval}
        upgrade = (PAID[target_plan] > PAID[plan] and not (interval == "year" and target_interval == "month")) or (
            PAID[target_plan] == PAID[plan] and interval == "month" and target_interval == "year")
        if not upgrade or status not in {"active", "trialing"}:
            return 409, {"error": {"code": "UPGRADE_NOT_ALLOWED"}}
        product_id = _product(products, target_plan, target_interval)
        if not product_id:
            return 503, {"error": {"code": "BILLING_NOT_CONFIGURED"}}
        # Claim the change before sending a potentially chargeable provider request.
        # A lost response remains pending; retrying it must not charge a second time.
        claimed_user = {**user, "billingChange": {"plan": target_plan,
            "interval": target_interval, "subscriptionId": subscription_id,
            "requestedAt": now}}
        await _write_user(binding, claimed_user, now, expected_user=user)
        try:
            result = await gateway.post(f"v1/subscriptions/{subscription_id}/upgrade",
                {"product_id": product_id, "update_behavior": "proration-charge-immediately"})
        except CreemRequestRejected:
            # Clear only our rejected claim, preserving any concurrent identity/webhook update.
            latest = await _user(binding, user["id"])
            if latest and latest.get("billingChange") == claimed_user["billingChange"]:
                restored = {key: value for key, value in latest.items() if key != "billingChange"}
                await _write_user(binding, restored, now, expected_user=latest)
            raise
        _, public = _subscription_result(result, billing, plan=target_plan, interval=target_interval)
        return 200, {**public, "pending": True}
    next_billing, public = _subscription_result(result, billing, plan=plan, interval=interval)
    await _write_user(binding, {**user, "billing": next_billing}, now, expected_user=user)
    return 200, public
