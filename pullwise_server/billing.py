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


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def billing_currency() -> str:
    return env("PULLWISE_BILLING_CURRENCY", "USD").upper()


def review_limit(plan: str) -> int:
    if plan == "pro":
        return max(0, env_int("PULLWISE_PRO_REVIEW_LIMIT", 100))
    return max(0, env_int("PULLWISE_FREE_REVIEW_LIMIT", 5))


def pro_amount(interval: str) -> str:
    if interval == "year":
        return env("PULLWISE_PRO_YEARLY_AMOUNT", "290")
    return env("PULLWISE_PRO_MONTHLY_AMOUNT", env("PULLWISE_BILLING_AMOUNT", "29"))


def stripe_price_id(interval: str) -> str:
    if interval == "year":
        return env("PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID", env("PULLWISE_STRIPE_YEARLY_PRICE_ID"))
    return env("PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID", env("PULLWISE_STRIPE_PRICE_ID"))


def creem_product_id(interval: str) -> str:
    if interval == "year":
        return env("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID", env("PULLWISE_CREEM_YEARLY_PRODUCT_ID"))
    return env("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID", env("PULLWISE_CREEM_PRODUCT_ID"))


def stripe_configured() -> bool:
    return bool(env("PULLWISE_STRIPE_SECRET_KEY") and (stripe_price_id("month") or stripe_price_id("year")))


def creem_configured() -> bool:
    return bool(env("PULLWISE_CREEM_API_KEY") and (creem_product_id("month") or creem_product_id("year")))


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
    currency = billing_currency()
    pro_name = env("PULLWISE_BILLING_PLAN_NAME", "Pullwise Pro")
    pro_description = env("PULLWISE_BILLING_PLAN_DESCRIPTION", "Repository review for production teams.")
    pro_prices = {
        "month": {
            "amount": pro_amount("month"),
            "currency": currency,
            "interval": "month",
            "configured": provider_price_configured(provider, "month"),
        },
        "year": {
            "amount": pro_amount("year"),
            "currency": currency,
            "interval": "year",
            "configured": provider_price_configured(provider, "year"),
        },
    }
    return {
        "provider": provider,
        "enabled": provider != "disabled",
        "currency": currency,
        "name": pro_name,
        "description": pro_description,
        "interval": "month",
        "amount": pro_prices["month"]["amount"],
        "plans": [
            {
                "id": "free",
                "name": "Free",
                "description": "Try Pullwise with a small monthly review allowance.",
                "currency": currency,
                "reviewLimit": review_limit("free"),
                "prices": {
                    "month": {
                        "amount": "0",
                        "currency": currency,
                        "interval": "month",
                        "configured": True,
                    }
                },
            },
            {
                "id": "pro",
                "name": pro_name,
                "description": pro_description,
                "currency": currency,
                "reviewLimit": review_limit("pro"),
                "prices": pro_prices,
            },
        ],
    }


def provider_price_configured(provider: str, interval: str) -> bool:
    if provider == "stripe":
        return bool(stripe_price_id(interval))
    if provider == "creem":
        return bool(creem_product_id(interval))
    return False


def default_success_url() -> str:
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/?screen=settings&billing=success"


def default_cancel_url() -> str:
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/?screen=settings&billing=cancel"


def create_checkout_session(
    user: dict,
    *,
    success_url: str | None = None,
    cancel_url: str | None = None,
    plan: str = "pro",
    interval: str = "month",
) -> dict:
    plan, interval = validate_checkout_selection(plan, interval)
    provider = selected_provider()
    if provider == "stripe":
        return create_stripe_checkout_session(user, success_url=success_url, cancel_url=cancel_url, plan=plan, interval=interval)
    if provider == "creem":
        return create_creem_checkout_session(user, success_url=success_url or default_success_url(), plan=plan, interval=interval)
    raise BillingConfigurationError("Billing is not configured.")


def validate_checkout_selection(plan: str, interval: str) -> tuple[str, str]:
    normalized_plan = (plan or "pro").strip().lower()
    normalized_interval = (interval or "month").strip().lower()
    if normalized_plan != "pro":
        raise BillingConfigurationError("Only the Pro plan can be purchased.")
    if normalized_interval not in {"month", "year"}:
        raise BillingConfigurationError("Billing interval must be month or year.")
    return normalized_plan, normalized_interval


def create_stripe_checkout_session(user: dict, *, success_url: str | None, cancel_url: str | None, plan: str, interval: str) -> dict:
    price_id = stripe_price_id(interval)
    if not price_id:
        raise BillingConfigurationError(f"Stripe Pro {interval} price is not configured.")
    data = {
        "mode": env("PULLWISE_STRIPE_CHECKOUT_MODE", "subscription"),
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url or default_success_url(),
        "cancel_url": cancel_url or default_cancel_url(),
        "client_reference_id": user["id"],
        "metadata[userId]": user["id"],
        "metadata[plan]": plan,
        "metadata[interval]": interval,
        "subscription_data[metadata][userId]": user["id"],
        "subscription_data[metadata][plan]": plan,
        "subscription_data[metadata][interval]": interval,
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
        "plan": plan,
        "interval": interval,
        "id": payload.get("id"),
        "customerId": payload.get("customer") or customer_id,
        "url": checkout_url,
    }


def create_creem_checkout_session(user: dict, *, success_url: str, plan: str, interval: str) -> dict:
    product_id = creem_product_id(interval)
    if not product_id:
        raise BillingConfigurationError(f"Creem Pro {interval} product is not configured.")
    request_id = f"pw_{user['id']}_{secrets.token_urlsafe(8)}"
    customer = {}
    if user.get("email"):
        customer["email"] = user["email"]
    payload = {
        "product_id": product_id,
        "request_id": request_id,
        "units": 1,
        "success_url": success_url,
        "metadata": {"userId": user["id"], "plan": plan, "interval": interval},
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
        "plan": plan,
        "interval": interval,
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


def change_subscription_interval(user: dict, *, interval: str, return_url: str | None = None) -> dict:
    target_interval = (interval or "").strip().lower()
    if target_interval != "year":
        raise BillingConfigurationError("Only switching Pro monthly subscriptions to yearly is supported.")
    billing = user.get("billing") or {}
    if (billing.get("plan") or "pro") != "pro":
        raise BillingConfigurationError("Only Pro subscriptions can be changed.")
    if billing.get("interval") == "year":
        return {"provider": billing.get("provider") or selected_provider(), "plan": "pro", "interval": "year", "alreadyActive": True}
    if billing.get("interval") not in {None, "", "month"}:
        raise BillingConfigurationError("Only monthly Pro subscriptions can switch to yearly.")
    if (billing.get("status") or "").lower() not in {"active", "trialing"}:
        raise BillingConfigurationError("Only active subscriptions can switch to yearly.")

    provider = selected_provider()
    billing_provider = (billing.get("provider") or provider).lower()
    if billing_provider and billing_provider != provider:
        raise BillingConfigurationError("Configured billing provider does not match this subscription.")
    if provider == "stripe":
        return create_stripe_interval_change_session(billing, return_url=return_url or default_success_url())
    if provider == "creem":
        return create_creem_interval_change(billing)
    raise BillingConfigurationError("Billing is not configured.")


def create_stripe_interval_change_session(billing: dict, *, return_url: str) -> dict:
    customer_id = billing.get("customerId")
    subscription_id = billing.get("subscriptionId")
    subscription_item_id = billing.get("subscriptionItemId")
    yearly_price = stripe_price_id("year")
    if not customer_id or not subscription_id:
        raise BillingConfigurationError("No active Stripe subscription is linked to this account.")
    if not yearly_price:
        raise BillingConfigurationError("Stripe Pro yearly price is not configured.")
    if not subscription_item_id:
        subscription_item_id = fetch_stripe_subscription_item_id(subscription_id)
    if not subscription_item_id:
        raise BillingConfigurationError("Stripe subscription item is unavailable.")

    data = {
        "customer": customer_id,
        "return_url": return_url,
        "flow_data[type]": "subscription_update_confirm",
        "flow_data[after_completion][type]": "redirect",
        "flow_data[after_completion][redirect][return_url]": return_url,
        "flow_data[subscription_update_confirm][subscription]": subscription_id,
        "flow_data[subscription_update_confirm][items][0][id]": subscription_item_id,
        "flow_data[subscription_update_confirm][items][0][price]": yearly_price,
        "flow_data[subscription_update_confirm][items][0][quantity]": "1",
    }
    response = requests.post(
        "https://api.stripe.com/v1/billing_portal/sessions",
        auth=(env("PULLWISE_STRIPE_SECRET_KEY"), ""),
        data=data,
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    portal_url = payload.get("url")
    if not portal_url:
        raise RuntimeError("Stripe did not return a portal URL.")
    return {"provider": "stripe", "plan": "pro", "interval": "year", "id": payload.get("id"), "url": portal_url}


def fetch_stripe_subscription_item_id(subscription_id: str) -> str | None:
    response = requests.get(
        f"https://api.stripe.com/v1/subscriptions/{subscription_id}",
        auth=(env("PULLWISE_STRIPE_SECRET_KEY"), ""),
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    item = first_subscription_item(payload)
    return item.get("id") if item else None


def create_creem_interval_change(billing: dict) -> dict:
    subscription_id = billing.get("subscriptionId")
    yearly_product = creem_product_id("year")
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    if not yearly_product:
        raise BillingConfigurationError("Creem Pro yearly product is not configured.")
    response = requests.post(
        urljoin(creem_api_base_url() + "/", f"v1/subscriptions/{subscription_id}/upgrade"),
        headers={"x-api-key": env("PULLWISE_CREEM_API_KEY"), "Content-Type": "application/json"},
        json={
            "product_id": yearly_product,
            "update_behavior": env("PULLWISE_CREEM_UPGRADE_BEHAVIOR", "proration-charge-immediately"),
        },
        timeout=int(env("PULLWISE_BILLING_TIMEOUT_SECONDS", "15")),
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "provider": "creem",
        "plan": "pro",
        "interval": "year",
        "subscriptionId": payload.get("id") or subscription_id,
        "status": normalize_subscription_status(payload.get("status")),
        "currentPeriodStart": payload.get("current_period_start_date"),
        "currentPeriodEnd": payload.get("current_period_end_date"),
    }


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


def dict_payload(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def text_payload(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def billing_update_from_creem_event(event: dict) -> dict | None:
    event_type = text_payload(event.get("eventType") or event.get("type"), "")
    obj = dict_payload(event.get("object"))
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
    product = obj.get("product") if isinstance(obj.get("product"), dict) else None
    if not product and isinstance(subscription, dict) and isinstance(subscription.get("product"), dict):
        product = subscription.get("product")
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

    plan = normalize_plan(metadata.get("plan") or subscription_metadata.get("plan") or "pro")
    interval = normalize_interval(
        metadata.get("interval")
        or subscription_metadata.get("interval")
        or interval_from_creem_product(product)
        or "month"
    )
    return {
        "userId": user_id,
        "provider": "creem",
        "customerId": customer_id,
        "customerEmail": customer.get("email"),
        "subscriptionId": subscription.get("id") if isinstance(subscription, dict) else None,
        "status": normalize_creem_subscription_status(event_type, subscription.get("status") if isinstance(subscription, dict) else "active"),
        "plan": plan,
        "interval": interval,
        "currentPeriodStart": subscription.get("current_period_start_date") if isinstance(subscription, dict) else None,
        "currentPeriodEnd": subscription.get("current_period_end_date") if isinstance(subscription, dict) else None,
        "canceledAt": subscription.get("canceled_at") if isinstance(subscription, dict) else None,
        "eventType": event_type,
        "eventId": event.get("id") or event.get("eventId"),
        "eventCreated": event_created(event),
    }


def billing_update_from_stripe_event(event: dict) -> dict | None:
    event_type = text_payload(event.get("type"), "")
    data = dict_payload(event.get("data"))
    obj = dict_payload(data.get("object"))
    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        customer_details = dict_payload(obj.get("customer_details"))
        user_id = obj.get("client_reference_id") or metadata.get("userId")
        if not user_id:
            return None
        return {
            "userId": user_id,
            "provider": "stripe",
            "customerId": obj.get("customer"),
            "customerEmail": customer_details.get("email") or obj.get("customer_email"),
            "subscriptionId": obj.get("subscription"),
            "status": "active",
            "plan": normalize_plan(metadata.get("plan") or "pro"),
            "interval": normalize_interval(metadata.get("interval") or "month"),
            "eventType": event_type,
            "eventId": event.get("id"),
            "eventCreated": event_created(event),
        }
    if event_type.startswith("customer.subscription."):
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        item = first_subscription_item(obj)
        price = item.get("price") if item and isinstance(item.get("price"), dict) else {}
        price_id = price.get("id") if isinstance(price, dict) else None
        interval = normalize_interval(
            metadata.get("interval")
            or interval_from_stripe_price(price)
            or interval_from_provider_price("stripe", price_id)
            or "month"
        )
        status = normalize_subscription_status(obj.get("status"))
        if obj.get("cancel_at_period_end") and status == "active":
            status = "canceling"
        return {
            "userId": metadata.get("userId") or metadata.get("internal_customer_id"),
            "provider": "stripe",
            "customerId": obj.get("customer"),
            "subscriptionId": obj.get("id"),
            "subscriptionItemId": item.get("id") if item else None,
            "status": status,
            "plan": normalize_plan(metadata.get("plan") or "pro"),
            "interval": interval,
            "currentPeriodStart": obj.get("current_period_start") or (item or {}).get("current_period_start"),
            "currentPeriodEnd": obj.get("current_period_end") or (item or {}).get("current_period_end"),
            "cancelAtPeriodEnd": bool(obj.get("cancel_at_period_end")),
            "canceledAt": obj.get("canceled_at"),
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


def normalize_subscription_status(status: object) -> str:
    normalized = text_payload(status, "active").strip().lower()
    if normalized in {"active", "trialing", "paid"}:
        return "active"
    if normalized in {"scheduled_cancel"}:
        return "canceling"
    if normalized in {"past_due", "unpaid", "paused"}:
        return normalized
    if normalized in {"canceled", "cancelled", "expired", "incomplete_expired"}:
        return "canceled"
    return normalized or "active"


def normalize_creem_subscription_status(event_type: str | None, status: object) -> str:
    if event_type == "subscription.expired":
        return "past_due"
    return normalize_subscription_status(status)


def normalize_plan(plan: object) -> str:
    normalized = text_payload(plan, "pro").strip().lower()
    return normalized if normalized in {"free", "pro"} else "pro"


def normalize_interval(interval: object) -> str:
    normalized = text_payload(interval, "month").strip().lower()
    return normalized if normalized in {"month", "year"} else "month"


def first_subscription_item(subscription: dict) -> dict | None:
    items = subscription.get("items") if isinstance(subscription, dict) else None
    if not isinstance(items, dict):
        return None
    data = items.get("data")
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    return item if isinstance(item, dict) else None


def interval_from_stripe_price(price: dict | None) -> str | None:
    if not isinstance(price, dict):
        return None
    recurring = price.get("recurring")
    if isinstance(recurring, dict):
        interval = recurring.get("interval")
        if interval in {"month", "year"}:
            return interval
    return interval_from_provider_price("stripe", price.get("id"))


def interval_from_provider_price(provider: str, identifier: str | None) -> str | None:
    if not identifier:
        return None
    if provider == "stripe":
        if identifier == stripe_price_id("year"):
            return "year"
        if identifier == stripe_price_id("month"):
            return "month"
    if provider == "creem":
        if identifier == creem_product_id("year"):
            return "year"
        if identifier == creem_product_id("month"):
            return "month"
    return None


def interval_from_creem_product(product: dict | None) -> str | None:
    if not isinstance(product, dict):
        return None
    product_id = product.get("id")
    inferred = interval_from_provider_price("creem", product_id)
    if inferred:
        return inferred
    period = str(product.get("billing_period") or "").strip().lower()
    if period in {"every-year", "year", "yearly", "annual", "annually"}:
        return "year"
    if period in {"every-month", "month", "monthly"}:
        return "month"
    return None
