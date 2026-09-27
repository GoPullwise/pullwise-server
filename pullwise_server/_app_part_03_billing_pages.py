from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_02_http_auth_settings as _previous_app_part
from . import account_cycle_rules
from ._app_imports import import_compat_globals as _import_compat_globals
from .api_key_dto_rules import api_key_public_payload as _pure_api_key_public_payload
from .api_key_dto_rules import requested_api_key_scopes as _pure_requested_api_key_scopes
from .api_key_dto_rules import parse_api_key_restrictions as _pure_parse_api_key_restrictions
from .product_store import ProductStore as _ProductStore
from .entitlements import product_usage_payload as _product_usage_payload
from .product_billing_projection import (
    billing_account_dto as _pure_billing_account_dto,
    subscription_event_dto as _pure_subscription_event_dto,
    subscription_events_dto as _pure_subscription_events_dto,
)

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part





def effective_billing_plan(user: dict | None) -> str:
    return account_cycle_rules.effective_user_plan(user)


def user_billing_state(user: dict) -> dict:
    return user.get("billing") if isinstance(user.get("billing"), dict) else {}


def non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, candidate)








def billing_subscription_event_payload(record: dict) -> dict:
    return _pure_subscription_event_dto(record)


def billing_subscription_events_payload(user: dict) -> list[dict]:
    return _pure_subscription_events_dto(user)


BILLING_QUOTA_ACTIVITY_LIMIT = 100












def billing_account_payload(user: dict) -> dict:
    store = _ProductStore(db.database_path())
    store.initialize()
    product = _product_usage_payload(store, user)
    activity = store.list_processing_usage_events(user["id"], limit=20)["items"]
    return _pure_billing_account_dto(user, product, activity)


def clean_api_key_scopes(value: object) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        candidates = API_KEY_DEFAULT_SCOPES
    scopes: list[str] = []
    for scope in candidates:
        normalized = scope.strip().lower()
        if normalized in API_KEY_ALLOWED_SCOPES and normalized not in scopes:
            scopes.append(normalized)
    return scopes


def requested_api_key_scopes(value: object, *, provided: bool) -> tuple[list[str], str | None]:
    return _pure_requested_api_key_scopes(value, provided=provided)






def parse_api_key_scopes(value: object) -> list[str]:
    if isinstance(value, list):
        return clean_api_key_scopes(value)
    if not isinstance(value, str):
        return list(API_KEY_DEFAULT_SCOPES)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return clean_api_key_scopes(decoded)


def parse_api_key_restrictions(value: object) -> dict:
    return _pure_parse_api_key_restrictions(value)


def api_key_public_payload(record: dict, *, token: str | None = None) -> dict:
    return _pure_api_key_public_payload(record, token=token)


def navigation_payload() -> dict:
    return {
        "top": [
            {"id": "product", "label": "Product", "href": "/"},
            {"id": "pricing", "label": "Pricing", "href": "/pricing"},
            {"id": "api", "label": "API", "href": "/api-docs"},
        ],
        "dashboard": [
            {"id": "overview", "label": "Overview", "href": "/dashboard/overview"},
            {"id": "repositories", "label": "Repositories", "href": "/repositories"},
            {"id": "api-keys", "label": "API Keys", "href": "/api-keys"},
            {"id": "billing", "label": "Billing", "href": "/billing"},
        ],
    }


def pricing_payload(user: dict | None = None) -> dict:
    payload = billing.public_plan()
    payload["page"] = {
        "id": "pricing",
        "checkoutAction": {"method": "POST", "href": "/billing/checkout-sessions"},
        "billingRoute": "/billing",
    }
    if user:
        payload["account"] = billing_account_payload(user)
    return payload


def billing_page_payload(user: dict) -> dict:
    return {
        "page": {
            "id": "billing",
            "subscriptionAction": {"label": "View pricing", "href": "/pricing"},
            "checkoutAction": None,
        },
        "account": billing_account_payload(user),
    }








def public_billing_text(value: object) -> str | None:
    return public_issue_text(value) or None


def public_billing_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in BILLING_PUBLIC_STATUSES else "none"


def safe_billing_redirect_response(result: dict, label: str, *, require_url: bool = False) -> dict:
    if not isinstance(result, dict):
        raise billing.BillingProviderResponseError("Billing provider returned an invalid response.")
    payload = dict(result)
    provider = public_billing_text(payload.get("provider")) or "Billing provider"
    if "url" not in payload:
        if require_url:
            billing.provider_redirect_url(None, provider, label)
        return payload
    payload["url"] = billing.provider_redirect_url(payload.get("url"), provider, label)
    return payload
