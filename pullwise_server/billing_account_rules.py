"""Pure account and audit decision for an accepted billing update."""
from __future__ import annotations

from copy import deepcopy
import math

from .creem_event_rules import PLAN_IDS

MAX_BILLING_SUBSCRIPTION_RECORDS = 25
MAX_BILLING_SUBSCRIPTION_EVENTS = 100
MAX_BILLING_EVENT_RECORDS = 5000
MAX_BILLING_PENDING_UPDATES = 1000


def billing_update_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    return text


def billing_event_id(update: dict) -> str:
    return billing_update_text(update.get("eventId"))


def billing_update_scalar(value: object) -> object | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int | float):
        return value if math.isfinite(value) else None
    return None


def billing_update_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def billing_event_created(update: dict) -> int | float | None:
    value = update.get("eventCreated")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        candidate = float(value)
        return int(candidate) if candidate.is_integer() else candidate
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def billing_update_matches_user(update: dict, user: dict) -> bool:
    current = user.get("billing") or {}
    checkout = user.get("billingCheckout") if isinstance(user.get("billingCheckout"), dict) else {}
    customer_id = billing_update_text(update.get("customerId"))
    subscription_id = billing_update_text(update.get("subscriptionId"))
    user_id = billing_update_text(update.get("userId"))
    request_id = billing_update_text(update.get("requestId"))
    if customer_id and current.get("customerId") == customer_id:
        return True
    if subscription_id and current.get("subscriptionId") == subscription_id:
        return True
    if request_id and checkout.get("requestId") == request_id:
        return True
    return bool(user_id and user_id == user.get("id"))


def billing_event_record(update: dict, *, processed_at: int,
                         applied: bool, stale: bool = False) -> dict | None:
    if not billing_event_id(update):
        return None
    return {"eventType": billing_update_text(update.get("eventType")) or None,
            "eventCreated": billing_event_created(update),
            "processedAt": processed_at, "applied": applied, "stale": stale}


def with_billing_event(events: dict, event_id: str, record: dict,
                       *, limit: int = MAX_BILLING_EVENT_RECORDS) -> dict:
    next_events = {**events, event_id: record}
    if len(next_events) > limit:
        ordered = sorted(next_events.items(), key=lambda item: item[1].get("processedAt") or 0)
        for old_id, _ in ordered[:len(next_events) - limit]:
            next_events.pop(old_id, None)
    return next_events


def upsert_billing_subscription_record(user: dict, billing_state: dict,
                                       *, processed_at: int) -> None:
    subscription_id = billing_update_text(billing_state.get("subscriptionId"))
    customer_id = billing_update_text(billing_state.get("customerId"))
    provider = billing_update_text(billing_state.get("provider"))
    if not (subscription_id or customer_id):
        return
    record = {
        "provider": provider or None,
        "customerId": customer_id or None,
        "customerEmail": billing_update_text(billing_state.get("customerEmail")) or None,
        "subscriptionId": subscription_id or None,
        "subscriptionItemId": billing_update_text(billing_state.get("subscriptionItemId")) or None,
        "status": billing_update_text(billing_state.get("status")) or None,
        "plan": billing_update_text(billing_state.get("plan")) or None,
        "interval": billing_update_text(billing_state.get("interval")) or None,
        "currentPeriodStart": billing_update_scalar(billing_state.get("currentPeriodStart")),
        "currentPeriodEnd": billing_update_scalar(billing_state.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": billing_update_bool(billing_state.get("cancelAtPeriodEnd")),
        "canceledAt": billing_update_scalar(billing_state.get("canceledAt")),
        "lastEventType": billing_update_text(billing_state.get("lastEventType")) or None,
        "lastEventId": billing_update_text(billing_state.get("lastEventId")) or None,
        "lastEventCreated": billing_event_created({"eventCreated": billing_state.get("lastEventCreated")}),
        "updatedAt": billing_event_created({"eventCreated": billing_state.get("updatedAt")}) or processed_at,
    }
    existing_records = user.get("billingSubscriptions") if isinstance(user.get("billingSubscriptions"), list) else []
    records = [item for item in existing_records if isinstance(item, dict)]
    replaced = False
    for index, existing in enumerate(records):
        existing_subscription_id = billing_update_text(existing.get("subscriptionId"))
        existing_customer_id = billing_update_text(existing.get("customerId"))
        existing_provider = billing_update_text(existing.get("provider"))
        matches_subscription = bool(subscription_id and existing_subscription_id == subscription_id)
        matches_customer = bool(not subscription_id and customer_id and existing_customer_id == customer_id and existing_provider == provider)
        if matches_subscription or matches_customer:
            records[index] = {**existing, **record}
            replaced = True
            break
    if not replaced:
        records.insert(0, record)
    records.sort(key=lambda item: billing_event_created({"eventCreated": item.get("updatedAt")}) or 0, reverse=True)
    user["billingSubscriptions"] = records[:MAX_BILLING_SUBSCRIPTION_RECORDS]


def append_billing_subscription_event(user: dict, update: dict, billing_state: dict,
                                      *, processed_at: int, stale: bool = False) -> None:
    event_id = billing_event_id(update)
    event_type = billing_update_text(update.get("eventType"))
    if not event_id or not event_type:
        return
    subscription_id = billing_update_text(update.get("subscriptionId")) or billing_update_text(billing_state.get("subscriptionId"))
    customer_id = billing_update_text(update.get("customerId")) or billing_update_text(billing_state.get("customerId"))
    provider = billing_update_text(update.get("provider")) or billing_update_text(billing_state.get("provider"))
    if not (subscription_id or customer_id):
        return
    record = {
        "provider": provider or None,
        "customerId": customer_id or None,
        "customerEmail": billing_update_text(update.get("customerEmail")) or billing_update_text(billing_state.get("customerEmail")) or None,
        "subscriptionId": subscription_id or None,
        "subscriptionItemId": billing_update_text(update.get("subscriptionItemId")) or billing_update_text(billing_state.get("subscriptionItemId")) or None,
        "status": billing_update_text(update.get("status")) or billing_update_text(billing_state.get("status")) or None,
        "plan": billing_update_text(update.get("plan")) or billing_update_text(billing_state.get("plan")) or None,
        "interval": billing_update_text(update.get("interval")) or billing_update_text(billing_state.get("interval")) or None,
        "currentPeriodStart": billing_update_scalar(update.get("currentPeriodStart")) if billing_update_scalar(update.get("currentPeriodStart")) is not None else billing_update_scalar(billing_state.get("currentPeriodStart")),
        "currentPeriodEnd": billing_update_scalar(update.get("currentPeriodEnd")) if billing_update_scalar(update.get("currentPeriodEnd")) is not None else billing_update_scalar(billing_state.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": billing_update_bool(update.get("cancelAtPeriodEnd")) if billing_update_bool(update.get("cancelAtPeriodEnd")) is not None else billing_update_bool(billing_state.get("cancelAtPeriodEnd")),
        "canceledAt": billing_update_scalar(update.get("canceledAt")) if billing_update_scalar(update.get("canceledAt")) is not None else billing_update_scalar(billing_state.get("canceledAt")),
        "eventType": event_type,
        "eventId": event_id,
        "eventCreated": billing_event_created(update),
        "processedAt": processed_at,
        "stale": stale,
    }
    existing_events = user.get("billingSubscriptionEvents") if isinstance(user.get("billingSubscriptionEvents"), list) else []
    events = [item for item in existing_events if isinstance(item, dict) and billing_update_text(item.get("eventId")) != event_id]
    events.insert(0, record)
    events.sort(key=lambda item: (
        billing_event_created({"eventCreated": item.get("eventCreated")}) or 0,
        billing_event_created({"eventCreated": item.get("processedAt")}) or 0,
    ), reverse=True)
    user["billingSubscriptionEvents"] = events[:MAX_BILLING_SUBSCRIPTION_EVENTS]


def reduce_billing_update(user: dict, update: dict, *, processed_at: int) -> dict:
    """Return the account and event write set; never mutate the input account."""
    next_user = deepcopy(user)
    current = next_user.get("billing") or {}
    incoming_created = billing_event_created(update)
    current_created = billing_event_created({"eventCreated": current.get("lastEventCreated")})
    stale = current_created is not None and (incoming_created is None or incoming_created < current_created)
    if stale:
        append_billing_subscription_event(next_user, update, current,
            stale=True, processed_at=processed_at)
        return {"user": next_user, "eventRecord": billing_event_record(update,
            processed_at=processed_at, applied=False, stale=True),
            "applied": False, "quotaRefresh": False}

    customer_id = billing_update_text(update.get("customerId"))
    customer_email = billing_update_text(update.get("customerEmail"))
    subscription_id = billing_update_text(update.get("subscriptionId"))
    subscription_item_id = billing_update_text(update.get("subscriptionItemId"))
    status = billing_update_text(update.get("status"))
    plan = billing_update_text(update.get("plan"))
    if plan and plan not in set(PLAN_IDS):
        plan = ""
    interval = billing_update_text(update.get("interval"))
    current_period_start = billing_update_scalar(update.get("currentPeriodStart"))
    current_period_end = billing_update_scalar(update.get("currentPeriodEnd"))
    cancel_at_period_end = billing_update_bool(update.get("cancelAtPeriodEnd"))
    canceled_at = billing_update_scalar(update.get("canceledAt"))
    provider = billing_update_text(update.get("provider"))
    event_type = billing_update_text(update.get("eventType"))
    event_id = billing_event_id(update)
    request_id = billing_update_text(update.get("requestId"))
    next_user["billing"] = {
        **current,
        "provider": provider or current.get("provider"),
        "customerId": customer_id or current.get("customerId"),
        "customerEmail": customer_email or current.get("customerEmail"),
        "subscriptionId": subscription_id or current.get("subscriptionId"),
        "subscriptionItemId": subscription_item_id or current.get("subscriptionItemId"),
        "status": status or current.get("status") or "active",
        "plan": plan or current.get("plan") or "pro",
        "interval": interval or current.get("interval") or "month",
        "currentPeriodStart": current_period_start if current_period_start is not None else current.get("currentPeriodStart"),
        "currentPeriodEnd": current_period_end if current_period_end is not None else current.get("currentPeriodEnd"),
        "cancelAtPeriodEnd": cancel_at_period_end if cancel_at_period_end is not None else current.get("cancelAtPeriodEnd"),
        "canceledAt": canceled_at if canceled_at is not None else current.get("canceledAt"),
        "updatedAt": processed_at,
        "lastEventType": event_type or current.get("lastEventType"),
        "lastEventId": event_id or current.get("lastEventId"),
        "lastEventCreated": incoming_created if incoming_created is not None else current.get("lastEventCreated"),
    }
    checkout = next_user.get("billingCheckout") if isinstance(next_user.get("billingCheckout"), dict) else {}
    if request_id and checkout.get("requestId") == request_id:
        next_user["billingCheckout"] = {**checkout, "status": "completed",
            "completedAt": processed_at, "eventId": event_id or checkout.get("eventId")}
    upsert_billing_subscription_record(next_user, next_user["billing"], processed_at=processed_at)
    append_billing_subscription_event(next_user, update, next_user["billing"],
        processed_at=processed_at)
    return {"user": next_user, "eventRecord": billing_event_record(update,
        processed_at=processed_at, applied=True), "applied": True, "quotaRefresh": True}
