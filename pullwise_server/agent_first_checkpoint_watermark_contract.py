"""Strict internal contract for Server checkpoint watermark acknowledgements."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from typing import Mapping

from ._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    canonical_document_bytes,
    package_tuple,
)
from .agent_first_authority_store import AuthorityStoreError


REQUEST_SCHEMA_ID = "checkpoint-watermark-request/internal-v1"
ACK_SCHEMA_ID = "checkpoint-watermark-ack/internal-v1"
_REQUEST_DOMAIN = b"pullwise:checkpoint-watermark-request:internal-v1\0"
_ACK_DOMAIN = b"pullwise:checkpoint-watermark-ack:internal-v1\0"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_REQUEST_KEYS = frozenset(
    {
        "schema_id",
        "package",
        "task_id",
        "generation",
        "previous_manifest_hash",
        "manifest_hash",
        "committed_from_task_version",
        "committed_task_version",
        "attempt_id",
        "owner_id",
        "owner_epoch",
        "lease_id",
        "authority_digest",
        "grant_id",
        "grant_digest",
        "deletion_version",
        "native_epoch",
        "transport_epoch",
        "same_run_resume_selected",
        "request_digest",
    }
)
_ACK_KEYS = _REQUEST_KEYS | {"accepted_at", "ack_digest"}
_ID_PREFIXES = {
    "task_id": "task_",
    "attempt_id": "attempt_",
    "owner_id": "owner_",
    "lease_id": "lease_",
    "grant_id": "grant_",
}


def _now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _digest(domain: bytes, document: Mapping[str, object]) -> str:
    return hashlib.sha256(domain + canonical_document_bytes(document)).hexdigest()


def _is_int(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _is_prefixed_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == len(prefix) + 32
        and all(character in "0123456789abcdef" for character in value[len(prefix):])
    )


def _validate_shared(document: Mapping[str, object]) -> None:
    if document.get("package") != package_tuple():
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    for field, prefix in _ID_PREFIXES.items():
        if not _is_prefixed_id(document.get(field), prefix):
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    for field in ("manifest_hash", "authority_digest", "grant_digest"):
        value = document.get(field)
        if not isinstance(value, str) or _HASH.fullmatch(value) is None:
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    previous = document.get("previous_manifest_hash")
    if previous is not None and (
        not isinstance(previous, str) or _HASH.fullmatch(previous) is None
    ):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    for field, minimum in (
        ("generation", 1),
        ("committed_from_task_version", 1),
        ("committed_task_version", 2),
        ("owner_epoch", 1),
        ("deletion_version", 0),
        ("native_epoch", 1),
        ("transport_epoch", 1),
    ):
        if not _is_int(document.get(field), minimum):
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    if (
        document["committed_task_version"]
        != document["committed_from_task_version"] + 1
    ):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    if document.get("same_run_resume_selected") is not True:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_CAPABILITY_FENCED")


def prepare_checkpoint_watermark_request(
    request: object,
    *,
    accepted_at: str | None = None,
) -> dict[str, object]:
    if type(request) is not dict or set(request) != _REQUEST_KEYS:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    document = dict(request)
    if document.get("schema_id") != REQUEST_SCHEMA_ID:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    _validate_shared(document)
    request_digest = document.get("request_digest")
    if not isinstance(request_digest, str) or _HASH.fullmatch(request_digest) is None:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    unsigned = {key: value for key, value in document.items() if key != "request_digest"}
    if not hmac.compare_digest(request_digest, _digest(_REQUEST_DOMAIN, unsigned)):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    request_bytes = canonical_document_bytes(document)
    accepted = _now() if accepted_at is None else accepted_at
    if not isinstance(accepted, str) or _TIMESTAMP.fullmatch(accepted) is None:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    ack_unsigned = {
        "schema_id": ACK_SCHEMA_ID,
        **{key: document[key] for key in _REQUEST_KEYS if key != "schema_id"},
        "accepted_at": accepted,
    }
    ack = {**ack_unsigned, "ack_digest": _digest(_ACK_DOMAIN, ack_unsigned)}
    return {
        **document,
        "package_tuple": PACKAGE_TUPLE,
        "request_bytes": request_bytes,
        "ack_digest": ack["ack_digest"],
        "ack_bytes": canonical_document_bytes(ack),
    }


def verify_checkpoint_watermark_ack(value: object) -> dict[str, object]:
    try:
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            document = json.loads(raw)
            if canonical_document_bytes(document) != raw:
                raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
        elif type(value) is dict:
            document = dict(value)
        else:
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    except (UnicodeError, ValueError, TypeError):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID") from None
    if set(document) != _ACK_KEYS or document.get("schema_id") != ACK_SCHEMA_ID:
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    _validate_shared(document)
    accepted_at = document.get("accepted_at")
    ack_digest = document.get("ack_digest")
    request_digest = document.get("request_digest")
    if (
        not isinstance(accepted_at, str)
        or _TIMESTAMP.fullmatch(accepted_at) is None
        or not isinstance(ack_digest, str)
        or _HASH.fullmatch(ack_digest) is None
        or not isinstance(request_digest, str)
        or _HASH.fullmatch(request_digest) is None
    ):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    ack_unsigned = {key: item for key, item in document.items() if key != "ack_digest"}
    if not hmac.compare_digest(ack_digest, _digest(_ACK_DOMAIN, ack_unsigned)):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    request_unsigned = {
        **{
            key: document[key]
            for key in _REQUEST_KEYS
            if key not in {"schema_id", "request_digest"}
        },
        "schema_id": REQUEST_SCHEMA_ID,
    }
    if not hmac.compare_digest(
        request_digest,
        _digest(_REQUEST_DOMAIN, request_unsigned),
    ):
        raise AuthorityStoreError("CHECKPOINT_WATERMARK_INVALID")
    return document


__all__ = [
    "ACK_SCHEMA_ID",
    "REQUEST_SCHEMA_ID",
    "prepare_checkpoint_watermark_request",
    "verify_checkpoint_watermark_ack",
]
