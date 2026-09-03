from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .model_gateway_pool_store import ModelGatewayPoolStore
from .model_gateway_route_availability import worker_assignment_routes_available
from .model_gateway_store import ConnectFactory
from .model_gateway_tokens import GatewayTokenAuthority


MANIFEST_SIGNATURE_PREFIX = b"pullwise-model-profile-manifest/v1\0"


def _gateway_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError("gateway_base_url must be an HTTPS /v1 URL without credentials, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("gateway_base_url port is invalid") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit(("https", authority, "/v1", "", ""))


def _canonical_manifest(manifest: object) -> bytes:
    if not isinstance(manifest, dict):
        raise RuntimeError("stored profile manifest is invalid")
    try:
        return json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise RuntimeError("stored profile manifest is invalid") from exc


class WorkerProfileIssuer:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        token_authority: GatewayTokenAuthority,
        manifest_private_key: Ed25519PrivateKey,
        key_id: str,
        gateway_base_url: str,
    ) -> None:
        if not isinstance(manifest_private_key, Ed25519PrivateKey):
            raise ValueError("manifest signing key is invalid")
        if not key_id or len(key_id) > 128:
            raise ValueError("manifest signing key id is invalid")
        self._connect_factory = connect_factory
        self._pool_store = ModelGatewayPoolStore(connect_factory)
        self._token_authority = token_authority
        self._manifest_private_key = manifest_private_key
        self._key_id = key_id
        self._gateway_base_url = _gateway_base_url(gateway_base_url)

    def issue(self, worker_id: str) -> dict[str, object]:
        assignment = self._pool_store.worker_profile_assignment(worker_id)
        if assignment is None:
            raise ValueError("worker has no active model profile assignment")
        if not worker_assignment_routes_available(self._connect_factory, worker_id):
            raise ValueError("worker model profile routes are unavailable")
        manifest = assignment["manifest"]
        manifest_bytes = _canonical_manifest(manifest)
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_digest != assignment["manifest_digest"]:
            raise RuntimeError("stored profile manifest digest mismatch")
        route_ids = assignment["route_ids"]
        if not isinstance(route_ids, list) or not route_ids:
            raise ValueError("worker model profile has no enabled routes")
        signature = self._manifest_private_key.sign(MANIFEST_SIGNATURE_PREFIX + manifest_bytes)
        grant = self._token_authority.issue(
            worker_id=str(assignment["worker_id"]),
            profile_set_id=str(assignment["profile_set_id"]),
            profile_revision=int(assignment["desired_revision"]),
            manifest_digest=manifest_digest,
            route_ids=[str(route_id) for route_id in route_ids],
            generation=int(assignment["gateway_token_generation"]),
        )
        worker_path = quote(str(assignment["worker_id"]), safe="")
        profile_path = quote(str(assignment["profile_set_id"]), safe="")
        revision = int(assignment["desired_revision"])
        scoped_gateway_url = (
            f"{self._gateway_base_url}/workers/{worker_path}"
            f"/profiles/{profile_path}/revisions/{revision}"
        )
        return {
            "schema_id": "pullwise-worker-model-profile/v1",
            "worker_id": assignment["worker_id"],
            "worker_pool_id": assignment["worker_pool_id"],
            "profile_set_id": assignment["profile_set_id"],
            "profile_revision": assignment["desired_revision"],
            "manifest": manifest,
            "manifest_digest": manifest_digest,
            "manifest_signature": {
                "alg": "Ed25519",
                "kid": self._key_id,
                "value": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
            },
            "gateway": {
                "provider": "pullwise-gateway",
                "base_url": scoped_gateway_url,
            },
            "authorization": {
                "scheme": "Bearer",
                "access_token": grant.token,
                "expires_at": grant.expires_at,
                "jti": grant.jti,
            },
        }
