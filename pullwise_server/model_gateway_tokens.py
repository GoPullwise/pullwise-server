from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .model_gateway_store import ConnectFactory, ModelGatewayStore
from .model_gateway_token_codec import (
    AUDIENCE,
    GatewayGrantStatus,
    GatewayTokenError,
    GatewayTokenVerifier,
    b64_encode,
    digest,
    json_bytes,
    positive_integer,
    route_ids as normalize_route_ids,
    safe_id,
)


@dataclass(frozen=True)
class GatewayAccessGrant:
    token: str
    token_hash: str
    jti: str
    expires_at: int


def _default_jti_factory() -> str:
    return f"gtj_{secrets.token_urlsafe(18)}"


class GatewayTokenAuthority:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        private_key: Ed25519PrivateKey,
        key_id: str,
        issuer: str,
        ttl_seconds: int,
        clock: Callable[[], int],
        jti_factory: Callable[[], str] = _default_jti_factory,
    ) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise GatewayTokenError("GATEWAY_SIGNING_KEY_INVALID")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 900:
            raise GatewayTokenError("GATEWAY_TOKEN_TTL_INVALID")
        self._store = ModelGatewayStore(connect_factory)
        self._private_key = private_key
        self._key_id = safe_id(key_id, "gateway_token_key_id")
        self._issuer = issuer
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._jti_factory = jti_factory

    def issue(
        self,
        *,
        worker_id: str,
        profile_set_id: str,
        profile_revision: int,
        manifest_digest: str,
        route_ids: list[str],
        generation: int,
    ) -> GatewayAccessGrant:
        subject = safe_id(worker_id, "gateway_token_worker")
        profile_set = safe_id(profile_set_id, "gateway_token_profile_set")
        revision = positive_integer(profile_revision, "gateway_token_profile_revision")
        manifest_hash = digest(manifest_digest)
        safe_generation = positive_integer(generation, "gateway_token_generation")
        routes = normalize_route_ids(route_ids)
        issued_at = int(self._clock())
        expires_at = issued_at + self._ttl_seconds
        jti = safe_id(self._jti_factory(), "gateway_token_jti")
        header = {"alg": "EdDSA", "kid": self._key_id, "typ": "JWT"}
        claims = {
            "iss": self._issuer,
            "sub": subject,
            "aud": AUDIENCE,
            "profile_set": profile_set,
            "profile_revision": revision,
            "manifest_digest": manifest_hash,
            "routes": routes,
            "generation": safe_generation,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": expires_at,
            "jti": jti,
        }
        signing_input = f"{b64_encode(json_bytes(header))}.{b64_encode(json_bytes(claims))}"
        signature = self._private_key.sign(signing_input.encode("ascii"))
        token = f"{signing_input}.{b64_encode(signature)}"
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        self._store.record_gateway_token_grant(
            {
                "jti": jti,
                "token_hash": token_hash,
                "worker_id": subject,
                "profile_set_id": profile_set,
                "profile_revision": revision,
                "manifest_digest": manifest_hash,
                "route_ids_json": json.dumps(routes, separators=(",", ":")),
                "generation": safe_generation,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }
        )
        return GatewayAccessGrant(token=token, token_hash=token_hash, jti=jti, expires_at=expires_at)
