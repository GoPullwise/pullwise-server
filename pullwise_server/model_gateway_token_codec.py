from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


AUDIENCE = "pullwise-model-gateway"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
CLAIM_KEYS = frozenset(
    {
        "iss", "sub", "aud", "profile_set", "profile_revision",
        "manifest_digest", "routes", "generation", "iat", "nbf", "exp", "jti",
    }
)


class GatewayTokenError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GatewayGrantStatus:
    active: bool
    worker_enabled: bool
    generation: int
    desired_profile_revision: int


def safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise GatewayTokenError(f"{label.upper()}_INVALID")
    return value


def positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GatewayTokenError(f"{label.upper()}_INVALID")
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or not SHA256_DIGEST.fullmatch(value):
        raise GatewayTokenError("GATEWAY_MANIFEST_DIGEST_INVALID")
    return value


def route_ids(values: object) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > 32:
        raise GatewayTokenError("GATEWAY_TOKEN_ROUTES_INVALID")
    routes = [safe_id(value, "gateway_token_route") for value in values]
    if routes != sorted(set(routes)):
        raise GatewayTokenError("GATEWAY_TOKEN_ROUTES_INVALID")
    return routes


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    if not isinstance(value, str) or not BASE64URL.fullmatch(value):
        raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED") from exc
    if b64_encode(decoded) != value:
        raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED")
    return decoded


def _closed_json(value: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED")
            result[key] = item
        return result

    try:
        parsed = json.loads(value.decode("ascii"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, GatewayTokenError):
            raise
        raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED") from exc
    if not isinstance(parsed, dict):
        raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED")
    return parsed


class GatewayTokenVerifier:
    def __init__(
        self,
        *,
        public_keys: Mapping[str, Ed25519PublicKey],
        issuer: str,
        clock: Callable[[], int],
        grant_status_resolver: Callable[[str, str], GatewayGrantStatus],
    ) -> None:
        if not public_keys or any(not isinstance(key, Ed25519PublicKey) for key in public_keys.values()):
            raise GatewayTokenError("GATEWAY_TRUST_SET_INVALID")
        self._public_keys = dict(public_keys)
        self._issuer = issuer
        self._clock = clock
        self._grant_status_resolver = grant_status_resolver

    def verify(
        self,
        token: str,
        *,
        worker_id: str,
        profile_set_id: str,
        profile_revision: int,
        manifest_digest: str,
        route_id: str,
    ) -> dict[str, object]:
        if not isinstance(token, str) or token.count(".") != 2:
            raise GatewayTokenError("GATEWAY_TOKEN_MALFORMED")
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = _closed_json(_b64_decode(encoded_header))
        claims = _closed_json(_b64_decode(encoded_claims))
        if set(header) != {"alg", "kid", "typ"} or header.get("alg") != "EdDSA" or header.get("typ") != "JWT":
            raise GatewayTokenError("GATEWAY_TOKEN_HEADER_INVALID")
        key_id = safe_id(header.get("kid"), "gateway_token_key_id")
        public_key = self._public_keys.get(key_id)
        if public_key is None:
            raise GatewayTokenError("GATEWAY_TOKEN_KEY_UNKNOWN")
        try:
            public_key.verify(_b64_decode(encoded_signature), f"{encoded_header}.{encoded_claims}".encode("ascii"))
        except InvalidSignature as exc:
            raise GatewayTokenError("GATEWAY_TOKEN_SIGNATURE_INVALID") from exc
        if set(claims) != CLAIM_KEYS:
            raise GatewayTokenError("GATEWAY_TOKEN_CLAIMS_INVALID")
        if claims.get("iss") != self._issuer or claims.get("aud") != AUDIENCE:
            raise GatewayTokenError("GATEWAY_TOKEN_AUDIENCE_INVALID")
        issued_at = positive_integer(claims.get("iat"), "gateway_token_iat")
        not_before = positive_integer(claims.get("nbf"), "gateway_token_nbf")
        expires_at = positive_integer(claims.get("exp"), "gateway_token_exp")
        now = int(self._clock())
        if issued_at != not_before or expires_at <= issued_at or expires_at - issued_at > 900:
            raise GatewayTokenError("GATEWAY_TOKEN_TIME_INVALID")
        if now < not_before:
            raise GatewayTokenError("GATEWAY_TOKEN_NOT_YET_VALID")
        if now >= expires_at:
            raise GatewayTokenError("GATEWAY_TOKEN_EXPIRED")
        routes = route_ids(claims.get("routes"))
        if (
            claims.get("sub") != safe_id(worker_id, "gateway_token_worker")
            or claims.get("profile_set") != safe_id(profile_set_id, "gateway_token_profile_set")
            or claims.get("profile_revision") != positive_integer(profile_revision, "gateway_token_profile_revision")
            or claims.get("manifest_digest") != digest(manifest_digest)
            or safe_id(route_id, "gateway_token_route") not in routes
        ):
            raise GatewayTokenError("GATEWAY_TOKEN_SCOPE_DENIED")
        generation = positive_integer(claims.get("generation"), "gateway_token_generation")
        jti = safe_id(claims.get("jti"), "gateway_token_jti")
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        try:
            status = self._grant_status_resolver(jti, token_hash)
        except BaseException as exc:
            raise GatewayTokenError("GATEWAY_TOKEN_STATUS_UNAVAILABLE") from exc
        if not isinstance(status, GatewayGrantStatus):
            raise GatewayTokenError("GATEWAY_TOKEN_STATUS_INVALID")
        if not status.active:
            raise GatewayTokenError("GATEWAY_TOKEN_REVOKED")
        if not status.worker_enabled:
            raise GatewayTokenError("GATEWAY_WORKER_DISABLED")
        if status.generation != generation:
            raise GatewayTokenError("GATEWAY_TOKEN_GENERATION_STALE")
        if status.desired_profile_revision != profile_revision:
            raise GatewayTokenError("GATEWAY_PROFILE_REVISION_STALE")
        return claims

