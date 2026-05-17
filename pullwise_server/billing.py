from __future__ import annotations

import os
import hashlib
import hmac
import secrets
import time
from urllib.parse import urljoin

import requests


class BillingConfigurationError(RuntimeError):
    pass


class BillingProviderConflict(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: str = "false") -> bool:
    return env(name, default).strip().lower() in {"1", "true", "yes", "on"}


def stripe_configured() -> bool:
    return bool(env("PULLWISE_STRIPE_SECRET_KEY") and env("PULLWISE_STRIPE_PRICE_ID"))


def creem_configured() -> bool:
    return bool(env("PULLWISE_CREEM_API_KEY") and env("PULLWISE_CREEM_PRODUCT_ID"))


def selected_provider() -> str:
    configured = env("PULLWISE_BILLING_PROVIDER").strip().lower()
    if configured:
        if configured not in {"stripe", "creem"}:
            raise BillingConfigurationError("PULLWISE_BILLING_PROVIDER must be stripe or creem.")
        if configured == "stripe" and not stripe_configured():
            raise BillingConfigurationError("Stripe billing is selected but Stripe environment variables are incomplete.")
        if configured == "creem" and not creem_configured():
            raise BillingConfigurationError("Creem billing is selected but Creem environment variables are incomplete.")
        return configured

    stripe = stripe_configured()
    creem = creem_configured()
    if stripe and creem:
        raise BillingProviderConflict("Both Stripe and Creem are configured. Set PULLWISE_BILLING_PROVIDER to choose one.")
    if stripe:
        return "stripe"
    if creem:
        return "creem"
    return "disabled"


def public_plan() -> dict:
    provider = selected_provider()
    return {
        "provider": provider,
        "enabled": provider != "disabled",
        "name": env("PULLWISE_BILLING_PLAN_NAME", "Pullwise Pro"),
        "description": env("PULLWISE_BILLING_PLAN_DESCRIPTION", "Repository review for production teams."),
        "currency": env("PULLWISE_BILLING_CURRENCY", "USD"),
        "interval": env("PULLWISE_BILLING_INTERVAL", "month"),
        "amount": env("PULLWISE_BILLING_AMOUNT", ""),
    }


def default_success_url() -> str:
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/?screen=settings&billing=success"


def default_cancel_url() -> str:
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/?screen=settings&billing=cancel"


def create_checkout_session(
    user: dict,
    *,
    success_url: str | None = None,
    cancel_url: str | None = None,
) -> dict:
    provider = selected_provider()
    if provider == "stripe":
        return create_stripe_checkout_session(user, success_url=success_url, cancel_url=cancel_url)
    if provider == "creem":
        return create_creem_checkout_session(user, success_url=success_url or default_success_url())
    raise BillingConfigurationError("Billing is not configured.")


def create_stripe_checkout_session(user: dict, *, success_url: str | None, cancel_url: str | None) -> dict:
    data = {
        "mode": env("PULLWISE_STRIPE_CHECKOUT_MODE", "subscription"),
        "line_items[0][price]": env("PULLWISE_STRIPE_PRICE_ID"),
        "line_items[0][quantity]": "1",
        "success_url": success_url or default_success_url(),
        "cancel_url": cancel_url or default_cancel_url(),
        "client_reference_id": user["id"],
        "metadata[userId]": user["id"],
    }
    customer_id = (user.get("billing") or {}).get("customerId")
    if customer_id:
        data["customer"] = customer_id
    elif user.get("email"):
        data["customer_email"] = user["email"]

    response = requests.post(
        "https://api.stripe.com/v1/checkout/sessions",
        auth=(env("PULLWISE_STRIPE_SECRET_KEY"), ""),
        data=data,
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    checkout_url = payload.get("url")
    if not checkout_url:
        raise RuntimeError("Stripe did not return a Checkout URL.")
    return {
        "provider": "stripe",
        "id": payload.get("id"),
        "customerId": payload.get("customer") or customer_id,
        "url": checkout_url,
    }


def create_creem_checkout_session(user: dict, *, success_url: str) -> dict:
    request_id = f"pw_{user['id']}_{secrets.token_urlsafe(8)}"
    customer = {}
    if user.get("email"):
        customer["email"] = user["email"]
    payload = {
        "product_id": env("PULLWISE_CREEM_PRODUCT_ID"),
        "request_id": request_id,
        "units": 1,
        "success_url": success_url,
        "metadata": {"userId": user["id"]},
    }
    if customer:
        payload["customer"] = customer
    response = requests.post(
        urljoin(creem_api_base_url() + "/", "v1/checkouts"),
        headers={"x-api-key": env("PULLWISE_CREEM_API_KEY"), "Content-Type": "application/json"},
        json=payload,
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    checkout_url = payload.get("checkout_url") or payload.get("url")
    if not checkout_url:
        raise RuntimeError("Creem did not return a Checkout URL.")
    return {
        "provider": "creem",
        "id": payload.get("id"),
        "requestId": request_id,
        "url": checkout_url,
    }


def create_portal_session(user: dict, *, return_url: str | None = None) -> dict:
    provider = selected_provider()
    billing = user.get("billing") or {}
    customer_id = billing.get("customerId")
    if not customer_id:
        raise BillingConfigurationError("No billing customer is linked to this account yet.")
    if provider == "stripe":
        return create_stripe_portal_session(customer_id, return_url=return_url or default_cancel_url())
    if provider == "creem":
        return create_creem_portal_session(customer_id)
    raise BillingConfigurationError("Billing is not configured.")


def create_stripe_portal_session(customer_id: str, *, return_url: str) -> dict:
    response = requests.post(
        "https://api.stripe.com/v1/billing_portal/sessions",
        auth=(env("PULLWISE_STRIPE_SECRET_KEY"), ""),
        data={"customer": customer_id, "return_url": return_url},
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    portal_url = payload.get("url")
    if not portal_url:
        raise RuntimeError("Stripe did not return a portal URL.")
    return {"provider": "stripe", "id": payload.get("id"), "url": portal_url}


def create_creem_portal_session(customer_id: str) -> dict:
    response = requests.post(
        urljoin(creem_api_base_url() + "/", "v1/customers/billing"),
        headers={"x-api-key": env("PULLWISE_CREEM_API_KEY"), "Content-Type": "application/json"},
        json={"customer_id": customer_id},
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    portal_url = payload.get("customer_portal_link") or payload.get("url")
    if not portal_url:
        raise RuntimeError("Creem did not return a portal URL.")
    return {"provider": "creem", "url": portal_url}


def creem_api_base_url() -> str:
    return env(
        "PULLWISE_CREEM_API_BASE_URL",
        "https://test-api.creem.io" if env_flag("PULLWISE_CREEM_TEST_MODE") else "https://api.creem.io",
    ).rstrip("/")


def verify_creem_webhook(raw_body: bytes, signature: str | None) -> bool:
    secret = env("PULLWISE_CREEM_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return timing_safe_hex_equal(expected, signature.strip())


def verify_stripe_webhook(raw_body: bytes, signature_header: str | None) -> bool:
    secret = env("PULLWISE_STRIPE_WEBHOOK_SECRET")
    if not secret or not signature_header:
        return False
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key, []).append(value)
    timestamp = (parts.get("t") or [""])[0]
    if not timestamp or not (parts.get("v1") or []):
        return False
    try:
        if abs(int(time.time()) - int(timestamp)) > int(env("PULLWISE_STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300")):
            return False
    except ValueError:
        return False
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(timing_safe_hex_equal(expected, candidate) for candidate in parts.get("v1") or [])


def timing_safe_hex_equal(expected: str, actual: str) -> bool:
    try:
        return hmac.compare_digest(bytes.fromhex(expected), bytes.fromhex(actual))
    except ValueError:
        return False


def billing_update_from_creem_event(event: dict) -> dict | None:
    event_type = event.get("eventType") or event.get("type")
    obj = event.get("object") or {}
    if event_type not in {
        "checkout.completed",
        "subscription.active",
        "subscription.paid",
        "subscription.canceled",
        "subscription.scheduled_cancel",
        "subscription.past_due",
        "subscription.expired",
        "subscription.trialing",
        "subscription.paused",
        "subscription.update",
    }:
        return None

    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    customer = obj.get("customer") if isinstance(obj.get("customer"), dict) else {}
    subscription = obj.get("subscription") if isinstance(obj.get("subscription"), dict) else obj
    subscription_metadata = subscription.get("metadata") if isinstance(subscription.get("metadata"), dict) else {}
    user_id = (
        metadata.get("userId")
        or metadata.get("internal_customer_id")
        or subscription_metadata.get("userId")
        or subscription_metadata.get("internal_customer_id")
    )
    subscription_customer = subscription.get("customer") if isinstance(subscription, dict) else None
    if isinstance(subscription_customer, dict):
        subscription_customer_id = subscription_customer.get("id")
    else:
        subscription_customer_id = subscription_customer
    customer_id = customer.get("id") or subscription_customer_id
    if not user_id and not customer_id:
        return None

    return {
        "userId": user_id,
        "provider": "creem",
        "customerId": customer_id,
        "customerEmail": customer.get("email"),
        "subscriptionId": subscription.get("id") if isinstance(subscription, dict) else None,
        "status": normalize_subscription_status(subscription.get("status") if isinstance(subscription, dict) else "active"),
        "eventType": event_type,
        "eventId": event.get("id") or event.get("eventId"),
        "eventCreated": event_created(event),
    }


def billing_update_from_stripe_event(event: dict) -> dict | None:
    event_type = event.get("type") or ""
    obj = ((event.get("data") or {}).get("object") or {})
    if event_type == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("userId")
        if not user_id:
            return None
        return {
            "userId": user_id,
            "provider": "stripe",
            "customerId": obj.get("customer"),
            "customerEmail": (obj.get("customer_details") or {}).get("email") or obj.get("customer_email"),
            "subscriptionId": obj.get("subscription"),
            "status": "active",
            "eventType": event_type,
            "eventId": event.get("id"),
            "eventCreated": event_created(event),
        }
    if event_type.startswith("customer.subscription."):
        return {
            "provider": "stripe",
            "customerId": obj.get("customer"),
            "subscriptionId": obj.get("id"),
            "status": normalize_subscription_status(obj.get("status")),
            "eventType": event_type,
            "eventId": event.get("id"),
            "eventCreated": event_created(event),
        }
    return None


def event_created(event: dict) -> int | None:
    value = event.get("created") or event.get("createdAt")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def normalize_subscription_status(status: str | None) -> str:
    normalized = (status or "active").strip().lower()
    if normalized in {"active", "trialing", "paid"}:
        return "active"
    if normalized in {"scheduled_cancel"}:
        return "canceling"
    if normalized in {"past_due", "unpaid", "paused"}:
        return normalized
    if normalized in {"canceled", "cancelled", "expired", "incomplete_expired"}:
        return "canceled"
    return normalized or "active"
