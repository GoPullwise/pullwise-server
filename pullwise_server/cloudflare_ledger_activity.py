"""Immutable, target-scoped operation history for the latest server-clock day."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import re
import uuid
from datetime import datetime, timezone

from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_principal import PrincipalAuthError, _bearer, _header

RESOURCE = "/api/v1/activity"
WINDOW_SECONDS = 86400
RETENTION_PAGE = 16
_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
_FIELDS = {
    "expense": ("target", "purpose", "amount", "currency", "occurredOn", "categoryId", "note", "quantity", "unit"),
    "project": ("target", "name", "description", "developmentUrl", "productUrl", "status", "githubRepoIds", "githubOrganizationId"),
    "recurring_rule": ("target", "purpose", "amount", "currency", "categoryId", "note", "quantity", "unit", "schedule", "status"),
}


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value, maximum=16384):
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(result.encode("utf-8")) > maximum:
        raise ValueError("activity snapshot size")
    return result


def _actor(user, proof=None, *, scheduled=False):
    actor = user.get("_actor", user)
    # Email-only accounts use their chosen display name. Private email, access
    # tokens, API-key hashes and the full stored identity are never retained.
    return {"kind": "system" if scheduled else "api_key" if (proof or {}).get("key") is not None else "user",
            "userId": actor["id"], "name": str(actor.get("name") or "")[:120] or None,
            "githubLogin": str(actor.get("githubLogin") or "")[:39] or None}


def project_snapshot(row, bindings):
    return {"target": {"kind": "project", "projectId": row["id"]},
            "name": row.get("name", ""), "description": row.get("description", ""),
            "developmentUrl": row.get("development_url"), "productUrl": row.get("product_url"),
            "status": row["status"],
            "githubRepoIds": sorted({item["github_repo_id"] for item in bindings}),
            "githubOrganizationId": row.get("github_organization_id")}


async def _enrich(binding, owner, snapshots):
    categories = sorted({item.get("categoryId") for item in snapshots if item.get("categoryId")})
    projects = sorted({item["target"].get("projectId") for item in snapshots
                       if item.get("target", {}).get("kind") == "project"})
    commands = []
    if categories:
        commands.append(binding.prepare("SELECT id,name FROM expense_categories WHERE owner_id=? AND id IN (" +
            ",".join("?" for _ in categories) + ") LIMIT 2").bind(owner, *categories))
    if projects:
        commands.append(binding.prepare("SELECT id,name FROM ledger_projects WHERE owner_id=? AND id IN (" +
            ",".join("?" for _ in projects) + ") LIMIT 2").bind(owner, *projects))
    if not commands:
        return
    parts = await binding.batch(commands)
    names = {row["id"]: row["name"] for row in parts[0].results} if categories else {}
    project_names = {row["id"]: row["name"] for row in parts[-1].results} if projects else {}
    for item in snapshots:
        if item.get("categoryId"):
            item["categoryName"] = names.get(item["categoryId"])
        target = item.get("target", {})
        if target.get("kind") == "project":
            # A repository's protected full_name is deliberately not a fallback.
            target["projectName"] = project_names.get(target["projectId"]) or None


async def activity_commands(binding, user, resource_kind, resource_id, action, before, after,
                            now, *, proof=None, scheduled=False, operation_id=None):
    """Prepare history and finite retention companions for the original CAS batch.

    One operation touching two pools publishes one row in each affected pool.
    It never creates another commercial write or a separate commit. No-op
    revisions and exact idempotency replays publish no operation.
    """
    selected = [({field: deepcopy(value[field]) for field in _FIELDS[resource_kind] if field in value}
                 if value is not None else None) for value in (before, after)]
    if selected[0] == selected[1]:
        return []
    snapshots = [item for item in selected if item is not None]
    await _enrich(binding, user["id"], snapshots)
    scopes = {(item["target"]["kind"], item["target"].get("projectId")) for item in snapshots}
    operation_id = operation_id or "op_" + uuid.uuid4().hex
    actor_json = _json(_actor(user, proof, scheduled=scheduled), 2048)
    before_json, after_json = (_json(item) if item is not None else None for item in selected)
    commands = [binding.prepare("""INSERT INTO ledger_activity_events
        (id,operation_id,owner_id,target_kind,project_id,actor_json,resource_kind,
         resource_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""").bind(
            "act_" + uuid.uuid4().hex, operation_id, user["id"], target, project,
            actor_json, resource_kind, resource_id, action, before_json, after_json, _stamp(now))
        for target, project in sorted(scopes, key=lambda item: (item[0], item[1] or ""))]
    commands.extend(await retention_commands(binding, now))
    return commands


async def retention_commands(binding, now):
    """Bounded indexed retirement; callers commit inside a write/maintenance ticket."""
    expired = (await binding.batch([binding.prepare("""SELECT id FROM ledger_activity_events
        WHERE created_at<=? ORDER BY created_at,id LIMIT 16""").bind(_stamp(now - WINDOW_SECONDS))]))[0]
    return [binding.prepare("DELETE FROM ledger_activity_events WHERE id=?").bind(row["id"])
            for row in expired.results]


def _inputs(params, now):
    def value(name):
        item = params.get(name, "")
        if isinstance(item, list):
            item = item[-1] if item else ""
        if not isinstance(item, str):
            raise ValueError("activity parameter")
        return item
    target, project, raw_limit, raw_cursor = (value(key) for key in ("target", "projectId", "limit", "cursor"))
    if (target not in {"project", "shared"} or target == "project" and not _ID.fullmatch(project)
            or target == "shared" and project):
        raise ValueError("activity target")
    if raw_limit and (len(raw_limit) > 3 or not raw_limit.isascii() or not raw_limit.isdigit()):
        raise ValueError("activity limit")
    limit = int(raw_limit) if raw_limit else 50
    if not 1 <= limit <= 100:
        raise ValueError("activity limit")
    cursor = None
    if raw_cursor:
        if len(raw_cursor) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw_cursor):
            raise ValueError("activity cursor")
        try:
            decoded = base64.urlsafe_b64decode(raw_cursor + "=" * (-len(raw_cursor) % 4)).decode("ascii")
            anchor, timestamp, identifier = decoded.split("|", 2)
            timestamps = (anchor, timestamp)
            if (not all(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", item)
                        and datetime.fromisoformat(item.replace("Z", "+00:00")).isoformat().replace("+00:00", "Z") == item
                        for item in timestamps)
                    or not _ID.fullmatch(identifier) or timestamp > anchor or anchor > _stamp(now)):
                raise ValueError("activity cursor")
            cursor = (anchor, timestamp, identifier)
        except (ValueError, UnicodeError):
            raise ValueError("activity cursor") from None
    return target, project or None, limit, cursor


def _cursor(row, anchor):
    return base64.urlsafe_b64encode((anchor + "|" + row["created_at"] + "|" + row["id"]).encode("ascii")).decode().rstrip("=")


def _visible(value, restrictions):
    if isinstance(value, dict) and value.get("kind") in {"project", "shared"}:
        return value if target_allowed(restrictions, value["kind"], value.get("projectId")) else {"kind": "restricted"}
    return value


def _dto(row, restrictions):
    before, after = (json.loads(row[field]) if row[field] is not None else {} for field in ("before_json", "after_json"))
    restricted = [bool(item) and not target_allowed(restrictions, item.get("target", {}).get("kind"),
                  item.get("target", {}).get("projectId")) for item in (before, after)]
    snapshot = before if restricted[1] else after or before
    # A project-limited expense key can see that a known record moved out of
    # its scope. The simultaneous opposite-side edits are not a new grant to
    # read another pool's purpose, note, categories, money or settings.
    fields = ("target",) if any(restricted) else _FIELDS[row["resource_kind"]]
    changes = []
    for field in fields:
        old, new = before.get(field), after.get(field)
        if old == new and not (field == "amount" and before.get("currency") != after.get("currency")):
            continue
        if field == "amount":
            old = {"amount": old, "currency": before.get("currency")} if old is not None else None
            new = {"amount": new, "currency": after.get("currency")} if new is not None else None
        if field == "categoryId":
            old = {"id": old, "name": before.get("categoryName")} if old else None
            new = {"id": new, "name": after.get("categoryName")} if new else None
        changes.append({"field": field, "before": _visible(old, restrictions), "after": _visible(new, restrictions)})
    return {"id": row["id"], "operationId": row["operation_id"], "createdAt": row["created_at"],
            "actor": json.loads(row["actor_json"]),
            "resource": {"kind": row["resource_kind"], "id": row["resource_id"],
                         "label": snapshot.get("purpose") or snapshot.get("name") or row["resource_id"]},
            "action": row["action"], "target": {"kind": row["target_kind"],
                **({"projectId": row["project_id"]} if row["project_id"] else {})}, "changes": changes}


async def handle_activity_request(*, binding, gateway, method, path, headers, params, now):
    if path != RESOURCE:
        return None
    if method != "GET":
        return 405, {"error": {"code": "METHOD_NOT_ALLOWED"}}
    try:
        target, project, limit, cursor = _inputs(params, now)
    except ValueError:
        return 422, {"error": {"code": "INVALID_INPUT"}}
    try:
        proof = {}
        user, restrictions, auth, validate = await ledger_principal(binding=binding, headers=headers,
            scope="expenses:read", now=now, target_kind=target, project_id=project, proof=proof)
        # The authoritative proof is finalized only after the read batch. This
        # selector is derived from the same already authenticated credential.
        key_only = _bearer(headers).startswith("pwk_") or bool(_header(headers, "X-Pullwise-Api-Key"))
        anchor = cursor[0] if cursor else _stamp(now)
        clauses = ["owner_id=?", "target_kind=?", "project_id IS ?", "created_at>?", "created_at<=?"]
        values = [user["id"], target, project, _stamp(now - WINDOW_SECONDS), anchor]
        if key_only:
            # An expense-read key cannot gain access to session-only recurring
            # settings or project settings through their companion history.
            clauses.append("resource_kind='expense'")
        if cursor:
            clauses.append("(created_at,id)<(?,?)")
            values.extend(cursor[1:])
        query = binding.prepare("SELECT * FROM ledger_activity_events WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC,id DESC LIMIT ?").bind(*values, limit + 1)
        commands = [query]
        if project:
            commands.append(binding.prepare("SELECT id FROM ledger_projects WHERE owner_id=? AND id=? AND deleted_at IS NULL").bind(user["id"], project))
        parts = await binding.batch([*auth, *commands])
        validate([part.results for part in parts[:len(auth)]])
        found = parts[len(auth)].results
        if project:
            project_rows = parts[-1].results
            if not project_rows:
                return 404, {"error": {"code": "NOT_FOUND"}}
        page = found[:limit]
        return 200, {"items": [_dto(row, restrictions) for row in page],
                     "nextCursor": _cursor(page[-1], anchor) if len(found) > limit else None,
                     "windowStart": _stamp(now - WINDOW_SECONDS), "windowEnd": anchor}
    except PrincipalAuthError as error:
        return error.status, {"error": {"code": error.code}}
