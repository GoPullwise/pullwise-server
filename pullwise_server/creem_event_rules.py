"""Pure Creem event normalization shared by Server and Python Worker."""
from __future__ import annotations

import math

PAID_PLAN_IDS = ("pro", "max")
PLAN_IDS = ("free", *PAID_PLAN_IDS)
PAID_PLAN_ENTITLEMENT_STATUSES = {"active", "trialing", "canceling"}


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


def creem_product_configured_for_plan(product: dict | None, plan: str, configured_ids_by_plan: dict) -> bool:
    product_id = object_id(product)
    return bool(product_id and product_id in configured_ids_by_plan.get(plan, ()))


def creem_product_configured_for_pro(product: dict | None, configured_ids_by_plan: dict) -> bool:
    return creem_product_configured_for_plan(product, "pro", configured_ids_by_plan)


def creem_plan_from_product(product: dict | None, configured_ids_by_plan: dict) -> str | None:
    product_id = object_id(product)
    if not product_id:
        return None
    for plan in PAID_PLAN_IDS:
        if product_id in configured_ids_by_plan.get(plan, ()):
            return plan
    return None


def billing_update_from_creem_event(event: dict, configured_ids_by_plan: dict) -> dict | None:
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

    product_plan = creem_plan_from_product(product, configured_ids_by_plan)
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
    if plan in PAID_PLAN_IDS and status in PAID_PLAN_ENTITLEMENT_STATUSES and not creem_product_configured_for_plan(product, plan, configured_ids_by_plan):
        return None
    interval = normalize_interval(
        interval_from_creem_product(product, configured_ids_by_plan)
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


def interval_from_configured_creem_product_id(product_id: object, configured_ids_by_plan: dict) -> str | None:
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    normalized_product_id = product_id.strip()
    for plan in PAID_PLAN_IDS:
        configured_ids = list(configured_ids_by_plan.get(plan, ()))
        if configured_ids[:1] == [normalized_product_id]:
            return "month"
        if len(configured_ids) > 1 and configured_ids[1] == normalized_product_id:
            return "year"
    return None


def interval_from_creem_product(product: dict | None, configured_ids_by_plan: dict) -> str | None:
    if not isinstance(product, dict):
        return None
    period = str(product.get("billing_period") or "").strip().lower()
    if period in {"every-year", "year", "yearly", "annual", "annually"}:
        return "year"
    if period in {"every-month", "month", "monthly"}:
        return "month"
    inferred = interval_from_configured_creem_product_id(product.get("id"), configured_ids_by_plan)
    if inferred:
        return inferred
    return None
