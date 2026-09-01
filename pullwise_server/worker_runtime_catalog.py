from __future__ import annotations

import json
import re
from typing import Any


SCHEMA_ID = "pullwise-pi-runtime-catalog/v1"
AUTH_TYPES = {"api_key", "oauth", "subscription", "custom"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _decode(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _text(value: object, *, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or any(ord(character) < 32 for character in text):
        raise ValueError("runtime catalog contains invalid text")
    return text


def normalize_runtime_catalog(value: object, *, strict: bool = False) -> dict[str, Any] | None:
    decoded = _decode(value)
    if decoded in (None, ""):
        return None
    if not isinstance(decoded, dict):
        if strict:
            raise ValueError("runtime_catalog must be an object")
        return None
    if set(decoded) != {"schema_id", "credentials"} or decoded.get("schema_id") != SCHEMA_ID:
        if strict:
            raise ValueError(f"runtime_catalog must be a closed {SCHEMA_ID} object")
        return None
    raw_credentials = decoded.get("credentials")
    if not isinstance(raw_credentials, list) or len(raw_credentials) > 32:
        raise ValueError("runtime_catalog.credentials must be an array with at most 32 entries")

    credentials: list[dict[str, Any]] = []
    credential_ids: set[str] = set()
    for raw_credential in raw_credentials:
        if not isinstance(raw_credential, dict) or set(raw_credential) != {
            "credential_id",
            "label",
            "provider",
            "auth_type",
            "models",
        }:
            raise ValueError("runtime catalog credential must be a closed metadata object")
        credential_id = _text(raw_credential.get("credential_id"), max_length=128)
        provider = _text(raw_credential.get("provider"), max_length=60).lower()
        auth_type = _text(raw_credential.get("auth_type"), max_length=32).lower()
        if not _IDENTIFIER.fullmatch(credential_id):
            raise ValueError("runtime catalog credential_id is invalid")
        if credential_id in credential_ids:
            raise ValueError("runtime catalog credential_id values must be unique")
        if not _PROVIDER.fullmatch(provider):
            raise ValueError("runtime catalog provider is invalid")
        if auth_type not in AUTH_TYPES:
            raise ValueError("runtime catalog auth_type is invalid")
        raw_models = raw_credential.get("models")
        if not isinstance(raw_models, list) or not raw_models or len(raw_models) > 256:
            raise ValueError("runtime catalog models must contain 1 through 256 entries")
        models: list[dict[str, str]] = []
        model_ids: set[str] = set()
        for raw_model in raw_models:
            if not isinstance(raw_model, dict) or set(raw_model) != {"id", "name"}:
                raise ValueError("runtime catalog model must contain exactly id and name")
            model_id = _text(raw_model.get("id"), max_length=200)
            if not _MODEL.fullmatch(model_id) or model_id in model_ids:
                raise ValueError("runtime catalog model id is invalid or duplicated")
            model_ids.add(model_id)
            models.append({"id": model_id, "name": _text(raw_model.get("name"), max_length=160)})
        credential_ids.add(credential_id)
        credentials.append(
            {
                "credential_id": credential_id,
                "label": _text(raw_credential.get("label"), max_length=120),
                "provider": provider,
                "auth_type": auth_type,
                "models": models,
            }
        )
    return {"schema_id": SCHEMA_ID, "credentials": credentials}


def runtime_catalog_json(value: object) -> str | None:
    catalog = normalize_runtime_catalog(value, strict=True)
    return json.dumps(catalog, ensure_ascii=False, sort_keys=True) if catalog is not None else None


def public_runtime_catalog(value: object) -> dict[str, Any] | None:
    catalog = normalize_runtime_catalog(value)
    if catalog is None:
        return None
    return {
        "schemaId": catalog["schema_id"],
        "credentials": [
            {
                "credentialId": credential["credential_id"],
                "label": credential["label"],
                "provider": credential["provider"],
                "authType": credential["auth_type"],
                "models": list(credential["models"]),
            }
            for credential in catalog["credentials"]
        ],
    }


def available_models(value: object, *, include_credentials: bool) -> list[dict[str, str]]:
    catalog = normalize_runtime_catalog(value)
    if catalog is None:
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for credential in catalog["credentials"]:
        for model in credential["models"]:
            identity = (credential["provider"], model["id"])
            if identity in seen and not include_credentials:
                continue
            seen.add(identity)
            item = {"provider": credential["provider"], "model": model["id"]}
            if include_credentials:
                item.update(
                    {
                        "credentialId": credential["credential_id"],
                        "credentialLabel": credential["label"],
                        "modelName": model["name"],
                    }
                )
            result.append(item)
    return result


def normalize_runtime_selection(value: object, catalog_value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"credentialId", "provider", "model"}:
        raise ValueError("runtimeSelection must contain exactly credentialId, provider, and model")
    selection = {
        "credential_id": _text(value.get("credentialId"), max_length=128),
        "provider": _text(value.get("provider"), max_length=60).lower(),
        "model": _text(value.get("model"), max_length=200),
    }
    catalog = normalize_runtime_catalog(catalog_value, strict=True)
    for credential in (catalog or {}).get("credentials", []):
        if (
            credential["credential_id"] == selection["credential_id"]
            and credential["provider"] == selection["provider"]
            and any(model["id"] == selection["model"] for model in credential["models"])
        ):
            return selection
    raise ValueError("runtimeSelection must reference an available credential/provider/model")


def selection_from_worker(worker: dict[str, Any]) -> dict[str, str] | None:
    credential_id = str(worker.get("selected_credential_id") or "").strip()
    provider = str(worker.get("selected_provider") or "").strip()
    model = str(worker.get("selected_model") or "").strip()
    if not credential_id or not provider or not model:
        return None
    try:
        selection = normalize_runtime_selection(
            {"credentialId": credential_id, "provider": provider, "model": model},
            worker.get("runtime_catalog"),
        )
    except ValueError:
        return None
    return {
        "credentialId": selection["credential_id"],
        "provider": selection["provider"],
        "model": selection["model"],
    }
