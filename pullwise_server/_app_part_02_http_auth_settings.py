from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_01_bootstrap_state as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals
from ._app_imports import sync_compat_globals as _sync_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

def readiness_payload() -> dict:
    try:
        billing_provider = billing.selected_provider()
    except billing.BillingConfigurationError:
        billing_provider = "error"
    return {
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
    if proxy_headers_trusted(handler):
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


DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.0/8", "::1/128")


def direct_client_ip(handler: BaseHTTPRequestHandler) -> str:
    address = getattr(handler, "client_address", None)
    if isinstance(address, tuple | list) and address:
        candidate = str(address[0]).strip()
        if candidate and not any(char in candidate for char in "\r\n"):
            return candidate[:128]
    return "unknown"


def trusted_proxy_networks() -> list:
    configured = env("PULLWISE_TRUSTED_PROXY_CIDRS", "")
    if not configured.strip():
        configured = env("PULLWISE_TRUSTED_PROXY_IPS", "")
    cidrs = [item.strip() for item in configured.split(",") if item.strip()]
    if not cidrs:
        cidrs = list(DEFAULT_TRUSTED_PROXY_CIDRS)
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid trusted proxy CIDR %r", cidr)
    return networks


def proxy_headers_trusted(handler: BaseHTTPRequestHandler) -> bool:
    if not env_flag("PULLWISE_TRUST_PROXY_HEADERS"):
        return False
    try:
        peer = ipaddress.ip_address(direct_client_ip(handler))
    except ValueError:
        return False
    return any(peer in network for network in trusted_proxy_networks())


def forwarded_client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded_chain = request_header(handler, "X-Forwarded-For") or ""
    if not forwarded_chain or any(char in forwarded_chain for char in "\r\n"):
        return ""
    trusted_networks = trusted_proxy_networks()
    for raw_candidate in reversed(forwarded_chain.split(",")):
        candidate = raw_candidate.strip()
        if not candidate:
            return ""
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return ""
        if any(address in network for network in trusted_networks):
            continue
        return address.compressed
    return ""


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
    with STATE_LOCK:
        GITHUB_STATES[state] = {
            "kind": kind,
            "redirectTo": redirect_to,
            "expiresAt": now() + GITHUB_STATE_MAX_AGE,
            **extra,
        }
        mark_state_dirty()
    return state


def github_state_record(state: str, *, consume: bool, expected_kind: str | None = None) -> dict:
    with STATE_LOCK:
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
    with STATE_LOCK:
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
    with STATE_LOCK:
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

    with STATE_LOCK:
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

    with STATE_LOCK:
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
        "providers": [text for item in user.get("providers", [])
                      if (text := public_issue_text(item))]
        if isinstance(user.get("providers"), list) else [],
    }


def public_user_email(value: object) -> str:
    return github_auth.clean_account_email_address(value) or ""

def get_or_create_github_user() -> dict:
    login = env("PULLWISE_DEV_GITHUB_LOGIN", "taylor-dev")
    email = env("PULLWISE_DEV_EMAIL", "taylor@acme.io")
    user_id = "usr_github_" + re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")
    with STATE_LOCK:
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
    with STATE_LOCK:
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
    with STATE_LOCK:
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
        with STATE_LOCK:
            SETTINGS = persisted
            _sync_compat_globals(globals(), ("SETTINGS",))


def persist_settings_to_storage() -> None:
    with STATE_LOCK:
        db.save_state_item("settings", SETTINGS)


def settings_payload(user_id: str) -> dict:
    refresh_settings_from_storage()
    with STATE_LOCK:
        return clean_settings_payload(user_id, SETTINGS.get(user_id))


def default_settings(user_id: str) -> dict:
    refresh_settings_from_storage()
    with STATE_LOCK:
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
    review_body = body.get("review") if isinstance(body.get("review"), dict) else {}
    with STATE_LOCK:
        if name:
            settings["profile"]["name"] = name
        if email:
            settings["profile"]["email"] = email
        if "outputLanguage" in review_body:
            settings["review"]["outputLanguage"] = clean_review_output_language(review_body.get("outputLanguage"))
        SETTINGS[user_id] = settings
        persist_settings_to_storage()
        mark_state_dirty()
        return settings
