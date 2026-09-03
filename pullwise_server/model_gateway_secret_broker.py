from __future__ import annotations

import hmac
from typing import Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from .model_gateway_secrets import StoredSecretVersion


class WritableSecretStore(Protocol):
    def put_candidate(self, secret_ref: str, secret: bytes) -> StoredSecretVersion: ...
    def read_version(self, secret_ref: str, version: str) -> bytes: ...
    def retire_version(self, secret_ref: str, version: str) -> None: ...


class SecretValidator(Protocol):
    def validate(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> list[str]: ...
    def canary(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> None: ...


BrokerTransport = Callable[..., dict[str, object]]


class SecretBrokerError(RuntimeError):
    pass


def _broker_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/internal/provider-secrets"
        or parsed.query
        or parsed.fragment
    ):
        raise SecretBrokerError("secret broker URL must be the fixed HTTPS provider-secrets endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SecretBrokerError("secret broker URL port is invalid") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit(("https", authority, parsed.path, "", ""))


def _write_token(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 33 for char in value):
        raise SecretBrokerError("secret broker write credential is invalid")
    return value


def _requests_transport(
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
        raise SecretBrokerError("secret broker write failed") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise SecretBrokerError("secret broker write failed")
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise SecretBrokerError("secret broker returned invalid metadata") from exc
    if not isinstance(body, dict):
        raise SecretBrokerError("secret broker returned invalid metadata")
    return body


class HttpSecretWriter:
    def __init__(
        self,
        *,
        broker_url: str,
        write_token: str,
        transport: BrokerTransport = _requests_transport,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 0.1 <= float(timeout_seconds) <= 30:
            raise SecretBrokerError("secret broker timeout is invalid")
        self._url = _broker_url(broker_url)
        self._write_token = _write_token(write_token)
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)

    def _call(self, path_suffix: str, payload: dict[str, object]) -> dict[str, object]:
        url = self._url if not path_suffix else f"{self._url}/{path_suffix}"
        try:
            return self._transport(
                url,
                headers={
                    "Authorization": f"Bearer {self._write_token}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=self._timeout_seconds,
            )
        except SecretBrokerError:
            raise
        except BaseException:
            raise SecretBrokerError("secret broker write failed") from None

    def put_candidate(self, secret_ref: str, secret: bytes) -> StoredSecretVersion:
        try:
            secret_text = secret.decode("utf-8")
        except (AttributeError, UnicodeDecodeError) as exc:
            raise SecretBrokerError("provider secret must be valid UTF-8") from exc
        response = self._call("", {"secret_ref": secret_ref, "secret": secret_text})
        if set(response) != {"fingerprint", "version"}:
            raise SecretBrokerError("secret broker returned invalid metadata")
        version = response.get("version")
        fingerprint = response.get("fingerprint")
        if not isinstance(version, str) or not isinstance(fingerprint, str):
            raise SecretBrokerError("secret broker returned invalid metadata")
        return StoredSecretVersion(version=version, fingerprint=fingerprint)

    def validate_candidate(self, secret_ref: str, version: str, **metadata: object) -> list[str]:
        response = self._call("validate", {"secret_ref": secret_ref, "version": version, **metadata})
        if set(response) != {"models", "ok"} or response.get("ok") is not True or not isinstance(response.get("models"), list):
            raise SecretBrokerError("secret broker validation failed")
        models = response["models"]
        if not models or any(not isinstance(model, str) for model in models):
            raise SecretBrokerError("secret broker validation failed")
        return models

    def canary_candidate(self, secret_ref: str, version: str, **metadata: object) -> None:
        response = self._call("canary", {"secret_ref": secret_ref, "version": version, **metadata})
        if response != {"ok": True}:
            raise SecretBrokerError("secret broker canary failed")

    def retire_version(self, secret_ref: str, version: str) -> None:
        response = self._call("retire", {"secret_ref": secret_ref, "version": version})
        if response != {"retired": True}:
            raise SecretBrokerError("secret broker retirement failed")


class GatewaySecretBroker:
    def __init__(
        self,
        *,
        secret_store: WritableSecretStore,
        write_token: str,
        validator: SecretValidator | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._write_token = _write_token(write_token)
        self._validator = validator

    def _authorize(self, authorization: str) -> None:
        expected = f"Bearer {self._write_token}"
        if not isinstance(authorization, str) or not hmac.compare_digest(authorization, expected):
            raise SecretBrokerError("secret broker authorization failed")

    def put_candidate(
        self,
        *,
        authorization: str,
        payload: object,
    ) -> dict[str, object]:
        self._authorize(authorization)
        if not isinstance(payload, dict) or set(payload) != {"secret_ref", "secret"}:
            raise SecretBrokerError("secret broker payload must be a closed object")
        secret_ref = payload.get("secret_ref")
        secret = payload.get("secret")
        if not isinstance(secret_ref, str) or not isinstance(secret, str):
            raise SecretBrokerError("secret broker payload is invalid")
        secret_bytes = secret.encode("utf-8")
        if not secret_bytes or len(secret_bytes) > 16_384:
            raise SecretBrokerError("secret broker payload is invalid")
        stored = self._secret_store.put_candidate(secret_ref, secret_bytes)
        return {"version": stored.version, "fingerprint": stored.fingerprint}

    def _validation_payload(self, authorization: str, payload: object) -> tuple[str, str, str, str, str]:
        self._authorize(authorization)
        if not isinstance(payload, dict) or set(payload) != {
            "secret_ref", "version", "provider", "adapter", "endpoint_origin",
        }:
            raise SecretBrokerError("secret validation payload must be a closed object")
        values = tuple(payload.get(key) for key in ("secret_ref", "version", "provider", "adapter", "endpoint_origin"))
        if not all(isinstance(value, str) and value for value in values):
            raise SecretBrokerError("secret validation payload is invalid")
        return values

    def validate_candidate(self, *, authorization: str, payload: object) -> dict[str, object]:
        if self._validator is None:
            raise SecretBrokerError("secret validator is unavailable")
        secret_ref, version, provider, adapter, endpoint_origin = self._validation_payload(authorization, payload)
        secret = self._secret_store.read_version(secret_ref, version)
        models = self._validator.validate(endpoint_origin=endpoint_origin, adapter=adapter, provider=provider, secret=secret)
        if not isinstance(models, list) or not models:
            raise SecretBrokerError("secret validator returned invalid model metadata")
        return {"ok": True, "models": models}

    def canary_candidate(self, *, authorization: str, payload: object) -> dict[str, object]:
        if self._validator is None:
            raise SecretBrokerError("secret validator is unavailable")
        secret_ref, version, provider, adapter, endpoint_origin = self._validation_payload(authorization, payload)
        secret = self._secret_store.read_version(secret_ref, version)
        self._validator.canary(endpoint_origin=endpoint_origin, adapter=adapter, provider=provider, secret=secret)
        return {"ok": True}

    def retire_version(self, *, authorization: str, payload: object) -> dict[str, object]:
        self._authorize(authorization)
        if not isinstance(payload, dict) or set(payload) != {"secret_ref", "version"}:
            raise SecretBrokerError("secret retirement payload must be a closed object")
        secret_ref = payload.get("secret_ref")
        version = payload.get("version")
        if not isinstance(secret_ref, str) or not isinstance(version, str):
            raise SecretBrokerError("secret retirement payload is invalid")
        self._secret_store.retire_version(secret_ref, version)
        return {"retired": True}
