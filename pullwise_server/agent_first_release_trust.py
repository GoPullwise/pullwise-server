"""Server-owned verification facade for release trust authorities."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import ModuleType
from typing import Callable, Collection, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import _generated_agent_task_contract as _default_contract
from . import db
from .agent_first_authority import AuthorityError
from .agent_first_release_trust_store import (
    FaultInjector,
    ReleaseTrustStore,
    ReleaseTrustStoreError,
    StoredReleaseAuthorityRows,
)


_PURPOSE_BY_SCHEMA = {
    "benchmark-bundle/v1": ("benchmark_signing", "benchmark_owner"),
    "release-gate-policy/v1": ("release_signing", "release_operator"),
    "release-gate-attestation/v1": ("release_signing", "release_operator"),
}
_PURPOSE_BY_ROLE = {
    "benchmark_owner": "benchmark_signing",
    "release_operator": "release_signing",
}


@dataclass(frozen=True)
class StoredReleaseAuthority:
    trust_root_id: str
    principal_id: str
    key_id: str


@dataclass(frozen=True)
class VerifiedReleaseSignature:
    schema_id: str
    organization_id: str
    principal_id: str
    key_id: str
    key_purpose: str
    verified_at: str


@dataclass(frozen=True)
class StoredReleaseRevocation:
    revocation_id: str
    key_id: str
    effective_at: str


class AgentFirstReleaseTrust:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection] = db.connect,
        *,
        trusted_root_digests: Mapping[str, Collection[str]],
        contract: ModuleType = _default_contract,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        fault_injector: FaultInjector | None = None,
    ) -> None:
        contract.verify_bundle()
        self._contract = contract
        self._clock = clock
        self._store = ReleaseTrustStore(connect_factory, fault_injector)
        self._trusted_roots = {
            organization_id: frozenset(digests)
            for organization_id, digests in trusted_root_digests.items()
        }

    @staticmethod
    def _raise_untrusted() -> None:
        raise AuthorityError("AUTHORITY_INPUT_UNTRUSTED")

    @staticmethod
    def _store_error(error: ReleaseTrustStoreError) -> AuthorityError:
        return AuthorityError(
            {
                "AUTHORITY_STORAGE_CORRUPT": "AUTHORITY_RELOAD_REQUIRED",
                "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
                "RELEASE_TRUST_NOT_FOUND": "AUTHORITY_INPUT_UNTRUSTED",
            }.get(error.code, "AUTHORITY_INPUT_UNTRUSTED")
        )

    def _now(self) -> datetime:
        return self._utc(self._clock())

    @staticmethod
    def _utc(value: object) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("release trust clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            AgentFirstReleaseTrust._raise_untrusted()
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            AgentFirstReleaseTrust._raise_untrusted()

    @staticmethod
    def _time_text(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _require_active(self, document: Mapping[str, object], now: datetime) -> None:
        issued_at = self._timestamp(document["issued_at"])
        expires_at = self._timestamp(document["expires_at"])
        if not issued_at <= now < expires_at:
            self._raise_untrusted()

    def _validated(
        self, schema_id: str, document: object
    ) -> tuple[dict[str, object], bytes]:
        try:
            checked = self._contract.verify_document_digest(schema_id, document)
            encoded = self._contract.canonical_validated_bytes(schema_id, checked)
        except (
            self._contract.ContractValidationError,
            KeyError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            self._raise_untrusted()
        return checked, encoded

    def _require_ref(
        self,
        ref: object,
        schema_id: str,
        document_bytes: bytes,
    ) -> None:
        if not isinstance(ref, dict):
            self._raise_untrusted()
        if (
            ref.get("schema_id") != "content-ref/v1"
            or ref.get("content_schema_id") != schema_id
            or ref.get("sha256") != hashlib.sha256(document_bytes).hexdigest()
            or ref.get("size_bytes") != len(document_bytes)
        ):
            self._raise_untrusted()

    @staticmethod
    def _decode(value: object, padding: str) -> bytes:
        if not isinstance(value, str) or "=" in value:
            AgentFirstReleaseTrust._raise_untrusted()
        try:
            return base64.urlsafe_b64decode(value + padding)
        except (ValueError, TypeError):
            AgentFirstReleaseTrust._raise_untrusted()

    def _verify_signature(
        self,
        public_key: object,
        schema_id: str,
        document: Mapping[str, object],
    ) -> None:
        try:
            verifier = Ed25519PublicKey.from_public_bytes(
                self._decode(public_key, "=")
            )
            verifier.verify(
                self._decode(document["signature"], "=="),
                self._contract.signature_message(schema_id, document),
            )
        except (InvalidSignature, ValueError, TypeError, KeyError):
            self._raise_untrusted()

    def _validated_chain(
        self,
        trust_root: object,
        principal: object,
        signing_key: object,
        *,
        check_time: bool,
    ) -> tuple[
        dict[str, object], bytes,
        dict[str, object], bytes,
        dict[str, object], bytes,
    ]:
        root, root_bytes = self._validated("release-trust-root/v1", trust_root)
        principal_value, principal_bytes = self._validated(
            "release-principal/v1", principal
        )
        key, key_bytes = self._validated("release-signing-key/v1", signing_key)
        trusted = self._trusted_roots.get(str(root["organization_id"]), frozenset())
        if root["root_digest"] not in trusted:
            self._raise_untrusted()
        self._require_ref(principal_value["trust_root_ref"], "release-trust-root/v1", root_bytes)
        principal_binding = (
            principal_value["organization_id"] == root["organization_id"]
            and principal_value["trust_root_id"] == root["trust_root_id"]
            and principal_value["trust_root_digest"] == root["root_digest"]
            and principal_value["signer_id"] == root["root_principal_id"]
            and principal_value["key_id"] == root["root_key_id"]
        )
        self._require_ref(key["principal_ref"], "release-principal/v1", principal_bytes)
        key_binding = (
            key["organization_id"] == root["organization_id"]
            and key["principal_id"] == principal_value["principal_id"]
            and key["principal_digest"] == principal_value["principal_digest"]
            and key["trust_root_id"] == root["trust_root_id"]
            and key["trust_root_digest"] == root["root_digest"]
            and key["signer_id"] == root["root_principal_id"]
            and key["signer_key_id"] == root["root_key_id"]
            and key["key_purpose"] == _PURPOSE_BY_ROLE[principal_value["role"]]
        )
        if not principal_binding or not key_binding:
            self._raise_untrusted()
        self._verify_signature(root["public_key"], "release-principal/v1", principal_value)
        self._verify_signature(root["public_key"], "release-signing-key/v1", key)
        if check_time:
            now = self._now()
            for item in (root, principal_value, key):
                self._require_active(item, now)
        return root, root_bytes, principal_value, principal_bytes, key, key_bytes

    def register_authority(
        self,
        trust_root: object,
        principal: object,
        signing_key: object,
    ) -> StoredReleaseAuthority:
        root, root_bytes, principal_value, principal_bytes, key, key_bytes = (
            self._validated_chain(
                trust_root, principal, signing_key, check_time=True
            )
        )
        try:
            self._store.store_authority(
                root=root,
                root_bytes=root_bytes,
                principal=principal_value,
                principal_bytes=principal_bytes,
                signing_key=key,
                key_bytes=key_bytes,
            )
        except ReleaseTrustStoreError as error:
            raise self._store_error(error) from None
        return StoredReleaseAuthority(
            str(root["trust_root_id"]),
            str(principal_value["principal_id"]),
            str(key["key_id"]),
        )

    def _require_revocation_binding(
        self,
        revocation: Mapping[str, object],
        root: Mapping[str, object],
        root_bytes: bytes,
        principal: Mapping[str, object],
        signing_key: Mapping[str, object],
        key_bytes: bytes,
    ) -> None:
        self._require_ref(
            revocation["trust_root_ref"], "release-trust-root/v1", root_bytes
        )
        self._require_ref(
            revocation["revoked_key_ref"], "release-signing-key/v1", key_bytes
        )
        binding = (
            revocation["organization_id"] == root["organization_id"]
            and revocation["trust_root_id"] == root["trust_root_id"]
            and revocation["trust_root_digest"] == root["root_digest"]
            and revocation["revoked_key_id"] == signing_key["key_id"]
            and revocation["revoked_key_digest"]
            == signing_key["signing_key_digest"]
            and revocation["revoked_principal_id"] == principal["principal_id"]
            and revocation["signer_id"] == root["root_principal_id"]
            and revocation["signer_key_id"] == root["root_key_id"]
        )
        if not binding:
            self._raise_untrusted()
        self._verify_signature(
            root["public_key"], "release-key-revocation/v1", revocation
        )

    def revoke_key(self, revocation: object) -> StoredReleaseRevocation:
        checked, revocation_bytes = self._validated(
            "release-key-revocation/v1", revocation
        )
        organization_id = checked["organization_id"]
        key_id = checked["revoked_key_id"]
        try:
            stored = self._store.load_authority(
                str(organization_id), str(key_id)
            )
        except ReleaseTrustStoreError as error:
            raise self._store_error(error) from None
        try:
            root = json.loads(stored.root_bytes)
            principal = json.loads(stored.principal_bytes)
            signing_key = json.loads(stored.key_bytes)
            chain = self._validated_chain(root, principal, signing_key, check_time=False)
            if (chain[1], chain[3], chain[5]) != (
                stored.root_bytes, stored.principal_bytes, stored.key_bytes
            ):
                raise ValueError("stored authority is not canonical")
        except AuthorityError:
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        root, root_bytes, principal, _, signing_key, key_bytes = chain
        self._require_revocation_binding(
            checked, root, root_bytes, principal, signing_key, key_bytes
        )
        now = self._now()
        self._require_active(root, now)
        if self._timestamp(checked["issued_at"]) > now:
            self._raise_untrusted()
        try:
            self._store.store_revocation(
                revocation=checked,
                revocation_bytes=revocation_bytes,
            )
        except ReleaseTrustStoreError as error:
            raise self._store_error(error) from None
        return StoredReleaseRevocation(
            str(checked["revocation_id"]),
            str(key_id),
            str(checked["effective_at"]),
        )

    def _verify_document_at(
        self,
        document: object,
        now: datetime,
    ) -> VerifiedReleaseSignature:
        if not isinstance(document, dict):
            self._raise_untrusted()
        schema_id = document.get("schema_id")
        expected = _PURPOSE_BY_SCHEMA.get(schema_id)
        if expected is None:
            self._raise_untrusted()
        checked, _ = self._validated(str(schema_id), document)
        if checked.get("package") != self._contract.package_tuple():
            self._raise_untrusted()
        organization_id = checked.get("organization_id")
        key_id = checked.get("key_id")
        if not isinstance(organization_id, str) or not isinstance(key_id, str):
            self._raise_untrusted()
        try:
            stored = self._store.load_authority(organization_id, key_id)
        except ReleaseTrustStoreError as error:
            raise self._store_error(error) from None
        try:
            root = json.loads(stored.root_bytes)
            principal = json.loads(stored.principal_bytes)
            signing_key = json.loads(stored.key_bytes)
            chain = self._validated_chain(root, principal, signing_key, check_time=False)
            if (chain[1], chain[3], chain[5]) != (
                stored.root_bytes,
                stored.principal_bytes,
                stored.key_bytes,
            ):
                raise ValueError("stored authority is not canonical")
            revocations = []
            for encoded in stored.revocation_bytes:
                revocation = json.loads(encoded)
                checked_revocation, canonical = self._validated(
                    "release-key-revocation/v1", revocation
                )
                if canonical != encoded:
                    raise ValueError("stored revocation is not canonical")
                self._require_revocation_binding(
                    checked_revocation,
                    chain[0],
                    chain[1],
                    chain[2],
                    chain[4],
                    chain[5],
                )
                revocations.append(checked_revocation)
        except AuthorityError:
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        root, _, principal_value, _, key, _ = chain
        for item in (root, principal_value, key, checked):
            self._require_active(item, now)
        if any(
            self._timestamp(item["effective_at"]) <= now
            for item in revocations
        ):
            self._raise_untrusted()
        purpose, role = expected
        if (
            key["key_purpose"] != purpose
            or principal_value["role"] != role
            or checked.get("signer_role") != role
            or checked.get("signer_id") != principal_value["principal_id"]
        ):
            self._raise_untrusted()
        self._verify_signature(key["public_key"], str(schema_id), checked)
        return VerifiedReleaseSignature(
            str(schema_id), organization_id, str(principal_value["principal_id"]),
            key_id, purpose, self._time_text(now)
        )

    def verify_document(self, document: object) -> VerifiedReleaseSignature:
        return self._verify_document_at(document, self._now())

    def verify_document_at(
        self,
        document: object,
        verified_at: datetime,
    ) -> VerifiedReleaseSignature:
        return self._verify_document_at(document, self._utc(verified_at))


__all__ = [
    "AgentFirstReleaseTrust",
    "StoredReleaseAuthority",
    "StoredReleaseRevocation",
    "VerifiedReleaseSignature",
]
