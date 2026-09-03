from __future__ import annotations

import re
import sqlite3

from .model_gateway_pool_store import ModelGatewayPoolStore
from .model_gateway_route_availability import worker_assignment_routes_available
from .model_gateway_store import ConnectFactory


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROFILE_STATE_FIELDS = frozenset(
    {
        "schema_id",
        "worker_id",
        "worker_pool_id",
        "profile_set_id",
        "desired_revision",
        "applied_revision",
        "manifest_digest",
        "catalog_digest",
        "gateway_token_expires_at",
        "gateway_token_id",
        "last_apply_result",
        "applied_at",
    }
)


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"profile_state.{label} is invalid")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0) or value < 0:
        raise ValueError(f"profile_state.{label} is invalid")
    return value


def validate_worker_profile_observation(
    connect_factory: ConnectFactory,
    *,
    worker_id: str,
    value: object,
    observed_at: int,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != PROFILE_STATE_FIELDS:
        raise ValueError("profile_state must be a closed pullwise-worker-profile-state/v1 object")
    if value.get("schema_id") != "pullwise-worker-profile-state/v1":
        raise ValueError("profile_state.schema_id is invalid")
    state_worker_id = _safe_id(value.get("worker_id"), "worker_id")
    if state_worker_id != worker_id:
        raise ValueError("profile_state.worker_id does not match the authenticated Worker")
    worker_pool_id = _safe_id(value.get("worker_pool_id"), "worker_pool_id")
    profile_set_id = _safe_id(value.get("profile_set_id"), "profile_set_id")
    desired_revision = _integer(value.get("desired_revision"), "desired_revision", positive=True)
    applied_revision = _integer(value.get("applied_revision"), "applied_revision")
    manifest_digest = value.get("manifest_digest")
    catalog_digest = value.get("catalog_digest")
    if not isinstance(manifest_digest, str) or not SHA256.fullmatch(manifest_digest):
        raise ValueError("profile_state.manifest_digest is invalid")
    if not isinstance(catalog_digest, str) or not SHA256.fullmatch(catalog_digest):
        raise ValueError("profile_state.catalog_digest is invalid")
    expires_at = _integer(value.get("gateway_token_expires_at"), "gateway_token_expires_at", positive=True)
    token_id = _safe_id(value.get("gateway_token_id"), "gateway_token_id")
    applied_at = _integer(value.get("applied_at"), "applied_at", positive=True)
    if value.get("last_apply_result") != "succeeded" or applied_revision != desired_revision:
        raise ValueError("profile_state is not a successfully converged revision")
    if expires_at <= observed_at:
        raise ValueError("profile_state gateway token is expired")
    if applied_at > observed_at + 300:
        raise ValueError("profile_state.applied_at is in the future")
    assignment = ModelGatewayPoolStore(connect_factory).worker_profile_assignment(worker_id)
    if assignment is None:
        raise ValueError("profile_state Worker has no active model profile assignment")
    if (
        worker_pool_id != assignment["worker_pool_id"]
        or profile_set_id != assignment["profile_set_id"]
        or desired_revision != assignment["desired_revision"]
        or manifest_digest != assignment["manifest_digest"]
    ):
        raise ValueError("profile_state does not match Server desired state")
    return {
        "worker_id": worker_id,
        "worker_pool_id": worker_pool_id,
        "profile_set_id": profile_set_id,
        "desired_revision": desired_revision,
        "applied_revision": applied_revision,
        "manifest_digest": manifest_digest,
        "catalog_digest": catalog_digest,
        "gateway_token_expires_at": expires_at,
        "gateway_token_id": token_id,
        "last_apply_result": "succeeded",
        "applied_at": applied_at,
        "observed_at": observed_at,
    }


def store_worker_profile_observation(
    connect_factory: ConnectFactory,
    record: dict[str, object],
) -> None:
    connection = connect_factory()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO worker_profile_observations (
                    worker_id, worker_pool_id, profile_set_id, desired_revision,
                    applied_revision, manifest_digest, catalog_digest,
                    gateway_token_expires_at, gateway_token_id, last_apply_result,
                    applied_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    worker_pool_id = excluded.worker_pool_id,
                    profile_set_id = excluded.profile_set_id,
                    desired_revision = excluded.desired_revision,
                    applied_revision = excluded.applied_revision,
                    manifest_digest = excluded.manifest_digest,
                    catalog_digest = excluded.catalog_digest,
                    gateway_token_expires_at = excluded.gateway_token_expires_at,
                    gateway_token_id = excluded.gateway_token_id,
                    last_apply_result = excluded.last_apply_result,
                    applied_at = excluded.applied_at,
                    observed_at = excluded.observed_at
                WHERE excluded.observed_at >= worker_profile_observations.observed_at
                """,
                tuple(
                    record[key]
                    for key in (
                        "worker_id",
                        "worker_pool_id",
                        "profile_set_id",
                        "desired_revision",
                        "applied_revision",
                        "manifest_digest",
                        "catalog_digest",
                        "gateway_token_expires_at",
                        "gateway_token_id",
                        "last_apply_result",
                        "applied_at",
                        "observed_at",
                    )
                ),
            )
    finally:
        connection.close()


def get_worker_profile_observation(
    connect_factory: ConnectFactory,
    worker_id: str,
) -> dict[str, object] | None:
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM worker_profile_observations WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def worker_model_profile_readiness(
    connect_factory: ConnectFactory,
    *,
    worker_id: str,
    timestamp: int,
) -> tuple[bool, bool, str]:
    assignment = ModelGatewayPoolStore(connect_factory).worker_profile_assignment(worker_id)
    if assignment is None:
        return False, False, "profile_unassigned"
    if not worker_assignment_routes_available(connect_factory, worker_id):
        return True, False, "profile_routes_unavailable"
    observed = get_worker_profile_observation(connect_factory, worker_id)
    if observed is None:
        return True, False, "profile_state_missing"
    if (
        observed.get("worker_pool_id") != assignment.get("worker_pool_id")
        or observed.get("profile_set_id") != assignment.get("profile_set_id")
        or observed.get("desired_revision") != assignment.get("desired_revision")
        or observed.get("applied_revision") != assignment.get("desired_revision")
        or observed.get("manifest_digest") != assignment.get("manifest_digest")
        or observed.get("last_apply_result") != "succeeded"
    ):
        return True, False, "profile_state_diverged"
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        grant = connection.execute(
            """
            SELECT worker_id, profile_set_id, profile_revision, manifest_digest,
                   generation, expires_at, revoked_at
            FROM gateway_token_grants WHERE jti = ?
            """,
            (observed.get("gateway_token_id"),),
        ).fetchone()
    finally:
        connection.close()
    if (
        not grant
        or grant["revoked_at"] is not None
        or grant["worker_id"] != worker_id
        or grant["profile_set_id"] != assignment.get("profile_set_id")
        or grant["profile_revision"] != assignment.get("desired_revision")
        or grant["manifest_digest"] != assignment.get("manifest_digest")
        or grant["generation"] != assignment.get("gateway_token_generation")
        or grant["expires_at"] != observed.get("gateway_token_expires_at")
    ):
        return True, False, "gateway_token_stale"
    if int(grant["expires_at"] or 0) <= timestamp + 30:
        return True, False, "gateway_token_expiring"
    return True, True, "ready"
