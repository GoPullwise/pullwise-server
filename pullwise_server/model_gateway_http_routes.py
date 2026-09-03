from __future__ import annotations

from http import HTTPStatus
import time
from typing import Callable

from . import db, model_gateway_api, model_gateway_bootstrap, model_gateway_introspection
from .model_gateway_provider_removal import ProviderDependencyError


def handle_internal_post(
    handler: object,
    *,
    path: str,
    body: object,
    presented_token: str | None,
    timestamp: int,
) -> bool:
    if path not in {
        "/internal/model-gateway/grants/introspect",
        "/internal/model-gateway/routes/resolve",
    }:
        return False
    try:
        result = (
            model_gateway_introspection.introspect_gateway_grant(
                db.connect,
                presented_token=presented_token,
                payload=body,
                timestamp=timestamp,
            )
            if path.endswith("/grants/introspect")
            else model_gateway_introspection.resolve_gateway_route(
                db.connect,
                presented_token=presented_token,
                payload=body,
            )
        )
    except model_gateway_introspection.IntrospectionUnauthorized:
        handler.error(HTTPStatus.UNAUTHORIZED, "Model Gateway introspection authorization failed.")
    except model_gateway_introspection.GatewayRouteNotFound:
        handler.error(HTTPStatus.NOT_FOUND, "Gateway route is unavailable.")
    except ValueError as exc:
        handler.error(HTTPStatus.BAD_REQUEST, str(exc))
    except RuntimeError:
        handler.error(HTTPStatus.SERVICE_UNAVAILABLE, "Model Gateway introspection is unavailable.")
    else:
        handler.json(result, headers={"Cache-Control": "no-store"})
    return True


def handle_admin_get(handler: object, segments: list[str]) -> bool:
    if segments == ["admin", "provider-connections"]:
        handler.json({
            "items": model_gateway_api.list_provider_connections(connect_factory=db.connect),
        })
        return True
    if segments == ["admin", "model-gateway"]:
        handler.json(model_gateway_api.admin_snapshot(connect_factory=db.connect))
        return True
    return False


def handle_admin_post(
    handler: object,
    *,
    segments: list[str],
    body: dict[str, object],
    actor_user_id: str,
    request_id: str | None,
) -> bool:
    try:
        if segments == ["admin", "model-gateway", "secret-cleanup", "retry"]:
            if body:
                handler.error(HTTPStatus.BAD_REQUEST, "secret cleanup retry payload must be empty")
                return True
            result = model_gateway_api.retry_pending_secret_cleanup(connect_factory=db.connect)
            handler.json({"secretCleanup": result})
            return True
        if len(segments) == 5 and segments[:2] == ["admin", "provider-connections"] and segments[3] == "rotation":
            action = segments[4]
            if action == "stage":
                if set(body) != {"secret"}:
                    handler.error(HTTPStatus.BAD_REQUEST, "provider rotation stage payload must contain only secret")
                    return True
                provider = model_gateway_api.stage_provider_rotation(
                    connect_factory=db.connect,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    provider_connection_id=segments[2],
                    secret=body.get("secret"),
                )
            elif action == "promote":
                if body:
                    handler.error(HTTPStatus.BAD_REQUEST, "provider rotation action payload must be empty")
                    return True
                provider = model_gateway_api.advance_provider_rotation(
                    connect_factory=db.connect,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    provider_connection_id=segments[2],
                    action=action,
                )
            elif action == "retire":
                if set(body) != {"upstream_revoked"}:
                    handler.error(HTTPStatus.BAD_REQUEST, "provider rotation retirement payload is invalid")
                    return True
                provider = model_gateway_api.advance_provider_rotation(
                    connect_factory=db.connect,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    provider_connection_id=segments[2],
                    action=action,
                    upstream_revoked=body.get("upstream_revoked"),
                )
            else:
                return False
            handler.json({"providerConnection": provider})
            return True
        if len(segments) == 5 and segments[:2] == ["admin", "provider-connections"] and segments[3] == "removal":
            action = segments[4]
            if action == "prepare":
                if body:
                    handler.error(HTTPStatus.BAD_REQUEST, "provider removal prepare payload must be empty")
                    return True
                provider = model_gateway_api.prepare_provider_removal(
                    connect_factory=db.connect,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    provider_connection_id=segments[2],
                )
            elif action == "finalize":
                if set(body) != {"upstream_revoked"}:
                    handler.error(HTTPStatus.BAD_REQUEST, "provider removal finalize payload is invalid")
                    return True
                provider = model_gateway_api.finalize_provider_removal(
                    connect_factory=db.connect,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    provider_connection_id=segments[2],
                    upstream_revoked=body.get("upstream_revoked"),
                )
            else:
                return False
            handler.json({"providerConnection": provider})
            return True
        if len(segments) == 5 and segments[:2] == ["admin", "worker-pools"] and segments[3:] == ["workers", "batch"]:
            items = model_gateway_bootstrap.create_worker_batch(
                db.connect,
                worker_pool_id=segments[2],
                actor_user_id=actor_user_id,
                request_id=request_id,
                payload=body,
                timestamp=int(time.time()),
            )
            handler.json({"items": items}, HTTPStatus.CREATED, headers={"Cache-Control": "no-store"})
            return True
        if segments == ["admin", "provider-connections"]:
            connection = model_gateway_api.create_provider_connection(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                payload=body,
            )
            handler.json({"providerConnection": connection}, HTTPStatus.CREATED)
            return True
        if segments == ["admin", "profile-sets"]:
            profile_set = model_gateway_api.publish_profile_set(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                payload=body,
            )
            handler.json({"profileSet": profile_set}, HTTPStatus.CREATED)
            return True
        if segments == ["admin", "worker-pools"]:
            worker_pool = model_gateway_api.create_worker_pool(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                payload=body,
            )
            handler.json({"workerPool": worker_pool}, HTTPStatus.CREATED)
            return True
        if len(segments) == 4 and segments[:2] == ["admin", "worker-pools"] and segments[3] == "members":
            if set(body) != {"worker_id"}:
                handler.error(HTTPStatus.BAD_REQUEST, "worker pool membership payload must be a closed object")
                return True
            membership = model_gateway_api.bind_worker_to_pool(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                worker_id=body.get("worker_id"),
                worker_pool_id=segments[2],
            )
            handler.json({"membership": membership})
            return True
        if len(segments) == 4 and segments[:2] == ["admin", "worker-pools"] and segments[3] == "rotate-gateway-tokens":
            if body:
                handler.error(HTTPStatus.BAD_REQUEST, "gateway token rotation payload must be empty")
                return True
            worker_pool = model_gateway_api.rotate_worker_pool_gateway_tokens(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                worker_pool_id=segments[2],
            )
            handler.json({"workerPool": worker_pool})
            return True
        if len(segments) == 4 and segments[:2] == ["admin", "worker-pools"] and segments[3] == "desired-revision":
            if set(body) != {"profile_revision"}:
                handler.error(HTTPStatus.BAD_REQUEST, "worker pool desired revision payload is invalid")
                return True
            worker_pool = model_gateway_api.set_worker_pool_desired_revision(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                worker_pool_id=segments[2],
                profile_revision=body.get("profile_revision"),
            )
            handler.json({"workerPool": worker_pool})
            return True
        if len(segments) == 4 and segments[:2] == ["admin", "worker-pools"] and segments[3] == "rollout-wave":
            if set(body) != {"profile_revision", "worker_ids"}:
                handler.error(HTTPStatus.BAD_REQUEST, "worker pool rollout wave payload is invalid")
                return True
            rollout = model_gateway_api.rollout_worker_pool_wave(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                worker_pool_id=segments[2],
                profile_revision=body.get("profile_revision"),
                worker_ids=body.get("worker_ids"),
            )
            handler.json({"rollout": rollout})
            return True
        if len(segments) == 4 and segments[:2] == ["admin", "provider-connections"] and segments[3] == "emergency-revoke":
            if body:
                handler.error(HTTPStatus.BAD_REQUEST, "emergency revoke payload must be empty")
                return True
            provider = model_gateway_api.emergency_revoke_provider_connection(
                connect_factory=db.connect,
                actor_user_id=actor_user_id,
                request_id=request_id,
                provider_connection_id=segments[2],
            )
            handler.json({"providerConnection": provider})
            return True
    except ProviderDependencyError as exc:
        handler.json(
            {
                "message": str(exc),
                "dependencies": exc.dependencies,
            },
            HTTPStatus.CONFLICT,
        )
        return True
    except ValueError as exc:
        handler.error(HTTPStatus.BAD_REQUEST, str(exc))
        return True
    except RuntimeError:
        handler.error(HTTPStatus.SERVICE_UNAVAILABLE, "Provider secret broker is unavailable.")
        return True
    return False


def handle_worker_bootstrap_post(
    handler: object,
    *,
    path: str,
    body: object,
    presented_token: str | None,
    timestamp: int,
) -> bool:
    if path != "/v1/workers/bootstrap":
        return False
    try:
        result = model_gateway_bootstrap.exchange_worker_bootstrap(
            db.connect,
            presented_token=presented_token,
            payload=body,
            timestamp=timestamp,
        )
    except PermissionError:
        handler.error(HTTPStatus.UNAUTHORIZED, "Worker bootstrap credential is invalid or expired.")
    except ValueError as exc:
        handler.error(HTTPStatus.BAD_REQUEST, str(exc))
    else:
        handler.json(result, headers={"Cache-Control": "no-store"})
    return True


def handle_worker_profile_post(
    handler: object,
    *,
    segments: list[str],
    body: dict[str, object],
    worker_record: dict[str, object],
    worker_id_matches: Callable[[dict[str, object], str], bool],
) -> bool:
    if not (
        len(segments) == 4
        and segments[:2] == ["v1", "workers"]
        and segments[3] in {"model-profile", "model-profile-trust"}
    ):
        return False
    worker_id = str(segments[2] or "").strip()
    if not worker_id:
        handler.error(HTTPStatus.BAD_REQUEST, "worker_id is required.")
        return True
    if not worker_id_matches(worker_record, worker_id):
        handler.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        return True
    expected_schema = (
        "pullwise-worker-model-profile-request/v1"
        if segments[3] == "model-profile"
        else "pullwise-model-gateway-manifest-trust-request/v1"
    )
    if (
        set(body) != {"schema_id", "worker_id"}
        or body.get("schema_id") != expected_schema
        or body.get("worker_id") != worker_id
    ):
        handler.error(HTTPStatus.BAD_REQUEST, "Worker model profile request is invalid.")
        return True
    try:
        payload = (
            model_gateway_api.issue_worker_profile(connect_factory=db.connect, worker_id=worker_id)
            if segments[3] == "model-profile"
            else model_gateway_api.gateway_manifest_trust()
        )
    except ValueError as exc:
        handler.error(HTTPStatus.CONFLICT, str(exc))
    except RuntimeError:
        handler.error(HTTPStatus.SERVICE_UNAVAILABLE, "Model Gateway authorization is unavailable.")
    else:
        handler.json(payload, headers={"Cache-Control": "no-store"})
    return True
