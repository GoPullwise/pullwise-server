from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from .model_gateway_token_codec import GatewayGrantStatus, SAFE_ID, SHA256_DIGEST
from .model_gateway_runtime import GatewayRoute


IntrospectionTransport = Callable[..., dict[str, object]]


class IntrospectionClientError(RuntimeError):
    pass


def _introspection_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/internal/model-gateway/grants/introspect"
        or parsed.query
        or parsed.fragment
    ):
        raise IntrospectionClientError("introspection URL must be the fixed HTTPS grant endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IntrospectionClientError("introspection URL port is invalid") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit(("https", authority, parsed.path, "", ""))


def _route_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/internal/model-gateway/routes/resolve"
        or parsed.query
        or parsed.fragment
    ):
        raise IntrospectionClientError("route URL must be the fixed HTTPS resolution endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IntrospectionClientError("route URL port is invalid") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit(("https", authority, parsed.path, "", ""))


def _transport(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise IntrospectionClientError("grant introspection failed") from exc
    if response.status_code != 200:
        raise IntrospectionClientError("grant introspection failed")
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise IntrospectionClientError("grant introspection returned invalid state") from exc
    if not isinstance(body, dict):
        raise IntrospectionClientError("grant introspection returned invalid state")
    return body


class HttpGrantStatusResolver:
    def __init__(
        self,
        *,
        introspection_url: str,
        introspection_token: str,
        transport: IntrospectionTransport = _transport,
        timeout_seconds: float = 3.0,
    ) -> None:
        if (
            not isinstance(introspection_token, str)
            or not introspection_token
            or len(introspection_token) > 4096
            or any(ord(character) < 33 for character in introspection_token)
        ):
            raise IntrospectionClientError("introspection credential is invalid")
        if not isinstance(timeout_seconds, (int, float)) or not 0.1 <= float(timeout_seconds) <= 30:
            raise IntrospectionClientError("introspection timeout is invalid")
        self._url = _introspection_url(introspection_url)
        self._token = introspection_token
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)

    def __call__(self, jti: str, token_hash: str) -> GatewayGrantStatus:
        if not isinstance(jti, str) or not SAFE_ID.fullmatch(jti):
            raise IntrospectionClientError("grant jti is invalid")
        if not isinstance(token_hash, str) or not SHA256_DIGEST.fullmatch(token_hash):
            raise IntrospectionClientError("grant token hash is invalid")
        try:
            response = self._transport(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                payload={"jti": jti, "token_hash": token_hash},
                timeout_seconds=self._timeout_seconds,
            )
        except IntrospectionClientError:
            raise
        except BaseException as exc:
            raise IntrospectionClientError("grant introspection failed") from exc
        if set(response) != {
            "active",
            "worker_enabled",
            "generation",
            "desired_profile_revision",
        }:
            raise IntrospectionClientError("grant introspection returned invalid state")
        active = response.get("active")
        worker_enabled = response.get("worker_enabled")
        generation = response.get("generation")
        revision = response.get("desired_profile_revision")
        if (
            not isinstance(active, bool)
            or not isinstance(worker_enabled, bool)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise IntrospectionClientError("grant introspection returned invalid state")
        return GatewayGrantStatus(
            active=active,
            worker_enabled=worker_enabled,
            generation=generation,
            desired_profile_revision=revision,
        )


class HttpGatewayRouteResolver:
    def __init__(
        self,
        *,
        route_url: str,
        introspection_token: str,
        transport: IntrospectionTransport = _transport,
        timeout_seconds: float = 3.0,
    ) -> None:
        if (
            not isinstance(introspection_token, str)
            or not introspection_token
            or len(introspection_token) > 4096
            or any(ord(character) < 33 for character in introspection_token)
        ):
            raise IntrospectionClientError("route resolution credential is invalid")
        if not isinstance(timeout_seconds, (int, float)) or not 0.1 <= float(timeout_seconds) <= 30:
            raise IntrospectionClientError("route resolution timeout is invalid")
        self._url = _route_url(route_url)
        self._token = introspection_token
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)

    def resolve(
        self,
        profile_set_id: str,
        profile_revision: int,
        model_alias: str,
    ) -> GatewayRoute:
        if not isinstance(profile_set_id, str) or not SAFE_ID.fullmatch(profile_set_id):
            raise IntrospectionClientError("route profile_set_id is invalid")
        if not isinstance(model_alias, str) or not SAFE_ID.fullmatch(model_alias):
            raise IntrospectionClientError("route model_alias is invalid")
        if isinstance(profile_revision, bool) or not isinstance(profile_revision, int) or profile_revision <= 0:
            raise IntrospectionClientError("route profile_revision is invalid")
        try:
            response = self._transport(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                payload={
                    "profile_set_id": profile_set_id,
                    "profile_revision": profile_revision,
                    "model_alias": model_alias,
                },
                timeout_seconds=self._timeout_seconds,
            )
        except IntrospectionClientError:
            raise
        except BaseException as exc:
            raise IntrospectionClientError("route resolution failed") from exc
        if set(response) != set(GatewayRoute.__dataclass_fields__):
            raise IntrospectionClientError("route resolution returned invalid metadata")
        try:
            route = GatewayRoute(**response)
        except (TypeError, ValueError) as exc:
            raise IntrospectionClientError("route resolution returned invalid metadata") from exc
        if (
            route.profile_set_id != profile_set_id
            or route.profile_revision != profile_revision
            or route.model_alias != model_alias
        ):
            raise IntrospectionClientError("route resolution returned mismatched metadata")
        return route
