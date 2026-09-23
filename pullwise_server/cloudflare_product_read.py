"""Authenticated product-v1 profile and usage reads over async D1."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Mapping

from .account_cycle_rules import period_start_for_key
from .product_entitlement_rules import (
    entitlements_for_user,
    product_usage_payload_from_usage,
)
from .product_dto_rules import watch_dto
from .cloudflare_source_read import D1SourceReads
from .cloudflare_item_read import D1ItemReads
from .cloudflare_item_handling import D1ItemHandling
from .cloudflare_watch_adapter import D1WatchTransactions
from .product_source_filters import apply_source_restrictions, filter_sources
from .product_item_filters import apply_item_restrictions, filter_items
from .product_usage_events import parse_usage_events_query, usage_events_page

SESSION_COOKIE = "pw_session"
API_KEY_PREFIX = "pwk_"


class ProductReadAuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def _header(headers: Mapping[str, object], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value).strip()
    return ""


def _bearer(headers: Mapping[str, object]) -> str:
    parts = _header(headers, "Authorization").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1] if not any(char in parts[1] for char in "\r\n") else ""
    return ""


def _cookie_sessions(headers: Mapping[str, object]) -> list[str]:
    sessions = []
    for entry in _header(headers, "Cookie").split(";"):
        name, separator, value = entry.partition("=")
        if separator and name.strip() == SESSION_COOKIE:
            candidate = value.strip().strip('"')
            if candidate:
                sessions.append(candidate)
    return sessions


def _timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


async def _user(binding: Any, owner_id: str) -> dict | None:
    row = await binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
        json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
    if not row:
        return None
    user = json.loads(row["snapshot"])
    return user if isinstance(user, dict) and user.get("id") == owner_id else None


async def _principal(binding: Any, headers: Mapping[str, object],
                     *, scope: str, now: int) -> tuple[dict, dict]:
    bearer = _bearer(headers)
    header_key = _header(headers, "X-Pullwise-Api-Key")
    cookie_sessions = _cookie_sessions(headers)
    session_ids = ([bearer] if bearer and not bearer.startswith(API_KEY_PREFIX)
                   else cookie_sessions)
    api_token = bearer if bearer.startswith(API_KEY_PREFIX) else ""
    if header_key and not header_key.startswith(API_KEY_PREFIX):
        if cookie_sessions or bearer:
            raise ProductReadAuthError(400, "AMBIGUOUS_AUTH",
                "Use either a session or an API key, not both.")
        raise ProductReadAuthError(401, "UNAUTHENTICATED",
            "A session or API key is required.")
    if header_key.startswith(API_KEY_PREFIX):
        if api_token and header_key != api_token:
            raise ProductReadAuthError(400, "AMBIGUOUS_AUTH", "Use one API key.")
        api_token = header_key
    if api_token and (cookie_sessions or (bearer and not bearer.startswith(API_KEY_PREFIX))):
        raise ProductReadAuthError(400, "AMBIGUOUS_AUTH",
            "Use either a session or an API key, not both.")
    if api_token:
        key_hash = hashlib.sha256(api_token.encode("utf-8")).hexdigest()
        record = await binding.prepare("""SELECT user_id,scopes,expires_at,restrictions
            FROM api_keys WHERE key_hash=? AND revoked_at IS NULL""").bind(key_hash).first()
        expires_at = _timestamp(record["expires_at"]) if record else None
        if (not record or (record["expires_at"] is not None
                           and (expires_at is None or expires_at < now))):
            raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        try:
            scopes = json.loads(record["scopes"])
            restrictions = json.loads(record["restrictions"])
        except (TypeError, ValueError):
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "API key scope is invalid.") from None
        if not isinstance(scopes, list) or scope not in scopes:
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", f"API key scope {scope} is required.")
        if not isinstance(restrictions, dict) or restrictions.get("kind") == "audit_bundle":
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "API key restriction forbids this read.")
        user = await _user(binding, str(record["user_id"]))
        if user is None:
            raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        return user, restrictions
    if session_ids:
        row = await binding.prepare("SELECT payload FROM app_state WHERE name='sessions'").first()
        sessions = json.loads(row["payload"]) if row else None
        if isinstance(sessions, dict):
            for session_id in session_ids:
                session = sessions.get(session_id)
                if not isinstance(session, dict):
                    continue
                expires_at = _timestamp(session.get("expiresAt"))
                owner_id = session.get("userId")
                if expires_at is None or expires_at < now or not isinstance(owner_id, str):
                    continue
                user = await _user(binding, owner_id)
                if (user is not None and not (
                        "github" in (user.get("providers") or [])
                        and not user.get("githubAccessToken"))):
                    return user, {}
    raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")


def _usage_statements(binding: Any, user: dict, now: int):
    entitlement = entitlements_for_user(user, timestamp=now)
    owner_id, period = user["id"], entitlement["period"]
    statements = [binding.prepare("""SELECT used,reserved,limit_value
        FROM processing_usage_buckets WHERE billing_owner_id=? AND period=?
        AND metric='intelligent_processing'""").bind(owner_id, period),
        binding.prepare("""SELECT module,COUNT(*) AS count
        FROM processing_usage_ledger WHERE billing_owner_id=? AND period=?
        AND state='consumed' GROUP BY module""").bind(owner_id, period),
        binding.prepare("""SELECT COUNT(*) AS count FROM provider_attempts
        WHERE billing_owner_id=? AND occurred_at>=? AND occurred_at<?""").bind(
            owner_id, period_start_for_key(period, entitlement["resetAt"]),
            entitlement["resetAt"])]
    return period, statements


def _usage_from_results(user: dict, period: str, parts: list, now: int) -> dict:
    bucket_rows, counts, attempt_rows = (part.results for part in parts)
    bucket = bucket_rows[0] if bucket_rows else None
    by_module = {"pr": 0, "ci": 0, "updates": 0}
    for row in counts:
        if row["module"] in by_module:
            by_module[row["module"]] = int(row["count"])
    attempts = attempt_rows[0] if attempt_rows else None
    usage = {"metric": "intelligent_processing", "period": period,
        "used": int(bucket["used"]) if bucket else 0,
        "reserved": int(bucket["reserved"]) if bucket else 0,
        "limit": int(bucket["limit_value"]) if bucket else 0,
        "byModule": by_module}
    return product_usage_payload_from_usage(user, usage,
        int(attempts["count"]) if attempts else 0, timestamp=now)


async def _usage(binding: Any, user: dict, restrictions: dict,
                 headers: Mapping[str, object], *, now: int) -> dict:
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "usage:read")
    period, statements = _usage_statements(binding, user, now)
    result = await binding.batch([*auth, *statements])
    validate([part.results for part in result[:len(auth)]])
    return _usage_from_results(user, period, result[len(auth):], now)


async def _watches(binding: Any, user: dict, restrictions: dict,
                   headers: Mapping[str, object], now: int) -> dict:
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "watches:read")
    result = await binding.batch([*auth, binding.prepare("""SELECT * FROM update_watches
        WHERE billing_owner_id=? AND archived_at IS NULL
        ORDER BY created_at,id""").bind(user["id"])])
    validate([part.results for part in result[:len(auth)]])
    items = [watch_dto(row) for row in result[-1].results]
    if "watchIds" in restrictions:
        ids = restrictions["watchIds"]
        allowed = set(ids) if isinstance(ids, list) and all(
            isinstance(value, str) for value in ids) else set()
        items = [item for item in items if item["id"] in allowed]
    elif restrictions:
        items = []
    return {"items": items, "nextCursor": None, "hasMore": False,
            "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}


async def _usage_events(binding: Any, user: dict, restrictions: dict,
                        headers: Mapping[str, object], now: int,
                        params: Mapping[str, object]) -> dict:
    module, position, limit = parse_usage_events_query(params, owner_id=user["id"])
    at, reservation = position if position else (None, None)
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "usage:read")
    result = await binding.batch([*auth, binding.prepare("""SELECT
        reservation_id,module,period,finished_at FROM processing_usage_ledger
        WHERE billing_owner_id=? AND state='consumed' AND finished_at IS NOT NULL
          AND (? IS NULL OR module=?)
          AND (? IS NULL OR finished_at<?
               OR (finished_at=? AND reservation_id<?))
        ORDER BY finished_at DESC,reservation_id DESC LIMIT ?""").bind(
            user["id"], module, module, at, at, at, reservation, limit + 1)])
    validate([part.results for part in result[:len(auth)]])
    page = usage_events_page(result[-1].results, limit,
        owner_id=user["id"], module=module)
    return {**page, "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}


def _resource_auth_snapshot(binding: Any, headers: Mapping[str, object],
                            user: dict, restrictions: dict, now: int,
                            scope: str | tuple[str, ...], proof: dict | None = None):
    bearer = _bearer(headers)
    header_key = _header(headers, "X-Pullwise-Api-Key")
    token = bearer if bearer.startswith(API_KEY_PREFIX) else header_key
    sessions = ([bearer] if bearer and not bearer.startswith(API_KEY_PREFIX)
                else _cookie_sessions(headers))
    owner_id = user["id"]
    statements = [
        binding.prepare("""SELECT user_id,scopes,expires_at,restrictions,revoked_at
            FROM api_keys WHERE key_hash=?""").bind(
                hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""),
        binding.prepare("SELECT payload FROM app_state WHERE name='sessions'"),
        binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id),
    ]

    def validate(rows: list) -> None:
        key_rows, session_rows, user_rows = rows
        saved_user = json.loads(user_rows[0]["snapshot"]) if len(user_rows) == 1 else None
        if saved_user != user:
            raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        if token:
            record = key_rows[0] if len(key_rows) == 1 else None
            expiry = _timestamp(record["expires_at"]) if record else None
            if (not record or record["revoked_at"] is not None
                    or record["user_id"] != owner_id
                    or (record["expires_at"] is not None and (expiry is None or expiry < now))):
                raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
            try:
                scopes = json.loads(record["scopes"])
                current_restrictions = json.loads(record["restrictions"])
            except (TypeError, ValueError):
                scopes, current_restrictions = None, None
            required_scopes = (scope,) if isinstance(scope, str) else scope
            if (not isinstance(scopes, list) or any(value not in scopes for value in required_scopes)
                    or current_restrictions != restrictions
                    or not isinstance(current_restrictions, dict)
                    or current_restrictions.get("kind") == "audit_bundle"):
                raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "API key scope is invalid.")
            if proof is not None:
                proof.update(key=record, sessions=None, user=user_rows[0]["snapshot"], token=token)
            return
        saved_sessions = json.loads(session_rows[0]["payload"]) if len(session_rows) == 1 else None
        if isinstance(saved_sessions, dict):
            for session_id in sessions:
                session = saved_sessions.get(session_id)
                if (isinstance(session, dict) and session.get("userId") == owner_id
                        and (expiry := _timestamp(session.get("expiresAt"))) is not None
                        and expiry >= now):
                    if proof is not None:
                        proof.update(key=None, sessions=session_rows[0]["payload"],
                                     user=user_rows[0]["snapshot"], token=None)
                    return
        raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")

    return statements, validate


async def _overview(binding: Any, user: dict, restrictions: dict,
                    headers: Mapping[str, object], now: int,
                    params: Mapping[str, object]) -> tuple[int, dict]:
    owner_id = user["id"]
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "items:read")
    source_reader, item_reader = D1SourceReads(binding), D1ItemReads(binding)
    snapshot = await binding.batch([
        *auth, *source_reader.statements(owner_id=owner_id, now=now),
        *item_reader.statements(owner_id=owner_id, now=now),
    ])
    validate([part.results for part in snapshot[:len(auth)]])
    sources = source_reader.project_snapshot(snapshot[len(auth):len(auth) + 4],
        owner_id=owner_id, now=now)
    items = item_reader.project_snapshot(snapshot[len(auth) + 4:],
        owner_id=owner_id, now=now)
    sources = filter_sources(apply_source_restrictions(sources, restrictions), params)
    items = apply_item_restrictions(items, restrictions)
    github_id = str(user.get("githubId") or "")
    from .product_item_filters import item_in_view
    resource_items = filter_items(items, params, github_id, include_view=False)
    selected = filter_items(items, params, github_id, include_view=True)

    def query(name: str) -> str:
        value = params.get(name)
        return str((value[0] if value else "") if isinstance(value, list)
                   else (value or "")).strip()

    statuses = {context.get("processingStatus") for source in sources
                for context in source.get("contexts") or []}
    return 200, {
        "scope": {"module": query("module") or None,
                  "repositoryId": query("repositoryId") or None,
                  "watchId": query("watchId") or None,
                  "view": query("view") or "all"},
        "totalCount": len(selected),
        "counts": {state: sum(item.get("attentionState") == state for item in selected)
                   for state in ("needs_action", "needs_confirmation", "waiting", "optional", "closed")},
        "viewCounts": {view: sum(item_in_view(item, view, github_id) for item in resource_items)
                       for view in ("mine", "unassigned", "waiting", "all")},
        "sourceCoverage": {"unit": "source_context",
            "total": sum(len(source.get("contexts") or []) for source in sources),
            "processingStatus": {status: sum(context.get("processingStatus") == status
                for source in sources for context in source.get("contexts") or [])
                for status in statuses if status}},
        "lastSyncedAt": max((source["lastSyncedAt"] for source in sources), default=None),
        "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}",
    }


async def read_product(*, binding: Any, path: str, headers: Mapping[str, object],
                       now: int, params: Mapping[str, object] | None = None) -> tuple[int, dict]:
    source_path = path == "/api/v1/sources" or path.startswith("/api/v1/sources/")
    item_path = path == "/api/v1/items" or path.startswith("/api/v1/items/")
    watch_path = path.startswith("/api/v1/watches/")
    job_path = path.startswith("/api/v1/jobs/")
    if path not in {"/api/v1/me", "/api/v1/usage", "/api/v1/usage/events", "/api/v1/watches"} and not source_path and not item_path and not job_path and not watch_path:
        return 404, {"error": {"code": "NOT_FOUND"}}
    scope = ("profile:read" if path.endswith("/me") else "usage:read"
             if path in {"/api/v1/usage", "/api/v1/usage/events"}
             else "items:read" if source_path or item_path or job_path else "watches:read")
    try:
        user, restrictions = await _principal(binding, headers, scope=scope, now=now)
    except ProductReadAuthError as error:
        return error.status, {"error": {"code": error.code,
            "message": error.message, "retryable": False},
            "requestId": f"req_{uuid.uuid4().hex}"}
    if path.endswith("/me"):
        auth, validate = _resource_auth_snapshot(binding, headers, user,
            restrictions, now, "profile:read")
        result = await binding.batch(auth)
        try:
            validate([part.results for part in result])
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        return 200, {"id": user["id"], "name": user.get("name") or "",
                     "email": user.get("email") or "",
                     "modules": ["pr", "ci", "updates"]}
    if path == "/api/v1/usage/events":
        try:
            return 200, await _usage_events(binding, user, restrictions,
                headers, now, params or {})
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        except ValueError as error:
            code = str(error) if str(error) in {"INVALID_CONFIGURATION", "INVALID_CURSOR"} else "INVALID_CONFIGURATION"
            return 422, {"error": {"code": code,
                "message": "Invalid usage event filters.", "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
    if path.endswith("/watches"):
        try:
            return 200, await _watches(binding, user, restrictions, headers, now)
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
    if watch_path:
        watch_id = path[len("/api/v1/watches/"):]
        if not watch_id or "/" in watch_id:
            return 404, {"error": {"code": "NOT_FOUND"}}
        try:
            watches = await _watches(binding, user, restrictions, headers, now)
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        watch = next((entry for entry in watches["items"] if entry["id"] == watch_id), None)
        return (200, watch) if watch else (404, {"error": {"code": "NOT_FOUND"}})
    if job_path:
        job_id = path[len("/api/v1/jobs/"):]
        if not job_id or "/" in job_id:
            return 404, {"error": {"code": "NOT_FOUND"}}
        auth, validate = _resource_auth_snapshot(binding, headers, user,
            restrictions, now, "items:read")
        result = await binding.batch([*auth, binding.prepare("""SELECT id,job_type,
            state,attempt,requester_id,
            CASE WHEN job_type='sync_watch' THEN EXISTS(
                SELECT 1 FROM update_watches w WHERE w.id=substr(logical_key,length('sync_watch:')+1)
                  AND w.billing_owner_id=? AND w.archived_at IS NULL)
            WHEN job_type='sync_repository' THEN EXISTS(
                SELECT 1 FROM repository_services s
                WHERE s.repository_id=substr(logical_key,length('sync_repository:')+1)
                  AND s.billing_owner_id=? AND s.status='active')
            ELSE 0 END AS resource_access
            FROM background_jobs WHERE id=?""").bind(user["id"], user["id"], job_id)])
        try:
            validate([part.results for part in result[:len(auth)]])
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        rows = result[-1].results
        job = rows[0] if len(rows) == 1 else None
        if (job is None or job["requester_id"] != user["id"] or not job["resource_access"]
                or job["job_type"] not in {"sync_repository", "sync_watch"}):
            return 404, {"error": {"code": "NOT_FOUND", "message": "Sync job was not found.",
                "retryable": False}, "requestId": f"req_{uuid.uuid4().hex}"}
        return 200, {"id": job["id"], "operation": job["job_type"],
            "status": job["state"], "attempt": int(job["attempt"]),
            "links": {"self": f"/api/v1/jobs/{job['id']}"},
            "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}
    if source_path:
        source_id = path[len("/api/v1/sources/"):] if path != "/api/v1/sources" else None
        if source_id is not None and (not source_id or "/" in source_id):
            return 404, {"error": {"code": "NOT_FOUND"}}
        auth_statements, validate_auth = _resource_auth_snapshot(
            binding, headers, user, restrictions, now, "items:read")
        try:
            sources = await D1SourceReads(binding).list_sources_for_billing_owner(
                owner_id=user["id"], now=now, source_id=source_id,
                include_content=source_id is not None,
                auth_statements=auth_statements, validate_auth=validate_auth)
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        try:
            sources = filter_sources(apply_source_restrictions(sources, restrictions), params or {})
        except ValueError:
            return 422, {"error": {"code": "INVALID_CONFIGURATION",
                "message": "Invalid source filters.", "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        if source_id is not None:
            return (200, sources[0]) if sources else (404, {"error": {"code": "NOT_FOUND"}})
        return 200, {"items": sources, "nextCursor": None, "hasMore": False,
            "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}
    if path == "/api/v1/items/overview":
        try:
            return await _overview(binding, user, restrictions, headers, now, params or {})
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        except ValueError:
            return 422, {"error": {"code": "INVALID_CONFIGURATION",
                "message": "Invalid overview filters.", "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
    if item_path:
        item_id = path[len("/api/v1/items/"):] if path != "/api/v1/items" else None
        if item_id is not None and (not item_id or "/" in item_id or item_id == "overview"):
            return 404, {"error": {"code": "NOT_FOUND"}}
        auth_statements, validate_auth = _resource_auth_snapshot(
            binding, headers, user, restrictions, now, "items:read")
        try:
            items = await D1ItemReads(binding).list_items_for_billing_owner(
                owner_id=user["id"], now=now, item_id=item_id,
                include_history=item_id is not None,
                auth_statements=auth_statements, validate_auth=validate_auth)
        except ProductReadAuthError as error:
            return error.status, {"error": {"code": error.code,
                "message": error.message, "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        try:
            items = filter_items(apply_item_restrictions(items, restrictions),
                params or {}, str(user.get("githubId") or ""), include_view=item_id is None)
        except ValueError as error:
            code = str(error) if str(error) in {"INVALID_VIEW", "INVALID_CONFIGURATION"} else "INVALID_CONFIGURATION"
            return 422, {"error": {"code": code,
                "message": "Invalid Item filters.", "retryable": False},
                "requestId": f"req_{uuid.uuid4().hex}"}
        if item_id is not None:
            return (200, items[0]) if items else (404, {"error": {"code": "NOT_FOUND"}})
        return 200, {"items": items, "nextCursor": None, "hasMore": False,
            "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}
    try:
        return 200, await _usage(binding, user, restrictions, headers, now=now)
    except ProductReadAuthError as error:
        return error.status, {"error": {"code": error.code,
            "message": error.message, "retryable": False},
            "requestId": f"req_{uuid.uuid4().hex}"}


async def patch_item(*, binding: Any, item_id: str, headers: Mapping[str, object],
                     body: object, now: int) -> tuple[int, dict]:
    request_id = _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"

    def error(status: int, code: str, message: str) -> tuple[int, dict]:
        return status, {"error": {"code": code, "message": message,
                                  "retryable": False}, "requestId": request_id}

    try:
        user, restrictions = await _principal(binding, headers, scope="items:write", now=now)
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    if not isinstance(body, dict):
        return error(400, "INVALID_REQUEST", "Request body must be an object.")
    if set(body) - {"itemVersion", "disposition", "assigneeId", "note", "feedback"} or "itemVersion" not in body:
        return error(400, "INVALID_REQUEST", "Invalid Item handling fields.")
    raw_revision = _header(headers, "If-Match")
    if not raw_revision:
        return error(428, "PRECONDITION_REQUIRED", "If-Match is required.")
    revision_text = raw_revision.strip().strip('"')
    if not revision_text.isdigit() or int(revision_text) < 1:
        return error(400, "INVALID_REQUEST", "If-Match must contain a valid revision")
    expected_revision = int(revision_text)
    if type(body["itemVersion"]) is not int or body["itemVersion"] < 1:
        return error(400, "INVALID_REQUEST", "item_version must be a positive integer")
    proof: dict = {}
    auth_statements, validate_auth = _resource_auth_snapshot(
        binding, headers, user, restrictions, now, ("items:read", "items:write"), proof)
    try:
        items = await D1ItemReads(binding).list_items_for_billing_owner(
            owner_id=user["id"], now=now, item_id=item_id, include_history=True,
            auth_statements=auth_statements, validate_auth=validate_auth)
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    items = apply_item_restrictions(items, restrictions)
    if not items:
        return error(404, "NOT_FOUND", "Item was not found.")
    item = items[0]
    if item["itemVersion"] != body["itemVersion"]:
        return error(409, "STALE_ITEM", "Item version is stale.")
    if item["revision"] != expected_revision:
        return error(412, "REVISION_MISMATCH", "Item revision is stale.")
    try:
        await D1ItemHandling(binding).patch(item=item, owner_id=user["id"],
            body=body, expected_revision=expected_revision, now=now, proof=proof)
    except ValueError as failure:
        return error(400, "INVALID_REQUEST", str(failure))
    except Exception:
        return error(412, "REVISION_MISMATCH", "Item or authority changed.")
    return await read_product(binding=binding, path=f"/api/v1/items/{item_id}",
                              headers=headers, now=now)


async def patch_watch(*, binding: Any, watch_id: str,
                      headers: Mapping[str, object], body: object,
                      now: int) -> tuple[int, dict]:
    request_id = _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"

    def error(status: int, code: str, message: str) -> tuple[int, dict]:
        return status, {"error": {"code": code, "message": message,
                                  "retryable": False}, "requestId": request_id}

    try:
        user, restrictions = await _principal(binding, headers, scope="watches:write", now=now)
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    if (not isinstance(body, dict) or not body
            or set(body) - {"interests", "enabled", "analysisEnabled",
                            "includePrerelease", "priorityOrder"}):
        return error(400, "INVALID_REQUEST", "Invalid watch fields.")
    raw_revision = _header(headers, "If-Match")
    if not raw_revision:
        return error(428, "PRECONDITION_REQUIRED", "If-Match is required.")
    revision_text = raw_revision.strip().strip('"')
    if not revision_text.isdigit() or int(revision_text) < 1:
        return error(400, "INVALID_REQUEST", "If-Match must contain a valid revision")
    expected_revision = int(revision_text)
    proof: dict = {}
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "watches:write", proof)
    result = await binding.batch([*auth, binding.prepare("""SELECT id,revision,
        target_repository_id FROM update_watches WHERE id=?
        AND billing_owner_id=? AND archived_at IS NULL""").bind(watch_id, user["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    rows = result[-1].results
    watch = rows[0] if len(rows) == 1 else None
    allowed_ids = restrictions.get("watchIds") if restrictions else None
    if (watch is None or watch["target_repository_id"] is not None
            or (restrictions and (not isinstance(allowed_ids, list)
                or watch_id not in allowed_ids))):
        return error(404, "NOT_FOUND", "Watch was not found.")
    if int(watch["revision"]) != expected_revision:
        return error(412, "REVISION_MISMATCH", "Watch revision is stale.")
    try:
        updated = await D1WatchTransactions(binding).update_public_watch(
            owner_id=user["id"], watch_id=watch_id,
            expected_revision=expected_revision, changes=body, now=now, proof=proof)
    except ValueError as failure:
        code = str(failure)
        if code == "WATCH_LIMIT_REACHED":
            return error(402, code, "Active watch limit reached.")
        return error(400, "INVALID_REQUEST", code)
    except Exception:
        return error(412, "REVISION_MISMATCH", "Watch or authority changed.")
    return 200, updated


async def delete_watch(*, binding: Any, watch_id: str,
                       headers: Mapping[str, object], now: int) -> tuple[int, dict]:
    request_id = _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"

    def error(status: int, code: str, message: str) -> tuple[int, dict]:
        return status, {"error": {"code": code, "message": message,
                                  "retryable": False}, "requestId": request_id}

    try:
        user, restrictions = await _principal(binding, headers, scope="watches:write", now=now)
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    raw_revision = _header(headers, "If-Match")
    if not raw_revision:
        return error(428, "PRECONDITION_REQUIRED", "If-Match is required.")
    revision_text = raw_revision.strip().strip('"')
    if not revision_text.isdigit() or int(revision_text) < 1:
        return error(400, "INVALID_REQUEST", "If-Match must contain a valid revision")
    expected_revision = int(revision_text)
    proof: dict = {}
    auth, validate = _resource_auth_snapshot(binding, headers, user,
        restrictions, now, "watches:write", proof)
    result = await binding.batch([*auth, binding.prepare("""SELECT id,revision,
        target_repository_id FROM update_watches WHERE id=?
        AND billing_owner_id=? AND archived_at IS NULL""").bind(watch_id, user["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError as failure:
        return error(failure.status, failure.code, failure.message)
    rows = result[-1].results
    watch = rows[0] if len(rows) == 1 else None
    allowed_ids = restrictions.get("watchIds") if restrictions else None
    if (watch is None or watch["target_repository_id"] is not None
            or (restrictions and (not isinstance(allowed_ids, list)
                or watch_id not in allowed_ids))):
        return error(404, "NOT_FOUND", "Watch was not found.")
    if int(watch["revision"]) != expected_revision:
        return error(412, "REVISION_MISMATCH", "Watch revision is stale.")
    try:
        await D1WatchTransactions(binding).archive_watch(owner_id=user["id"],
            watch_id=watch_id, expected_revision=expected_revision,
            now=now, proof=proof)
    except ValueError as failure:
        return error(400, "INVALID_REQUEST", str(failure))
    except Exception:
        return error(412, "REVISION_MISMATCH", "Watch or authority changed.")
    return 204, {}
