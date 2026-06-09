from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

def idempotency_key_reused_payload(scan: dict | None) -> dict:
    payload = {"message": IDEMPOTENCY_KEY_REUSED_MESSAGE, "code": "IDEMPOTENCY_KEY_REUSED"}
    if isinstance(scan, dict):
        if repo_id := clean_github_access_text(scan.get("repoId"), allow_int=True):
            payload["repoId"] = repo_id
    return payload


def current_review_usage_period(timestamp: int | None = None) -> str:
    return quota.current_period(timestamp or now())


def effective_billing_plan(user: dict | None) -> str:
    return quota.effective_user_plan(user)


def user_billing_state(user: dict) -> dict:
    return user.get("billing") if isinstance(user.get("billing"), dict) else {}


def non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, candidate)


def billing_usage_for_user(user: dict, plan_id: str, *, timestamp: int | None = None, mutate: bool = False) -> dict:
    period = current_review_usage_period(timestamp)
    current = user.get("billingUsage") if isinstance(user.get("billingUsage"), dict) else {}
    used = non_negative_int(current.get("used"))
    if current.get("period") != period or current.get("plan") != plan_id:
        usage = {"period": period, "plan": plan_id, "used": 0}
    else:
        usage = {"period": period, "plan": plan_id, "used": used}
    if mutate:
        user["billingUsage"] = usage
        mark_state_dirty()
        return user["billingUsage"]
    return usage


def billing_entitlement_for_user(user: dict | None, *, timestamp: int | None = None, mutate: bool = False) -> dict:
    current = user_billing_state(user) if user else {}
    if user:
        usage = quota.quota_payload_for_user(user, timestamp=timestamp)
    else:
        entitlement = quota.quota_entitlement_for_user(None, timestamp=timestamp)
        usage = {
            "period": entitlement["period"],
            "plan": entitlement["plan"],
            "used": 0,
            "limit": entitlement["userLimit"],
            "remaining": entitlement["userLimit"],
        }
    plan_id = usage["plan"]
    return {
        "plan": plan_id,
        "interval": current.get("interval") if plan_id == "pro" else "month",
        "period": usage["period"],
        "used": usage["used"],
        "limit": usage["limit"],
        "remaining": usage["remaining"],
    }


def consume_review_quota(user: dict) -> tuple[bool, dict]:
    entitlement = quota.quota_entitlement_for_user(user)
    bucket = quota.ensure_quota_bucket(
        scope_type="user",
        scope_id=str(user["id"]),
        period=entitlement["period"],
        plan=entitlement["plan"],
        limit=entitlement["userLimit"],
        reset_at=entitlement["resetAt"],
    )
    payload = quota.quota_payload(bucket, scope="user")
    if payload["remaining"] <= 0:
        return False, billing_entitlement_for_user(user)
    connection = db.connect()
    try:
        with connection:
            connection.execute(
                """
                UPDATE quota_buckets
                SET used = used + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used < quota_limit
                """,
                (bucket["id"],),
            )
    finally:
        connection.close()
    return True, billing_entitlement_for_user(user)


def billing_account_payload(user: dict) -> dict:
    current = user_billing_state(user)
    entitlement = billing_entitlement_for_user(user)
    scan_usage = quota.quota_payload_for_user(user)
    return {
        "provider": public_billing_text(current.get("provider")),
        "status": public_billing_status(current.get("status")),
        "plan": scan_usage["plan"],
        "interval": billing.normalize_interval(entitlement["interval"]),
        "customerId": public_billing_text(current.get("customerId")),
        "subscriptionId": public_billing_text(current.get("subscriptionId")),
        "subscriptionItemId": public_billing_text(current.get("subscriptionItemId")),
        "customerEmail": public_billing_text(current.get("customerEmail")),
        "currentPeriodStart": pull_request_timestamp(current.get("currentPeriodStart")),
        "currentPeriodEnd": pull_request_timestamp(current.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": current.get("cancelAtPeriodEnd") if isinstance(current.get("cancelAtPeriodEnd"), bool) else None,
        "canceledAt": pull_request_timestamp(current.get("canceledAt")),
        "lastEventId": public_billing_text(current.get("lastEventId")),
        "lastEventType": public_billing_text(current.get("lastEventType")),
        "lastEventCreated": pull_request_timestamp(current.get("lastEventCreated")),
        "updatedAt": pull_request_timestamp(current.get("updatedAt")),
        "reviewLimit": scan_usage["limit"],
        "usage": {
            "period": scan_usage["period"],
            "used": scan_usage["used"],
            "limit": scan_usage["limit"],
            "remaining": scan_usage["remaining"],
            "plan": scan_usage["plan"],
            "scope": scan_usage["scope"],
            "resetAt": scan_usage["resetAt"],
        },
    }


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
    if not provided or value is None:
        return list(API_KEY_DEFAULT_SCOPES), None
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        candidates = value
    else:
        return [], "API key scopes must be a string or a list of strings."

    scopes: list[str] = []
    invalid: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if normalized in API_KEY_ALLOWED_SCOPES:
            if normalized not in scopes:
                scopes.append(normalized)
        else:
            invalid.append(candidate)
    if invalid or not scopes:
        allowed = ", ".join(sorted(API_KEY_ALLOWED_SCOPES))
        return [], f"API key scopes must include only: {allowed}."
    return scopes, None


def scan_request_id_from_body(body: dict) -> str:
    for key in ("requestId", "idempotencyKey"):
        value = clean_github_access_text(body.get(key), allow_int=True)
        if value and "\x00" not in value:
            return value[:128]
    return ""


def scan_commit_from_body(body: dict) -> tuple[str, str | None]:
    commit = clean_github_access_text(body.get("commit"))
    if not commit or commit.lower() == "pending":
        return "pending", None
    if SCAN_REQUEST_COMMIT_SHA_RE.fullmatch(commit):
        return commit.lower(), None
    return "", "Scan commit must be a 7-40 character hexadecimal SHA."


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


def api_key_public_payload(record: dict, *, token: str | None = None) -> dict:
    payload = {
        "id": public_issue_text(record.get("id")),
        "name": public_issue_text(record.get("name")) or "API key",
        "userId": public_issue_text(record.get("user_id")),
        "prefix": public_issue_text(record.get("key_prefix")),
        "scopes": parse_api_key_scopes(record.get("scopes")),
        "createdAt": pull_request_timestamp(record.get("created_at")) or 0,
        "lastUsedAt": pull_request_timestamp(record.get("last_used_at")),
        "revokedAt": pull_request_timestamp(record.get("revoked_at")),
    }
    if token:
        payload["key"] = token
    return payload


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


def api_docs_payload() -> dict:
    return {
        "page": {"id": "api", "title": "Pullwise API"},
        "baseUrl": "https://api.pull-wise.com",
        "website": "https://pull-wise.com",
        "contact": "contact@pull-wise.com",
        "authentication": {
            "type": "apiKey",
            "headers": ["Authorization: Bearer <api_key>", "X-Pullwise-Api-Key: <api_key>"],
            "createKey": {"method": "POST", "href": "/api-keys"},
            "scopes": API_KEY_DEFAULT_SCOPES,
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/v1/repositories",
                "scope": "repositories:read",
                "description": "List authorized repositories for the API key, including repoId.",
            },
            {
                "method": "POST",
                "path": "/api/v1/repositories/{repoId}/scans",
                "scope": "scans:write",
                "description": "Start a scan for an authorized repository.",
            },
            {
                "method": "POST",
                "path": "/api/v1/repositories/{repoId}/scans/stop",
                "scope": "scans:write",
                "description": "Cancel the latest queued or running scan for the repository.",
            },
            {
                "method": "GET",
                "path": "/api/v1/repositories/{repoId}/scans/current",
                "scope": "scans:read",
                "description": "Read the latest scan status for the repository.",
            },
            {
                "method": "GET",
                "path": "/api/v1/repositories/{repoId}/quota",
                "scope": "quota:read",
                "description": "Read remaining account and repository scan quota.",
            },
        ],
        "errors": [
            {"status": 400, "description": "Malformed JSON, invalid scope, invalid repoId, or invalid request body."},
            {"status": 401, "description": "Missing or invalid Pullwise API key."},
            {"status": 403, "description": "API key is valid but lacks the required scope."},
            {"status": 404, "description": "Route not found, repository not authorized, or no active scan exists."},
            {"status": 409, "description": "requestId was reused for a different repository."},
            {"status": 402, "description": "Scan quota is exhausted."},
            {"status": 413, "description": "Request body is too large."},
            {"status": 429, "description": "Rate limit exceeded when rate limiting is enabled."},
            {"status": 503, "description": "Review provider is not configured."},
        ],
    }


def dashboard_overview_payload(session: dict) -> dict:
    user = USERS.get(session["userId"])
    scans = [scan_payload(scan) for scan in user_scans(session)]
    repositories = repository_items_for_response(user, user.get("githubRepositoryAccess") if user else None) if user else []
    status_counts: dict[str, int] = {}
    for scan in scans:
        status = scan.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "breadcrumbs": [{"label": "Overview", "href": "/dashboard/overview"}],
        "scanTotals": {
            "total": len(scans),
            "byStatus": status_counts,
        },
        "authorizedRepositories": {
            "count": len(repositories),
            "href": "/repositories",
            "items": repositories,
        },
        "recentScans": scans[:10],
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


