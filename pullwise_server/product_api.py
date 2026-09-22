from __future__ import annotations

import hashlib
import json
import time
import uuid
from http import HTTPStatus
from typing import Mapping

from . import db
from .entitlements import product_usage_payload
from .github_sources import resolve_upstream_repository
from .product_jobs import ProductJobScheduler
from .product_store import ProductStore


def _header(handler: object, name: str) -> str:
    headers = getattr(handler, "headers", {})
    if not isinstance(headers, Mapping):
        return ""
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value).strip()
    return ""


def _request_id(handler: object) -> str:
    return _header(handler, "X-Request-Id") or f"req_{uuid.uuid4().hex}"


def _if_match_revision(handler: object, *, allow_zero: bool = False) -> int | None:
    raw_revision = _header(handler, "If-Match")
    if not raw_revision:
        return None
    revision_text = raw_revision.strip().strip('"')
    minimum = 0 if allow_zero else 1
    if not revision_text.isdigit() or int(revision_text) < minimum:
        raise ValueError("If-Match must contain a valid revision")
    return int(revision_text)


def _error(
    handler: object,
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    handler.json(
        {
            "error": {"code": code, "message": message, "retryable": retryable},
            "requestId": _request_id(handler),
        },
        status,
    )


def _authenticate(
    handler: object,
    users: Mapping[str, dict],
    *,
    required_scopes: tuple[str, ...],
) -> dict | None:
    session = handler.current_session()
    api_context = handler.current_api_key_context()
    if session is not None and api_context is not None:
        _error(
            handler,
            HTTPStatus.BAD_REQUEST,
            "AMBIGUOUS_AUTH",
            "Use either a session or an API key, not both.",
        )
        return None
    if session is not None:
        user = users.get(str(session.get("userId") or ""))
        if user is None:
            _error(handler, HTTPStatus.UNAUTHORIZED, "UNAUTHENTICATED", "Sign in is required.")
            return None
        return {"kind": "session", "user": user, "subjectId": user["id"], "restrictions": {}}
    if api_context is not None:
        scopes = set(api_context.get("scopes") or ())
        missing = [scope for scope in required_scopes if scope not in scopes]
        if missing:
            _error(
                handler,
                HTTPStatus.FORBIDDEN,
                "INSUFFICIENT_SCOPE",
                f"API key scope {missing[0]} is required.",
            )
            return None
        restrictions = api_context.get("restrictions")
        if restrictions and restrictions.get("kind") == "audit_bundle":
            _error(
                handler,
                HTTPStatus.FORBIDDEN,
                "INSUFFICIENT_SCOPE",
                "This API key is restricted to an audit bundle.",
            )
            return None
        user = api_context["user"]
        return {
            "kind": "api_key",
            "user": user,
            "subjectId": user["id"],
            "apiKey": api_context["apiKey"],
            "restrictions": restrictions or {},
        }
    _error(handler, HTTPStatus.UNAUTHORIZED, "UNAUTHENTICATED", "A session or API key is required.")
    return None


def _store() -> ProductStore:
    store = ProductStore(db.database_path())
    store.initialize()
    return store


def _query_value(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _source_module(source: Mapping[str, object]) -> str:
    source_type = source.get("type")
    if source_type == "ci_failure":
        return "ci"
    if source_type == "release":
        return "updates"
    return "pr"


def _filter_sources(sources: list[dict], params: Mapping[str, object]) -> list[dict]:
    module = _query_value(params, "module")
    repository_id = _query_value(params, "repositoryId")
    watch_id = _query_value(params, "watchId")
    result = []
    for source in sources:
        if module and _source_module(source) != module:
            continue
        if repository_id and source.get("repositoryId") != repository_id:
            continue
        if watch_id and not any(context.get("watchId") == watch_id for context in source.get("contexts") or []):
            continue
        result.append(source)
    return result


def _apply_source_restrictions(sources: list[dict], restrictions: Mapping[str, object]) -> list[dict]:
    repository_ids = restrictions.get("repositoryIds")
    watch_ids = restrictions.get("watchIds")
    if repository_ids is None and watch_ids is None:
        return sources
    allowed_repositories = set(repository_ids or ())
    allowed_watches = set(watch_ids or ())
    return [
        source
        for source in sources
        if (
            source.get("repositoryId") in allowed_repositories
            or any(context.get("watchId") in allowed_watches for context in source.get("contexts") or [])
        )
    ]


def _apply_item_restrictions(items: list[dict], restrictions: Mapping[str, object]) -> list[dict]:
    repository_ids = restrictions.get("repositoryIds")
    watch_ids = restrictions.get("watchIds")
    if repository_ids is None and watch_ids is None:
        return items
    allowed_repositories = set(repository_ids or ())
    allowed_watches = set(watch_ids or ())
    return [
        item
        for item in items
        if item.get("repositoryId") in allowed_repositories or item.get("watchId") in allowed_watches
    ]


def _item_in_view(item: Mapping[str, object], view: str, github_user_id: str) -> bool:
    if view == "all":
        return True
    attention = item.get("attentionState")
    handling = item.get("handling") if isinstance(item.get("handling"), Mapping) else {}
    next_actors = item.get("nextActors") if isinstance(item.get("nextActors"), list) else []
    assignee = handling.get("assigneeId")
    users = [actor.get("githubId") for actor in next_actors
             if isinstance(actor, Mapping) and actor.get("kind") == "user" and actor.get("githubId")]
    assigned_to_user = bool(github_user_id) and (assignee == github_user_id if assignee else github_user_id in users)
    # Team membership has no verified resolver yet. Keep such work visible in
    # unassigned rather than guessing that another person is handling it.
    has_actor = bool(assignee) or bool(users)
    if view == "mine":
        return attention in {"needs_action", "needs_confirmation"} and assigned_to_user
    if view == "unassigned":
        return attention in {"needs_action", "needs_confirmation"} and not has_actor
    if view == "waiting":
        return attention == "waiting" or (
            attention in {"needs_action", "needs_confirmation"} and has_actor and not assigned_to_user
        )
    return False


def _filter_items(items: list[dict], params: Mapping[str, object], user_id: str, *, include_view: bool) -> list[dict]:
    module = _query_value(params, "module")
    repository_id = _query_value(params, "repositoryId")
    watch_id = _query_value(params, "watchId")
    attention_state = _query_value(params, "attentionState")
    view = _query_value(params, "view") or "all"
    if view not in {"mine", "unassigned", "waiting", "all"}:
        raise ValueError("INVALID_VIEW")
    result = []
    for item in items:
        if module and item.get("module") != module:
            continue
        if repository_id and item.get("repositoryId") != repository_id:
            continue
        if watch_id and item.get("watchId") != watch_id:
            continue
        if attention_state and item.get("attentionState") != attention_state:
            continue
        if include_view and not _item_in_view(item, view, user_id):
            continue
        result.append(item)
    return result


def handle_get(handler: object, segments: list[str], params: dict, users: Mapping[str, dict]) -> bool:
    recognized = (
        segments == ["me"]
        or segments == ["watches"]
        or segments == ["repositories"]
        or segments == ["sources"]
        or segments == ["items"]
        or segments == ["items", "overview"]
        or segments == ["usage"]
        or (len(segments) == 2 and segments[0] == "jobs")
        or (len(segments) == 3 and segments[0] == "repositories" and segments[2] == "service")
        or (len(segments) == 2 and segments[0] in {"sources", "items"})
    )
    if not recognized:
        return False
    required_scope = (
        "profile:read"
        if segments == ["me"]
        else "watches:read"
        if segments == ["watches"]
        else "repositories:read"
        if segments == ["repositories"]
        else "usage:read"
        if segments == ["usage"]
        else "repositories:read"
        if len(segments) == 3 and segments[0] == "repositories"
        else "items:read"
    )
    principal = _authenticate(handler, users, required_scopes=(required_scope,))
    if principal is None:
        return True
    store = _store()
    user_id = principal["user"]["id"]
    if segments == ["me"]:
        user = principal["user"]
        handler.json(
            {
                "id": user["id"],
                "name": user.get("name") or "",
                "email": user.get("email") or "",
                "modules": ["pr", "ci", "updates"],
            }
        )
        return True
    if segments == ["repositories"]:
        access = principal["user"].get("githubRepositoryAccess")
        repository_items = access.get("repositoryItems") if isinstance(access, Mapping) else []
        restrictions = principal.get("restrictions") or {}
        allowed_ids = set(restrictions.get("repositoryIds") or ()) if restrictions else None
        items = []
        seen = set()
        for repository_item in repository_items if isinstance(repository_items, list) else []:
            if not isinstance(repository_item, Mapping):
                continue
            github_id = repository_item.get("githubRepoId") or repository_item.get("id")
            repository = db.get_repository(str(repository_item.get("id") or ""))
            if repository is None:
                repository = db.get_repository_by_github_repo_id(github_id)
            if repository is None or repository["id"] in seen:
                continue
            if allowed_ids is not None and repository["id"] not in allowed_ids:
                continue
            seen.add(repository["id"])
            items.append(
                {
                    "id": repository["id"],
                    "githubRepoId": str(repository.get("github_repo_id") or ""),
                    "fullName": repository.get("full_name") or "",
                    "defaultBranch": repository.get("default_branch") or "main",
                    "private": bool(repository.get("private")),
                    "service": store.get_repository_service(repository["id"]),
                }
            )
        handler.json(
            {
                "items": items,
                "nextCursor": None,
                "hasMore": False,
                "requestId": _request_id(handler),
            }
        )
        return True
    if len(segments) == 2 and segments[0] == "jobs":
        job = store.get_background_job(segments[1])
        if (
            job is None
            or job.get("requesterId") != user_id
            or job.get("jobType") not in {"sync_repository", "sync_watch"}
        ):
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Sync job was not found.")
            return True
        handler.json(
            {
                "id": job["id"],
                "operation": job["jobType"],
                "status": job["status"],
                "attempt": job["attempt"],
                "links": {"self": f"/api/v1/jobs/{job['id']}"},
                "requestId": _request_id(handler),
            }
        )
        return True
    if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "service":
        repository_context = handler.api_repository_context({"user": principal["user"]}, segments[1])
        if not repository_context:
            return True
        repository = repository_context[0]
        restrictions = principal.get("restrictions") or {}
        if restrictions and repository["id"] not in set(restrictions.get("repositoryIds") or ()):
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Repository service was not found.")
            return True
        service = store.get_repository_service(repository["id"])
        if service is None:
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Repository service was not found.")
            return True
        handler.json(service, headers={"ETag": f'"{service["revision"]}"'})
        return True
    if segments == ["usage"]:
        handler.json(product_usage_payload(store, principal["user"]))
        return True
    if segments == ["watches"]:
        items = store.list_watches_for_billing_owner(user_id)
        restrictions = principal.get("restrictions") or {}
        if "watchIds" in restrictions:
            allowed_watch_ids = set(restrictions["watchIds"])
            items = [item for item in items if item["id"] in allowed_watch_ids]
        elif restrictions:
            items = []
        handler.json(
            {
                "items": items,
                "nextCursor": None,
                "hasMore": False,
                "requestId": _request_id(handler),
            }
        )
        return True
    restrictions = principal.get("restrictions") or {}
    sources = _filter_sources(
        _apply_source_restrictions(store.list_sources_for_billing_owner(user_id), restrictions),
        params,
    )
    if segments == ["sources"]:
        handler.json(
            {
                "items": sources,
                "nextCursor": None,
                "hasMore": False,
                "requestId": _request_id(handler),
            }
        )
        return True
    if len(segments) == 2 and segments[0] == "sources":
        source = next((item for item in sources if item["id"] == segments[1]), None)
        if source is None:
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Source was not found.")
        else:
            handler.json(source)
        return True
    all_items = _apply_item_restrictions(store.list_items_for_billing_owner(user_id), restrictions)
    github_user_id = str(principal["user"].get("githubId") or "")
    resource_items = _filter_items(all_items, params, github_user_id, include_view=False)
    if segments == ["items", "overview"]:
        selected = _filter_items(all_items, params, github_user_id, include_view=True)
        counts = {
            state: sum(1 for item in selected if item.get("attentionState") == state)
            for state in ("needs_action", "needs_confirmation", "waiting", "optional", "closed")
        }
        view_counts = {
            view: sum(1 for item in resource_items if _item_in_view(item, view, github_user_id))
            for view in ("mine", "unassigned", "waiting", "all")
        }
        handler.json(
            {
                "scope": {
                    "module": _query_value(params, "module") or None,
                    "repositoryId": _query_value(params, "repositoryId") or None,
                    "watchId": _query_value(params, "watchId") or None,
                    "view": _query_value(params, "view") or "all",
                },
                "totalCount": len(selected),
                "counts": counts,
                "viewCounts": view_counts,
                "sourceCoverage": {
                    "unit": "source_context",
                    "total": sum(len(source.get("contexts") or []) for source in sources),
                    "processingStatus": {
                        status: sum(
                            1
                            for source in sources
                            for context in source.get("contexts") or []
                            if context.get("processingStatus") == status
                        )
                        for status in {
                            context.get("processingStatus")
                            for source in sources
                            for context in source.get("contexts") or []
                        }
                        if status
                    },
                },
                "lastSyncedAt": max((source["lastSyncedAt"] for source in sources), default=None),
                "requestId": _request_id(handler),
            }
        )
        return True
    if segments == ["items"]:
        items = _filter_items(all_items, params, github_user_id, include_view=True)
        handler.json(
            {
                "items": items,
                "nextCursor": None,
                "hasMore": False,
                "requestId": _request_id(handler),
            }
        )
        return True
    item = next((candidate for candidate in all_items if candidate["id"] == segments[1]), None)
    if item is None:
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Item was not found.")
    else:
        handler.json(item, headers={"ETag": f'"{item["revision"]}"'})
    return True


def handle_post(handler: object, segments: list[str], body: dict, users: Mapping[str, dict]) -> bool:
    is_create_watch = segments == ["watches"]
    is_sync_watch = len(segments) == 3 and segments[0] == "watches" and segments[2] == "sync"
    is_sync_repository = len(segments) == 3 and segments[0] == "repositories" and segments[2] == "sync"
    if not (is_create_watch or is_sync_watch or is_sync_repository):
        return False
    if is_create_watch:
        principal = _authenticate(handler, users, required_scopes=("watches:write",))
        if principal is None:
            return True
        if not isinstance(body, dict):
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Request body must be an object.")
            return True
        allowed = {
            "upstream",
            "targetRepositoryId",
            "interests",
            "includePrerelease",
            "analysisEnabled",
            "enabled",
            "priorityOrder",
        }
        upstream = body.get("upstream")
        if set(body) - allowed or not isinstance(upstream, Mapping):
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Invalid watch fields.")
            return True
        owner = upstream.get("owner")
        repository = upstream.get("repository")
        interests = body.get("interests")
        if not isinstance(interests, list):
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "interests must be an array.")
            return True
        for boolean_field in ("includePrerelease", "analysisEnabled", "enabled"):
            if boolean_field in body and not isinstance(body[boolean_field], bool):
                _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"{boolean_field} must be boolean.")
                return True
        priority_order = body.get("priorityOrder", 0)
        if isinstance(priority_order, bool) or not isinstance(priority_order, int) or priority_order < 0:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "priorityOrder must be non-negative.")
            return True
        idempotency_key = _header(handler, "Idempotency-Key")
        if not idempotency_key or len(idempotency_key) > 128:
            _error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "Idempotency-Key is required and must be at most 128 characters.",
            )
            return True
        restrictions = principal.get("restrictions") or {}
        target_repository_id = body.get("targetRepositoryId")
        if target_repository_id is not None and not isinstance(target_repository_id, str):
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "targetRepositoryId must be a string or null.")
            return True
        if restrictions and target_repository_id is None:
            _error(
                handler,
                HTTPStatus.FORBIDDEN,
                "INSUFFICIENT_SCOPE",
                "A resource-restricted key cannot create a personal watch.",
            )
            return True
        canonical_path = "/api/v1/watches"
        body_hash = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        timestamp = int(time.time())
        store = _store()
        try:
            idempotency = store.begin_idempotent_request(
                subject_id=principal["subjectId"],
                method="POST",
                path=canonical_path,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                timestamp=timestamp,
            )
        except ValueError as error:
            if str(error) == "IDEMPOTENCY_CONFLICT":
                _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was reused with another request.")
            else:
                _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
            return True
        if idempotency["state"] == "replay":
            handler.json(idempotency["response"], idempotency["statusCode"], headers={"Location": idempotency["response"]["links"]["self"]})
            return True
        if idempotency["state"] == "pending":
            _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_IN_PROGRESS", "The same request is still in progress.", retryable=True)
            return True
        try:
            resolved = resolve_upstream_repository(
                str(owner or ""),
                str(repository or ""),
                token=str(principal["user"].get("githubAccessToken") or ""),
            )
            if resolved["private"] and target_repository_id is not None:
                raise ValueError("SHARED_PRIVATE_WATCH_UNSUPPORTED")
            if resolved["private"] and not principal["user"].get("githubAccessToken"):
                raise ValueError("GITHUB_REPOSITORY_UNAVAILABLE")
            entitlement = product_usage_payload(store, principal["user"])["entitlements"]
            watch = store.create_watch(
                owner_id=principal["user"]["id"],
                target_repository_id=target_repository_id,
                upstream_repository_id=resolved["id"],
                billing_owner_id=principal["user"]["id"],
                interests=interests,
                enabled=body.get("enabled", True),
                analysis_enabled=body.get("analysisEnabled", False),
                include_prerelease=body.get("includePrerelease", False),
                priority_order=priority_order,
                active_limit=entitlement["activeWatchLimit"],
            )
        except BaseException as error:
            store.abandon_idempotent_request(
                subject_id=principal["subjectId"],
                method="POST",
                path=canonical_path,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
            )
            if isinstance(error, ValueError):
                code = str(error)
                status = (
                    HTTPStatus.PAYMENT_REQUIRED
                    if code == "WATCH_LIMIT_REACHED"
                    else HTTPStatus.UNPROCESSABLE_ENTITY
                    if code == "SHARED_PRIVATE_WATCH_UNSUPPORTED"
                    else HTTPStatus.CONFLICT
                    if code == "WATCH_ALREADY_EXISTS"
                    else HTTPStatus.NOT_FOUND
                    if code in {"GITHUB_REPOSITORY_NOT_FOUND", "GITHUB_REPOSITORY_UNAVAILABLE"}
                    else HTTPStatus.BAD_REQUEST
                )
                _error(handler, status, code, code.replace("_", " ").title())
                return True
            raise
        response = {
            **watch,
            "upstream": resolved["fullName"],
            "status": "active" if watch["enabled"] else "paused",
            "lastSyncedAt": None,
            "links": {"self": f"/api/v1/watches/{watch['id']}"},
            "requestId": _request_id(handler),
        }
        store.complete_idempotent_request(
            subject_id=principal["subjectId"],
            method="POST",
            path=canonical_path,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
            status_code=int(HTTPStatus.CREATED),
            response=response,
            timestamp=timestamp,
        )
        handler.json(response, HTTPStatus.CREATED, headers={"Location": response["links"]["self"]})
        return True
    if is_sync_repository:
        principal = _authenticate(
            handler,
            users,
            required_scopes=("repositories:read", "sync:write"),
        )
        if principal is None:
            return True
        if not isinstance(body, dict) or body:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Manual sync requires an empty JSON object.")
            return True
        idempotency_key = _header(handler, "Idempotency-Key")
        if not idempotency_key or len(idempotency_key) > 128:
            _error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "Idempotency-Key is required and must be at most 128 characters.",
            )
            return True
        repository_context = handler.api_repository_context({"user": principal["user"]}, segments[1])
        if not repository_context:
            return True
        repository = repository_context[0]
        restrictions = principal.get("restrictions") or {}
        if restrictions and repository["id"] not in set(restrictions.get("repositoryIds") or ()):
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Repository service was not found.")
            return True
        store = _store()
        service = store.get_repository_service(repository["id"])
        if service is None or not service["enabled"]:
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Repository service was not found.")
            return True
        if service["billingOwnerId"] != principal["user"]["id"] and not service["allowMemberSync"]:
            _error(handler, HTTPStatus.FORBIDDEN, "MEMBER_SYNC_DISABLED", "Member sync is disabled.")
            return True
        canonical_path = f"/api/v1/repositories/{repository['id']}/sync"
        body_hash = hashlib.sha256(b"{}").hexdigest()
        timestamp = int(time.time())
        try:
            idempotency = store.begin_idempotent_request(
                subject_id=principal["subjectId"],
                method="POST",
                path=canonical_path,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                timestamp=timestamp,
            )
        except ValueError as error:
            if str(error) == "IDEMPOTENCY_CONFLICT":
                _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was reused with another request.")
            else:
                _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
            return True
        if idempotency["state"] == "replay":
            handler.json(idempotency["response"], idempotency["statusCode"])
            return True
        if idempotency["state"] == "pending":
            _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_IN_PROGRESS", "The same request is still in progress.", retryable=True)
            return True
        try:
            job = ProductJobScheduler(store).request_manual_sync(
                resource_kind="repository",
                resource_id=repository["id"],
                requester_id=principal["user"]["id"],
            )
        except BaseException:
            store.abandon_idempotent_request(
                subject_id=principal["subjectId"],
                method="POST",
                path=canonical_path,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
            )
            raise
        response = {
            "id": job["id"],
            "operation": "sync_repository",
            "status": job["status"],
            "links": {"self": f"/api/v1/jobs/{job['id']}"},
            "requestId": _request_id(handler),
        }
        store.complete_idempotent_request(
            subject_id=principal["subjectId"],
            method="POST",
            path=canonical_path,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
            status_code=int(HTTPStatus.ACCEPTED),
            response=response,
            timestamp=timestamp,
        )
        handler.json(response, HTTPStatus.ACCEPTED)
        return True
    principal = _authenticate(
        handler,
        users,
        required_scopes=("watches:read", "sync:write"),
    )
    if principal is None:
        return True
    if not isinstance(body, dict) or body:
        _error(
            handler,
            HTTPStatus.BAD_REQUEST,
            "INVALID_REQUEST",
            "Manual sync requires an empty JSON object.",
        )
        return True
    idempotency_key = _header(handler, "Idempotency-Key")
    if not idempotency_key or len(idempotency_key) > 128:
        _error(
            handler,
            HTTPStatus.BAD_REQUEST,
            "INVALID_REQUEST",
            "Idempotency-Key is required and must be at most 128 characters.",
        )
        return True
    store = _store()
    watch = store.get_watch(segments[1])
    if watch is None or watch["billingOwnerId"] != principal["user"]["id"]:
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Watch was not found.")
        return True
    restrictions = principal.get("restrictions") or {}
    if restrictions and watch["id"] not in set(restrictions.get("watchIds") or ()):
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Watch was not found.")
        return True
    canonical_path = f"/api/v1/watches/{watch['id']}/sync"
    body_hash = hashlib.sha256(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    timestamp = int(time.time())
    try:
        idempotency = store.begin_idempotent_request(
            subject_id=principal["subjectId"],
            method="POST",
            path=canonical_path,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
            timestamp=timestamp,
        )
    except ValueError as error:
        if str(error) == "IDEMPOTENCY_CONFLICT":
            _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was reused with another request.")
        else:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
        return True
    if idempotency["state"] == "replay":
        handler.json(idempotency["response"], idempotency["statusCode"])
        return True
    if idempotency["state"] == "pending":
        _error(handler, HTTPStatus.CONFLICT, "IDEMPOTENCY_IN_PROGRESS", "The same request is still in progress.", retryable=True)
        return True
    try:
        job = ProductJobScheduler(store).request_manual_sync(
            resource_kind="watch",
            resource_id=watch["id"],
            requester_id=principal["user"]["id"],
        )
    except BaseException:
        store.abandon_idempotent_request(
            subject_id=principal["subjectId"],
            method="POST",
            path=canonical_path,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
        )
        raise
    response = {
        "id": job["id"],
        "operation": "sync_watch",
        "status": job["status"],
        "links": {"self": f"/api/v1/jobs/{job['id']}"},
        "requestId": _request_id(handler),
    }
    store.complete_idempotent_request(
        subject_id=principal["subjectId"],
        method="POST",
        path=canonical_path,
        idempotency_key=idempotency_key,
        body_hash=body_hash,
        status_code=int(HTTPStatus.ACCEPTED),
        response=response,
        timestamp=timestamp,
    )
    handler.json(response, HTTPStatus.ACCEPTED)
    return True


def handle_patch(handler: object, segments: list[str], body: dict, users: Mapping[str, dict]) -> bool:
    if not (len(segments) == 2 and segments[0] in {"items", "watches"}):
        return False
    is_watch = segments[0] == "watches"
    principal = _authenticate(
        handler,
        users,
        required_scopes=("watches:write",) if is_watch else ("items:read", "items:write"),
    )
    if principal is None:
        return True
    if not isinstance(body, dict):
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Request body must be an object.")
        return True
    try:
        expected_revision = _if_match_revision(handler)
    except ValueError as error:
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
        return True
    if expected_revision is None:
        _error(
            handler,
            HTTPStatus.PRECONDITION_REQUIRED,
            "PRECONDITION_REQUIRED",
            "If-Match is required.",
        )
        return True
    if is_watch:
        allowed = {"interests", "enabled", "analysisEnabled", "includePrerelease", "priorityOrder"}
        if not body or set(body) - allowed:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Invalid watch fields.")
            return True
        for boolean_field in ("enabled", "analysisEnabled", "includePrerelease"):
            if boolean_field in body and not isinstance(body[boolean_field], bool):
                _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"{boolean_field} must be boolean.")
                return True
        if "priorityOrder" in body and (
            isinstance(body["priorityOrder"], bool)
            or not isinstance(body["priorityOrder"], int)
            or body["priorityOrder"] < 0
        ):
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "priorityOrder must be non-negative.")
            return True
        store = _store()
        watch = store.get_watch(segments[1])
        user_id = principal["user"]["id"]
        restrictions = principal.get("restrictions") or {}
        if (
            watch is None
            or watch["billingOwnerId"] != user_id
            or (restrictions and watch["id"] not in set(restrictions.get("watchIds") or ()))
        ):
            _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Watch was not found.")
            return True
        try:
            active_limit = product_usage_payload(store, principal["user"])["entitlements"]["activeWatchLimit"]
            updated = store.update_watch(
                watch["id"],
                expected_revision=expected_revision,
                interests=body.get("interests") if "interests" in body else None,
                enabled=body.get("enabled") if "enabled" in body else None,
                analysis_enabled=body.get("analysisEnabled") if "analysisEnabled" in body else None,
                include_prerelease=body.get("includePrerelease") if "includePrerelease" in body else None,
                priority_order=body.get("priorityOrder") if "priorityOrder" in body else None,
                active_limit=active_limit,
            )
        except ValueError as error:
            code = str(error)
            if code == "REVISION_MISMATCH":
                _error(handler, HTTPStatus.PRECONDITION_FAILED, code, "Watch revision is stale.")
            elif code == "WATCH_LIMIT_REACHED":
                _error(handler, HTTPStatus.PAYMENT_REQUIRED, code, "Active watch limit reached.")
            else:
                _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", code)
            return True
        handler.json(updated, headers={"ETag": f'"{updated["revision"]}"'})
        return True
    allowed = {"itemVersion", "disposition", "assigneeId", "note", "feedback"}
    if set(body) - allowed or "itemVersion" not in body:
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Invalid Item handling fields.")
        return True
    store = _store()
    user_id = principal["user"]["id"]
    current = next(
        (item for item in store.list_items_for_billing_owner(user_id) if item["id"] == segments[1]),
        None,
    )
    if current is None:
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Item was not found.")
        return True
    patch_fields = {
        field: body[field]
        for field in ("disposition", "assigneeId", "note", "feedback")
        if field in body
    }
    field_names = {
        "assigneeId": "assignee_id",
    }
    patch_fields = {field_names.get(key, key): value for key, value in patch_fields.items()}
    try:
        store.patch_item_handling(
            item_id=segments[1],
            item_version=body["itemVersion"],
            expected_revision=expected_revision,
            actor_id=user_id,
            **patch_fields,
        )
    except ValueError as error:
        code = str(error)
        if code == "STALE_ITEM":
            _error(handler, HTTPStatus.CONFLICT, code, "Item version is stale.")
        elif code == "REVISION_MISMATCH":
            _error(handler, HTTPStatus.PRECONDITION_FAILED, code, "Item revision is stale.")
        elif code == "NOT_FOUND":
            _error(handler, HTTPStatus.NOT_FOUND, code, "Item was not found.")
        else:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", code)
        return True
    updated = next(
        item for item in store.list_items_for_billing_owner(user_id) if item["id"] == segments[1]
    )
    handler.json(updated, headers={"ETag": f'"{updated["revision"]}"'})
    return True


def handle_put(handler: object, segments: list[str], body: dict, users: Mapping[str, dict]) -> bool:
    if not (len(segments) == 3 and segments[0] == "repositories" and segments[2] == "service"):
        return False
    principal = _authenticate(handler, users, required_scopes=("repositories:manage",))
    if principal is None:
        return True
    if not isinstance(body, dict):
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Request body must be an object.")
        return True
    required = {
        "enabled",
        "modules",
        "analysisEnabled",
        "allowMemberSync",
        "defaultAssigneeId",
        "priorityOrder",
    }
    if set(body) != required:
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Repository service fields are incomplete.")
        return True
    try:
        expected_revision = _if_match_revision(handler, allow_zero=True)
    except ValueError as error:
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
        return True
    if expected_revision is None:
        _error(handler, HTTPStatus.PRECONDITION_REQUIRED, "PRECONDITION_REQUIRED", "If-Match is required.")
        return True
    repository_context = handler.api_repository_context({"user": principal["user"]}, segments[1])
    if not repository_context:
        return True
    repository, repository_meta = repository_context
    restrictions = principal.get("restrictions") or {}
    if restrictions and repository["id"] not in set(restrictions.get("repositoryIds") or ()):
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Repository service was not found.")
        return True
    store = _store()
    limit = product_usage_payload(store, principal["user"])["entitlements"]["activeRepositoryLimit"]
    installation_id = str(
        repository_meta.get("installationId")
        or principal["user"].get("githubRepositoryAccess", {}).get("installationId")
        or ""
    )
    try:
        service = store.put_repository_service(
            repository_id=repository["id"],
            installation_id=installation_id,
            billing_owner_id=principal["user"]["id"],
            expected_revision=expected_revision,
            enabled=body["enabled"],
            modules=body["modules"],
            analysis_enabled=body["analysisEnabled"],
            allow_member_sync=body["allowMemberSync"],
            default_assignee_id=body["defaultAssigneeId"],
            priority_order=body["priorityOrder"],
            active_limit=limit,
        )
    except ValueError as error:
        code = str(error)
        if code == "REVISION_MISMATCH":
            _error(handler, HTTPStatus.PRECONDITION_FAILED, code, "Repository service revision is stale.")
        elif code == "REPOSITORY_LIMIT_REACHED":
            _error(handler, HTTPStatus.PAYMENT_REQUIRED, code, "Active repository limit reached.")
        elif code == "REPOSITORY_ALREADY_MANAGED":
            _error(handler, HTTPStatus.CONFLICT, code, "Repository is already managed by another billing owner.")
        else:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", code)
        return True
    handler.json(service, headers={"ETag": f'"{service["revision"]}"'})
    return True


def handle_delete(handler: object, segments: list[str], users: Mapping[str, dict]) -> bool:
    if not (len(segments) == 2 and segments[0] == "watches"):
        return False
    principal = _authenticate(handler, users, required_scopes=("watches:write",))
    if principal is None:
        return True
    try:
        expected_revision = _if_match_revision(handler)
    except ValueError as error:
        _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(error))
        return True
    if expected_revision is None:
        _error(
            handler,
            HTTPStatus.PRECONDITION_REQUIRED,
            "PRECONDITION_REQUIRED",
            "If-Match is required.",
        )
        return True
    store = _store()
    watch = store.get_watch(segments[1])
    user_id = principal["user"]["id"]
    restrictions = principal.get("restrictions") or {}
    if (
        watch is None
        or watch["billingOwnerId"] != user_id
        or (restrictions and watch["id"] not in set(restrictions.get("watchIds") or ()))
    ):
        _error(handler, HTTPStatus.NOT_FOUND, "NOT_FOUND", "Watch was not found.")
        return True
    try:
        store.archive_watch(watch["id"], expected_revision=expected_revision)
    except ValueError as error:
        code = str(error)
        if code == "REVISION_MISMATCH":
            _error(handler, HTTPStatus.PRECONDITION_FAILED, code, "Watch revision is stale.")
        else:
            _error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", code)
        return True
    handler.json({}, HTTPStatus.NO_CONTENT)
    return True
