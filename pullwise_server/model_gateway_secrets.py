from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
AAD_PREFIX = b"pullwise-model-gateway-secret/v1\0"


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredSecretVersion:
    version: str
    fingerprint: str


def _default_version_factory() -> str:
    return f"v_{secrets.token_urlsafe(18)}"


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise SecretStoreError("secret store path must be a real directory")
    lexical = os.path.normcase(os.path.abspath(path))
    resolved = os.path.normcase(os.path.realpath(path))
    if lexical != resolved:
        raise SecretStoreError("secret store path must not traverse links")
    try:
        path.chmod(0o700)
    except OSError as exc:
        if os.name != "nt":
            raise SecretStoreError("secret store directory permissions could not be set") from exc
    return path


def _safe_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SecretStoreError(f"{label} is invalid")
    return value


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _aad(secret_ref: str, version: str) -> bytes:
    return AAD_PREFIX + secret_ref.encode("utf-8") + b"\0" + version.encode("ascii")


class EncryptedFileSecretStore:
    def __init__(
        self,
        *,
        root: Path,
        master_key: bytes,
        version_factory: Callable[[], str] = _default_version_factory,
    ) -> None:
        if not isinstance(master_key, bytes) or len(master_key) != 32:
            raise SecretStoreError("secret store master key must be exactly 32 bytes")
        self._root = _private_directory(Path(root))
        self._cipher = AESGCM(master_key)
        self._version_factory = version_factory

    @classmethod
    def from_key_file(cls, *, root: Path, key_path: Path) -> "EncryptedFileSecretStore":
        path = Path(key_path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise SecretStoreError("secret store master key must be a regular file")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecretStoreError("secret store master key permissions must be 0600 or stricter")
        key = path.read_bytes()
        return cls(root=root, master_key=key)

    def _version_path(self, secret_ref: str, version: str) -> Path:
        reference_digest = hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()
        directory = _private_directory(self._root / reference_digest)
        return directory / f"{version}.json"

    def put_candidate(self, secret_ref: str, secret: bytes) -> StoredSecretVersion:
        reference = _safe_identifier(secret_ref, SECRET_REF, "secret_ref")
        if not isinstance(secret, bytes) or not secret or len(secret) > 16_384:
            raise SecretStoreError("secret value is invalid")
        version = _safe_identifier(self._version_factory(), VERSION, "secret version")
        final_path = self._version_path(reference, version)
        if final_path.exists():
            raise SecretStoreError("secret version already exists")
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, secret, _aad(reference, version))
        envelope = {
            "schema_id": "pullwise-model-gateway-encrypted-secret/v1",
            "reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
            "version": version,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        payload = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        temporary_path = final_path.with_name(f".{final_path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = None
        try:
            descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = None
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary_path, final_path)
            _sync_directory(final_path.parent)
        except FileExistsError as exc:
            raise SecretStoreError("secret version already exists") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        return StoredSecretVersion(
            version=version,
            fingerprint=f"sha256:{hashlib.sha256(secret).hexdigest()[:12]}",
        )

    def read_version(self, secret_ref: str, version: str) -> bytes:
        reference = _safe_identifier(secret_ref, SECRET_REF, "secret_ref")
        safe_version = _safe_identifier(version, VERSION, "secret version")
        path = self._version_path(reference, safe_version)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise SecretStoreError("secret version is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise SecretStoreError("secret version must be a regular file")
        try:
            envelope = json.loads(path.read_text(encoding="ascii"))
            if set(envelope) != {
                "algorithm",
                "ciphertext",
                "nonce",
                "reference_sha256",
                "schema_id",
                "version",
            }:
                raise ValueError
            if (
                envelope["schema_id"] != "pullwise-model-gateway-encrypted-secret/v1"
                or envelope["algorithm"] != "AES-256-GCM"
                or envelope["version"] != safe_version
                or envelope["reference_sha256"] != hashlib.sha256(reference.encode("utf-8")).hexdigest()
            ):
                raise ValueError
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            if len(nonce) != 12:
                raise ValueError
            return self._cipher.decrypt(nonce, ciphertext, _aad(reference, safe_version))
        except (InvalidTag, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SecretStoreError("secret version failed integrity validation") from exc

    def retire_version(self, secret_ref: str, version: str) -> None:
        reference = _safe_identifier(secret_ref, SECRET_REF, "secret_ref")
        safe_version = _safe_identifier(version, VERSION, "secret version")
        path = self._version_path(reference, safe_version)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise SecretStoreError("secret version must be a regular file")
        path.unlink()
        _sync_directory(path.parent)
