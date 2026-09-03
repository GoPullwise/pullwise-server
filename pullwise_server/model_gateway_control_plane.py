from __future__ import annotations

import re
import secrets
import json
from typing import Callable, Protocol

from .model_gateway_endpoints import official_provider_origin
from .model_gateway_secret_cleanup import SecretCleanupCoordinator
from .model_gateway_store import ConnectFactory, ModelGatewayStore
from .model_gateway_pool_store import ModelGatewayPoolStore


CONNECTION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{12,64}$")
ALLOWED_PROVIDERS = frozenset({"openai", "deepseek", "minimax"})
ALLOWED_ADAPTERS = frozenset({"openai-completions"})
CREATE_FIELDS = frozenset(
    {
        "provider_connection_id",
        "display_name",
        "provider",
        "adapter",
        "endpoint_origin",
        "secret",
    }
)
PUBLISH_PROFILE_FIELDS = frozenset({"profile_set_id", "display_name", "routes"})
CREATE_POOL_FIELDS = frozenset(
    {"worker_pool_id", "display_name", "profile_set_id", "profile_revision"}
)
PROFILE_ROUTE_FIELDS = frozenset(
    {
        "route_id",
        "provider_connection_id",
        "provider",
        "model_alias",
        "upstream_model",
        "api",
        "enabled",
    }
)


class SecretVersion(Protocol):
    version: str
    fingerprint: str


class SecretWriter(Protocol):
    def put_candidate(self, secret_ref: str, secret: bytes) -> SecretVersion: ...
    def validate_candidate(self, secret_ref: str, version: str, **metadata: object) -> list[str]: ...
    def retire_version(self, secret_ref: str, version: str) -> None: ...


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _required_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    text = value.strip()
    if not text or len(text) > max_length or any(ord(character) < 32 for character in text):
        raise ValueError(f"{label} is invalid")
    return text


def _public_provider_connection(record: dict[str, object]) -> dict[str, object]:
    payload = {
        "providerConnectionId": record["provider_connection_id"],
        "displayName": record["display_name"],
        "provider": record["provider"],
        "adapter": record["adapter"],
        "endpointOrigin": record["endpoint_origin"],
        "status": record["status"],
        "secretVersion": record["secret_version"],
        "secretFingerprint": record["secret_fingerprint"],
        "validatedModels": json.loads(str(record["validated_models_json"])),
        "lastValidatedAt": record["last_validated_at"],
        "lastRotatedAt": record["last_rotated_at"],
        "createdAt": record["created_at"],
        "updatedAt": record["updated_at"],
    }
    for source, target in (
        ("candidate_secret_version", "candidateSecretVersion"),
        ("candidate_secret_fingerprint", "candidateSecretFingerprint"),
        ("previous_secret_version", "previousSecretVersion"),
        ("previous_secret_fingerprint", "previousSecretFingerprint"),
    ):
        if record.get(source) is not None:
            payload[target] = record[source]
    return payload


def _public_profile_set(record: dict[str, object]) -> dict[str, object]:
    return {
        "profileSetId": record["profile_set_id"],
        "displayName": record["display_name"],
        "revision": record["revision"],
        "status": record["status"],
        "manifest": record["manifest"],
        "manifestDigest": record["manifest_digest"],
        "createdAt": record["created_at"],
        "updatedAt": record["updated_at"],
    }


class ModelGatewayControlPlane:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        secret_writer: SecretWriter,
        clock: Callable[[], int],
        id_factory: Callable[[str], str] = _default_id_factory,
    ) -> None:
        self._store = ModelGatewayStore(connect_factory)
        self._pool_store = ModelGatewayPoolStore(connect_factory)
        self._secret_writer = secret_writer
        self._secret_cleanup = SecretCleanupCoordinator(
            connect_factory=connect_factory, secret_writer=secret_writer, clock=clock
        )
        self._clock = clock
        self._id_factory = id_factory

    def create_provider_connection(
        self,
        *,
        actor_user_id: str,
        request_id: str | None,
        payload: object,
    ) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != CREATE_FIELDS:
            raise ValueError("provider connection payload must be a closed object")
        connection_id = _required_text(
            payload["provider_connection_id"],
            "provider_connection_id",
            max_length=64,
        ).lower()
        if not CONNECTION_ID.fullmatch(connection_id):
            raise ValueError("provider_connection_id is invalid")
        display_name = _required_text(payload["display_name"], "display_name", max_length=120)
        provider = _required_text(payload["provider"], "provider", max_length=32).lower()
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError("provider is unsupported")
        adapter = _required_text(payload["adapter"], "adapter", max_length=64).lower()
        if adapter not in ALLOWED_ADAPTERS:
            raise ValueError("adapter is unsupported")
        endpoint_origin = official_provider_origin(payload["endpoint_origin"], provider)
        secret = _required_text(payload["secret"], "secret", max_length=16_384).encode("utf-8")
        actor = _required_text(actor_user_id, "actor_user_id", max_length=128)
        safe_request_id = request_id.strip()[:128] if isinstance(request_id, str) and request_id.strip() else None

        secret_ref = f"provider/{connection_id}/{self._id_factory('secret')}"
        try:
            stored = self._secret_writer.put_candidate(secret_ref, secret)
        except BaseException:
            raise RuntimeError("provider secret write failed") from None
        secret_version = _required_text(getattr(stored, "version", None), "secret version", max_length=128)
        fingerprint = _required_text(getattr(stored, "fingerprint", None), "secret fingerprint", max_length=71)
        if not SAFE_TOKEN.fullmatch(secret_version) or not SAFE_FINGERPRINT.fullmatch(fingerprint):
            raise RuntimeError("secret writer returned invalid metadata")
        try:
            discovered_models = self._secret_writer.validate_candidate(
                secret_ref,
                secret_version,
                provider=provider,
                adapter=adapter,
                endpoint_origin=endpoint_origin,
            )
        except BaseException:
            self._secret_cleanup.retire_or_queue(
                secret_ref, secret_version, reason="provider_create_validation_failed"
            )
            raise RuntimeError("provider secret validation failed") from None
        if not isinstance(discovered_models, list) or not discovered_models or len(discovered_models) > 512:
            self._secret_cleanup.retire_or_queue(
                secret_ref, secret_version, reason="provider_create_models_invalid"
            )
            raise RuntimeError("provider validation returned invalid model metadata")
        validated_models = sorted(set(discovered_models))
        if (
            len(validated_models) != len(discovered_models)
            or any(not isinstance(model, str) or not SAFE_TOKEN.fullmatch(model) for model in validated_models)
        ):
            self._secret_cleanup.retire_or_queue(
                secret_ref, secret_version, reason="provider_create_models_invalid"
            )
            raise RuntimeError("provider validation returned invalid model metadata")

        timestamp = int(self._clock())
        record: dict[str, object] = {
            "provider_connection_id": connection_id,
            "display_name": display_name,
            "provider": provider,
            "adapter": adapter,
            "endpoint_origin": endpoint_origin,
            "secret_ref": secret_ref,
            "status": "configured",
            "secret_version": secret_version,
            "secret_fingerprint": fingerprint,
            "validated_models_json": json.dumps(validated_models, separators=(",", ":")),
            "candidate_secret_version": None,
            "candidate_secret_fingerprint": None,
            "previous_secret_version": None,
            "previous_secret_fingerprint": None,
            "last_validated_at": timestamp,
            "last_rotated_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            self._store.create_provider_connection(
                record,
                audit_event={
                "id": self._id_factory("audit"),
                "actor_user_id": actor,
                "action": "provider_connection.created",
                "subject_type": "provider_connection",
                "subject_id": connection_id,
                "changed_fields": {
                    "adapter": adapter,
                    "endpointOrigin": endpoint_origin,
                    "provider": provider,
                    "secretVersion": secret_version,
                    "status": "configured",
                },
                "request_id": safe_request_id,
                "created_at": timestamp,
                "success": True,
                "error_code": None,
                },
            )
        except BaseException:
            self._secret_cleanup.retire_or_queue(
                secret_ref, secret_version, reason="provider_create_persistence_failed"
            )
            raise
        return _public_provider_connection(record)

    def list_provider_connections(self) -> list[dict[str, object]]:
        return [
            _public_provider_connection(record)
            for record in self._store.list_provider_connections()
        ]

    def publish_profile_set(
        self,
        *,
        actor_user_id: str,
        request_id: str | None,
        payload: object,
    ) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != PUBLISH_PROFILE_FIELDS:
            raise ValueError("profile set payload must be a closed object")
        profile_set_id = _required_text(payload["profile_set_id"], "profile_set_id", max_length=64).lower()
        if not CONNECTION_ID.fullmatch(profile_set_id):
            raise ValueError("profile_set_id is invalid")
        display_name = _required_text(payload["display_name"], "display_name", max_length=120)
        raw_routes = payload["routes"]
        if not isinstance(raw_routes, list) or not 1 <= len(raw_routes) <= 32:
            raise ValueError("routes must contain between 1 and 32 entries")
        routes: list[dict[str, object]] = []
        for raw_route in raw_routes:
            if not isinstance(raw_route, dict) or set(raw_route) != PROFILE_ROUTE_FIELDS:
                raise ValueError("profile route must be a closed object")
            route_id = _required_text(raw_route["route_id"], "route_id", max_length=64).lower()
            connection_id = _required_text(
                raw_route["provider_connection_id"],
                "provider_connection_id",
                max_length=64,
            ).lower()
            model_alias = _required_text(raw_route["model_alias"], "model_alias", max_length=128)
            if not SAFE_TOKEN.fullmatch(route_id) or not CONNECTION_ID.fullmatch(connection_id) or not SAFE_TOKEN.fullmatch(model_alias):
                raise ValueError("profile route identifier is invalid")
            provider = _required_text(raw_route["provider"], "provider", max_length=64).lower()
            if provider != "pullwise-gateway":
                raise ValueError("profile route provider must be pullwise-gateway")
            api = _required_text(raw_route["api"], "api", max_length=64).lower()
            if api not in ALLOWED_ADAPTERS:
                raise ValueError("profile route api is unsupported")
            if not isinstance(raw_route["enabled"], bool):
                raise ValueError("profile route enabled must be a boolean")
            routes.append(
                {
                    "route_id": route_id,
                    "provider_connection_id": connection_id,
                    "provider": provider,
                    "model_alias": model_alias,
                    "upstream_model": _required_text(raw_route["upstream_model"], "upstream_model", max_length=128),
                    "api": api,
                    "enabled": raw_route["enabled"],
                }
            )
        routes.sort(key=lambda route: str(route["route_id"]))
        route_ids = [str(route["route_id"]) for route in routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route_id values must be unique")
        enabled_aliases = [str(route["model_alias"]) for route in routes if route["enabled"] is True]
        if len(enabled_aliases) != len(set(enabled_aliases)):
            raise ValueError("enabled model_alias values must be unique")
        actor = _required_text(actor_user_id, "actor_user_id", max_length=128)
        safe_request_id = request_id.strip()[:128] if isinstance(request_id, str) and request_id.strip() else None
        timestamp = int(self._clock())
        record = self._store.publish_profile_set(
            profile_set_id=profile_set_id,
            display_name=display_name,
            routes=routes,
            actor_user_id=actor,
            request_id=safe_request_id,
            timestamp=timestamp,
            audit_id=self._id_factory("audit"),
        )
        return _public_profile_set(record)

    def create_worker_pool(
        self,
        *,
        actor_user_id: str,
        request_id: str | None,
        payload: object,
    ) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != CREATE_POOL_FIELDS:
            raise ValueError("worker pool payload must be a closed object")
        pool_id = _required_text(payload["worker_pool_id"], "worker_pool_id", max_length=64).lower()
        profile_set_id = _required_text(payload["profile_set_id"], "profile_set_id", max_length=64).lower()
        if not CONNECTION_ID.fullmatch(pool_id) or not CONNECTION_ID.fullmatch(profile_set_id):
            raise ValueError("worker pool identifier is invalid")
        revision = payload["profile_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("profile_revision must be a positive integer")
        actor = _required_text(actor_user_id, "actor_user_id", max_length=128)
        timestamp = int(self._clock())
        record = self._pool_store.create_worker_pool(
            worker_pool_id=pool_id,
            display_name=_required_text(payload["display_name"], "display_name", max_length=120),
            profile_set_id=profile_set_id,
            profile_revision=revision,
            actor_user_id=actor,
            request_id=request_id,
            timestamp=timestamp,
            audit_id=self._id_factory("audit"),
        )
        return {
            "workerPoolId": record["worker_pool_id"],
            "displayName": record["display_name"],
            "profileSetId": record["profile_set_id"],
            "desiredRevision": record["desired_revision"],
            "gatewayTokenGeneration": record["gateway_token_generation"],
            "status": record["status"],
            "createdAt": record["created_at"],
            "updatedAt": record["updated_at"],
        }

    def bind_worker_to_pool(
        self,
        *,
        actor_user_id: str,
        request_id: str | None,
        worker_id: str,
        worker_pool_id: str,
    ) -> dict[str, object]:
        safe_worker_id = _required_text(worker_id, "worker_id", max_length=128)
        safe_pool_id = _required_text(worker_pool_id, "worker_pool_id", max_length=64).lower()
        if not SAFE_TOKEN.fullmatch(safe_worker_id) or not CONNECTION_ID.fullmatch(safe_pool_id):
            raise ValueError("worker pool membership identifier is invalid")
        timestamp = int(self._clock())
        record = self._pool_store.bind_worker_to_pool(
            worker_id=safe_worker_id,
            worker_pool_id=safe_pool_id,
            actor_user_id=_required_text(actor_user_id, "actor_user_id", max_length=128),
            request_id=request_id,
            timestamp=timestamp,
            audit_id=self._id_factory("audit"),
        )
        return {
            "workerId": record["worker_id"],
            "workerPoolId": record["worker_pool_id"],
            "createdAt": record["created_at"],
            "updatedAt": record["updated_at"],
        }

    def worker_profile_assignment(self, worker_id: str) -> dict[str, object]:
        safe_worker_id = _required_text(worker_id, "worker_id", max_length=128)
        record = self._pool_store.worker_profile_assignment(safe_worker_id)
        if record is None:
            raise ValueError("worker has no active model profile assignment")
        return {
            "workerId": record["worker_id"],
            "workerPoolId": record["worker_pool_id"],
            "profileSetId": record["profile_set_id"],
            "profileRevision": record["desired_revision"],
            "gatewayTokenGeneration": record["gateway_token_generation"],
            "manifest": record["manifest"],
            "manifestDigest": record["manifest_digest"],
            "routeIds": record["route_ids"],
        }
