from __future__ import annotations

import copy
import os
import threading
import time
from urllib.parse import urlparse

from . import db


STATE_KEY = "system_config"
CACHE_TTL_SECONDS = 2.0
PLAN_IDS = ("free", "pro", "max")
PAID_PLAN_IDS = ("pro", "max")
CREEM_UPDATE_BEHAVIORS = {"proration-charge-immediately", "proration-none"}
DEFAULT_CREEM_API_BASE_URL = "https://api.creem.io"
DEFAULT_CREEM_TEST_API_BASE_URL = "https://test-api.creem.io"

DEFAULT_CONFIG = {
    "version": 1,
    "plans": {
        "free": {
            "userReviewLimit": 5,
            "repositoryReviewLimit": 5,
            "maxRepoFiles": 200,
            "maxRepoBytes": 5 * 1024 * 1024,
        },
        "pro": {
            "userReviewLimit": 60,
            "repositoryReviewLimit": 60,
            "maxRepoFiles": 1000,
            "maxRepoBytes": 20 * 1024 * 1024,
        },
        "max": {
            "userReviewLimit": 90,
            "repositoryReviewLimit": 90,
            "maxRepoFiles": 2000,
            "maxRepoBytes": 50 * 1024 * 1024,
        },
    },
    "scan": {
        "maxQueuedScansGlobal": 1000,
        "jobRetryAttempts": 1,
        "jobLeaseSeconds": 3600,
        "jobStartupGraceSeconds": 120,
    },
    "worker": {
        "heartbeatTimeoutSeconds": 120,
        "minVersion": "",
        "allowedProviders": ["codex"],
        "defaultVersion": "",
        "defaultPackage": "",
        "releaseApiUrl": "https://api.github.com/repos/GoPullwise/pullwise-worker/releases/latest",
        "releaseFetchTimeoutSeconds": 3,
        "releaseCacheSeconds": 300,
        "codexTimeoutSeconds": 1800,
    },
    "billing": {
        "billingTimeoutSeconds": 15,
        "creemProProductIds": [],
        "creemMaxProductIds": [],
        "creemApiBaseUrl": "",
        "creemTestMode": False,
        "creemUpgradeBehavior": "proration-charge-immediately",
    },
    "rateLimit": {
        "enabled": False,
        "requests": 600,
        "windowSeconds": 60,
    },
    "alerts": {
        "email": {
            "enabled": False,
            "to": [],
            "from": "",
            "smtpHost": "",
            "smtpPort": 465,
            "smtpUsername": "",
            "smtpPassword": "",
            "smtpSsl": True,
            "smtpStarttls": False,
        },
    },
}


FIELD_METADATA = [
    {
        "id": "plans",
        "title": "Plan quotas",
        "description": "Monthly scan quotas by subscription plan. These values are read when pricing, quota status, and scan quota checks are computed.",
        "fields": [
            field
            for plan in PLAN_IDS
            for field in (
                {
                    "path": f"plans.{plan}.userReviewLimit",
                    "label": f"{plan.title()} user review limit",
                    "type": "integer",
                    "min": 0,
                    "description": f"Maximum monthly scans one {plan.title()} user can start across all repositories.",
                },
                {
                    "path": f"plans.{plan}.repositoryReviewLimit",
                    "label": f"{plan.title()} repository review limit",
                    "type": "integer",
                    "min": 0,
                    "description": f"Maximum monthly scans allowed for a single repository under the {plan.title()} plan.",
                },
                {
                    "path": f"plans.{plan}.maxRepoFiles",
                    "label": f"{plan.title()} repository file limit",
                    "type": "integer",
                    "min": 1,
                    "description": f"Repository file-count ceiling for {plan.title()} worker checkouts before verifier or AI review.",
                },
                {
                    "path": f"plans.{plan}.maxRepoBytes",
                    "label": f"{plan.title()} repository byte limit",
                    "type": "integer",
                    "min": 1,
                    "description": f"Repository checkout byte ceiling for {plan.title()} worker checkouts before verifier or AI review.",
                },
            )
        ],
    },
    {
        "id": "scan",
        "title": "Scan scheduling",
        "description": "Global queue, retry, and lease policy used by the server and worker job payloads.",
        "fields": [
            {
                "path": "scan.maxQueuedScansGlobal",
                "label": "Max queued scans global",
                "type": "integer",
                "min": 1,
                "description": "Maximum queued scans across the whole service before new scan requests are rejected.",
            },
            {
                "path": "scan.jobRetryAttempts",
                "label": "Scan job retry attempts",
                "type": "integer",
                "min": 0,
                "description": "Automatic retries after a worker result fails. The server caps total attempts by the number of available worker instances so each attempt can use a different worker.",
            },
            {
                "path": "scan.jobLeaseSeconds",
                "label": "Scan job lease seconds",
                "type": "integer",
                "min": 60,
                "description": "How long a claimed job lease may run before the server can recover it as expired.",
            },
            {
                "path": "scan.jobStartupGraceSeconds",
                "label": "Scan job startup grace seconds",
                "type": "integer",
                "min": 30,
                "description": "Grace period before an unstarted claimed job missing from worker heartbeats is recovered.",
            },
        ],
    },
    {
        "id": "worker",
        "title": "Worker control plane",
        "description": "Worker compatibility, heartbeat, and release defaults used by admin worker management.",
        "fields": [
            {
                "path": "worker.heartbeatTimeoutSeconds",
                "label": "Heartbeat timeout seconds",
                "type": "integer",
                "min": 60,
                "description": "Seconds without heartbeat before an online worker is treated as offline.",
            },
            {
                "path": "worker.minVersion",
                "label": "Minimum worker version",
                "type": "string",
                "maxLength": 32,
                "description": "Lowest worker version allowed to be healthy. Leave blank to allow every version.",
            },
            {
                "path": "worker.allowedProviders",
                "label": "Allowed worker providers",
                "type": "stringList",
                "maxLength": 128,
                "description": "Comma-separated worker provider names allowed to claim jobs. Currently only codex is supported.",
            },
            {
                "path": "worker.defaultVersion",
                "label": "Default worker version",
                "type": "string",
                "maxLength": 32,
                "description": "Pinned worker release version for generated install commands. Leave blank to use the built-in default or latest release.",
            },
            {
                "path": "worker.defaultPackage",
                "label": "Default worker package",
                "type": "string",
                "maxLength": 512,
                "description": "Explicit pip package or wheel URL for generated worker installs. Leave blank to use the selected release wheel.",
            },
            {
                "path": "worker.releaseApiUrl",
                "label": "Worker release API URL",
                "type": "url",
                "maxLength": 512,
                "description": "GitHub releases API URL used to discover the latest worker package when no default version is pinned.",
            },
            {
                "path": "worker.releaseFetchTimeoutSeconds",
                "label": "Release fetch timeout seconds",
                "type": "integer",
                "min": 1,
                "description": "HTTP timeout for latest worker release discovery.",
            },
            {
                "path": "worker.releaseCacheSeconds",
                "label": "Release cache seconds",
                "type": "integer",
                "min": 0,
                "description": "How long the server caches the latest worker release response in memory.",
            },
            {
                "path": "worker.codexTimeoutSeconds",
                "label": "Codex timeout seconds",
                "type": "integer",
                "min": 60,
                "description": "Default Codex subprocess timeout written to generated worker env files.",
            },
        ],
    },
    {
        "id": "billing",
        "title": "Billing catalog",
        "description": "Non-secret billing provider settings. API keys and webhook secrets remain deployment secrets, not admin settings.",
        "fields": [
            {
                "path": "billing.billingTimeoutSeconds",
                "label": "Billing timeout seconds",
                "type": "integer",
                "min": 1,
                "description": "HTTP timeout for billing provider requests.",
            },
            {
                "path": "billing.creemProProductIds",
                "label": "Creem Pro product IDs",
                "type": "stringList",
                "maxLength": 256,
                "description": "Creem product IDs that grant the Pro plan. Add both monthly and yearly products when both are sold.",
            },
            {
                "path": "billing.creemMaxProductIds",
                "label": "Creem Max product IDs",
                "type": "stringList",
                "maxLength": 256,
                "description": "Creem product IDs that grant the Max plan. Add both monthly and yearly products when both are sold.",
            },
            {
                "path": "billing.creemApiBaseUrl",
                "label": "Creem API base URL",
                "type": "url",
                "maxLength": 256,
                "description": "Optional Creem API base URL override. Leave blank to use Creem production or test defaults.",
            },
            {
                "path": "billing.creemTestMode",
                "label": "Creem test mode",
                "type": "boolean",
                "description": "When enabled, the default Creem API host is the test endpoint.",
            },
            {
                "path": "billing.creemUpgradeBehavior",
                "label": "Creem upgrade behavior",
                "type": "select",
                "options": ["proration-charge-immediately", "proration-none"],
                "description": "Creem subscription upgrade behavior sent when users upgrade plans or billing intervals.",
            },
        ],
    },
    {
        "id": "rateLimit",
        "title": "API rate limit",
        "description": "Server-side request rate limiting for public REST API traffic. Browser web app session routes and authenticated worker endpoints are exempt.",
        "fields": [
            {
                "path": "rateLimit.enabled",
                "label": "Rate limit enabled",
                "type": "boolean",
                "description": "Turns request rate limiting on or off for public REST API endpoints and unauthenticated worker probes.",
            },
            {
                "path": "rateLimit.requests",
                "label": "Rate limit requests",
                "type": "integer",
                "min": 0,
                "description": "Allowed requests per subject during one rate-limit window. Zero disables blocking.",
            },
            {
                "path": "rateLimit.windowSeconds",
                "label": "Rate limit window seconds",
                "type": "integer",
                "min": 1,
                "description": "Length of one rate-limit accounting window.",
            },
        ],
    },
    {
        "id": "alerts",
        "title": "Operational alerts",
        "description": "Admin-managed email notifications for server and worker health problems.",
        "fields": [
            {
                "path": "alerts.email.enabled",
                "label": "Alert email enabled",
                "type": "boolean",
                "description": "Turns operational alert email delivery on or off.",
            },
            {
                "path": "alerts.email.to",
                "label": "Alert recipients",
                "type": "stringList",
                "maxLength": 256,
                "description": "Comma-separated admin email recipients for server and worker problem notifications.",
            },
            {
                "path": "alerts.email.from",
                "label": "Alert sender",
                "type": "string",
                "maxLength": 256,
                "description": "Sender address used for alert emails. Leave blank to use the SMTP username or first recipient.",
            },
            {
                "path": "alerts.email.smtpHost",
                "label": "SMTP host",
                "type": "string",
                "maxLength": 256,
                "description": "SMTP server hostname used to send alert email.",
            },
            {
                "path": "alerts.email.smtpPort",
                "label": "SMTP port",
                "type": "integer",
                "min": 1,
                "description": "SMTP server port.",
            },
            {
                "path": "alerts.email.smtpUsername",
                "label": "SMTP username",
                "type": "string",
                "maxLength": 256,
                "description": "Optional SMTP username for authenticated email delivery.",
            },
            {
                "path": "alerts.email.smtpPassword",
                "label": "SMTP password",
                "type": "password",
                "maxLength": 512,
                "description": "Optional SMTP password. Leave blank to keep the saved password.",
            },
            {
                "path": "alerts.email.smtpSsl",
                "label": "SMTP SSL",
                "type": "boolean",
                "description": "Use implicit SMTP over SSL.",
            },
            {
                "path": "alerts.email.smtpStarttls",
                "label": "SMTP STARTTLS",
                "type": "boolean",
                "description": "Upgrade plain SMTP with STARTTLS when SMTP SSL is disabled.",
            },
        ],
    },
]

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"loaded_at": 0.0, "config": None}


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def metadata() -> list[dict]:
    return copy.deepcopy(FIELD_METADATA)


def _field_specs() -> dict[str, dict]:
    return {
        field["path"]: field
        for group in FIELD_METADATA
        for field in group["fields"]
    }


def _secret_paths() -> tuple[str, ...]:
    return tuple(path for path, spec in _field_specs().items() if spec.get("type") == "password")


def admin_settings_payload(current: dict) -> tuple[dict, dict]:
    settings = copy.deepcopy(current)
    secrets: dict[str, dict[str, bool]] = {}
    for path in _secret_paths():
        found, value = nested_get(settings, path)
        if not found:
            continue
        secrets[path] = {"hasValue": bool(value)}
        nested_set(settings, path, "")
    return settings, secrets


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["loaded_at"] = 0.0
        _CACHE["config"] = None


def config() -> dict:
    current = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get("config")
        loaded_at = float(_CACHE.get("loaded_at") or 0.0)
        if isinstance(cached, dict) and current - loaded_at < CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)

    try:
        raw = db.load_state_item(STATE_KEY)
    except Exception:
        return default_config()
    normalized = normalize_config(raw if isinstance(raw, dict) else {})
    with _CACHE_LOCK:
        _CACHE["config"] = copy.deepcopy(normalized)
        _CACHE["loaded_at"] = current
    return copy.deepcopy(normalized)


def admin_payload() -> dict:
    settings, secrets = admin_settings_payload(config())
    return {
        "source": "database",
        "settings": settings,
        "defaults": default_config(),
        "groups": metadata(),
        "secrets": secrets,
        "cache": {"ttlSeconds": CACHE_TTL_SECONDS, "strategy": "process_ttl_with_write_invalidation"},
    }


def public_docs_payload() -> dict:
    current = config()
    pro_products = list_setting("billing.creemProProductIds")
    max_products = list_setting("billing.creemMaxProductIds")
    return {
        "source": "database",
        "settings": {
            "plans": current["plans"],
            "scan": {
                "maxQueuedScansGlobal": current["scan"]["maxQueuedScansGlobal"],
            },
            "rateLimit": current["rateLimit"],
            "billing": {
                "creemProProductCount": len(pro_products),
                "creemMaxProductCount": len(max_products),
                "creemTestMode": current["billing"]["creemTestMode"],
                "creemUpgradeBehavior": current["billing"]["creemUpgradeBehavior"],
            },
        },
        "groups": public_docs_groups(current, pro_products=pro_products, max_products=max_products),
        "excluded": [
            "deployment secrets",
            "database paths",
            "cookie and proxy trust policy",
            "GitHub OAuth/App credentials",
            "worker tokens and host-local paths",
        ],
    }


def public_docs_groups(current: dict, *, pro_products: list[str], max_products: list[str]) -> list[dict]:
    return [
        {
            "id": "plans",
            "title": "Plan quotas",
            "description": "Monthly scan quotas enforced by the server for each subscription plan.",
            "fields": [
                field
                for plan in PLAN_IDS
                for field in (
                    public_field(
                        f"plans.{plan}.userReviewLimit",
                        f"{plan.title()} user monthly scans",
                        current["plans"][plan]["userReviewLimit"],
                        f"Maximum scans one {plan.title()} user can start in a billing cycle.",
                    ),
                    public_field(
                        f"plans.{plan}.repositoryReviewLimit",
                        f"{plan.title()} repository monthly scans",
                        current["plans"][plan]["repositoryReviewLimit"],
                        f"Maximum scans one repository can receive in a billing cycle for {plan.title()} users.",
                    ),
                    public_field(
                        f"plans.{plan}.maxRepoFiles",
                        f"{plan.title()} repository file limit",
                        current["plans"][plan]["maxRepoFiles"],
                        f"Repository checkouts above this file count stop before verifier or AI review for {plan.title()} users.",
                    ),
                    public_field(
                        f"plans.{plan}.maxRepoBytes",
                        f"{plan.title()} repository byte limit",
                        current["plans"][plan]["maxRepoBytes"],
                        f"Repository checkouts above this size stop before verifier or AI review for {plan.title()} users.",
                    ),
                )
            ],
        },
        {
            "id": "scan",
            "title": "Scan limits",
            "description": "Global queue limits visible to users when scans are accepted or rejected.",
            "fields": [
                public_field(
                    "scan.maxQueuedScansGlobal",
                    "Global queued scans",
                    current["scan"]["maxQueuedScansGlobal"],
                    "Maximum queued scans across the service.",
                ),
            ],
        },
        {
            "id": "rateLimit",
            "title": "API rate limit",
            "description": "Request rate limiting applied by the server to public REST API traffic, not normal web app session traffic.",
            "fields": [
                public_field(
                    "rateLimit.enabled",
                    "Rate limiting enabled",
                    current["rateLimit"]["enabled"],
                    "Whether public REST API requests are rate limited.",
                ),
                public_field(
                    "rateLimit.requests",
                    "Requests per window",
                    current["rateLimit"]["requests"],
                    "Allowed requests per subject in one rate-limit window.",
                ),
                public_field(
                    "rateLimit.windowSeconds",
                    "Rate-limit window",
                    current["rateLimit"]["windowSeconds"],
                    "Rate-limit accounting window in seconds.",
                ),
            ],
        },
        {
            "id": "billing",
            "title": "Billing catalog",
            "description": "Non-secret billing catalog status. API keys and webhook secrets are intentionally not exposed.",
            "fields": [
                public_field(
                    "billing.creemProProductCount",
                    "Creem Pro products",
                    len(pro_products),
                    "Number of Creem product IDs configured to grant Pro access.",
                ),
                public_field(
                    "billing.creemMaxProductCount",
                    "Creem Max products",
                    len(max_products),
                    "Number of Creem product IDs configured to grant Max access.",
                ),
                public_field(
                    "billing.creemTestMode",
                    "Creem test mode",
                    current["billing"]["creemTestMode"],
                    "Whether the server uses Creem's test API host when no custom base URL is configured.",
                ),
            ],
        },
    ]


def public_field(path: str, label: str, value: object, description: str) -> dict:
    return {"path": path, "label": label, "value": value, "description": description}


def update(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("System config update must be a JSON object.")
    updates = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    if not isinstance(updates, dict):
        raise ValueError("settings must be a JSON object.")

    specs = _field_specs()
    unknown = sorted(path for path in flatten_paths(updates) if path not in specs and path != "version")
    if unknown:
        raise ValueError(f"Unknown system config field: {unknown[0]}.")

    next_config = config()
    for path, spec in specs.items():
        found, value = nested_get(updates, path)
        if found:
            if spec.get("type") == "password" and clean_text(value, max_length=int(spec.get("maxLength", 128))) == "":
                continue
            nested_set(next_config, path, clean_value(value, spec))

    normalized = normalize_config(next_config)
    db.save_state_item(STATE_KEY, normalized)
    invalidate_cache()
    return admin_payload()


def normalize_config(raw: dict) -> dict:
    normalized = default_config()
    specs = _field_specs()
    for path, spec in specs.items():
        found, value = nested_get(raw, path)
        if not found:
            continue
        try:
            nested_set(normalized, path, clean_value(value, spec))
        except ValueError:
            continue
    return normalized


def flatten_paths(value: object, prefix: str = "") -> list[str]:
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            paths.extend(flatten_paths(item, path))
        else:
            paths.append(path)
    return paths


def nested_get(source: dict, path: str) -> tuple[bool, object]:
    current: object = source
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def nested_set(target: dict, path: str, value: object) -> None:
    current = target
    segments = path.split(".")
    for segment in segments[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[segments[-1]] = value


def clean_value(value: object, spec: dict) -> object:
    kind = spec.get("type")
    if kind == "integer":
        return clean_int(value, minimum=int(spec.get("min", 0)))
    if kind == "number":
        return clean_number(value, minimum=float(spec.get("min", 0)))
    if kind == "boolean":
        return clean_bool(value)
    if kind == "stringList":
        return clean_string_list(value, max_length=int(spec.get("maxLength", 128)))
    if kind == "url":
        return clean_url(value, max_length=int(spec.get("maxLength", 512)))
    if kind == "select":
        text = clean_text(value, max_length=int(spec.get("maxLength", 128))).lower()
        options = spec.get("options") if isinstance(spec.get("options"), list) else []
        if text not in options:
            raise ValueError(f"{spec['label']} must be one of: {', '.join(options)}.")
        return text
    if kind == "password":
        return clean_text(value, max_length=int(spec.get("maxLength", 128)))
    return clean_text(value, max_length=int(spec.get("maxLength", 128)))


def clean_int(value: object, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("Integer settings must be numbers.")
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        raise ValueError("Integer settings must be numbers.") from None
    if candidate < minimum:
        return minimum
    return candidate


def clean_number(value: object, *, minimum: float) -> float:
    if isinstance(value, bool):
        raise ValueError("Number settings must be numbers.")
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        raise ValueError("Number settings must be numbers.") from None
    if candidate < minimum:
        return minimum
    return candidate


def clean_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ValueError("Boolean settings must be true or false.")


def clean_text(value: object, *, max_length: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_length:
        raise ValueError(f"Text settings must be at most {max_length} characters.")
    if any(char in text for char in "\r\n\x00"):
        raise ValueError("Text settings cannot contain newlines or NUL bytes.")
    return text


def clean_string_list(value: object, *, max_length: int) -> list[str]:
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, list):
        candidates = value
    else:
        raise ValueError("List settings must be a comma-separated string or array.")
    items: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = clean_text(candidate, max_length=max_length)
        if not text:
            continue
        if text not in seen:
            items.append(text)
            seen.add(text)
    return items


def clean_url(value: object, *, max_length: int) -> str:
    text = clean_text(value, max_length=max_length)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL settings must be absolute HTTP(S) URLs.")
    return text.rstrip("/")


def plan_id(plan: object, default: str = "free") -> str:
    candidate = str(plan or default).strip().lower()
    return candidate if candidate in PLAN_IDS else default


def int_setting(path: str) -> int:
    found, value = nested_get(config(), path)
    if not found:
        found, value = nested_get(DEFAULT_CONFIG, path)
    return int(value or 0)


def number_setting(path: str) -> float:
    found, value = nested_get(config(), path)
    if not found:
        found, value = nested_get(DEFAULT_CONFIG, path)
    return float(value or 0.0)


def bool_setting(path: str) -> bool:
    found, value = nested_get(config(), path)
    if not found:
        found, value = nested_get(DEFAULT_CONFIG, path)
    return bool(value)


def env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def text_setting(path: str) -> str:
    found, value = nested_get(config(), path)
    if not found:
        found, value = nested_get(DEFAULT_CONFIG, path)
    return str(value or "")


def list_setting(path: str) -> list[str]:
    found, value = nested_get(config(), path)
    if not found:
        found, value = nested_get(DEFAULT_CONFIG, path)
    return list(value) if isinstance(value, list) else []


def alert_email_enabled() -> bool:
    return bool_setting("alerts.email.enabled")


def alert_email_recipients() -> list[str]:
    return list_setting("alerts.email.to")


def alert_email_from() -> str:
    return text_setting("alerts.email.from")


def alert_smtp_host() -> str:
    return text_setting("alerts.email.smtpHost")


def alert_smtp_port() -> int:
    return max(1, int_setting("alerts.email.smtpPort"))


def alert_smtp_username() -> str:
    return text_setting("alerts.email.smtpUsername")


def alert_smtp_password() -> str:
    return text_setting("alerts.email.smtpPassword")


def alert_smtp_ssl() -> bool:
    return bool_setting("alerts.email.smtpSsl")


def alert_smtp_starttls() -> bool:
    return bool_setting("alerts.email.smtpStarttls")


def plan_user_review_limit(plan: object) -> int:
    return max(0, int_setting(f"plans.{plan_id(plan)}.userReviewLimit"))


def plan_repository_review_limit(plan: object) -> int:
    return max(0, int_setting(f"plans.{plan_id(plan)}.repositoryReviewLimit"))


def plan_repository_file_limit(plan: object) -> int:
    return max(1, int_setting(f"plans.{plan_id(plan, default='max')}.maxRepoFiles"))


def plan_repository_byte_limit(plan: object) -> int:
    return max(1, int_setting(f"plans.{plan_id(plan, default='max')}.maxRepoBytes"))


def repository_scan_limits(plan: object = "max") -> dict:
    normalized_plan = plan_id(plan, default="max")
    return {
        "maxFiles": plan_repository_file_limit(normalized_plan),
        "maxBytes": plan_repository_byte_limit(normalized_plan),
        "source": "database",
    }


def max_queued_scans_global() -> int:
    configured = env_int("PULLWISE_MAX_QUEUED_SCANS_GLOBAL")
    if configured is not None:
        return max(1, configured)
    return max(1, int_setting("scan.maxQueuedScansGlobal"))


def scan_job_max_attempts() -> int:
    return max(1, scan_job_retry_attempts() + 1)


def scan_job_retry_attempts() -> int:
    configured = env_int("PULLWISE_SCAN_JOB_RETRY_ATTEMPTS")
    if configured is not None:
        return max(0, configured)
    return max(0, int_setting("scan.jobRetryAttempts"))


def scan_job_lease_seconds() -> int:
    return max(60, int_setting("scan.jobLeaseSeconds"))


def scan_job_startup_grace_seconds() -> int:
    configured = env_int("PULLWISE_SCAN_JOB_STARTUP_GRACE_SECONDS")
    if configured is not None:
        return max(30, configured)
    return max(30, int_setting("scan.jobStartupGraceSeconds"))


def worker_heartbeat_timeout_seconds() -> int:
    return max(60, int_setting("worker.heartbeatTimeoutSeconds"))


def worker_min_version() -> str:
    return text_setting("worker.minVersion") or os.environ.get("PULLWISE_MIN_WORKER_VERSION", "")


def worker_allowed_providers() -> set[str]:
    providers = {item.strip() for item in list_setting("worker.allowedProviders") if item.strip()}
    return providers or {"codex"}


def worker_default_version() -> str:
    return text_setting("worker.defaultVersion")


def worker_default_package() -> str:
    return text_setting("worker.defaultPackage")


def worker_release_api_url() -> str:
    return text_setting("worker.releaseApiUrl")


def worker_release_fetch_timeout_seconds() -> int:
    return max(1, int_setting("worker.releaseFetchTimeoutSeconds"))


def worker_release_cache_seconds() -> int:
    return max(0, int_setting("worker.releaseCacheSeconds"))


def worker_codex_timeout_seconds() -> int:
    return max(60, int_setting("worker.codexTimeoutSeconds"))


def rate_limit_enabled() -> bool:
    configured = env_bool("PULLWISE_RATE_LIMIT_ENABLED")
    if configured is not None:
        return configured
    return bool_setting("rateLimit.enabled")


def rate_limit_requests() -> int:
    configured = env_int("PULLWISE_RATE_LIMIT_REQUESTS")
    if configured is not None:
        return max(0, configured)
    if "PULLWISE_RATE_LIMIT_ENABLED" in os.environ:
        return max(0, int(DEFAULT_CONFIG["rateLimit"]["requests"]))
    return max(0, int_setting("rateLimit.requests"))


def rate_limit_window_seconds() -> int:
    configured = env_int("PULLWISE_RATE_LIMIT_WINDOW_SECONDS")
    if configured is not None:
        return max(1, configured)
    if "PULLWISE_RATE_LIMIT_ENABLED" in os.environ:
        return max(1, int(DEFAULT_CONFIG["rateLimit"]["windowSeconds"]))
    return max(1, int_setting("rateLimit.windowSeconds"))


def billing_timeout_seconds() -> int:
    return max(1, int_setting("billing.billingTimeoutSeconds"))


def creem_product_ids_for_plan(plan: object) -> list[str]:
    normalized = plan_id(plan)
    if normalized == "pro":
        return list_setting("billing.creemProProductIds")
    if normalized == "max":
        return list_setting("billing.creemMaxProductIds")
    return []


def creem_test_mode() -> bool:
    return bool_setting("billing.creemTestMode")


def creem_api_base_url() -> str:
    configured = text_setting("billing.creemApiBaseUrl")
    if configured:
        return configured[:-3] if configured.endswith("/v1") else configured
    return DEFAULT_CREEM_TEST_API_BASE_URL if creem_test_mode() else DEFAULT_CREEM_API_BASE_URL


def creem_upgrade_behavior() -> str:
    behavior = text_setting("billing.creemUpgradeBehavior")
    return behavior if behavior in CREEM_UPDATE_BEHAVIORS else "proration-charge-immediately"
