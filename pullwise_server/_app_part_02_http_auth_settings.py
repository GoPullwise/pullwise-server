from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_01_bootstrap_state as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals
from ._app_imports import sync_compat_globals as _sync_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

def repository_scan_limits_payload(plan: object = "max") -> dict:
    return system_config.repository_scan_limits(plan)


def readiness_payload() -> dict:
    try:
        billing_provider = billing.selected_provider()
    except billing.BillingConfigurationError:
        billing_provider = "error"
    return {
        "reviewProvider": "worker",
        "github": {
            "oauthConfigured": github_auth.oauth_configured(),
            "appInstallConfigured": github_auth.app_install_configured(),
            "appApiConfigured": github_auth.app_api_configured(),
            "appVisibilityCheck": github_auth.app_visibility_check_enabled(),
        },
        "billing": {
            "provider": billing_provider,
            "enabled": billing_provider == "creem",
        },
        "limits": {
            "maxQueuedScansGlobal": max_queued_scans_global(),
            "repository": repository_scan_limits_payload(),
            "rateLimitEnabled": rate_limit_enabled(),
        },
    }

def allowed_origins() -> set[str]:
    raw = env(
        "PULLWISE_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174",
    )
    return {item.strip() for item in raw.split(",") if item.strip() and item.strip() != "*"}


def trusted_browser_origins() -> set[str]:
    allowed = allowed_origins()
    for value in (
        env("PULLWISE_APP_URL", "http://localhost:5173"),
        admin_app_url(),
        os.environ.get("PULLWISE_API_BASE_URL", ""),
    ):
        origin = url_origin(value)
        if origin:
            allowed.add(origin)
    return allowed


def admin_app_url() -> str:
    configured = os.environ.get("PULLWISE_ADMIN_APP_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    app_origin = url_origin(env("PULLWISE_APP_URL", "http://localhost:5173"))
    if app_origin == "https://pull-wise.com":
        return "https://admin.pull-wise.com"
    return ""


def api_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("PULLWISE_API_BASE_URL")
    if configured:
        return configured.rstrip("/")
    if env_flag("PULLWISE_TRUST_PROXY_HEADERS"):
        forwarded = forwarded_api_base_url(handler)
        if forwarded:
            return forwarded
    host = trusted_host_header(handler)
    if host:
        return f"http://{host}"
    return "http://localhost:8080"


def trusted_host_header(handler: BaseHTTPRequestHandler) -> str | None:
    host = first_header_value(handler, "Host") or "localhost:8080"
    if any(char in host for char in "/\r\n") or not re.match(r"^[A-Za-z0-9.:-]+$", host):
        return None
    if is_local_host(host):
        return host
    explicit_hosts = {
        item.strip().lower()
        for item in env("PULLWISE_API_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if host.lower() in explicit_hosts:
        return host
    allowed = allowed_origins()
    app_origin = url_origin(env("PULLWISE_APP_URL", "http://localhost:5173"))
    if app_origin:
        allowed.add(app_origin)
    if f"http://{host}" in allowed or f"https://{host}" in allowed:
        return host
    return None


def is_local_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0].lower()
    return name in {"localhost", "127.0.0.1"}


def forwarded_api_base_url(handler: BaseHTTPRequestHandler) -> str | None:
    proto = first_header_value(handler, "X-Forwarded-Proto")
    host = first_header_value(handler, "X-Forwarded-Host")
    prefix = first_header_value(handler, "X-Forwarded-Prefix") or ""

    if proto not in {"http", "https"} or not host:
        return None
    if any(char in host for char in "/\r\n") or not re.match(r"^[A-Za-z0-9.:-]+$", host):
        return None
    if prefix and (not prefix.startswith("/") or prefix.startswith("//") or any(char in prefix for char in "\r\n")):
        return None

    return f"{proto}://{host}{prefix.rstrip('/')}"


def first_header_value(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    value = request_header(handler, name)
    if not value:
        return None
    return value.split(",", 1)[0].strip()


def default_redirect(screen: str) -> str:
    app_url = env("PULLWISE_APP_URL", "http://localhost:5173").rstrip("/")
    # Use path-based URLs that match the frontend's client-side routing (e.g. /dashboard, /repos).
    # The "landing" screen maps to the root path "/".
    path = "/" if screen == "landing" else f"/{screen}"
    return f"{app_url}{path}"


def now() -> int:
    return int(time.time())


def make_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


def remember_github_state(kind: str, redirect_to: str, **extra: object) -> str:
    state = secrets.token_urlsafe(32)
    GITHUB_STATES[state] = {
        "kind": kind,
        "redirectTo": redirect_to,
        "expiresAt": now() + GITHUB_STATE_MAX_AGE,
        **extra,
    }
    mark_state_dirty()
    return state


def github_state_record(state: str, *, consume: bool, expected_kind: str | None = None) -> dict:
    record = GITHUB_STATES.pop(state, None) if consume else GITHUB_STATES.get(state)
    if consume and record is not None:
        mark_state_dirty()
    if not isinstance(record, dict):
        raise ValueError("GitHub authorization state is invalid or expired.")
    expires_at = pull_request_timestamp(record.get("expiresAt"))
    kind = record.get("kind")
    if expires_at is None or expires_at < now() or (expected_kind is not None and kind != expected_kind):
        if not consume and (expires_at is None or expires_at < now()):
            GITHUB_STATES.pop(state, None)
            mark_state_dirty()
        raise ValueError("GitHub authorization state is invalid or expired.")
    return record


def peek_github_state(kind: str, state: str) -> dict:
    return github_state_record(state, consume=False, expected_kind=kind)


def pop_any_github_state(state: str) -> dict:
    return github_state_record(state, consume=True)


def pop_github_state(kind: str, state: str) -> dict:
    return github_state_record(state, consume=True, expected_kind=kind)


def remember_github_repository_authorization(
    user: dict,
    redirect_to: str,
    requested_scope: str,
    *,
    manage: bool = False,
    selected_github_identity_id: str | None = None,
) -> str:
    state = remember_github_state(
        "install",
        redirect_to,
        userId=user["id"],
        requestedScope=requested_scope,
        selectedGithubIdentityId=selected_github_identity_id,
    )
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        github_access = {}
    timestamp = now()
    user["githubRepositoryAccessPending"] = {
        "state": state,
        "startedAt": timestamp,
        "expiresAt": timestamp + GITHUB_STATE_MAX_AGE,
        "previousInstallationId": github_access.get("installationId"),
        "manage": bool(manage),
    }
    mark_state_dirty()
    return state


def remember_github_repository_identity_authorization(
    user: dict,
    redirect_to: str,
    requested_scope: str,
    *,
    add: bool = False,
    manage: bool = False,
) -> str:
    state = remember_github_state(
        "install_identity",
        redirect_to,
        userId=user["id"],
        requestedScope=requested_scope,
        add=bool(add),
        manage=bool(manage),
    )
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        github_access = {}
    timestamp = now()
    user["githubRepositoryAccessPending"] = {
        "state": state,
        "startedAt": timestamp,
        "expiresAt": timestamp + GITHUB_STATE_MAX_AGE,
        "previousInstallationId": github_access.get("installationId"),
        "add": bool(add),
        "manage": bool(manage),
        "needsIdentitySelection": True,
    }
    mark_state_dirty()
    return state


def remember_github_installation_manage_state(
    user: dict,
    installation: dict,
    redirect_to: str,
    *,
    expected_github_identity_id: str | None = None,
) -> str:
    return remember_github_state(
        "manage_installation",
        redirect_to,
        purpose="manage_installation",
        userId=user["id"],
        expectedInstallationId=clean_installation_summary_text(installation.get("installationId")),
        expectedAccountLogin=clean_installation_summary_text(installation.get("installationAccount")),
        expectedInstallationTargetType=clean_installation_summary_text(installation.get("installationTargetType")),
        expectedInstallationHtmlUrl=trusted_github_web_url(installation.get("installationHtmlUrl")),
        expectedGithubIdentityId=expected_github_identity_id,
    )


def github_repository_authorization_pending(user: dict | None) -> dict | None:
    if not user:
        return None

    timestamp = now()
    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict):
        pending_expires_at = pull_request_timestamp(pending.get("expiresAt"))
        if pending_expires_at is not None and pending_expires_at >= timestamp:
            return pending
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    return None


def clear_github_repository_authorization_pending(user: dict | None, state: str | None = None) -> None:
    if not user:
        return

    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict) and (not state or pending.get("state") == state):
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    states_to_clear = []
    for stored_state, record in GITHUB_STATES.items():
        if not isinstance(record, dict):
            if not state or stored_state == state:
                states_to_clear.append(stored_state)
            continue
        if (
            record.get("kind") == "install"
            and record.get("userId") == user.get("id")
            and (not state or stored_state == state)
        ):
            states_to_clear.append(stored_state)
    for stored_state in states_to_clear:
        GITHUB_STATES.pop(stored_state, None)
    if states_to_clear:
        mark_state_dirty()


def url_origin(value: str) -> str | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def trusted_github_web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char in raw for char in "\r\n"):
        return None
    parsed = urlparse(raw)
    allowed = urlparse(github_auth.github_web_url())
    if not github_auth.github_web_url_transport_allowed(allowed):
        return None
    if not github_auth.github_web_url_transport_allowed(parsed):
        return None
    if parsed.scheme != allowed.scheme:
        return None
    if allowed.netloc and parsed.netloc.lower() != allowed.netloc.lower():
        return None
    return raw


def safe_redirect_to(value: object, screen: str) -> str:
    fallback = default_redirect(screen)
    if not isinstance(value, str) or not value:
        return fallback
    if any(char in value for char in "\r\n"):
        return fallback
    if value.startswith("/") and not value.startswith("//"):
        return env("PULLWISE_APP_URL", "http://localhost:5173").rstrip("/") + value

    origin = url_origin(value)
    allowed = allowed_origins()
    for trusted in (env("PULLWISE_APP_URL", "http://localhost:5173"), admin_app_url()):
        trusted_origin = url_origin(trusted)
        if trusted_origin:
            allowed.add(trusted_origin)
    if origin and origin in allowed:
        return value
    return fallback


def redirect_with_params(location: str, params: dict[str, str]) -> str:
    parsed = urlparse(location)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    query.update({key: value for key, value in params.items() if value})
    return urlunparse(parsed._replace(query=urlencode(query)))


def user_public(user: dict) -> dict:
    return {
        "id": public_issue_text(user.get("id")),
        "name": public_issue_text(user.get("name")) or "User",
        "email": public_user_email(user.get("email")),
        "avatarUrl": trusted_public_url(user.get("avatarUrl")),
        "createdAt": pull_request_timestamp(user.get("createdAt")) or 0,
        "providers": review._safe_text_list(user.get("providers")),
    }


def public_user_email(value: object) -> str:
    return github_auth.clean_account_email_address(value) or ""


def admin_user_subscription_payload(user: dict) -> dict:
    current = user_billing_state(user)
    plan = (public_billing_text(current.get("plan")) or "").lower()
    if plan not in set(billing.PLAN_IDS):
        plan = "free"
    return {
        "provider": public_billing_text(current.get("provider")),
        "status": public_billing_status(current.get("status")),
        "plan": plan,
        "effectivePlan": effective_billing_plan(user),
        "interval": billing.normalize_interval(current.get("interval")),
        "customerId": public_billing_text(current.get("customerId")),
        "customerEmail": public_billing_text(current.get("customerEmail")),
        "subscriptionId": public_billing_text(current.get("subscriptionId")),
        "subscriptionItemId": public_billing_text(current.get("subscriptionItemId")),
        "currentPeriodStart": pull_request_timestamp(current.get("currentPeriodStart")),
        "currentPeriodEnd": pull_request_timestamp(current.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": current.get("cancelAtPeriodEnd") if isinstance(current.get("cancelAtPeriodEnd"), bool) else None,
        "canceledAt": pull_request_timestamp(current.get("canceledAt")),
        "lastEventType": public_billing_text(current.get("lastEventType")),
        "lastEventCreated": pull_request_timestamp(current.get("lastEventCreated")),
        "updatedAt": pull_request_timestamp(current.get("updatedAt")),
    }


def admin_user_payload(user: dict, *, current_user_id: str | None = None) -> dict:
    user_id = public_issue_text(user.get("id"))
    github_access = user.get("githubRepositoryAccess")
    repository_count = 0
    if isinstance(github_access, dict):
        repository_count = len(repository_items_for_payload(github_access))
    scan_count = sum(1 for scan in SCANS if scan_user_id(scan) == user_id)
    scan_ids = {
        public_issue_text(scan.get("id"))
        for scan in SCANS
        if isinstance(scan, dict) and scan_user_id(scan) == user_id and public_issue_text(scan.get("id"))
    }
    issue_count = sum(
        1
        for issue in ISSUES
        if issue_user_id(issue) == user_id or issue_scan_id(issue) in scan_ids
    )
    return {
        **user_public(user),
        "githubLogin": public_issue_text(user.get("githubLogin")),
        "githubId": public_issue_text(user.get("githubId")),
        "admin": user_is_admin(user),
        "current": bool(current_user_id and user_id == current_user_id),
        "lastGitHubAccessTokenUpdatedAt": pull_request_timestamp(user.get("githubAccessTokenUpdatedAt")),
        "repositoryCount": repository_count,
        "scanCount": scan_count,
        "issueCount": issue_count,
        "subscription": admin_user_subscription_payload(user),
    }


def admin_users_payload(current_user_id: str | None = None) -> dict:
    users = [
        admin_user_payload(user, current_user_id=current_user_id)
        for user in USERS.values()
        if isinstance(user, dict)
    ]
    users.sort(key=lambda item: (str(item.get("email") or item.get("name") or "").lower(), str(item.get("id") or "")))
    return {"items": users, "users": users}


def scan_user_id(scan: dict | None) -> str:
    if not isinstance(scan, dict):
        return ""
    return public_issue_text(scan.get("userId") or scan.get("user_id"))


def issue_user_id(issue: dict | None) -> str:
    if not isinstance(issue, dict):
        return ""
    return public_issue_text(issue.get("userId") or issue.get("user_id"))


def issue_scan_id(issue: dict | None) -> str:
    if not isinstance(issue, dict):
        return ""
    return public_issue_text(issue.get("scanId") or issue.get("scan_id"))


def delete_authorized_user(user_id: str, *, actor_user_id: str | None = None) -> dict:
    target_user_id = public_issue_text(user_id)
    if not target_user_id:
        raise ValueError("User id is required.")
    if actor_user_id and target_user_id == actor_user_id:
        raise ValueError("Admins cannot delete their own user account.")
    target_user = USERS.get(target_user_id)
    if not isinstance(target_user, dict):
        raise ResourceNotFound("User")

    with STATE_LOCK:
        target_scan_ids = {
            public_issue_text(scan.get("id"))
            for scan in SCANS
            if isinstance(scan, dict) and scan_user_id(scan) == target_user_id and public_issue_text(scan.get("id"))
        }
        removed_scans = len(target_scan_ids)
        removed_issues = 0
        SCANS[:] = [
            scan
            for scan in SCANS
            if not (isinstance(scan, dict) and scan_user_id(scan) == target_user_id)
        ]
        kept_issues = []
        for issue in ISSUES:
            if isinstance(issue, dict) and (
                issue_user_id(issue) == target_user_id or issue_scan_id(issue) in target_scan_ids
            ):
                removed_issues += 1
                continue
            kept_issues.append(issue)
        ISSUES[:] = kept_issues
        removed_sessions = 0
        for session_id, session in list(SESSIONS.items()):
            if isinstance(session, dict) and session.get("userId") == target_user_id:
                SESSIONS.pop(session_id, None)
                removed_sessions += 1
        removed_github_states = 0
        for state_id, state in list(GITHUB_STATES.items()):
            if isinstance(state, dict) and state.get("userId") == target_user_id:
                GITHUB_STATES.pop(state_id, None)
                removed_github_states += 1
        removed_settings = 1 if SETTINGS.pop(target_user_id, None) is not None else 0
        USERS.pop(target_user_id, None)
        mark_state_dirty()

    database_counts = db.delete_user_related_records(target_user_id, target_scan_ids)
    return {
        "user": admin_user_payload(target_user, current_user_id=actor_user_id),
        "deleted": True,
        "removed": {
            "users": 1,
            "sessions": removed_sessions,
            "githubStates": removed_github_states,
            "settings": removed_settings,
            "scans": removed_scans,
            "issues": removed_issues,
            **database_counts,
        },
    }


def get_or_create_github_user() -> dict:
    login = env("PULLWISE_DEV_GITHUB_LOGIN", "taylor-dev")
    email = env("PULLWISE_DEV_EMAIL", "taylor@acme.io")
    user_id = "usr_github_" + re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": login,
            "email": email,
            "avatarUrl": None,
            "createdAt": now(),
            "providers": ["github"],
            "githubLogin": login,
            "githubRepositoryAccess": None,
        }
        mark_state_dirty()
    elif "github" not in USERS[user_id]["providers"]:
        USERS[user_id]["providers"].append("github")
        mark_state_dirty()
    return USERS[user_id]


def get_or_create_real_github_user(profile: dict, token_payload: dict) -> dict:
    login = profile["login"]
    github_id = github_profile_id(profile, login)
    user_id = "usr_github_" + github_id
    profile_name = clean_user_profile_text(profile.get("name"))
    email = (
        github_auth.clean_account_email_address(profile.get("primaryEmail"))
        or github_auth.clean_account_email_address(profile.get("email"))
        or ""
    )
    verified_emails = github_auth.clean_account_email_addresses(profile.get("verifiedEmails"))
    if email:
        verified_emails = github_auth.unique_account_email_addresses([email, *verified_emails])
    avatar_url = trusted_public_url(profile.get("avatar_url"))
    github_html_url = trusted_github_web_url(profile.get("html_url"))
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": profile_name or login,
            "email": email,
            "avatarUrl": avatar_url,
            "createdAt": now(),
            "providers": ["github"],
            "githubRepositoryAccess": None,
            "githubVerifiedEmails": verified_emails,
        }
        mark_state_dirty()

    user = USERS[user_id]
    user.update(
        {
            "name": profile_name or clean_user_profile_text(user.get("name")) or login,
            "email": email,
            "avatarUrl": avatar_url,
            "githubVerifiedEmails": verified_emails,
            "githubId": github_id,
            "githubLogin": login,
            "githubHtmlUrl": github_html_url,
            "githubAccessToken": token_payload.get("access_token"),
            "githubTokenType": token_payload.get("token_type"),
            "githubOAuthScope": token_payload.get("scope"),
            "githubAccessTokenUpdatedAt": now(),
        }
    )
    if "github" not in user["providers"]:
        user["providers"].append("github")
    upsert_github_identity(user, profile, token_payload)
    mark_state_dirty()
    return user


def github_profile_id(profile: dict, login: str) -> str:
    raw_id = profile.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 0:
        return str(raw_id)
    if isinstance(raw_id, str):
        candidate = raw_id.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            return candidate
    return re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")


def github_identity_record_id(github_user_id: object, login: object) -> str:
    source = str(github_user_id or login or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", source).strip("_")
    return f"ghi_{slug or secrets.token_urlsafe(6)}"


def github_identity_list(user: dict | None) -> list[dict]:
    if not user:
        return []
    identities = user.get("githubIdentities")
    if not isinstance(identities, list):
        identities = []
        user["githubIdentities"] = identities
    return identities


def upsert_github_identity(user: dict, profile: dict, token_payload: dict) -> dict:
    login = public_issue_text(profile.get("login")) or "github-user"
    github_user_id = github_profile_id(profile, login)
    identities = github_identity_list(user)
    identity = next(
        (
            item
            for item in identities
            if isinstance(item, dict) and str(item.get("githubUserId") or "") == str(github_user_id)
        ),
        None,
    )
    if identity is None:
        identity = {
            "id": github_identity_record_id(github_user_id, login),
            "userId": user.get("id"),
            "githubUserId": str(github_user_id),
        }
        identities.append(identity)

    timestamp = now()
    identity.update({
        "githubLogin": login,
        "login": login,
        "githubHtmlUrl": trusted_github_web_url(profile.get("html_url")),
        "avatarUrl": trusted_public_url(profile.get("avatar_url")),
        "accessToken": token_payload.get("access_token"),
        "oauthScope": token_payload.get("scope"),
        "tokenUpdatedAt": timestamp,
        "lastVerifiedAt": timestamp,
        "status": "active",
    })
    mark_state_dirty()
    return identity


def synthesized_current_github_identity(user: dict | None) -> dict | None:
    if not user or not user.get("githubAccessToken") or not user.get("githubLogin"):
        return None
    github_user_id = str(user.get("githubId") or user.get("githubLogin") or "")
    login = public_issue_text(user.get("githubLogin")) or "github-user"
    return {
        "id": github_identity_record_id(github_user_id, login),
        "userId": user.get("id"),
        "githubUserId": github_user_id,
        "githubLogin": login,
        "login": login,
        "githubHtmlUrl": trusted_github_web_url(user.get("githubHtmlUrl")),
        "avatarUrl": trusted_public_url(user.get("avatarUrl")),
        "accessToken": user.get("githubAccessToken"),
        "oauthScope": user.get("githubOAuthScope"),
        "tokenUpdatedAt": user.get("githubAccessTokenUpdatedAt"),
        "lastVerifiedAt": user.get("githubAccessTokenUpdatedAt") or user.get("createdAt"),
        "status": "active",
    }


def github_identities_for_user(user: dict | None) -> list[dict]:
    if not user:
        return []
    identities = [identity for identity in github_identity_list(user) if isinstance(identity, dict)]
    current_identity = synthesized_current_github_identity(user)
    if current_identity and not any(identity.get("id") == current_identity["id"] for identity in identities):
        identities = [*identities, current_identity]
    return identities


def public_github_identity(identity: dict) -> dict:
    return {
        "id": clean_github_access_text(identity.get("id")),
        "githubUserId": clean_github_access_text(identity.get("githubUserId"), allow_int=True),
        "login": clean_github_access_text(identity.get("githubLogin") or identity.get("login")),
        "githubHtmlUrl": trusted_github_web_url(identity.get("githubHtmlUrl")),
        "avatarUrl": trusted_public_url(identity.get("avatarUrl")),
        "status": clean_github_access_text(identity.get("status")) or "active",
        "lastVerifiedAt": pull_request_timestamp(identity.get("lastVerifiedAt")),
    }


def public_github_identities(user: dict | None) -> list[dict]:
    identities = []
    for identity in github_identities_for_user(user):
        public_identity = public_github_identity(identity)
        if public_identity["id"] and public_identity["login"]:
            identities.append(public_identity)
    return identities


def github_identity_by_id(user: dict | None, identity_id: str | None) -> dict | None:
    if not identity_id:
        return None
    for identity in github_identities_for_user(user):
        if identity.get("id") == identity_id:
            return identity
    return None


def github_identity_access_list(user: dict | None) -> list[dict]:
    if not user:
        return []
    records = user.get("githubIdentityInstallationAccess")
    if not isinstance(records, list):
        records = []
        user["githubIdentityInstallationAccess"] = records
    return records


def upsert_github_identity_installation_access(
    user: dict,
    identity: dict,
    installation_id: str,
    *,
    can_access: bool,
    last_error_code: str | None = None,
    verification_method: str = "user_installations_api",
) -> dict:
    records = github_identity_access_list(user)
    identity_id = clean_github_access_text(identity.get("id"))
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("githubIdentityId") == identity_id
            and str(item.get("githubAppInstallationId") or "") == str(installation_id)
        ),
        None,
    )
    if record is None:
        record = {
            "githubIdentityId": identity_id,
            "githubAppInstallationId": str(installation_id),
        }
        records.append(record)
    record.update({
        "canAccess": bool(can_access),
        "canManage": "unknown" if can_access else False,
        "verifiedAt": now(),
        "verificationMethod": verification_method,
        "lastErrorCode": last_error_code,
    })
    mark_state_dirty()
    return record


def latest_installation_access_record(user: dict | None, installation_id: str | None) -> dict | None:
    if not installation_id:
        return None
    candidates = [
        record
        for record in github_identity_access_list(user)
        if isinstance(record, dict)
        and str(record.get("githubAppInstallationId") or "") == str(installation_id)
    ]
    candidates.sort(key=lambda record: pull_request_timestamp(record.get("verifiedAt")) or 0, reverse=True)
    return candidates[0] if candidates else None


def clean_user_profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def trusted_public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if any(char in raw for char in "\r\n"):
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


DEFAULT_REVIEW_OUTPUT_LANGUAGE = "en"
REVIEW_OUTPUT_LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh-CN": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt-BR": "Portuguese",
    "it": "Italian",
}
def clean_review_output_language(value: object, *, default: str | None = DEFAULT_REVIEW_OUTPUT_LANGUAGE) -> str | None:
    text = public_issue_text(value)
    if not text:
        return default
    if text in REVIEW_OUTPUT_LANGUAGES:
        return text
    return default


def review_output_language_payload(value: object) -> dict:
    code = clean_review_output_language(value) or DEFAULT_REVIEW_OUTPUT_LANGUAGE
    return {
        "code": code,
        "label": REVIEW_OUTPUT_LANGUAGES.get(code, REVIEW_OUTPUT_LANGUAGES[DEFAULT_REVIEW_OUTPUT_LANGUAGE]),
    }


def create_session(user: dict) -> dict:
    session_id = make_id("ses")
    session = {
        "id": session_id,
        "userId": user["id"],
        "createdAt": now(),
        "expiresAt": now() + SESSION_MAX_AGE,
    }
    SESSIONS[session_id] = session
    mark_state_dirty()
    return session


def default_settings_payload(user_id: str) -> dict:
    user = USERS.get(user_id) or {}
    return {
        "profile": {
            "name": public_issue_text(user.get("name")) or "User",
            "email": public_user_email(user.get("email")),
        },
        "review": {
            "outputLanguage": DEFAULT_REVIEW_OUTPUT_LANGUAGE,
        },
    }


def refresh_settings_from_storage() -> None:
    global SETTINGS
    persisted = db.load_state_item("settings")
    if isinstance(persisted, dict):
        SETTINGS = persisted
        _sync_compat_globals(globals(), ("SETTINGS",))


def persist_settings_to_storage() -> None:
    db.save_state_item("settings", SETTINGS)


def settings_payload(user_id: str) -> dict:
    refresh_settings_from_storage()
    return clean_settings_payload(user_id, SETTINGS.get(user_id))


def default_settings(user_id: str) -> dict:
    refresh_settings_from_storage()
    if not isinstance(SETTINGS.get(user_id), dict):
        SETTINGS[user_id] = default_settings_payload(user_id)
        persist_settings_to_storage()
        mark_state_dirty()
    return SETTINGS[user_id]


def clean_settings_payload(user_id: str, value: object) -> dict:
    base = default_settings_payload(user_id)
    settings = value if isinstance(value, dict) else {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    review_settings = settings.get("review") if isinstance(settings.get("review"), dict) else {}
    return {
        "profile": {
            "name": public_issue_text(profile.get("name")) or base["profile"]["name"],
            "email": public_user_email(profile.get("email")) or base["profile"]["email"],
        },
        "review": {
            "outputLanguage": clean_review_output_language(review_settings.get("outputLanguage"))
            or base["review"]["outputLanguage"],
        },
    }


def apply_settings_update(user_id: str, body: dict) -> dict:
    settings = settings_payload(user_id)
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    name = public_issue_text(profile.get("name"))
    email = github_auth.clean_account_email_address(profile.get("email"))
    if name:
        settings["profile"]["name"] = name
    if email:
        settings["profile"]["email"] = email
    review_body = body.get("review") if isinstance(body.get("review"), dict) else {}
    if "outputLanguage" in review_body:
        settings["review"]["outputLanguage"] = clean_review_output_language(review_body.get("outputLanguage"))
    SETTINGS[user_id] = settings
    persist_settings_to_storage()
    mark_state_dirty()
    return settings


def user_scans(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [
        scan
        for scan in SCANS
        if scan.get("userId") == session["userId"]
    ]


def user_scans_for_read(session: dict | None) -> list[dict]:
    scans = user_scans(session)
    if not scans:
        return []
    with STATE_LOCK:
        scan_ids = [public_issue_text(scan.get("id")) for scan in scans]
        job_ids = [public_issue_text(scan.get("jobId")) for scan in scans if public_issue_text(scan.get("jobId"))]
        jobs = db.list_scan_jobs_for_scans(scan_ids, job_ids)
        job_lookup: dict[tuple[str, str], dict] = {}
        terminal_job_ids = []
        for job in jobs:
            job_id = public_issue_text(job.get("job_id"))
            scan_id = public_issue_text(job.get("scan_id"))
            if job_id:
                job_lookup[("job", job_id)] = job
            if scan_id:
                job_lookup[("scan", scan_id)] = job
            if job_id and scan_status_from_job_status(job.get("status")) in {"done", "failed"}:
                terminal_job_ids.append(job_id)
        result_lookup = {
            public_issue_text(result.get("job_id")): result
            for result in db.list_completed_scan_job_results_for_job_ids(terminal_job_ids)
            if public_issue_text(result.get("job_id"))
        }
        for scan in scans:
            reconcile_scan_job_state_locked(scan, job_lookup=job_lookup, result_lookup=result_lookup)
    return scans


def scan_snapshot_with_job_state(scan: dict, job: dict) -> dict:
    hydrated = dict(scan)
    status = scan_status_from_job_status(job.get("status"))
    if not status:
        return hydrated
    update = {
        "jobId": public_issue_text(job.get("job_id")) or hydrated.get("jobId"),
        "status": status,
        "error": clean_scan_error(job.get("error")),
    }
    commit = clean_github_access_text(job.get("commit"))
    if commit:
        update["commit"] = commit
    if status == "queued":
        update["progress"] = 0
        update["phase"] = None
        update["claimedAt"] = None
        update["claimedByWorkerId"] = None
    elif status == "running":
        update["progress"] = public_scan_progress(job.get("progress"))
        update["phase"] = public_scan_phase(job.get("progress_phase")) or None
        update["claimedByWorkerId"] = public_issue_text(job.get("claimed_by_worker_id"))
        claimed_at = pull_request_timestamp(job.get("claimed_at"))
        if claimed_at is not None:
            update["claimedAt"] = claimed_at
        started_at = pull_request_timestamp(job.get("started_at"))
        if started_at is not None:
            update["startedAt"] = started_at
    else:
        completed_at = pull_request_timestamp(job.get("completed_at"))
        if completed_at is not None:
            update["completedAt"] = completed_at
        update["resultChecksum"] = public_issue_text(job.get("result_checksum"))
        if status == "done":
            update["phase"] = "report"
            update["progress"] = 100
            update["error"] = ""
        elif status == "failed":
            update["phase"] = "report"
            update["progress"] = public_scan_progress(hydrated.get("progress"))
        else:
            update["phase"] = None
    hydrated.update(update)
    return hydrated


def hydrate_scan_jobs_for_read(jobs: list[dict]) -> list[dict]:
    if not jobs:
        return []
    scan_ids = {public_issue_text(job.get("scan_id")) for job in jobs if public_issue_text(job.get("scan_id"))}
    snapshot_lookup = {
        public_issue_text(scan.get("id")): scan
        for scan in db.list_scan_snapshots_for_scan_ids(scan_ids)
        if public_issue_text(scan.get("id"))
    }
    job_lookup: dict[tuple[str, str], dict] = {}
    terminal_job_ids = []
    for job in jobs:
        job_id = public_issue_text(job.get("job_id"))
        scan_id = public_issue_text(job.get("scan_id"))
        if job_id:
            job_lookup[("job", job_id)] = job
        if scan_id:
            job_lookup[("scan", scan_id)] = job
        if job_id and scan_status_from_job_status(job.get("status")) in {"done", "failed"}:
            terminal_job_ids.append(job_id)
    result_lookup = {
        public_issue_text(result.get("job_id")): result
        for result in db.list_completed_scan_job_results_for_job_ids(terminal_job_ids)
        if public_issue_text(result.get("job_id"))
    }
    indexed_memory_scans = {
        scan_id: scan
        for scan_id in scan_ids
        if (scan := memory_scan_by_id(scan_id)) is not None
    }
    if not indexed_memory_scans and len(snapshot_lookup) == len(scan_ids):
        return [
            scan_snapshot_with_job_state(snapshot_lookup[public_issue_text(job.get("scan_id"))], job)
            for job in jobs
            if public_issue_text(job.get("scan_id")) in snapshot_lookup
        ]
    with STATE_LOCK:
        hydrated = []
        for job in jobs:
            scan_id = public_issue_text(job.get("scan_id"))
            snapshot = snapshot_lookup.get(scan_id)
            if snapshot is not None:
                memory_scan = indexed_memory_scans.get(scan_id)
                if memory_scan is not None:
                    reconcile_scan_job_state_locked(memory_scan, job_lookup=job_lookup, result_lookup=result_lookup)
                hydrated.append(scan_snapshot_with_job_state(snapshot, job))
                continue
            scan = indexed_memory_scans.get(scan_id)
            if scan is None:
                scan = scan_from_recovered_job(job)
            if not scan:
                continue
            reconcile_scan_job_state_locked(scan, job_lookup=job_lookup, result_lookup=result_lookup)
            hydrated.append(scan)
        return hydrated


def user_scans_page_for_read(session: dict | None, params: dict) -> dict:
    if not session:
        return {"items": [], "total": 0, "limit": 50, "offset": 0}
    user_id = public_issue_text(session.get("userId"))
    limit, offset = pagination_params(params)
    raw_status = public_issue_text(params.get("status")).lower()
    status = public_scan_status(raw_status) if raw_status and raw_status != "all" else ""
    repo = clean_repository_full_name(params.get("repo"))
    page = db.list_user_scan_jobs_page(user_id, status=status, repo=repo, limit=limit, offset=offset)
    if page["total"] == 0 and db.count_user_scan_jobs(user_id) == 0:
        legacy_scans = filter_user_scan_records(user_scans_for_read(session), params)
        legacy_page = legacy_scans[offset : offset + limit]
        return {"items": legacy_page, "total": len(legacy_scans), "limit": limit, "offset": offset}
    return {
        "items": hydrate_scan_jobs_for_read(page["items"]),
        "total": page["total"],
        "limit": page["limit"],
        "offset": page["offset"],
    }


def user_scan_for_read(session: dict | None, scan_id: str) -> dict | None:
    if not session:
        return None
    user_id = public_issue_text(session.get("userId"))
    target_scan_id = public_issue_text(scan_id)
    if not user_id or not target_scan_id:
        return None
    job = db.get_user_scan_job(user_id, target_scan_id)
    if job:
        scans = hydrate_scan_jobs_for_read([job])
        return scans[0] if scans else None
    if db.count_user_scan_jobs(user_id) == 0:
        return next((scan for scan in user_scans_for_read(session) if public_issue_text(scan.get("id")) == target_scan_id), None)
    return None


def user_scan_by_request_id(user_id: str, request_id: str) -> dict | None:
    if not request_id:
        return None
    with STATE_LOCK:
        for scan in SCANS:
            if scan.get("userId") == user_id and scan.get("requestId") == request_id:
                return scan
    scan = db.find_user_scan_snapshot_by_request_id(user_id, request_id)
    if not scan:
        return None
    job = db.get_scan_job_for_scan(public_issue_text(scan.get("id")))
    if job:
        hydrated = hydrate_scan_jobs_for_read([job])
        scan = hydrated[0] if hydrated else scan
    with STATE_LOCK:
        return remember_scan_snapshot_locked(scan)


IDEMPOTENCY_KEY_REUSED_MESSAGE = "This idempotency key is already attached to a different repository scan."


def scan_matches_requested_repository(scan: dict, *, requested_repo_id: str | None = None, requested_repository: str | None = None) -> bool:
    if requested_repo_id:
        scan_repo_ids = {
            clean_github_access_text(scan.get("repoId"), allow_int=True),
            clean_github_access_text(scan.get("githubRepoId"), allow_int=True),
        }
        if requested_repo_id in scan_repo_ids:
            return True
    if requested_repository and clean_repository_full_name(scan.get("repo")) == requested_repository:
        return True
    return False
