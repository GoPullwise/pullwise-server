from __future__ import annotations

import os
import secrets
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
    response = requests.post(
        "https://api.stripe.com/v1/checkout/sessions",
        auth=(env("PULLWISE_STRIPE_SECRET_KEY"), ""),
        data={
            "mode": env("PULLWISE_STRIPE_CHECKOUT_MODE", "subscription"),
            "line_items[0][price]": env("PULLWISE_STRIPE_PRICE_ID"),
            "line_items[0][quantity]": "1",
            "customer_email": user.get("email") or "",
            "success_url": success_url or default_success_url(),
            "cancel_url": cancel_url or default_cancel_url(),
            "client_reference_id": user["id"],
            "metadata[userId]": user["id"],
        },
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
        "url": checkout_url,
    }


def create_creem_checkout_session(user: dict, *, success_url: str) -> dict:
    request_id = f"pw_{user['id']}_{secrets.token_urlsafe(8)}"
    response = requests.post(
        urljoin(creem_api_base_url() + "/", "v1/checkouts"),
        headers={"x-api-key": env("PULLWISE_CREEM_API_KEY"), "Content-Type": "application/json"},
        json={
            "product_id": env("PULLWISE_CREEM_PRODUCT_ID"),
            "request_id": request_id,
            "units": 1,
            "customer": {"email": user.get("email") or "", "id": user.get("billing", {}).get("customerId")},
            "success_url": success_url,
            "metadata": {"userId": user["id"]},
        },
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
