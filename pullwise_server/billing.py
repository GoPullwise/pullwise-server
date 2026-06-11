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


PAID_PLAN_IDS = ("pro", "max")
PLAN_IDS = ("free", *PAID_PLAN_IDS)
PLAN_RANK = {"free": 0, "pro": 1, "max": 2}
PAID_PLAN_ENTITLEMENT_STATUSES = {"active", "trialing", "canceling"}
CREEM_PRO_ENTITLEMENT_STATUSES = PAID_PLAN_ENTITLEMENT_STATUSES
CREEM_UPDATE_BEHAVIORS = {"proration-charge-immediately"}
REVIEW_CODEX_COMMAND_DEFAULT = "codex"
REVIEW_CODEX_MODEL_DEFAULT = "gpt-5.5"
REVIEW_OPENCODE_COMMAND_DEFAULT = "opencode"
REVIEW_OPENCODE_MODEL_DEFAULT = "opencode/big-pickle"
REVIEW_AGENT_EFFORT_DEFAULTS = {"free": "medium", "pro": "medium", "max": "xhigh"}
REVIEW_AGENT_PROVIDERS = ("codex", "opencode")
REVIEW_AGENT_CONFIG_TEXT_MAX_LENGTH = 128


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
    normalized_plan = normalize_plan(plan, default="free")
    if normalized_plan == "max":
        return max(
            0,
            env_int(
                [
                    "PULLWISE_MAX_USER_REVIEW_LIMIT",
                    "PULLWISE_MAX_REVIEW_LIMIT",
                ],
                90,
            ),
        )
    if normalized_plan == "pro":
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


def review_agent_env(plan: str, keys: list[str], default: str) -> str:
    normalized_plan = normalize_plan(plan, default="free")
    for key in keys:
        value = clean_review_agent_config_text(env(f"PULLWISE_{normalized_plan.upper()}_{key}", ""))
        if value:
            return value
    return default


def clean_review_agent_config_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > REVIEW_AGENT_CONFIG_TEXT_MAX_LENGTH:
        return ""
    if any(char in normalized for char in "\r\n\x00"):
        return ""
    if any(char.isspace() for char in normalized):
        return ""
    return normalized


def clean_review_agent_provider(value: object) -> str:
    provider = clean_review_agent_config_text(value).lower()
    return provider if provider in REVIEW_AGENT_PROVIDERS else ""


def review_agent_provider_chain(plan: str) -> list[str]:
    normalized_plan = normalize_plan(plan, default="free")
    raw = env(f"PULLWISE_{normalized_plan.upper()}_AGENT_PROVIDER_CHAIN", "")
    if not raw:
        raw = env(f"PULLWISE_{normalized_plan.upper()}_PROVIDER_CHAIN", "")
    providers: list[str] = []
    for item in raw.split(","):
        provider = clean_review_agent_provider(item)
        if provider and provider not in providers:
            providers.append(provider)
    if providers:
        return providers
    provider = clean_review_agent_provider(env(f"PULLWISE_{normalized_plan.upper()}_AGENT_CLI", ""))
    return [provider or "codex"]


def review_reasoning_effort(plan: str) -> str:
    normalized_plan = normalize_plan(plan, default="free")
    default = REVIEW_AGENT_EFFORT_DEFAULTS[normalized_plan]
    effort = review_agent_env(normalized_plan, ["CODEX_REASONING_EFFORT"], default).lower()
    return effort if effort in {"low", "medium", "high", "xhigh"} else default


def review_opencode_variant(plan: str) -> str:
    normalized_plan = normalize_plan(plan, default="free")
    default = REVIEW_AGENT_EFFORT_DEFAULTS[normalized_plan]
    variant = review_agent_env(normalized_plan, ["OPENCODE_VARIANT"], default).lower()
    return variant if variant in {"low", "medium", "high", "xhigh"} else default


def review_agent_config(plan: str) -> dict:
    normalized_plan = normalize_plan(plan, default="free")
    codex_cli = review_agent_env(normalized_plan, ["CODEX_CLI", "CODEX_COMMAND"], REVIEW_CODEX_COMMAND_DEFAULT)
    codex_command = review_agent_env(normalized_plan, ["CODEX_COMMAND", "CODEX_CLI"], REVIEW_CODEX_COMMAND_DEFAULT)
    codex_model = review_agent_env(normalized_plan, ["CODEX_MODEL"], REVIEW_CODEX_MODEL_DEFAULT)
    codex_effort = review_reasoning_effort(normalized_plan)
    opencode_cli = review_agent_env(
        normalized_plan,
        ["OPENCODE_CLI", "OPENCODE_COMMAND"],
        REVIEW_OPENCODE_COMMAND_DEFAULT,
    )
    opencode_command = review_agent_env(
        normalized_plan,
        ["OPENCODE_COMMAND", "OPENCODE_CLI"],
        REVIEW_OPENCODE_COMMAND_DEFAULT,
    )
    opencode_model = review_agent_env(normalized_plan, ["OPENCODE_MODEL"], REVIEW_OPENCODE_MODEL_DEFAULT)
    opencode_variant = review_opencode_variant(normalized_plan)
    provider_chain = review_agent_provider_chain(normalized_plan)
    primary_provider = provider_chain[0]
    primary_model = opencode_model if primary_provider == "opencode" else codex_model
    primary_effort = opencode_variant if primary_provider == "opencode" else codex_effort
    return {
        "plan": normalized_plan,
        "agent": {
            "cli": primary_provider,
            "model": primary_model,
            "reasoningEffort": primary_effort,
            "reasoning_effort": primary_effort,
        },
        "provider": primary_provider,
        "providerChain": provider_chain,
        "provider_chain": provider_chain,
        "codex": {
            "cli": codex_cli,
            "command": codex_command,
            "model": codex_model,
            "reasoningEffort": codex_effort,
            "reasoning_effort": codex_effort,
        },
        "opencode": {
            "cli": opencode_cli,
            "command": opencode_command,
            "model": opencode_model,
            "variant": opencode_variant,
        },
    }


def creem_product_id(interval: str, plan: str = "pro") -> str:
    product = creem_product_for_interval(interval, plan=plan)
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


def creem_configured_product_ids_for_plan(plan: str) -> list[str]:
    normalized_plan = normalize_plan(plan)
    if normalized_plan not in PAID_PLAN_IDS:
        return []
    raw_ids = creem_configured_paid_product_ids(normalized_plan)
    if normalized_plan == "pro" and not raw_ids:
        raw_ids = [
            env("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID", env("PULLWISE_CREEM_PRODUCT_ID")),
            env("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID", env("PULLWISE_CREEM_YEARLY_PRODUCT_ID")),
        ]
    elif normalized_plan == "max" and not raw_ids:
        raw_ids = [
            env("PULLWISE_CREEM_MAX_MONTHLY_PRODUCT_ID"),
            env("PULLWISE_CREEM_MAX_YEARLY_PRODUCT_ID"),
        ]

    product_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        product_id = raw.strip()
        if product_id and product_id not in seen:
            product_ids.append(product_id)
            seen.add(product_id)
    return product_ids


def creem_configured_paid_product_ids(plan: str) -> list[str]:
    normalized_plan = normalize_plan(plan)
    if normalized_plan not in PAID_PLAN_IDS:
        return []
    raw_ids: list[str] = []
    combined = env(f"PULLWISE_CREEM_{normalized_plan.upper()}_PRODUCT_IDS")
    if combined:
        raw_ids.extend(combined.split(","))
    product_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        product_id = raw.strip()
        if product_id and product_id not in seen:
            product_ids.append(product_id)
            seen.add(product_id)
    return product_ids


def creem_configured_product_ids() -> list[str]:
    product_ids: list[str] = []
    seen: set[str] = set()
    for plan in PAID_PLAN_IDS:
        for product_id in creem_configured_product_ids_for_plan(plan):
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


def creem_product_catalog(plan: str | None = None) -> dict:
    catalog: dict[str, dict[str, dict]] = {plan_id: {} for plan_id in PAID_PLAN_IDS}
    for plan_id in PAID_PLAN_IDS:
        for product_id in creem_configured_product_ids_for_plan(plan_id):
            product = fetch_creem_product(product_id)
            interval = interval_from_creem_product(product)
            if interval in {"month", "year"}:
                catalog[plan_id][interval] = product
    if plan is not None:
        return catalog.get(normalize_plan(plan), {})
    return catalog


def creem_product_for_interval(interval: str, *, plan: str = "pro") -> dict | None:
    normalized_interval = normalize_interval(interval)
    return creem_product_catalog(plan).get(normalized_interval)


def creem_product_text(product: dict | None, field: str) -> str:
    value = product.get(field) if isinstance(product, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else ""


def creem_catalog_currency(products: dict) -> str:
    for plan_id in PAID_PLAN_IDS:
        plan_products = products.get(plan_id) if isinstance(products.get(plan_id), dict) else products
        for interval in ("month", "year"):
            currency = creem_product_text(plan_products.get(interval), "currency").upper()
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
    currency = creem_catalog_currency(products)
    pro_plan = public_paid_plan_payload("pro", products.get("pro") if isinstance(products, dict) else {}, currency)
    max_plan = public_paid_plan_payload("max", products.get("max") if isinstance(products, dict) else {}, currency)
    return {
        "provider": provider,
        "enabled": provider != "disabled",
        "currency": currency,
        "name": pro_plan["name"],
        "description": pro_plan["description"],
        "interval": "month",
        "amount": pro_plan["prices"]["month"]["amount"],
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
            pro_plan,
            max_plan,
        ],
    }


def public_paid_plan_payload(plan: str, products: dict, currency: str) -> dict:
    normalized_plan = normalize_plan(plan)
    title = "Pullwise Max" if normalized_plan == "max" else "Pullwise Pro"
    default_description = (
        "Higher-capacity repository review for production teams."
        if normalized_plan == "max"
        else "Repository review for production teams."
    )
    monthly_product = products.get("month") if isinstance(products, dict) else None
    yearly_product = products.get("year") if isinstance(products, dict) else None
    name = creem_product_text(monthly_product, "name") or creem_product_text(yearly_product, "name") or title
    description = creem_product_text(monthly_product, "description") or creem_product_text(yearly_product, "description") or default_description
    return {
        "id": normalized_plan,
        "name": name,
        "description": f"{description} Quota is shared across your account and repositories.",
        "currency": currency,
        "reviewLimit": review_limit(normalized_plan),
        "prices": {
            "month": creem_price_payload(monthly_product, "month"),
            "year": creem_price_payload(yearly_product, "year"),
        },
    }


def provider_price_configured(provider: str, interval: str, plan: str = "pro") -> bool:
    if provider == "creem":
        return creem_price_payload(creem_product_for_interval(interval, plan=plan), interval)["configured"]
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
    normalized_plan = normalize_plan(plan)
    normalized_interval = (interval or "month").strip().lower()
    if normalized_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("Only paid plans can be purchased.")
    if normalized_interval not in {"month", "year"}:
        raise BillingConfigurationError("Billing interval must be month or year.")
    return normalized_plan, normalized_interval


def create_creem_checkout_session(user: dict, *, success_url: str, plan: str, interval: str) -> dict:
    success_url = request_redirect_url(success_url, default_success_url(), "success")
    product_id = creem_product_id(interval, plan=plan)
    if not product_id:
        raise BillingConfigurationError(f"Creem {plan.title()} {interval} product is not configured.")
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


def change_subscription_interval(user: dict, *, interval: str, plan: str | None = None, return_url: str | None = None) -> dict:
    target_plan = normalize_plan(plan or (user.get("billing") or {}).get("plan") or "pro")
    target_interval = normalize_interval(interval)
    if target_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("Only paid subscriptions can be changed.")
    billing = user.get("billing") or {}
    current_plan = normalize_plan(billing.get("plan") or "pro")
    current_interval = normalize_interval(billing.get("interval"))
    if current_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("Only paid subscriptions can be changed.")
    if current_plan == target_plan and current_interval == target_interval:
        return {
            "provider": billing.get("provider") or selected_provider(),
            "plan": target_plan,
            "interval": target_interval,
            "alreadyActive": True,
        }
    if not subscription_change_is_upgrade(current_plan, current_interval, target_plan, target_interval):
        raise BillingConfigurationError("Only subscription upgrades are supported.")
    if (billing.get("status") or "").lower() not in {"active", "trialing"}:
        raise BillingConfigurationError("Only active subscriptions can be changed.")

    provider = selected_provider()
    billing_provider = (billing.get("provider") or provider).lower()
    if billing_provider and billing_provider != provider:
        raise BillingConfigurationError("Configured billing provider does not match this subscription.")
    if provider == "creem":
        return create_creem_subscription_change(billing, plan=target_plan, interval=target_interval)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_subscription_change(billing: dict, *, plan: str, interval: str) -> dict:
    subscription_id = billing.get("subscriptionId")
    product_id = creem_product_id(interval, plan=plan)
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    if not product_id:
        raise BillingConfigurationError(f"Creem {plan.title()} {interval} product is not configured.")
    update_behavior = creem_subscription_update_behavior(
        normalize_plan(billing.get("plan") or "pro"),
        normalize_interval(billing.get("interval")),
        plan,
        interval,
    )
    response = requests.post(
        urljoin(creem_api_base_url() + "/", f"v1/subscriptions/{subscription_id}/upgrade"),
        headers=creem_api_headers(),
        json={
            "product_id": product_id,
            "update_behavior": update_behavior,
        },
        timeout=billing_timeout_seconds(),
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "provider": "creem",
        "plan": plan,
        "interval": interval,
        "updateBehavior": update_behavior,
        "subscriptionId": payload.get("id") or subscription_id,
        "status": normalize_subscription_status(payload.get("status")),
        "currentPeriodStart": payload.get("current_period_start_date"),
        "currentPeriodEnd": payload.get("current_period_end_date"),
    }


def creem_subscription_update_behavior(current_plan: str, current_interval: str, target_plan: str, target_interval: str) -> str:
    if not subscription_change_is_upgrade(current_plan, current_interval, target_plan, target_interval):
        raise BillingConfigurationError("Only subscription upgrades are supported.")
    return normalize_creem_update_behavior(env("PULLWISE_CREEM_UPGRADE_BEHAVIOR"), "proration-charge-immediately")


def subscription_change_is_upgrade(current_plan: str, current_interval: str, target_plan: str, target_interval: str) -> bool:
    current_rank = PLAN_RANK.get(normalize_plan(current_plan, default="free"), 0)
    target_rank = PLAN_RANK.get(normalize_plan(target_plan, default="free"), 0)
    if target_rank > current_rank:
        return not (normalize_interval(current_interval) == "year" and normalize_interval(target_interval) == "month")
    if target_rank == current_rank and normalize_interval(current_interval) == "month" and normalize_interval(target_interval) == "year":
        return True
    return False


def normalize_creem_update_behavior(value: object, default: str) -> str:
    normalized = text_payload(value, default).strip().lower()
    if normalized == "proration-charge":
        return "proration-charge-immediately"
    return normalized if normalized in CREEM_UPDATE_BEHAVIORS else default


def cancel_subscription(user: dict, *, mode: str = "scheduled", return_url: str | None = None) -> dict:
    billing = user.get("billing") or {}
    current_plan = normalize_plan(billing.get("plan") or "free", default="free")
    if current_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("No paid subscription is linked to this account.")
    status = (billing.get("status") or "").lower()
    if status == "canceling":
        return {
            "provider": billing.get("provider") or selected_provider(),
            "plan": current_plan,
            "interval": normalize_interval(billing.get("interval")),
            "status": "canceling",
            "alreadyScheduled": True,
        }
    if status not in {"active", "trialing"}:
        raise BillingConfigurationError("Only active subscriptions can be canceled.")
    normalized_mode = (mode or "scheduled").strip().lower()
    if normalized_mode != "scheduled":
        raise BillingConfigurationError("Only scheduled cancellation is supported.")
    provider = selected_provider()
    billing_provider = (billing.get("provider") or provider).lower()
    if billing_provider and billing_provider != provider:
        raise BillingConfigurationError("Configured billing provider does not match this subscription.")
    if provider == "creem":
        return create_creem_subscription_cancel(billing, mode=normalized_mode)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_subscription_cancel(billing: dict, *, mode: str = "scheduled") -> dict:
    subscription_id = billing.get("subscriptionId")
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    response = requests.post(
        urljoin(creem_api_base_url() + "/", f"v1/subscriptions/{subscription_id}/cancel"),
        headers=creem_api_headers(),
        json={"mode": mode},
        timeout=billing_timeout_seconds(),
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "provider": "creem",
        "plan": normalize_plan(billing.get("plan") or "pro"),
        "interval": normalize_interval(billing.get("interval")),
        "subscriptionId": payload.get("id") or subscription_id,
        "status": normalize_subscription_status(payload.get("status") or "scheduled_cancel"),
        "cancelAtPeriodEnd": True,
        "canceledAt": payload.get("canceled_at"),
        "currentPeriodStart": payload.get("current_period_start_date"),
        "currentPeriodEnd": payload.get("current_period_end_date"),
    }


def creem_api_base_url() -> str:
    default_url = "https://test-api.creem.io" if env_flag("PULLWISE_CREEM_TEST_MODE") else "https://api.creem.io"
    configured = env("PULLWISE_CREEM_API_BASE_URL", default_url).strip().rstrip("/") or default_url
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BillingConfigurationError("PULLWISE_CREEM_API_BASE_URL must be an absolute HTTP(S) URL.")
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


def object_id(value: object) -> str | None:
    if isinstance(value, dict):
        return text_payload(value.get("id"), "") or None
    return text_payload(value, "") or None


def product_payload(value: object) -> dict | None:
    if isinstance(value, dict):
        return value
    product_id = object_id(value)
    return {"id": product_id} if product_id else None


def first_subscription_item(subscription: dict, order: dict) -> dict:
    for source in (subscription.get("items"), order.get("items")):
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, dict):
                return item
    return {}


def product_payload_from_subscription_item(item: dict) -> dict | None:
    product = product_payload(item.get("product"))
    if product:
        return product
    product_id = object_id(item.get("product_id") or item.get("productId"))
    return {"id": product_id} if product_id else None


def first_text_value(*values: object) -> str:
    for value in values:
        text = text_payload(value, "")
        if text:
            return text
    return ""


def metadata_value(field: str, *metadata_objects: dict) -> object:
    for metadata in metadata_objects:
        if isinstance(metadata, dict) and metadata.get(field):
            return metadata.get(field)
    return None


def creem_event_subscription_payload(event_type: str, obj: dict) -> dict:
    raw_subscription = obj.get("subscription")
    if isinstance(raw_subscription, dict):
        return raw_subscription
    if event_type.startswith("subscription."):
        return obj
    checkout = dict_payload(obj.get("checkout"))
    checkout_subscription = checkout.get("subscription")
    return checkout_subscription if isinstance(checkout_subscription, dict) else {}


def creem_event_subscription_id(event_type: str, obj: dict, subscription: dict, transaction: dict, checkout: dict) -> str | None:
    return (
        object_id(subscription)
        or object_id(obj.get("subscription"))
        or object_id(transaction.get("subscription"))
        or object_id(checkout.get("subscription"))
        or (object_id(obj) if event_type.startswith("subscription.") else None)
    )


def creem_event_customer_payload(*values: object) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def creem_product_configured_for_plan(product: dict | None, plan: str) -> bool:
    product_id = object_id(product)
    return bool(product_id and product_id in creem_configured_product_ids_for_plan(plan))


def creem_product_configured_for_pro(product: dict | None) -> bool:
    return creem_product_configured_for_plan(product, "pro")


def creem_plan_from_product(product: dict | None) -> str | None:
    product_id = object_id(product)
    if not product_id:
        return None
    for plan in PAID_PLAN_IDS:
        if product_id in creem_configured_product_ids_for_plan(plan):
            return plan
    return None


def billing_update_from_creem_event(event: dict) -> dict | None:
    event_type = text_payload(event.get("eventType") or event.get("type"), "")
    obj = dict_payload(event.get("object"))
    if event_type not in {
        "checkout.completed",
        "refund.created",
        "dispute.created",
        "subscription.active",
        "subscription.paid",
        "subscription.canceled",
        "subscription.scheduled_cancel",
        "subscription.past_due",
        "subscription.expired",
        "subscription.trialing",
        "subscription.paused",
        "subscription.unpaid",
        "subscription.update",
    }:
        return None

    checkout = dict_payload(obj.get("checkout"))
    order = dict_payload(obj.get("order"))
    transaction = dict_payload(obj.get("transaction"))
    request_id = first_text_value(
        obj.get("request_id"),
        obj.get("requestId"),
        checkout.get("request_id"),
        checkout.get("requestId"),
        order.get("request_id"),
        order.get("requestId"),
    )
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    checkout_metadata = checkout.get("metadata") if isinstance(checkout.get("metadata"), dict) else {}
    order_metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    subscription = creem_event_subscription_payload(event_type, obj)
    subscription_metadata = subscription.get("metadata") if isinstance(subscription.get("metadata"), dict) else {}
    product = product_payload(obj.get("product"))
    if not product and isinstance(subscription, dict):
        product = product_payload(subscription.get("product"))
    if not product:
        product = product_payload(order.get("product"))
    if not product:
        product = product_payload(transaction.get("product"))
    user_id = (
        metadata_value("userId", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or metadata_value("user_id", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or metadata_value("internal_customer_id", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or metadata_value("internalCustomerId", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or metadata_value("referenceId", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or metadata_value("reference_id", metadata, checkout_metadata, order_metadata, subscription_metadata)
    )
    subscription_customer = subscription.get("customer") if isinstance(subscription, dict) else None
    raw_customer = obj.get("customer")
    customer = creem_event_customer_payload(raw_customer, subscription_customer, order.get("customer"), transaction.get("customer"))
    customer_id = (
        object_id(raw_customer)
        or object_id(subscription_customer)
        or object_id(order.get("customer"))
        or object_id(transaction.get("customer"))
    )
    subscription_id = creem_event_subscription_id(event_type, obj, subscription, transaction, checkout)
    if event_type in {"refund.created", "dispute.created"} and not subscription_id:
        return None
    if not user_id and not customer_id and not request_id:
        return None

    subscription_item = first_subscription_item(subscription, order) if isinstance(subscription, dict) else {}
    item_product = product_payload_from_subscription_item(subscription_item)
    if not product:
        product = item_product

    product_plan = creem_plan_from_product(product)
    metadata_plan = text_payload(metadata_value("plan", metadata, checkout_metadata, order_metadata, subscription_metadata), "").strip().lower()
    plan = product_plan or (metadata_plan if metadata_plan in PLAN_IDS else None)
    status = normalize_creem_subscription_status(
        event_type,
        subscription.get("status") if isinstance(subscription, dict) else None,
        transaction.get("status"),
    )
    if event_type == "refund.created" and status != "canceled":
        return None
    if status in PAID_PLAN_ENTITLEMENT_STATUSES and object_id(product) and not product_plan:
        return None
    if plan in PAID_PLAN_IDS and status in PAID_PLAN_ENTITLEMENT_STATUSES and not creem_product_configured_for_plan(product, plan):
        return None
    interval = normalize_interval(
        metadata_value("interval", metadata, checkout_metadata, order_metadata, subscription_metadata)
        or interval_from_creem_product(product)
        or "month"
    )
    return {
        "userId": user_id,
        "requestId": request_id or None,
        "provider": "creem",
        "customerId": customer_id,
        "customerEmail": customer.get("email"),
        "subscriptionId": subscription_id,
        "subscriptionItemId": object_id(subscription_item),
        "status": status,
        "plan": plan,
        "interval": interval,
        "currentPeriodStart": subscription.get("current_period_start_date") if isinstance(subscription, dict) else None,
        "currentPeriodEnd": subscription.get("current_period_end_date") if isinstance(subscription, dict) else None,
        "cancelAtPeriodEnd": subscription.get("cancel_at_period_end") if isinstance(subscription, dict) else None,
        "canceledAt": subscription.get("canceled_at") if isinstance(subscription, dict) else None,
        "eventType": event_type,
        "eventId": event.get("id") or event.get("eventId"),
        "eventCreated": event_created(event),
    }


def event_created(event: dict) -> int | None:
    value = event.get("created") or event.get("createdAt") or event.get("created_at")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        candidate = int(value)
        return candidate // 1000 if candidate >= 10_000_000_000 else candidate
    if isinstance(value, str) and value.isdigit():
        candidate = int(value)
        return candidate // 1000 if candidate >= 10_000_000_000 else candidate
    return None


def normalize_subscription_status(status: object) -> str:
    normalized = text_payload(status, "active").strip().lower()
    if normalized == "trialing":
        return "trialing"
    if normalized in {"active", "paid"}:
        return "active"
    if normalized in {"scheduled_cancel"}:
        return "canceling"
    if normalized in {"past_due", "unpaid", "paused"}:
        return normalized
    if normalized in {"canceled", "cancelled", "expired", "incomplete_expired"}:
        return "canceled"
    return normalized or "active"


def normalize_creem_subscription_status(event_type: str | None, status: object, transaction_status: object | None = None) -> str:
    normalized_transaction_status = text_payload(transaction_status, "").strip().lower()
    if event_type == "refund.created" and normalized_transaction_status in {"refunded", "chargeback"}:
        return "canceled"
    if event_type == "dispute.created":
        return "past_due"
    if event_type == "subscription.canceled":
        return "canceled"
    if event_type == "subscription.scheduled_cancel":
        return "canceling"
    if event_type == "subscription.past_due":
        return "past_due"
    if event_type == "subscription.unpaid":
        return "unpaid"
    if event_type == "subscription.expired":
        return "past_due"
    if event_type == "subscription.paused":
        return "paused"
    if event_type == "subscription.trialing":
        return "trialing"
    if event_type in {"subscription.active", "subscription.paid"}:
        return "active"
    return normalize_subscription_status(status)


def normalize_plan(plan: object, default: str = "pro") -> str:
    normalized_default = default if default in PLAN_IDS else "pro"
    normalized = text_payload(plan, normalized_default).strip().lower()
    return normalized if normalized in PLAN_IDS else normalized_default


def normalize_interval(interval: object) -> str:
    normalized = text_payload(interval, "month").strip().lower()
    return normalized if normalized in {"month", "year"} else "month"


def interval_from_legacy_creem_product_id(product_id: object) -> str | None:
    inferred = interval_from_explicit_creem_product_id(product_id)
    if inferred:
        return inferred
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    normalized_product_id = product_id.strip()
    for plan in PAID_PLAN_IDS:
        configured_ids = creem_configured_paid_product_ids(plan)
        if configured_ids[:1] == [normalized_product_id]:
            return "month"
        if len(configured_ids) > 1 and configured_ids[1] == normalized_product_id:
            return "year"
    return None


def interval_from_explicit_creem_product_id(product_id: object) -> str | None:
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    normalized_product_id = product_id.strip()
    yearly_ids = {
        env("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_YEARLY_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_MAX_YEARLY_PRODUCT_ID").strip(),
    }
    monthly_ids = {
        env("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_PRODUCT_ID").strip(),
        env("PULLWISE_CREEM_MAX_MONTHLY_PRODUCT_ID").strip(),
    }
    if normalized_product_id in yearly_ids - {""}:
        return "year"
    if normalized_product_id in monthly_ids - {""}:
        return "month"
    return None


def interval_from_creem_product(product: dict | None) -> str | None:
    if not isinstance(product, dict):
        return None
    inferred = interval_from_explicit_creem_product_id(product.get("id"))
    if inferred:
        return inferred
    period = str(product.get("billing_period") or "").strip().lower()
    if period in {"every-year", "year", "yearly", "annual", "annually"}:
        return "year"
    if period in {"every-month", "month", "monthly"}:
        return "month"
    inferred = interval_from_legacy_creem_product_id(product.get("id"))
    if inferred:
        return inferred
    return None
