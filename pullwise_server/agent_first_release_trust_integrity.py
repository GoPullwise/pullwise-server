"""Normalized-column integrity checks for stored release trust documents."""

from __future__ import annotations

import json
from typing import Mapping


def authority_metadata_matches(
    row: Mapping[str, object],
    root_bytes: bytes,
    principal_bytes: bytes,
    key_bytes: bytes,
) -> bool:
    try:
        root = json.loads(root_bytes)
        principal = json.loads(principal_bytes)
        key = json.loads(key_bytes)
        return (
            root["root_digest"] == row["stored_root_digest"]
            and root["trust_root_id"] == row["stored_root_id"]
            and root["organization_id"] == row["root_organization_id"]
            and root["root_principal_id"] == row["stored_root_principal_id"]
            and root["root_key_id"] == row["stored_root_key_id"]
            and root["public_key"] == row["root_public_key"]
            and root["issued_at"] == row["root_issued_at"]
            and root["expires_at"] == row["root_expires_at"]
            and principal["principal_digest"] == row["stored_principal_digest"]
            and principal["principal_id"] == row["stored_principal_id"]
            and principal["organization_id"] == row["principal_organization_id"]
            and principal["role"] == row["principal_role"]
            and principal["trust_root_id"] == row["principal_root_id"]
            and principal["trust_root_digest"] == row["principal_root_digest"]
            and principal["trust_root_ref"]["sha256"]
            == row["principal_root_ref_sha256"]
            and principal["trust_root_ref"]["size_bytes"]
            == row["principal_root_ref_size_bytes"]
            and principal["signer_id"] == row["principal_signer_id"]
            and principal["key_id"] == row["principal_signer_key_id"]
            and principal["issued_at"] == row["principal_issued_at"]
            and principal["expires_at"] == row["principal_expires_at"]
            and key["signing_key_digest"] == row["stored_key_digest"]
            and key["key_id"] == row["stored_key_id"]
            and key["organization_id"] == row["key_organization_id"]
            and key["principal_id"] == row["key_principal_id"]
            and key["principal_digest"] == row["key_principal_digest"]
            and key["principal_ref"]["sha256"] == row["key_principal_ref_sha256"]
            and key["principal_ref"]["size_bytes"]
            == row["key_principal_ref_size_bytes"]
            and key["key_purpose"] == row["stored_key_purpose"]
            and key["trust_root_id"] == row["key_root_id"]
            and key["trust_root_digest"] == row["key_root_digest"]
            and key["signer_id"] == row["key_signer_id"]
            and key["signer_key_id"] == row["key_signer_key_id"]
            and key["public_key"] == row["key_public_key"]
            and key["issued_at"] == row["key_issued_at"]
            and key["expires_at"] == row["key_expires_at"]
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return False


def revocation_metadata_matches(
    row: Mapping[str, object],
    revocation_bytes: bytes,
) -> bool:
    try:
        value = json.loads(revocation_bytes)
        return (
            value["revocation_digest"] == row["revocation_digest"]
            and value["revocation_id"] == row["revocation_id"]
            and value["organization_id"] == row["organization_id"]
            and value["trust_root_digest"] == row["root_digest"]
            and value["trust_root_ref"]["sha256"] == row["root_ref_sha256"]
            and value["trust_root_ref"]["size_bytes"]
            == row["root_ref_size_bytes"]
            and value["revoked_key_id"] == row["revoked_key_id"]
            and value["revoked_key_digest"] == row["signing_key_digest"]
            and value["revoked_key_ref"]["sha256"] == row["key_ref_sha256"]
            and value["revoked_key_ref"]["size_bytes"]
            == row["key_ref_size_bytes"]
            and value["revoked_principal_id"] == row["revoked_principal_id"]
            and value["reason_code"] == row["reason_code"]
            and value["signer_id"] == row["signer_id"]
            and value["signer_key_id"] == row["signer_key_id"]
            and value["issued_at"] == row["issued_at"]
            and value["effective_at"] == row["effective_at"]
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return False


__all__ = ["authority_metadata_matches", "revocation_metadata_matches"]
