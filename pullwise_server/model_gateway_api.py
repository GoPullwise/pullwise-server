from __future__ import annotations

import hashlib
import os
import stat
import time
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .model_gateway_control_plane import ModelGatewayControlPlane, SecretVersion, SecretWriter
from .model_gateway_store import ConnectFactory
from .model_gateway_secret_broker import HttpSecretWriter
from .model_gateway_worker_profiles import WorkerProfileIssuer
from .model_gateway_snapshot import admin_model_gateway_snapshot
from .model_gateway_tokens import GatewayTokenAuthority
from .model_gateway_pool_store import ModelGatewayPoolStore
from .model_gateway_provider_lifecycle import emergency_revoke_provider_connection as revoke_provider
from .model_gateway_provider_rotation import ProviderRotationService
from .model_gateway_provider_removal import ProviderRemovalService
from .model_gateway_secret_cleanup import SecretCleanupCoordinator
from .model_gateway_wave_rollout import WorkerPoolWaveRollout


class SecretBrokerUnavailable(RuntimeError):
    pass


class _UnavailableSecretWriter:
    def put_candidate(self, secret_ref: str, secret: bytes) -> SecretVersion:
        del secret_ref, secret
        raise SecretBrokerUnavailable("provider secret broker is not configured")

    def retire_version(self, secret_ref: str, version: str) -> None:
        del secret_ref, version
        raise SecretBrokerUnavailable("provider secret broker is not configured")


def provider_secret_writer() -> SecretWriter:
    broker_url = os.environ.get("PULLWISE_MODEL_GATEWAY_BROKER_URL", "").strip()
    broker_token = os.environ.get("PULLWISE_MODEL_GATEWAY_BROKER_TOKEN", "").strip()
    if broker_url and broker_token:
        return HttpSecretWriter(broker_url=broker_url, write_token=broker_token)
    return _UnavailableSecretWriter()


def _control_plane(connect_factory: ConnectFactory) -> ModelGatewayControlPlane:
    return ModelGatewayControlPlane(
        connect_factory=connect_factory,
        secret_writer=provider_secret_writer(),
        clock=lambda: int(time.time()),
    )


def create_provider_connection(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    payload: object,
) -> dict[str, object]:
    return _control_plane(connect_factory).create_provider_connection(
        actor_user_id=actor_user_id,
        request_id=request_id,
        payload=payload,
    )


def list_provider_connections(*, connect_factory: ConnectFactory) -> list[dict[str, object]]:
    return _control_plane(connect_factory).list_provider_connections()


def publish_profile_set(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    payload: object,
) -> dict[str, object]:
    return _control_plane(connect_factory).publish_profile_set(
        actor_user_id=actor_user_id,
        request_id=request_id,
        payload=payload,
    )


def worker_profile_issuer(*, connect_factory: ConnectFactory) -> WorkerProfileIssuer:
    loaded, key_id, issuer, gateway_url, ttl_seconds = _signing_key_config()
    authority = GatewayTokenAuthority(
        connect_factory=connect_factory,
        private_key=loaded,
        key_id=key_id,
        issuer=issuer,
        ttl_seconds=ttl_seconds,
        clock=lambda: int(time.time()),
    )
    return WorkerProfileIssuer(
        connect_factory=connect_factory,
        token_authority=authority,
        manifest_private_key=loaded,
        key_id=key_id,
        gateway_base_url=gateway_url,
    )


def _signing_key_config() -> tuple[Ed25519PrivateKey, str, str, str, int]:
    key_path_value = os.environ.get("PULLWISE_MODEL_GATEWAY_SIGNING_KEY_PATH", "").strip()
    key_id = os.environ.get("PULLWISE_MODEL_GATEWAY_SIGNING_KEY_ID", "").strip()
    issuer = os.environ.get("PULLWISE_MODEL_GATEWAY_TOKEN_ISSUER", "").strip()
    gateway_url = os.environ.get("PULLWISE_MODEL_GATEWAY_URL", "").strip()
    ttl_value = os.environ.get("PULLWISE_MODEL_GATEWAY_TOKEN_TTL_SECONDS", "300").strip()
    if not key_path_value or not key_id or not issuer or not gateway_url:
        raise SecretBrokerUnavailable("gateway signing authority is not configured")
    key_path = Path(key_path_value)
    try:
        metadata = key_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or key_path.is_symlink():
            raise ValueError
        loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError
        ttl_seconds = int(ttl_value)
    except (OSError, TypeError, ValueError) as exc:
        raise SecretBrokerUnavailable("gateway signing authority is not configured") from exc
    return loaded, key_id, issuer, gateway_url, ttl_seconds


def gateway_manifest_trust() -> dict[str, object]:
    private_key, key_id, _issuer, _gateway_url, _ttl_seconds = _signing_key_config()
    public_key = private_key.public_key()
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {
        "schema_id": "pullwise-model-gateway-manifest-trust/v1",
        "alg": "Ed25519",
        "kid": key_id,
        "public_key_pem": pem.decode("ascii"),
        "fingerprint": f"sha256:{hashlib.sha256(der).hexdigest()}",
    }


def issue_worker_profile(
    *,
    connect_factory: ConnectFactory,
    worker_id: str,
) -> dict[str, object]:
    return worker_profile_issuer(connect_factory=connect_factory).issue(worker_id)


def create_worker_pool(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    payload: object,
) -> dict[str, object]:
    return _control_plane(connect_factory).create_worker_pool(
        actor_user_id=actor_user_id,
        request_id=request_id,
        payload=payload,
    )


def bind_worker_to_pool(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    worker_id: str,
    worker_pool_id: str,
) -> dict[str, object]:
    return _control_plane(connect_factory).bind_worker_to_pool(
        actor_user_id=actor_user_id,
        request_id=request_id,
        worker_id=worker_id,
        worker_pool_id=worker_pool_id,
    )


def admin_snapshot(*, connect_factory: ConnectFactory) -> dict[str, object]:
    return admin_model_gateway_snapshot(connect_factory, timestamp=int(time.time()))


def retry_pending_secret_cleanup(*, connect_factory: ConnectFactory) -> dict[str, int]:
    return SecretCleanupCoordinator(
        connect_factory=connect_factory,
        secret_writer=provider_secret_writer(),
        clock=lambda: int(time.time()),
    ).retry_pending()


def rotate_worker_pool_gateway_tokens(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    worker_pool_id: str,
) -> dict[str, object]:
    record = ModelGatewayPoolStore(connect_factory).rotate_gateway_token_generation(
        worker_pool_id=worker_pool_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        timestamp=int(time.time()),
    )
    return {
        "workerPoolId": record["worker_pool_id"],
        "gatewayTokenGeneration": record["gateway_token_generation"],
        "updatedAt": record["updated_at"],
    }


def set_worker_pool_desired_revision(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    worker_pool_id: str,
    profile_revision: object,
) -> dict[str, object]:
    if isinstance(profile_revision, bool) or not isinstance(profile_revision, int):
        raise ValueError("profile_revision must be an integer")
    record = ModelGatewayPoolStore(connect_factory).set_desired_revision(
        worker_pool_id=worker_pool_id,
        profile_revision=profile_revision,
        actor_user_id=actor_user_id,
        request_id=request_id,
        timestamp=int(time.time()),
    )
    return {
        "workerPoolId": record["worker_pool_id"],
        "profileSetId": record["profile_set_id"],
        "desiredRevision": record["desired_revision"],
        "gatewayTokenGeneration": record["gateway_token_generation"],
        "status": record["status"],
        "updatedAt": record["updated_at"],
    }


def rollout_worker_pool_wave(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    worker_pool_id: str,
    profile_revision: object,
    worker_ids: object,
) -> dict[str, object]:
    record = WorkerPoolWaveRollout(connect_factory).apply(
        worker_pool_id=worker_pool_id,
        profile_revision=profile_revision,
        worker_ids=worker_ids,
        actor_user_id=actor_user_id,
        request_id=request_id,
        timestamp=int(time.time()),
    )
    return {
        "workerPoolId": record["worker_pool_id"],
        "desiredRevision": record["desired_revision"],
        "gatewayTokenGeneration": record["gateway_token_generation"],
        "updatedWorkerIds": record["updated_worker_ids"],
        "updatedAt": record["updated_at"],
    }


def emergency_revoke_provider_connection(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    provider_connection_id: str,
) -> dict[str, object]:
    return revoke_provider(
        connect_factory,
        provider_connection_id=provider_connection_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        timestamp=int(time.time()),
    )


def _provider_rotation_service(connect_factory: ConnectFactory) -> ProviderRotationService:
    return ProviderRotationService(
        connect_factory=connect_factory,
        secret_writer=provider_secret_writer(),
        clock=lambda: int(time.time()),
        audit_id_factory=lambda: f"audit_{secrets.token_urlsafe(18)}",
    )


def stage_provider_rotation(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    provider_connection_id: str,
    secret: object,
) -> dict[str, object]:
    if not isinstance(secret, str):
        raise ValueError("provider rotation secret is required")
    return _provider_rotation_service(connect_factory).stage(
        provider_connection_id=provider_connection_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        secret=secret,
    )


def advance_provider_rotation(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    provider_connection_id: str,
    action: str,
    upstream_revoked: object = None,
) -> dict[str, object]:
    service = _provider_rotation_service(connect_factory)
    if action == "promote":
        return service.promote(
            provider_connection_id=provider_connection_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
    if action != "retire":
        raise ValueError("provider rotation action is invalid")
    return service.retire_previous(
        provider_connection_id=provider_connection_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        upstream_revoked=upstream_revoked is True,
    )


def _provider_removal_service(connect_factory: ConnectFactory) -> ProviderRemovalService:
    return ProviderRemovalService(
        connect_factory=connect_factory,
        secret_writer=provider_secret_writer(),
        clock=lambda: int(time.time()),
        audit_id_factory=lambda: f"audit_{secrets.token_urlsafe(18)}",
    )


def prepare_provider_removal(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    provider_connection_id: str,
) -> dict[str, object]:
    return _provider_removal_service(connect_factory).prepare(
        provider_connection_id=provider_connection_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
    )


def finalize_provider_removal(
    *,
    connect_factory: ConnectFactory,
    actor_user_id: str,
    request_id: str | None,
    provider_connection_id: str,
    upstream_revoked: object,
) -> dict[str, object]:
    return _provider_removal_service(connect_factory).finalize(
        provider_connection_id=provider_connection_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        upstream_revoked=upstream_revoked is True,
    )
