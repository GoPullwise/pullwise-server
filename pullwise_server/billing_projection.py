"""Pure Billing account DTO over persisted payment facts and product usage."""
from __future__ import annotations

from .api_key_dto_rules import _timestamp
from .creem_event_rules import normalize_interval


_STATUSES = frozenset({"none", "active", "trialing", "canceling",
                       "past_due", "unpaid", "paused", "canceled"})
_PAID = frozenset({"pro", "max"})
_PLANS = frozenset({"free", "pro", "max"})


def _text(value: object) -> str | None:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        return None
    return value.strip() or None


def _status(value: object) -> str:
    text = (_text(value) or "").lower()
    return text if text in _STATUSES else "none"


def subscription_event_dto(record: dict) -> dict:
    plan = _text(record.get("plan"))
    return {"provider": _text(record.get("provider")),
        "customerId": _text(record.get("customerId")),
        "customerEmail": _text(record.get("customerEmail")),
        "subscriptionId": _text(record.get("subscriptionId")),
        "subscriptionItemId": _text(record.get("subscriptionItemId")),
        "status": _status(record.get("status")),
        "plan": plan if plan in _PLANS else None,
        "interval": normalize_interval(record.get("interval")),
        "currentPeriodStart": _timestamp(record.get("currentPeriodStart")),
        "currentPeriodEnd": _timestamp(record.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": record.get("cancelAtPeriodEnd") if isinstance(record.get("cancelAtPeriodEnd"), bool) else None,
        "canceledAt": _timestamp(record.get("canceledAt")),
        "eventType": _text(record.get("eventType")),
        "eventId": _text(record.get("eventId")),
        "eventCreated": _timestamp(record.get("eventCreated")),
        "processedAt": _timestamp(record.get("processedAt")),
        "stale": record.get("stale") if isinstance(record.get("stale"), bool) else False}


def subscription_events_dto(user: dict) -> list[dict]:
    records = user.get("billingSubscriptionEvents")
    result = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        event = subscription_event_dto(record)
        if event["eventId"] and (event["subscriptionId"] or event["customerId"]):
            result.append(event)
    result.sort(key=lambda item: (item.get("eventCreated") or 0,
                                  item.get("processedAt") or 0), reverse=True)
    return result[:50]


def billing_account_dto(user: dict, product_usage: dict,
                        processing_activity: list[dict]) -> dict:
    current = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    plan = product_usage["plan"]
    return {"provider": _text(current.get("provider")),
        "status": _status(current.get("status")),
        "plan": plan,
        "interval": normalize_interval(current.get("interval") if plan in _PAID else "month"),
        "customerId": _text(current.get("customerId")),
        "subscriptionId": _text(current.get("subscriptionId")),
        "subscriptionItemId": _text(current.get("subscriptionItemId")),
        "customerEmail": _text(current.get("customerEmail")),
        "currentPeriodStart": _timestamp(current.get("currentPeriodStart")),
        "currentPeriodEnd": _timestamp(current.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": current.get("cancelAtPeriodEnd") if isinstance(current.get("cancelAtPeriodEnd"), bool) else None,
        "canceledAt": _timestamp(current.get("canceledAt")),
        "lastEventId": _text(current.get("lastEventId")),
        "lastEventType": _text(current.get("lastEventType")),
        "lastEventCreated": _timestamp(current.get("lastEventCreated")),
        "updatedAt": _timestamp(current.get("updatedAt")),
        "entitlements": product_usage["entitlements"],
        "usage": product_usage["usage"],
        "runtimeUsage": product_usage["runtimeUsage"],
        "processingActivity": list(processing_activity),
        "subscriptionEvents": subscription_events_dto(user)}
