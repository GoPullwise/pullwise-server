from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_08_fix_pr_repository_access as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

MAX_BILLING_SUBSCRIPTION_RECORDS = 25
MAX_BILLING_SUBSCRIPTION_EVENTS = 100


def billing_event_id(update: dict) -> str:
    return billing_update_text(update.get("eventId"))


def billing_update_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    return text


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


def billing_event_processed(update: dict) -> bool:
    event_id = billing_event_id(update)
    return bool(event_id and event_id in BILLING_EVENTS)


def remember_billing_event(update: dict, *, applied: bool, stale: bool = False) -> None:
    event_id = billing_event_id(update)
    if not event_id:
        return
    BILLING_EVENTS[event_id] = {
        "eventType": billing_update_text(update.get("eventType")) or None,
        "eventCreated": billing_event_created(update),
        "processedAt": now(),
        "applied": applied,
        "stale": stale,
    }
    prune_billing_events()
    mark_state_dirty()


def prune_billing_events() -> None:
    if len(BILLING_EVENTS) <= MAX_BILLING_EVENT_RECORDS:
        return
    ordered = sorted(BILLING_EVENTS.items(), key=lambda item: item[1].get("processedAt") or 0)
    for event_id, _record in ordered[: len(BILLING_EVENTS) - MAX_BILLING_EVENT_RECORDS]:
        BILLING_EVENTS.pop(event_id, None)


def remember_pending_billing_update(update: dict) -> None:
    if not (billing_update_text(update.get("customerId")) or billing_update_text(update.get("subscriptionId"))):
        return
    event_id = billing_event_id(update)
    if event_id and any(billing_event_id(candidate) == event_id for candidate in BILLING_PENDING_UPDATES):
        return
    if billing_event_processed(update):
        return
    BILLING_PENDING_UPDATES.append(dict(update))
    if len(BILLING_PENDING_UPDATES) > MAX_BILLING_PENDING_UPDATES:
        del BILLING_PENDING_UPDATES[: len(BILLING_PENDING_UPDATES) - MAX_BILLING_PENDING_UPDATES]
    mark_state_dirty()


def billing_user_for_update(update: dict) -> dict | None:
    user = USERS.get(billing_update_text(update.get("userId")))
    if user:
        return user
    for candidate in USERS.values():
        if billing_update_matches_user(update, candidate):
            return candidate
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


def ensure_billing_quota_bucket_for_user(user: dict) -> None:
    entitlement = quota.quota_entitlement_for_user(user)
    quota.ensure_quota_bucket(
        scope_type="user",
        scope_id=str(user["id"]),
        period=entitlement["period"],
        plan=entitlement["plan"],
        limit=entitlement["userLimit"],
        reset_at=entitlement["resetAt"],
    )


def upsert_billing_subscription_record(user: dict, billing_state: dict) -> None:
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
        "updatedAt": billing_event_created({"eventCreated": billing_state.get("updatedAt")}) or now(),
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


def append_billing_subscription_event(user: dict, update: dict, billing_state: dict, *, stale: bool = False, processed_at: int | None = None) -> None:
    event_id = billing_event_id(update)
    event_type = billing_update_text(update.get("eventType"))
    if not event_id or not event_type:
        return

    subscription_id = billing_update_text(update.get("subscriptionId")) or billing_update_text(billing_state.get("subscriptionId"))
    customer_id = billing_update_text(update.get("customerId")) or billing_update_text(billing_state.get("customerId"))
    provider = billing_update_text(update.get("provider")) or billing_update_text(billing_state.get("provider"))
    if not (subscription_id or customer_id):
        return

    recorded_at = processed_at or now()
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
        "processedAt": recorded_at,
        "stale": stale,
    }
    existing_events = user.get("billingSubscriptionEvents") if isinstance(user.get("billingSubscriptionEvents"), list) else []
    events = [item for item in existing_events if isinstance(item, dict) and billing_update_text(item.get("eventId")) != event_id]
    events.insert(0, record)
    events.sort(
        key=lambda item: (
            billing_event_created({"eventCreated": item.get("eventCreated")}) or 0,
            billing_event_created({"eventCreated": item.get("processedAt")}) or 0,
        ),
        reverse=True,
    )
    user["billingSubscriptionEvents"] = events[:MAX_BILLING_SUBSCRIPTION_EVENTS]


def apply_billing_update_to_user(user: dict, update: dict) -> bool:
    current = user.get("billing") or {}
    incoming_created = billing_event_created(update)
    current_created = billing_event_created({"eventCreated": current.get("lastEventCreated")})
    if incoming_created is not None and current_created is not None and incoming_created < current_created:
        append_billing_subscription_event(user, update, current, stale=True)
        remember_billing_event(update, applied=False, stale=True)
        return False

    customer_id = billing_update_text(update.get("customerId"))
    customer_email = billing_update_text(update.get("customerEmail"))
    subscription_id = billing_update_text(update.get("subscriptionId"))
    subscription_item_id = billing_update_text(update.get("subscriptionItemId"))
    status = billing_update_text(update.get("status"))
    plan = billing_update_text(update.get("plan"))
    if plan and plan not in set(billing.PLAN_IDS):
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
    updated_at = now()

    user["billing"] = {
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
        "updatedAt": updated_at,
        "lastEventType": event_type or current.get("lastEventType"),
        "lastEventId": event_id or current.get("lastEventId"),
        "lastEventCreated": incoming_created if incoming_created is not None else current.get("lastEventCreated"),
    }
    checkout = user.get("billingCheckout") if isinstance(user.get("billingCheckout"), dict) else {}
    if request_id and checkout.get("requestId") == request_id:
        user["billingCheckout"] = {
            **checkout,
            "status": "completed",
            "completedAt": updated_at,
            "eventId": event_id or checkout.get("eventId"),
        }
    upsert_billing_subscription_record(user, user["billing"])
    append_billing_subscription_event(user, update, user["billing"], processed_at=updated_at)
    ensure_billing_quota_bucket_for_user(user)
    remember_billing_event(update, applied=True)
    mark_state_dirty()
    return True


def apply_pending_billing_updates_for_user(user: dict) -> None:
    matching = []
    remaining = []
    for update in BILLING_PENDING_UPDATES:
        if billing_update_matches_user(update, user):
            matching.append(update)
        else:
            remaining.append(update)
    if not matching:
        return

    BILLING_PENDING_UPDATES[:] = remaining
    mark_state_dirty()
    for update in sorted(matching, key=lambda item: billing_event_created(item) or 0):
        if not billing_event_processed(update):
            apply_billing_update_to_user(user, update)


def cookie_header(session_id: str) -> str:
    return f"{SESSION_COOKIE}={session_id}; {cookie_attributes()}; Max-Age={SESSION_MAX_AGE}"


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; {cookie_attributes()}; Max-Age=0"


def cookie_attributes() -> str:
    attributes = ["Path=/", "HttpOnly", f"SameSite={cookie_same_site()}"]
    if cookie_secure_enabled():
        attributes.append("Secure")
    domain = cookie_domain()
    if domain:
        attributes.append(f"Domain={domain}")
    return "; ".join(attributes)


def cookie_same_site() -> str:
    configured = os.environ.get("PULLWISE_COOKIE_SAME_SITE", "").strip().lower()
    if configured == "none":
        return "None"
    if configured == "strict":
        return "Strict"
    return "Lax"


def cookie_domain() -> str:
    """Extract the registrable domain from the API base URL for cross-subdomain cookie sharing.

    For example, if the API is at api.pull-wise.com, set Domain=.pull-wise.com so the session
    cookie is shared with the frontend at pull-wise.com. Returns empty string for localhost,
    IP addresses, or when no suitable domain can be extracted.
    """
    configured = os.environ.get("PULLWISE_COOKIE_DOMAIN", "").strip()
    if configured:
        return configured if configured.startswith(".") else f".{configured}"
    public_base = os.environ.get("PULLWISE_API_BASE_URL") or ""
    if not public_base.startswith("https://"):
        return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(public_base).hostname or ""
    except Exception:
        return ""
    # Skip localhost, IP addresses, and single-label names.
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return ""
    parts = host.split(".")
    # Need at least two labels (e.g. pull-wise.com) to set a domain cookie.
    if len(parts) < 2:
        return ""
    # Skip IP addresses (all numeric labels).
    if all(part.isdigit() for part in parts):
        return ""
    # Use the last two labels as the registrable domain (e.g. api.pull-wise.com → .pull-wise.com).
    # This is a simplification — for multi-level TLDs like .co.uk, set PULLWISE_COOKIE_DOMAIN explicitly.
    return f".{parts[-2]}.{parts[-1]}"


def cookie_secure_enabled() -> bool:
    if cookie_same_site() == "None":
        return True
    if os.environ.get("PULLWISE_COOKIE_SECURE", "").strip():
        return env_flag("PULLWISE_COOKIE_SECURE")
    public_base = os.environ.get("PULLWISE_API_BASE_URL") or os.environ.get("PULLWISE_APP_URL") or ""
    return public_base.startswith("https://")


def external_api_segments(segments: list[str]) -> list[str] | None:
    if len(segments) >= 2 and segments[0] == "v1":
        if segments[1] in {"workers", "review-runs"}:
            return None
        return segments[1:]
    if len(segments) >= 3 and segments[0] == "api" and segments[1] == "v1":
        if segments[2] in {"workers", "review-runs"}:
            return None
        return segments[2:]
    return None


def request_uses_session_cookie(handler: BaseHTTPRequestHandler) -> bool:
    raw_cookie = request_header(handler, "Cookie") or ""
    if not raw_cookie:
        return False
    cookie = SimpleCookie(raw_cookie)
    morsel = cookie.get(SESSION_COOKIE)
    return bool(morsel and morsel.value)


def request_origin_is_trusted(handler: BaseHTTPRequestHandler) -> bool:
    origin = first_header_value(handler, "Origin")
    if origin:
        return bool(url_origin(origin) in trusted_browser_origins())
    referer = first_header_value(handler, "Referer")
    if referer:
        return bool(url_origin(referer) in trusted_browser_origins())
    return False


def csrf_origin_check_exempt(path: str, segments: list[str]) -> bool:
    return (
        path.startswith("/webhooks/")
        or external_api_segments(segments) is not None
        or bool(segments and segments[0] == "worker")
    )


def cookie_state_change_needs_origin_check(method: str, path: str, segments: list[str], handler: BaseHTTPRequestHandler) -> bool:
    if method not in {"POST", "PATCH", "DELETE"}:
        return False
    if cookie_same_site() != "None":
        return False
    if csrf_origin_check_exempt(path, segments):
        return False
    return request_uses_session_cookie(handler)


def decode_permissions(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def api_repository_payload(row: dict, user: dict | None = None) -> dict:
    repository = db.get_repository(str(row.get("id") or "")) if not row.get("github_repo_id") else row
    repository = repository or row
    payload = {
        "id": public_issue_text(repository.get("id")),
        "repoId": public_issue_text(repository.get("id")),
        "githubRepoId": public_issue_text(repository.get("github_repo_id")),
        "fullName": public_issue_text(repository.get("full_name")),
        "ownerLogin": public_issue_text(repository.get("owner_login")),
        "defaultBranch": public_issue_text(repository.get("default_branch")) or "main",
        "private": bool(repository.get("private")),
        "fork": bool(repository.get("fork")),
        "htmlUrl": trusted_public_url(repository.get("html_url")),
        "cloneUrl": trusted_public_url(repository.get("clone_url")),
        "installationId": clean_github_access_text(row.get("github_app_installation_id"), allow_int=True),
        "installationAccount": public_issue_text(row.get("installation_account")),
        "repositorySelection": public_issue_text(row.get("repository_selection")),
        "lastAuthorizedAt": pull_request_timestamp(row.get("last_authorized_at")),
        "permissions": decode_permissions(row.get("permissions")),
    }
    if user and repository.get("id"):
        payload["quota"] = quota.quota_payload_for_repository(repository, user)
    return payload


def latest_scan_for_user_repo(user_id: str, repo_id: str) -> dict | None:
    job = db.get_latest_user_repo_scan_job(user_id, repo_id)
    if job:
        scans = hydrate_scan_jobs_for_read([job])
        if scans:
            with STATE_LOCK:
                return remember_scan_snapshot_locked(scans[0])
    with STATE_LOCK:
        for scan in SCANS:
            if scan.get("userId") == user_id and scan.get("repoId") == repo_id:
                reconcile_scan_job_state_locked(scan)
                return scan
    return None


def active_scan_for_user_repo(user_id: str, repo_id: str) -> dict | None:
    job = db.get_latest_user_repo_scan_job(user_id, repo_id, active_only=True)
    if job:
        scans = hydrate_scan_jobs_for_read([job])
        if scans:
            with STATE_LOCK:
                return remember_scan_snapshot_locked(scans[0])
    with STATE_LOCK:
        for scan in SCANS:
            if scan.get("userId") != user_id or scan.get("repoId") != repo_id:
                continue
            reconcile_scan_job_state_locked(scan)
            if scan.get("status") in {"queued", "running"}:
                return scan
    return None
