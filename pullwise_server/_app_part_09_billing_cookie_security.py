from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_08_fix_pr_repository_access as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

from . import billing_account_rules as billing_rules
from .billing_account_rules import (
    billing_event_id, billing_update_text, billing_update_scalar,
    billing_update_bool, billing_event_created, billing_update_matches_user,
)

MAX_BILLING_SUBSCRIPTION_RECORDS = billing_rules.MAX_BILLING_SUBSCRIPTION_RECORDS
MAX_BILLING_SUBSCRIPTION_EVENTS = billing_rules.MAX_BILLING_SUBSCRIPTION_EVENTS


def billing_event_processed(update: dict) -> bool:
    event_id = billing_event_id(update)
    return bool(event_id and event_id in BILLING_EVENTS)


def remember_billing_event(update: dict, *, applied: bool, stale: bool = False,
                           processed_at: int | None = None) -> None:
    event_id = billing_event_id(update)
    record = billing_rules.billing_event_record(update,
        processed_at=processed_at if processed_at is not None else now(),
        applied=applied, stale=stale)
    if record is None:
        return
    BILLING_EVENTS[event_id] = record
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


def ensure_billing_quota_bucket_for_user(user: dict) -> None:
    entitlement = quota.quota_entitlement_for_user(user)
    quota.ensure_quota_bucket(
        scope_type="user", scope_id=str(user["id"]),
        period=entitlement["period"], plan=entitlement["plan"],
        limit=entitlement["userLimit"], reset_at=entitlement["resetAt"],
    )


def upsert_billing_subscription_record(user: dict, billing_state: dict) -> None:
    billing_rules.upsert_billing_subscription_record(user, billing_state, processed_at=now())


def append_billing_subscription_event(user: dict, update: dict, billing_state: dict,
                                      *, stale: bool = False,
                                      processed_at: int | None = None) -> None:
    billing_rules.append_billing_subscription_event(user, update, billing_state,
        stale=stale, processed_at=processed_at if processed_at is not None else now())


def apply_billing_update_to_user(user: dict, update: dict) -> bool:
    processed_at = now()
    decision = billing_rules.reduce_billing_update(user, update, processed_at=processed_at)
    if decision["quotaRefresh"]:
        ensure_billing_quota_bucket_for_user(decision["user"])
    user.clear()
    user.update(decision["user"])
    if decision["eventRecord"] is not None:
        remember_billing_event(update, applied=decision["applied"],
            stale=not decision["applied"], processed_at=processed_at)
    if decision["applied"]:
        mark_state_dirty()
    return decision["applied"]


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


def csrf_origin_check_exempt(
    path: str,
    segments: list[str],
    handler: BaseHTTPRequestHandler,
) -> bool:
    if path.startswith("/webhooks/") or bool(segments and segments[0] == "worker"):
        return True
    if external_api_segments(segments) is not None:
        return not request_uses_session_cookie(handler)
    return False


def cookie_state_change_needs_origin_check(method: str, path: str, segments: list[str], handler: BaseHTTPRequestHandler) -> bool:
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if cookie_same_site() != "None":
        return False
    if csrf_origin_check_exempt(path, segments, handler):
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
