from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


OFFICIAL_PROVIDER_ORIGINS = {
    "openai": frozenset({"https://api.openai.com"}),
    "deepseek": frozenset({"https://api.deepseek.com"}),
    "minimax": frozenset({"https://api.minimax.io", "https://api.minimaxi.com"}),
}


def official_provider_origin(value: object, provider: str) -> str:
    if not isinstance(value, str):
        raise ValueError("endpoint_origin must be an official origin for provider")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("endpoint_origin must be an official origin for provider")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint_origin must be an official origin for provider") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    normalized = urlunsplit(("https", authority, "", "", ""))
    if provider not in OFFICIAL_PROVIDER_ORIGINS or normalized not in OFFICIAL_PROVIDER_ORIGINS[provider]:
        raise ValueError("endpoint_origin must be an official origin for provider")
    return normalized
