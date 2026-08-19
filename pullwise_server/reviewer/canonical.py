"""Strict JSON decoding and canonicalization for the Pullwise v1 contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import unicodedata
from typing import Any


_MAX_SAFE_INTEGER = 2**53 - 1


def _validate_text(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("strings and object keys must be NFC")
    if any(0xD800 <= codepoint <= 0xDFFF for codepoint in map(ord, value)):
        raise ValueError("strings must contain Unicode scalar values")
    return value


def _parse_int(raw: str) -> int:
    if raw == "-0":
        raise ValueError("negative zero is not permitted")
    value = int(raw)
    if abs(value) > _MAX_SAFE_INTEGER:
        raise ValueError("integer is outside the JavaScript safe range")
    return value


def _parse_float(raw: str) -> float:
    raise ValueError(f"floating-point number is not permitted: {raw}")


def _parse_constant(raw: str) -> Any:
    raise ValueError(f"non-standard JSON constant is not permitted: {raw}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _validate_text(key)
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _validate_value(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("integer is outside the JavaScript safe range")
        return
    if isinstance(value, float):
        raise ValueError("floating-point numbers are not permitted")
    if isinstance(value, str):
        _validate_text(value)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
            _validate_text(key)
            _validate_value(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _validate_value(nested)
        return
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def decode_strict_json(raw: bytes | bytearray | memoryview | str) -> Any:
    """Decode contract JSON while enforcing the v1 lexical restrictions."""

    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            text = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("input is not valid UTF-8") from error
    else:
        raise ValueError("JSON input must be UTF-8 bytes or text")

    if text.startswith("\ufeff"):
        raise ValueError("UTF-8 BOM is not permitted")

    try:
        value = json.loads(
            text,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
            object_pairs_hook=_object_pairs,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError("invalid JSON") from error
    _validate_value(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """Return restricted RFC 8785-style canonical JSON bytes."""

    _validate_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value cannot be canonicalized as JSON") from error
    try:
        return encoded.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("canonical JSON is not valid UTF-8") from error


def canonical_sha256(value: Any) -> str:
    """Return the contract-form SHA-256 digest of canonical JSON bytes."""

    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"


def require_registered_value(
    registries: Mapping[str, Sequence[str]], registry_name: str, value: str
) -> str:
    """Return a value only when it belongs to the named closed registry."""

    if not isinstance(registry_name, str) or not isinstance(value, str):
        raise ValueError("registry names and values must be strings")
    try:
        allowed_values = registries[registry_name]
    except (KeyError, TypeError) as error:
        raise ValueError(f"unknown registry: {registry_name}") from error
    if not isinstance(allowed_values, Sequence) or isinstance(
        allowed_values, (str, bytes, bytearray)
    ):
        raise ValueError(f"invalid registry: {registry_name}")
    if any(not isinstance(allowed, str) for allowed in allowed_values):
        raise ValueError(f"invalid registry values: {registry_name}")
    if value not in allowed_values:
        raise ValueError(f"unregistered value for {registry_name}: {value}")
    return value
