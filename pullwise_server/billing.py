from __future__ import annotations

import os
import hashlib
import hmac
import math
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

import requests


class BillingConfigurationError(RuntimeError):
    pass


class BillingProviderResponseError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: str = "false") -> bool:
    return env(name, default).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str | list[str], default: int) -> int:
    names = [name] if isinstance(name, str) else name
    for candidate in names:
        raw = os.environ.get(candidate)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def billing_timeout_seconds() -> int:
    value = env_int("PULLWISE_BILLING_TIMEOUT_SECONDS", 15)
    return value if value > 0 else 15


def review_limit(plan: str) -> int:
    if plan == "pro":
        return max(
            0,
            env_int(
                [
                    "PULLWISE_PRO_USER_REVIEW_LIMIT",
                    "PULLWISE_PRO_REVIEW_LIMIT",
                ],
                60,
            ),
        )
    return max(
        0,
        env_int(
            [
                "PULLWISE_FREE_USER_REVIEW_LIMIT",
                "PULLWISE_FREE_REVIEW_LIMIT",
            ],
            5,
        ),
    )


def creem_product_id(interval: str) -> str:
    product = creem_product_for_interval(interval)
    if isinstance(product, dict):
        product_id = product.get("id")
        return product_id if isinstance(product_id, str) else ""
    return ""


def creem_configured() -> bool:
    return bool(env("PULLWISE_CREEM_API_KEY") and creem_configured_product_ids())


def selected_provider() -> str:
    configured = env("PULLWISE_BILLING_PROVIDER").strip().lower()
    if configured:
        if configured != "creem":
            raise BillingConfigurationError("PULLWISE_BILLING_PROVIDER must be creem.")
        if not creem_configured():
            raise BillingConfigurationError("Creem billing is selected but Creem environment variables are incomplete.")
        return "creem"

    if creem_configured():
        return "creem"
    return "disabled"


def creem_configured_product_ids() -> list[str]:
    raw_ids: list[str] = []
    combined = env("PULLWISE_CREEM_PRO_PRODUCT_IDS")
    if combined:
        raw_ids.extend(combined.split(","))
    raw_ids.extend(
        [
            env("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID", env("PULLWISE_CREEM_PRODUCT_ID")),
            env("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID", env("PULLWISE_CREEM_YEARLY_PRODUCT_ID")),
        ]
    )

    product_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        product_id = raw.strip()
        if product_id and product_id not in seen:
            product_ids.append(product_id)
            seen.add(product_id)
    return product_ids


def creem_api_headers() -> dict[str, str]:
    return {"x-api-key": env("PULLWISE_CREEM_API_KEY"), "Content-Type": "application/json"}


def fetch_creem_product(product_id: str) -> dict:
    response = requests.get(
        urljoin(creem_api_base_url() + "/", "v1/products"),
        headers=creem_api_headers(),
        params={"product_id": product_id},
        timeout=billing_timeout_seconds(),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise BillingProviderResponseError("Creem did not return a valid product payload.")
    return payload


def creem_product_catalog() -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for product_id in creem_configured_product_ids():
        product = fetch_creem_product(product_id)
        interval = interval_from_creem_product(product)
        if interval in {"month", "year"}:
            catalog[interval] = product
    return catalog


def creem_product_for_interval(interval: str) -> dict | None:
    normalized_interval = normalize_interval(interval)
    return creem_product_catalog().get(normalized_interval)


def creem_product_text(product: dict | None, field: str) -> str:
    value = product.get(field) if isinstance(product, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else ""


def creem_catalog_currency(products: dict[str, dict]) -> str:
    for interval in ("month", "year"):
        currency = creem_product_text(products.get(interval), "currency").upper()
        if currency:
            return currency
    return "USD"


def creem_product_active(product: dict | None) -> bool:
    if not isinstance(product, dict):
        return False
    status = creem_product_text(product, "status").lower()
    return status in {"", "active"}


def creem_price_amount(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        cents = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not cents.is_finite() or cents < 0:
        return None
    amount = cents / Decimal("100")
    formatted = format(amount.quantize(Decimal("0.01")), "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def creem_price_payload(product: dict | None, interval: str) -> dict:
    currency = creem_product_text(product, "currency").upper() or "USD"
    amount = creem_price_amount(product.get("price")) if isinstance(product, dict) else None
    return {
        "amount": amount,
        "currency": currency,
        "interval": interval,
        "configured": bool(
            product
            and creem_product_active(product)
            and creem_product_text(product, "billing_type").lower() == "recurring"
            and interval_from_creem_product(product) == interval
            and amount is not None
        ),
        "productId": product.get("id") if isinstance(product, dict) else None,
        "billingPeriod": product.get("billing_period") if isinstance(product, dict) else None,
    }


def public_plan() -> dict:
    provider = selected_provider()
    products = creem_product_catalog() if provider == "creem" else {}
    monthly_product = products.get("month")
    yearly_product = products.get("year")
    currency = creem_catalog_currency(products)
    pro_name = creem_product_text(monthly_product, "name") or creem_product_text(yearly_product, "name") or "Pullwise Pro"
    pro_description = (
        creem_product_text(monthly_product, "description")
        or creem_product_text(yearly_product, "description")
        or "Repository review for production teams."
    )
    pro_prices = {
        "month": creem_price_payload(monthly_product, "month"),
        "year": creem_price_payload(yearly_product, "year"),
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
                "description": "Try Pullwise with monthly account and repository scan allowance.",
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
                "description": f"{pro_description} Quota is shared across your account and repositories.",
                "currency": currency,
                "reviewLimit": review_limit("pro"),
                "prices": pro_prices,
            },
        ],
    }


def provider_price_configured(provider: str, interval: str) -> bool:
    if provider == "creem":
        return creem_price_payload(creem_product_for_interval(interval), interval)["configured"]
    return False


def provider_redirect_url(value: object, provider: str, label: str) -> str:
    if not isinstance(value, str):
        raise BillingProviderResponseError(f"{provider} did not return a safe {label} URL.")
    raw = value.strip()
    parsed = urlparse(raw)
    if not raw or any(char in raw for char in "\r\n") or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BillingProviderResponseError(f"{provider} did not return a safe {label} URL.")
    return raw


def request_redirect_url(value: object, fallback: str, label: str) -> str:
    candidate = fallback if value is None or (isinstance(value, str) and not value.strip()) else value
    if not isinstance(candidate, str):
        raise BillingConfigurationError(f"Billing {label} URL must be an absolute HTTP(S) URL.")
    raw = candidate.strip()
    parsed = urlparse(raw)
    if not raw or any(char in raw for char in "\r\n") or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BillingConfigurationError(f"Billing {label} URL must be an absolute HTTP(S) URL.")
    return raw


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


def create_creem_checkout_session(user: dict, *, success_url: str, plan: str, interval: str) -> dict:
    success_url = request_redirect_url(success_url, default_success_url(), "success")
    product_id = creem_product_id(interval)
    if not product_id:
        raise BillingConfigurationError(f"Creem Pro {interval} product is not configured.")
    request_id = f"pw_{user['id']}_{secrets.token_urlsafe(8)}"
    customer = {}
    existing_customer_id = (user.get("billing") or {}).get("customerId")
    if isinstance(existing_customer_id, str) and existing_customer_id.strip():
        customer["id"] = existing_customer_id.strip()
    if user.get("email"):
        customer["email"] = user["email"]
    payload = {
        "product_id": product_id,
        "request_id": request_id,
        "units": 1,
        "success_url": success_url,
        "metadata": {
            "userId": user["id"],
            "plan": plan,
            "interval": interval,
        },
    }
    if customer:
        payload["customer"] = customer
    response = requests.post(
        urljoin(creem_api_base_url() + "/", "v1/checkouts"),
        headers=creem_api_headers(),
        json=payload,
        timeout=billing_timeout_seconds(),
    )
    response.raise_for_status()
    payload = response.json()
    checkout_url = provider_redirect_url(payload.get("checkout_url") or payload.get("url"), "Creem", "Checkout")
    returned_customer = payload.get("customer")
    if isinstance(returned_customer, dict):
        returned_customer_id = returned_customer.get("id")
    else:
        returned_customer_id = returned_customer
    if not isinstance(returned_customer_id, str) or not returned_customer_id.strip():
        returned_customer_id = None
    return {
        "provider": "creem",
        "plan": plan,
        "interval": interval,
        "id": payload.get("id"),
        "customerId": returned_customer_id or customer.get("id"),
        "requestId": request_id,
        "url": checkout_url,
    }


def create_portal_session(user: dict, *, return_url: str | None = None) -> dict:
    provider = selected_provider()
    billing = user.get("billing") or {}
    customer_id = billing.get("customerId")
    if not customer_id:
        raise BillingConfigurationError("No billing customer is linked to this account yet.")
    if provider == "creem":
        return create_creem_portal_session(customer_id)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_portal_session(customer_id: str) -> dict:
    response = requests.post(
        urljoin(creem_api_base_url() + "/", "v1/customers/billing"),
        headers=creem_api_headers(),
        json={"customer_id": customer_id},
        timeout=billing_timeout_seconds(),
    )
    response.raise_for_status()
    payload = response.json()
    portal_url = provider_redirect_url(payload.get("customer_portal_link") or payload.get("url"), "Creem", "portal")
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
    if provider == "creem":
        return create_creem_interval_change(billing)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_interval_change(billing: dict) -> dict:
    subscription_id = billing.get("subscriptionId")
    yearly_product = creem_product_id("year")
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    if not yearly_product:
        raise BillingConfigurationError("Creem Pro yearly product is not configured.")
    response = requests.post(
        urljoin(creem_api_base_url() + "/", f"v1/subscriptions/{subscription_id}/upgrade"),
        headers=creem_api_headers(),
        json={
            "product_id": yearly_product,
            "update_behavior": env("PULLWISE_CREEM_UPGRADE_BEHAVIOR", "proration-charge-immediately"),
        },
        timeout=billing_timeout_seconds(),
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
    configured = env(
        "PULLWISE_CREEM_API_BASE_URL",
        "https://test-api.creem.io" if env_flag("PULLWISE_CREEM_TEST_MODE") else "https://api.creem.io",
    ).rstrip("/")
    return configured[:-3] if configured.endswith("/v1") else configured


def verify_creem_webhook(raw_body: bytes, signature: str | None) -> bool:
    secret = env("PULLWISE_CREEM_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return timing_safe_hex_equal(expected, signature.strip())


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


def event_created(event: dict) -> int | None:
    value = event.get("created") or event.get("createdAt")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
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


def interval_from_legacy_creem_product_id(product_id: object) -> str | None:
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    normalized_product_id = product_id.strip()
    yearly_ids = {
        env("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_YEARLY_PRODUCT_ID").strip(),
    }
    monthly_ids = {
        env("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_PRODUCT_ID").strip(),
    }
    if normalized_product_id in yearly_ids - {""}:
        return "year"
    if normalized_product_id in monthly_ids - {""}:
        return "month"
    return None


def interval_from_creem_product(product: dict | None) -> str | None:
    if not isinstance(product, dict):
        return None
    inferred = interval_from_legacy_creem_product_id(product.get("id"))
    if inferred:
        return inferred
    period = str(product.get("billing_period") or "").strip().lower()
    if period in {"every-year", "year", "yearly", "annual", "annually"}:
        return "year"
    if period in {"every-month", "month", "monthly"}:
        return "month"
    return None
