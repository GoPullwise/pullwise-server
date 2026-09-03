from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .model_gateway_adapters import OpenAICompletionsAdapter, OpenAIConnectionValidator
from .model_gateway_audit import JsonlGatewayAuditSink
from .model_gateway_http import ModelGatewayHttpApplication, serve
from .model_gateway_introspection_client import HttpGatewayRouteResolver, HttpGrantStatusResolver
from .model_gateway_runtime import ModelGatewayRuntime
from .model_gateway_limits import GatewayLimiter, GatewayLimitPolicy
from .model_gateway_secret_broker import GatewaySecretBroker
from .model_gateway_secrets import EncryptedFileSecretStore
from .model_gateway_token_codec import GatewayTokenVerifier


CONFIG_FIELDS = frozenset(
    {
        "schema_id", "issuer", "public_keys", "introspection_url",
        "secret_root", "secret_key_path", "audit_path", "route_url",
    }
)
PUBLIC_KEY_FIELDS = frozenset({"kid", "path"})


def _closed_json(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("Gateway config must be a regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > 1024 * 1024:
        raise ValueError("Gateway config size is invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Gateway config contains a duplicate key")
            value[key] = item
        return value

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Gateway config must be valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != CONFIG_FIELDS:
        raise ValueError("Gateway config must be a closed object")
    return parsed


def _public_keys(value: object) -> dict[str, Ed25519PublicKey]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("Gateway public_keys must contain between 1 and 8 keys")
    result: dict[str, Ed25519PublicKey] = {}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != PUBLIC_KEY_FIELDS:
            raise ValueError("Gateway public key entry must be a closed object")
        key_id = raw.get("kid")
        key_path = raw.get("path")
        if not isinstance(key_id, str) or not key_id or key_id in result or not isinstance(key_path, str):
            raise ValueError("Gateway public key metadata is invalid")
        path = Path(key_path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ValueError("Gateway public key must be a regular file")
        loaded = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("Gateway public key must be Ed25519")
        result[key_id] = loaded
    return result


def build_application(config_path: Path) -> ModelGatewayHttpApplication:
    config = _closed_json(Path(config_path))
    if config.get("schema_id") != "pullwise-model-gateway-runtime/v1":
        raise ValueError("Gateway config schema is invalid")
    issuer = config.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("Gateway issuer is invalid")
    introspection_token = os.environ.get("PULLWISE_MODEL_GATEWAY_INTROSPECTION_TOKEN", "").strip()
    broker_token = os.environ.get("PULLWISE_MODEL_GATEWAY_BROKER_TOKEN", "").strip()
    if not introspection_token or not broker_token:
        raise RuntimeError("Gateway service credentials are not configured")
    store = EncryptedFileSecretStore.from_key_file(
        root=Path(str(config["secret_root"])),
        key_path=Path(str(config["secret_key_path"])),
    )
    status_resolver = HttpGrantStatusResolver(
        introspection_url=str(config["introspection_url"]),
        introspection_token=introspection_token,
    )
    verifier = GatewayTokenVerifier(
        public_keys=_public_keys(config["public_keys"]),
        issuer=issuer,
        clock=lambda: int(time.time()),
        grant_status_resolver=status_resolver,
    )
    runtime = ModelGatewayRuntime(
        token_verifier=verifier,
        route_resolver=HttpGatewayRouteResolver(
            route_url=str(config["route_url"]),
            introspection_token=introspection_token,
        ),
        secret_store=store,
        adapters={"openai-completions": OpenAICompletionsAdapter()},
        audit_sink=JsonlGatewayAuditSink(Path(str(config["audit_path"]))),
        request_id_factory=lambda: f"mgr_{secrets.token_urlsafe(18)}",
        clock=time.time,
        limiter=GatewayLimiter(
            policy=GatewayLimitPolicy(
                worker_requests_per_minute=60,
                route_requests_per_minute=600,
                worker_concurrency=1,
                route_concurrency=16,
                worker_output_tokens_per_minute=500_000,
                route_output_tokens_per_minute=10_000_000,
            ),
            clock=time.time,
        ),
    )
    return ModelGatewayHttpApplication(
        runtime=runtime,
        secret_broker=GatewaySecretBroker(
            secret_store=store,
            write_token=broker_token,
            validator=OpenAIConnectionValidator(),
        ),
    )


def main() -> None:
    config_path = os.environ.get("PULLWISE_MODEL_GATEWAY_CONFIG_PATH", "").strip()
    if not config_path:
        raise RuntimeError("PULLWISE_MODEL_GATEWAY_CONFIG_PATH is required")
    host = os.environ.get("PULLWISE_MODEL_GATEWAY_HOST", "127.0.0.1").strip()
    try:
        port = int(os.environ.get("PULLWISE_MODEL_GATEWAY_PORT", "8090"))
    except ValueError as exc:
        raise RuntimeError("PULLWISE_MODEL_GATEWAY_PORT is invalid") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("PULLWISE_MODEL_GATEWAY_PORT is invalid")
    serve(build_application(Path(config_path)), host=host, port=port)


if __name__ == "__main__":
    main()
