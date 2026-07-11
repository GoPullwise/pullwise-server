from __future__ import annotations

import os
import hashlib
import hmac
import math
import time
import copy
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

import requests

from . import db, system_config


class BillingConfigurationError(RuntimeError):
    pass


class BillingProviderResponseError(RuntimeError):
    pass


PAID_PLAN_IDS = ("pro", "max")
PLAN_IDS = ("free", *PAID_PLAN_IDS)
PLAN_RANK = {"free": 0, "pro": 1, "max": 2}
PAID_PLAN_ENTITLEMENT_STATUSES = {"active", "trialing", "canceling"}
PAID_PLAN_CHANGE_STATUSES = {"active", "trialing", "canceling"}
CREEM_CHECKOUT_REQUEST_ID_WINDOW_SECONDS = 10 * 60
CREEM_PRO_ENTITLEMENT_STATUSES = PAID_PLAN_ENTITLEMENT_STATUSES
CREEM_UPDATE_BEHAVIORS = {"proration-charge-immediately", "proration-none"}
REVIEW_CODEX_MODEL_DEFAULT = "gpt-5.5"
REVIEW_AGENT_EFFORT_DEFAULTS = {"free": "medium", "pro": "medium", "max": "xhigh"}
REVIEW_AGENT_PROVIDERS = ("codex",)
REVIEW_AGENT_EFFORT_DEFAULT_OPTIONS = ("low", "medium", "high", "xhigh")
REVIEW_AGENT_EFFORT_MODEL_FAMILIES = (
    {
        "modelPrefix": "gpt-5.6",
        "options": (*REVIEW_AGENT_EFFORT_DEFAULT_OPTIONS, "max", "ultra"),
    },
)
REVIEW_AGENT_REVIEW_WORKER_DEFAULTS_BY_PLAN = {
    "free": {
        "turnTimeoutSeconds": 3600,
        "scanDeadlineSeconds": 14400,
    },
    "pro": {
        "turnTimeoutSeconds": 3600,
        "scanDeadlineSeconds": 14400,
    },
    "max": {
        "turnTimeoutSeconds": 3600,
        "scanDeadlineSeconds": 14400,
    },
}
REVIEW_AGENT_CONFIG_TEXT_MAX_LENGTH = 128
REVIEW_AGENT_CONFIG_STATE_KEY = "review_agent_config"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def billing_timeout_seconds() -> int:
    return system_config.billing_timeout_seconds()


def review_limit(plan: str) -> int:
    return system_config.plan_user_review_limit(normalize_plan(plan, default="free"))



def repository_limits(plan: str) -> dict:
    return system_config.repository_scan_limits(normalize_plan(plan, default="free"))


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


def review_agent_effort_options(model: object) -> tuple[str, ...]:
    normalized_model = clean_review_agent_config_text(model).lower()
    matching_families = [
        family
        for family in REVIEW_AGENT_EFFORT_MODEL_FAMILIES
        if normalized_model == str(family["modelPrefix"]).lower()
        or normalized_model.startswith(str(family["modelPrefix"]).lower() + "-")
    ]
    if matching_families:
        family = max(matching_families, key=lambda item: len(str(item["modelPrefix"])))
        return tuple(family["options"])
    return REVIEW_AGENT_EFFORT_DEFAULT_OPTIONS


def review_agent_capabilities() -> dict:
    return {
        "codex": {
            "reasoningEffort": {
                "source": "server-fallback",
                "defaultOptions": list(REVIEW_AGENT_EFFORT_DEFAULT_OPTIONS),
                "models": [],
                "modelFamilies": [
                    {
                        "modelPrefix": family["modelPrefix"],
                        "options": list(family["options"]),
                    }
                    for family in REVIEW_AGENT_EFFORT_MODEL_FAMILIES
                ],
            }
        }
    }


def clean_review_agent_effort(value: object, default: str, *, model: object) -> str:
    effort = clean_review_agent_config_text(value).lower()
    options = review_agent_effort_options(model)
    if effort in options:
        return effort
    normalized_default = clean_review_agent_config_text(default).lower()
    if normalized_default in options:
        return normalized_default
    return "medium" if "medium" in options else options[0]


def clean_review_agent_config_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def clean_review_agent_provider_required(value: object, *, strict: bool = True) -> str:
    provider = clean_review_agent_provider(value)
    if provider:
        return provider
    if strict:
        raise ValueError("provider must be codex.")
    return "codex"


def default_review_agent_review_worker_config(plan: str) -> dict:
    normalized_plan = normalize_plan(plan, default="free")
    return copy.deepcopy(REVIEW_AGENT_REVIEW_WORKER_DEFAULTS_BY_PLAN[normalized_plan])


def default_review_agent_plan_config(plan: str) -> dict:
    normalized_plan = normalize_plan(plan, default="free")
    effort = REVIEW_AGENT_EFFORT_DEFAULTS[normalized_plan]
    return {
        "provider": "codex",
        "codex": {
            "model": REVIEW_CODEX_MODEL_DEFAULT,
            "reasoningEffort": effort,
        },
        "reviewWorker": default_review_agent_review_worker_config(normalized_plan),
    }


def default_review_agent_config_state() -> dict:
    return {
        "version": 2,
        "plans": {plan: default_review_agent_plan_config(plan) for plan in PLAN_IDS},
    }


def normalize_review_agent_provider_config(provider: str, value: object, defaults: dict) -> dict:
    source = value if isinstance(value, dict) else {}
    result = copy.deepcopy(defaults)
    model = clean_review_agent_config_text(source.get("model"))
    if model:
        result["model"] = model
    effort = clean_review_agent_effort(
        source.get("reasoningEffort"),
        result["reasoningEffort"],
        model=result["model"],
    )
    result["reasoningEffort"] = effort
    return result


def normalize_review_agent_review_worker_config(value: object, defaults: dict) -> dict:
    source = value if isinstance(value, dict) else {}
    result = copy.deepcopy(defaults)
    result["turnTimeoutSeconds"] = clean_review_agent_config_int(
        source.get("turnTimeoutSeconds"),
        int(result.get("turnTimeoutSeconds") or 3600),
        minimum=60,
        maximum=3600,
    )
    result["scanDeadlineSeconds"] = clean_review_agent_config_int(
        source.get("scanDeadlineSeconds"),
        int(result.get("scanDeadlineSeconds") or 14400),
        minimum=0,
        maximum=21600,
    )
    return result

def normalize_review_agent_plan_config(plan: str, value: object) -> dict:
    defaults = default_review_agent_plan_config(plan)
    source = value if isinstance(value, dict) else {}
    result = copy.deepcopy(defaults)
    if "provider" in source:
        result["provider"] = clean_review_agent_provider_required(source.get("provider"), strict=False)
    result["codex"] = normalize_review_agent_provider_config("codex", source.get("codex"), defaults["codex"])
    result["reviewWorker"] = normalize_review_agent_review_worker_config(
        source.get("reviewWorker"),
        defaults["reviewWorker"],
    )
    return result


def normalize_review_agent_config_state(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    plans = source.get("plans") if isinstance(source.get("plans"), dict) else {}
    return {
        "version": 2,
        "plans": {
            plan: normalize_review_agent_plan_config(plan, plans.get(plan))
            for plan in PLAN_IDS
        },
    }


def review_agent_config_state() -> dict:
    raw = db.load_state_item(REVIEW_AGENT_CONFIG_STATE_KEY)
    normalized = normalize_review_agent_config_state(raw)
    if raw != normalized:
        db.save_state_item(REVIEW_AGENT_CONFIG_STATE_KEY, normalized)
    return normalized


def review_agent_provider(plan: str) -> str:
    return review_agent_config_state()["plans"][normalize_plan(plan, default="free")]["provider"]


def review_reasoning_effort(plan: str) -> str:
    return review_agent_config_state()["plans"][normalize_plan(plan, default="free")]["codex"]["reasoningEffort"]


def review_agent_config(plan: str) -> dict:
    normalized_plan = normalize_plan(plan, default="free")
    configured = review_agent_config_state()["plans"][normalized_plan]
    codex_config = configured["codex"]
    return {
        "plan": normalized_plan,
        "provider": configured["provider"],
        "codex": {
            "model": codex_config["model"],
            "reasoningEffort": codex_config["reasoningEffort"],
        },
        "reviewWorker": copy.deepcopy(configured["reviewWorker"]),
    }


def review_agent_configs_admin_payload() -> dict:
    agent_configs = {plan: review_agent_config(plan) for plan in PLAN_IDS}
    plan_names = {"free": "Free", "pro": "Pro", "max": "Max"}
    return {
        "source": "database",
        "plans": [
            {
                "id": plan,
                "name": plan_names[plan],
                "reviewLimit": review_limit(plan),
                "repositoryLimits": repository_limits(plan),
                "agentConfig": agent_configs[plan],
                "source": "database",
            }
            for plan in PLAN_IDS
        ],
        "agentConfigs": agent_configs,
        "capabilities": review_agent_capabilities(),
    }


def update_review_agent_config(plan: str, payload: dict) -> dict:
    normalized_plan = normalize_plan(plan, default="")
    if normalized_plan not in PLAN_IDS:
        raise ValueError("Unknown subscription plan.")
    if not isinstance(payload, dict):
        raise ValueError("Plan agent config update must be a JSON object.")
    if "providerChain" in payload or "provider_chain" in payload:
        raise ValueError("provider is required; providerChain is not supported.")
    for provider in REVIEW_AGENT_PROVIDERS:
        provider_payload = payload.get(provider)
        if isinstance(provider_payload, dict) and "reasoning_effort" in provider_payload:
            raise ValueError(f"{provider}.reasoningEffort is required; reasoning_effort is not supported.")
    state = review_agent_config_state()
    current = copy.deepcopy(state["plans"][normalized_plan])
    if "provider" in payload:
        current["provider"] = clean_review_agent_provider_required(payload.get("provider"))
    for provider in REVIEW_AGENT_PROVIDERS:
        if provider in payload:
            provider_payload = payload[provider]
            if isinstance(provider_payload, dict) and "reasoningEffort" in provider_payload:
                requested_model = (
                    clean_review_agent_config_text(provider_payload.get("model"))
                    or current[provider]["model"]
                )
                requested_effort = clean_review_agent_config_text(
                    provider_payload.get("reasoningEffort")
                ).lower()
                supported_efforts = review_agent_effort_options(requested_model)
                if requested_effort not in supported_efforts:
                    raise ValueError(
                        f"{provider}.reasoningEffort {requested_effort or '(empty)'} is not supported "
                        f"by model {requested_model}. Supported values: {', '.join(supported_efforts)}."
                    )
            current[provider] = normalize_review_agent_provider_config(
                provider,
                provider_payload,
                current[provider],
            )
    if "reviewWorker" in payload:
        current["reviewWorker"] = normalize_review_agent_review_worker_config(
            payload.get("reviewWorker"),
            current.get("reviewWorker") or default_review_agent_plan_config(normalized_plan)["reviewWorker"],
        )
    state["plans"][normalized_plan] = normalize_review_agent_plan_config(normalized_plan, current)
    db.save_state_item(REVIEW_AGENT_CONFIG_STATE_KEY, state)
    return review_agent_config(normalized_plan)


def creem_product_id(interval: str, plan: str = "pro") -> str:
    product = creem_product_for_interval(interval, plan=plan)
    if isinstance(product, dict):
        product_id = product.get("id")
        return product_id if isinstance(product_id, str) else ""
    return ""


def creem_configured() -> bool:
    return bool(env("PULLWISE_CREEM_API_KEY") and creem_configured_product_ids())


def selected_provider() -> str:
    return "creem" if creem_configured() else "disabled"


def creem_configured_product_ids_for_plan(plan: str) -> list[str]:
    normalized_plan = normalize_plan(plan)
    if normalized_plan not in PAID_PLAN_IDS:
        return []
    return creem_configured_paid_product_ids(normalized_plan)


def creem_configured_paid_product_ids(plan: str) -> list[str]:
    normalized_plan = normalize_plan(plan)
    if normalized_plan not in PAID_PLAN_IDS:
        return []
    product_ids: list[str] = []
    seen: set[str] = set()
    for raw in system_config.creem_product_ids_for_plan(normalized_plan):
        product_id = str(raw or "").strip()
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


def creem_get_json(path: str, *, action: str, params: dict | None = None) -> dict:
    try:
        response = requests.get(
            urljoin(creem_api_base_url() + "/", path),
            headers=creem_api_headers(),
            params=params,
            timeout=billing_timeout_seconds(),
        )
    except requests.RequestException as exc:
        raise BillingProviderResponseError(f"Creem {action} request failed.") from exc
    return creem_response_json(response, action)


def creem_post_json(path: str, *, action: str, payload: dict | None = None) -> dict:
    request_kwargs = {
        "headers": creem_api_headers(),
        "timeout": billing_timeout_seconds(),
    }
    if payload is not None:
        request_kwargs["json"] = payload
    try:
        response = requests.post(
            urljoin(creem_api_base_url() + "/", path),
            **request_kwargs,
        )
    except requests.RequestException as exc:
        raise BillingProviderResponseError(f"Creem {action} request failed.") from exc
    return creem_response_json(response, action)


def creem_response_json(response: requests.Response, action: str) -> dict:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise BillingProviderResponseError(creem_error_message(response, action)) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise BillingProviderResponseError(f"Creem {action} did not return valid JSON.") from exc
    if not isinstance(payload, dict):
        raise BillingProviderResponseError(f"Creem {action} did not return a valid JSON object.")
    return payload


def creem_error_message(response: requests.Response, action: str) -> str:
    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    provider_status = ""
    error = ""
    trace_id = ""
    messages: list[str] = []
    if isinstance(payload, dict):
        provider_status = clean_creem_error_text(payload.get("status"))
        error = clean_creem_error_text(payload.get("error"))
        trace_id = clean_creem_error_text(payload.get("trace_id"))
        raw_messages = payload.get("message")
        if isinstance(raw_messages, list):
            messages = [message for message in (clean_creem_error_text(item) for item in raw_messages) if message]
        else:
            message = clean_creem_error_text(raw_messages)
            if message:
                messages = [message]
    status = provider_status or clean_creem_error_text(getattr(response, "status_code", ""))
    detail = "; ".join(messages) or error
    message = f"Creem {action} failed"
    if status:
        message += f" (status {status})"
    if detail:
        message += f": {detail}"
    if trace_id:
        message += f". Trace ID: {trace_id}"
    return message + "."


def clean_creem_error_text(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def fetch_creem_product(product_id: str) -> dict:
    return creem_get_json("v1/products", action="product lookup", params={"product_id": product_id})


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
        "checkoutTimeoutMs": billing_timeout_seconds() * 1000,
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
                "repositoryLimits": repository_limits("free"),
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
        "repositoryLimits": repository_limits(normalized_plan),
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
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/settings?billing=success"


def default_cancel_url() -> str:
    return f"{env('PULLWISE_APP_URL', 'http://localhost:5173').rstrip('/')}/settings?billing=cancel"


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
        return create_creem_checkout_session(
            user,
            success_url=success_url or default_success_url(),
            cancel_url=cancel_url or default_cancel_url(),
            plan=plan,
            interval=interval,
        )
    raise BillingConfigurationError("Billing is not configured.")


def validate_checkout_selection(plan: str, interval: str) -> tuple[str, str]:
    normalized_plan = normalize_plan(plan)
    normalized_interval = (interval or "month").strip().lower()
    if normalized_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("Only paid plans can be purchased.")
    if normalized_interval not in {"month", "year"}:
        raise BillingConfigurationError("Billing interval must be month or year.")
    return normalized_plan, normalized_interval


def creem_checkout_request_id(user_id: object, *, product_id: str, plan: str, interval: str, now: float | None = None) -> str:
    user_text = str(user_id or "").strip()
    bucket = int((time.time() if now is None else now) // CREEM_CHECKOUT_REQUEST_ID_WINDOW_SECONDS)
    source = "\0".join([user_text, product_id, plan, interval, str(bucket)])
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    safe_user = "".join(char if char.isalnum() else "_" for char in user_text)[:32].strip("_") or "user"
    return f"pw_checkout_{safe_user}_{digest}"


def create_creem_checkout_session(user: dict, *, success_url: str, cancel_url: str, plan: str, interval: str) -> dict:
    success_url = request_redirect_url(success_url, default_success_url(), "success")
    request_redirect_url(cancel_url, default_cancel_url(), "cancel")
    product_id = creem_product_id(interval, plan=plan)
    if not product_id:
        raise BillingConfigurationError(f"Creem {plan.title()} {interval} product is not configured.")
    request_id = creem_checkout_request_id(user["id"], product_id=product_id, plan=plan, interval=interval)
    existing_customer_id = None
    raw_existing_customer_id = (user.get("billing") or {}).get("customerId")
    if isinstance(raw_existing_customer_id, str) and raw_existing_customer_id.strip():
        existing_customer_id = raw_existing_customer_id.strip()
    customer = {}
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
    response_payload = creem_post_json("v1/checkouts", action="checkout", payload=payload)
    checkout_url = provider_redirect_url(response_payload.get("checkout_url"), "Creem", "Checkout")
    returned_customer = response_payload.get("customer")
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
        "id": response_payload.get("id"),
        "customerId": returned_customer_id or existing_customer_id,
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
    status = (billing.get("status") or "").lower()
    if status not in PAID_PLAN_CHANGE_STATUSES:
        raise BillingConfigurationError("Only active subscriptions can be changed.")

    provider = selected_provider()
    if provider == "creem":
        return create_creem_subscription_change(billing, plan=target_plan, interval=target_interval, resume_first=status == "canceling")
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_subscription_change(billing: dict, *, plan: str, interval: str, resume_first: bool = False) -> dict:
    subscription_id = billing.get("subscriptionId")
    product_id = creem_product_id(interval, plan=plan)
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    if not product_id:
        raise BillingConfigurationError(f"Creem {plan.title()} {interval} product is not configured.")
    if resume_first:
        create_creem_subscription_resume(billing)
    update_behavior = creem_subscription_update_behavior(
        normalize_plan(billing.get("plan") or "pro"),
        normalize_interval(billing.get("interval")),
        plan,
        interval,
    )
    payload = creem_post_json(
        f"v1/subscriptions/{subscription_id}/upgrade",
        action="subscription upgrade",
        payload={
            "product_id": product_id,
            "update_behavior": update_behavior,
        },
    )
    normalized_status = normalize_subscription_status(payload.get("status"))
    cancel_at_period_end = payload.get("cancel_at_period_end")
    return {
        "provider": "creem",
        "plan": plan,
        "interval": interval,
        "updateBehavior": update_behavior,
        "subscriptionId": payload.get("id") or subscription_id,
        "status": normalized_status,
        "cancelAtPeriodEnd": cancel_at_period_end if isinstance(cancel_at_period_end, bool) else normalized_status == "canceling",
        "canceledAt": payload.get("canceled_at") if normalized_status == "canceling" else None,
        "currentPeriodStart": payload.get("current_period_start_date"),
        "currentPeriodEnd": payload.get("current_period_end_date"),
    }


def creem_subscription_update_behavior(current_plan: str, current_interval: str, target_plan: str, target_interval: str) -> str:
    if not subscription_change_is_upgrade(current_plan, current_interval, target_plan, target_interval):
        raise BillingConfigurationError("Only subscription upgrades are supported.")
    return system_config.creem_upgrade_behavior()


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
    if provider == "creem":
        return create_creem_subscription_cancel(billing, mode=normalized_mode)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_subscription_cancel(billing: dict, *, mode: str = "scheduled") -> dict:
    subscription_id = billing.get("subscriptionId")
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    payload = creem_post_json(
        f"v1/subscriptions/{subscription_id}/cancel",
        action="subscription cancellation",
        payload={"mode": mode},
    )
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


def resume_subscription(user: dict, *, return_url: str | None = None) -> dict:
    billing = user.get("billing") or {}
    current_plan = normalize_plan(billing.get("plan") or "free", default="free")
    if current_plan not in PAID_PLAN_IDS:
        raise BillingConfigurationError("No paid subscription is linked to this account.")
    status = (billing.get("status") or "").lower()
    if status in {"active", "trialing"}:
        return {
            "provider": billing.get("provider") or selected_provider(),
            "plan": current_plan,
            "interval": normalize_interval(billing.get("interval")),
            "status": status,
            "cancelAtPeriodEnd": False,
            "canceledAt": None,
            "alreadyActive": True,
        }
    if status != "canceling":
        raise BillingConfigurationError("Only scheduled cancellations can be resumed.")
    provider = selected_provider()
    if provider == "creem":
        return create_creem_subscription_resume(billing)
    raise BillingConfigurationError("Billing is not configured.")


def create_creem_subscription_resume(billing: dict) -> dict:
    subscription_id = billing.get("subscriptionId")
    if not subscription_id:
        raise BillingConfigurationError("No active Creem subscription is linked to this account.")
    payload = creem_post_json(
        f"v1/subscriptions/{subscription_id}/resume",
        action="subscription resume",
    )
    normalized_status = normalize_subscription_status(payload.get("status"))
    cancel_at_period_end = payload.get("cancel_at_period_end")
    return {
        "provider": "creem",
        "plan": normalize_plan(billing.get("plan") or "pro"),
        "interval": normalize_interval(billing.get("interval")),
        "subscriptionId": payload.get("id") or subscription_id,
        "status": normalized_status,
        "cancelAtPeriodEnd": cancel_at_period_end if isinstance(cancel_at_period_end, bool) else normalized_status == "canceling",
        "canceledAt": payload.get("canceled_at") if normalized_status == "canceling" else None,
        "currentPeriodStart": payload.get("current_period_start_date"),
        "currentPeriodEnd": payload.get("current_period_end_date"),
    }


def creem_api_base_url() -> str:
    configured = system_config.creem_api_base_url().strip().rstrip("/")
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BillingConfigurationError("Creem API base URL must be an absolute HTTP(S) URL.")
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
        interval_from_creem_product(product)
        or metadata_value("interval", metadata, checkout_metadata, order_metadata, subscription_metadata)
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


def event_created(event: dict) -> int | float | None:
    value = event.get("created") or event.get("createdAt") or event.get("created_at")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        candidate = float(value)
        if candidate >= 10_000_000_000:
            candidate /= 1000
        return int(candidate) if candidate.is_integer() else candidate
    if isinstance(value, str) and value.isdigit():
        candidate = int(value)
        if candidate >= 10_000_000_000:
            seconds = candidate / 1000
            return int(seconds) if seconds.is_integer() else seconds
        return candidate
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


def interval_from_configured_creem_product_id(product_id: object) -> str | None:
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


def interval_from_creem_product(product: dict | None) -> str | None:
    if not isinstance(product, dict):
        return None
    period = str(product.get("billing_period") or "").strip().lower()
    if period in {"every-year", "year", "yearly", "annual", "annually"}:
        return "year"
    if period in {"every-month", "month", "monthly"}:
        return "month"
    inferred = interval_from_configured_creem_product_id(product.get("id"))
    if inferred:
        return inferred
    return None
