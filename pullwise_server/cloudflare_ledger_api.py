"""Owner-scoped project and category REST operations for the ledger Worker."""
from __future__ import annotations

from .cloudflare_plan_limits import PlanLimitError

import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_principal import PrincipalAuthError, _header


def _error(status: int, code: str):
    return status, {"error": {"code": code}}


def _timestamp(now: int) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _param(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[-1] if value else ""
    return value if isinstance(value, str) else ""


def _revision(headers: Mapping[str, object]):
    value = _header(headers, "If-Match")
    if not value:
        return None
    match = re.fullmatch(r'"([1-9][0-9]*)"', value)
    return int(match.group(1)) if match else -1


def _project(row: dict, allowed_repos: dict[int, str], totals: list[dict]):
    repo_id = row["github_repo_id"]
    name = allowed_repos.get(repo_id)
    return {"id": row["id"], "githubRepoId": repo_id,
            "githubFullName": name, "description": row["description"],
            "status": row["status"], "githubAccess": "authorized" if name else "lost",
            "revision": row["revision"], "totals": [{"currency": total["currency"],
                "amountMinor": total.get("amountMinor", total.get("amount_minor"))} for total in totals]}


def _category(row: dict):
    return {"id": row["id"], "name": row["name"], "color": row["color"],
            "revision": row["revision"], "archivedAt": row["archived_at"]}


def _write_guard(binding: Any, proof: dict, owner_id: str, now: int):
    """A failed credential/user fence aborts the D1 transaction."""
    user_check = """EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?)"""
    if proof.get("key") is not None:
        key = proof["key"]
        sql = f"""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
            {user_check} AND EXISTS(SELECT 1 FROM api_keys WHERE key_hash=?
              AND user_id=? AND scopes=? AND restrictions=? AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at>=?)) THEN 1 ELSE 0 END)"""
        token_hash = hashlib.sha256(proof["token"].encode()).hexdigest()
        return binding.prepare(sql).bind(owner_id, proof["user"], token_hash, owner_id,
            key["scopes"], key["restrictions"], now)
    sql = f"""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        {user_check} AND EXISTS(SELECT 1 FROM app_state WHERE name='sessions'
          AND payload=? AND json_extract(payload, ?) = ?
          AND CAST(json_extract(payload, ?) AS INTEGER)>=?)
        THEN 1 ELSE 0 END)"""
    session_id = proof["session_id"]
    path = '$."' + session_id.replace('"', '\\"') + '"'
    return binding.prepare(sql).bind(owner_id, proof["user"], proof["sessions"],
        path + ".userId", owner_id, path + ".expiresAt", now)


async def _authorized(binding: Any, headers: Mapping[str, object], scope: str,
                      now: int, commands: list, target_kind=None, project_id=None):
    proof: dict = {}
    user, restrictions, auth, validate = await ledger_principal(
        binding=binding, headers=headers, scope=scope, now=now,
        target_kind=target_kind, project_id=project_id, proof=proof)
    parts = await binding.batch([*auth, *commands])
    validate([part.results for part in parts[:len(auth)]])
    return user, restrictions, proof, [part.results for part in parts[len(auth):]]


async def _live_repos(user: dict, gateway: Any) -> dict[int, str]:
    access = user.get("githubRepositoryAccess")
    if not isinstance(access, dict) or access.get("status") != "authorized":
        return {}
    try:
        token = await gateway.unseal(user["githubAccessToken"])
        installation_id = int(access["installationId"])
        installations = await gateway.installations(token)
        if not isinstance(installations, list) or not any(
                isinstance(item, dict) and item.get("id") == installation_id
                for item in installations):
            return {}
        rows = await gateway.repositories(token, installation_id)
        if not isinstance(rows, list) or len(rows) > 1000:
            return {}
        result = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                return {}
            name = row.get("full_name")
            if not isinstance(name, str) or "/" not in name:
                return {}
            result[row["id"]] = name[:300]
        return result
    except Exception:
        return {}


async def handle_ledger_request(*, binding: Any, gateway: Any, method: str, path: str,
                                headers: Mapping[str, object], params: Mapping[str, object],
                                body: object, now: int, suggestion_gateway=None) -> tuple[int, object] | None:
    """Return None for non-ledger paths; structured status and payload otherwise."""
    if path.startswith("/api/v1/expense-suggestions/") and path.endswith("/decision"):
        from .cloudflare_ledger_suggestions import handle_suggestion_decision
        return await handle_suggestion_decision(binding=binding, method=method, path=path,
            headers=headers, body=body, now=now)
    if path == "/api/v1/expense-suggestions":
        from .cloudflare_ledger_suggestions import handle_suggestion_request
        return await handle_suggestion_request(binding=binding, method=method, headers=headers,
            body=body, now=now, gateway=suggestion_gateway)
    if path.startswith("/api/v1/reports/") or path == "/api/v1/expenses/export":
        from .cloudflare_ledger_reports import handle_report_request
        return await handle_report_request(binding=binding, method=method, path=path,
            headers=headers, params=params, now=now)
    if path.startswith("/api/v1/expenses"):
        from .cloudflare_ledger_expenses import handle_expense_request
        return await handle_expense_request(binding=binding, gateway=gateway,
            method=method, path=path, headers=headers, params=params, body=body, now=now)
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[:2] != ["api", "v1"] or parts[2] not in {"projects", "categories"}:
        return None
    kind = parts[2]
    if len(parts) > 4 or (len(parts) == 4 and not parts[3]):
        return _error(404, "NOT_FOUND")
    item_id = parts[3] if len(parts) == 4 else None
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        return _error(405, "METHOD_NOT_ALLOWED")
    if kind == "projects" and method == "DELETE":
        return _error(405, "METHOD_NOT_ALLOWED")
    if method in {"POST", "PATCH", "DELETE"} and (
            (method == "POST" and item_id) or (method in {"PATCH", "DELETE"} and not item_id)):
        return _error(404, "NOT_FOUND")
    scope = kind + (":read" if method == "GET" else ":write")
    try:
        if kind == "projects":
            return await _projects(binding, gateway, method, item_id, headers, params, body, now, scope)
        return await _categories(binding, method, item_id, headers, body, now, scope)
    except PrincipalAuthError as exc:
        return _error(exc.status, exc.code)


async def _projects(binding, gateway, method, item_id, headers, params, body, now, scope):
    if method == "POST":
        if (not isinstance(body, dict) or set(body) - {"githubRepoId", "description"}
                or type(body.get("githubRepoId")) is not int or body["githubRepoId"] <= 0
                or not isinstance(body.get("description", ""), str)
                or len(body.get("description", "")) > 2000):
            return _error(422, "INVALID_INPUT")
    elif method == "PATCH":
        if (not isinstance(body, dict) or not body or set(body) - {"description", "status"}
                or ("description" in body and (not isinstance(body["description"], str)
                    or len(body["description"]) > 2000))
                or ("status" in body and body["status"] not in {"active", "archived"})):
            return _error(422, "INVALID_INPUT")
    if method == "GET":
        if item_id:
            user, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
                scope=scope, now=now, target_kind="project", project_id=item_id)
            commands = [binding.prepare("SELECT * FROM ledger_projects WHERE owner_id=? AND id=?").bind(user["id"], item_id),
                binding.prepare("""SELECT currency,SUM(amount_minor) amount_minor FROM expenses
                    WHERE owner_id=? AND project_id=? AND deleted_at IS NULL GROUP BY currency""").bind(user["id"], item_id)]
            rows = await binding.batch([*auth, *commands])
            validate([part.results for part in rows[:len(auth)]])
            project_rows, totals = [part.results for part in rows[len(auth):]]
            if not project_rows:
                return _error(404, "NOT_FOUND")
            return 200, _project(project_rows[0], await _live_repos(user, gateway), totals)
        limit_text = _param(params, "limit")
        limit = int(limit_text) if limit_text.isdigit() else 50 if not limit_text else 0
        if not 1 <= limit <= 100:
            return _error(422, "INVALID_INPUT")
        cursor = _param(params, "cursor")
        start, end = _param(params, "from"), _param(params, "to")
        try:
            for value in (start, end):
                if value and (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
                              or date.fromisoformat(value).isoformat() != value):
                    raise ValueError("date")
            if start and end and start >= end:
                raise ValueError("range")
        except ValueError:
            return _error(422, "INVALID_INPUT")
        user, restrictions, auth, validate = await ledger_principal(
            binding=binding, headers=headers, scope=scope, now=now)
        project_ids = restrictions.get("projectIds")
        visible = "" if project_ids is None else (
            " AND id IN (" + ",".join("?" for _ in project_ids) + ")" if project_ids else " AND 0")
        date_where = (" AND occurred_on>=?" if start else "") + (" AND occurred_on<?" if end else "")
        date_values = ([start] if start else []) + ([end] if end else [])
        commands = [binding.prepare("SELECT * FROM ledger_projects WHERE owner_id=? AND id>?" +
            visible + " ORDER BY id LIMIT ?").bind(user["id"], cursor,
                *(project_ids or []), limit + 1),
            binding.prepare("""SELECT project_id,currency,SUM(amount_minor) amount_minor FROM expenses
            WHERE owner_id=? AND deleted_at IS NULL""" + date_where +
                " GROUP BY project_id,currency").bind(user["id"], *date_values)]
        rows = await binding.batch([*auth, *commands])
        validate([part.results for part in rows[:len(auth)]])
        projects, totals = [part.results for part in rows[len(auth):]]
        page = projects[:limit]
        by_project = {}
        for total in totals:
            by_project.setdefault(total["project_id"], []).append({
                "currency": total["currency"], "amountMinor": total["amount_minor"]})
        repos = await _live_repos(user, gateway)
        return 200, {"items": [_project(row, repos, by_project.get(row["id"], [])) for row in page],
                     "nextCursor": page[-1]["id"] if len(projects) > limit else None}
    user, restrictions, proof, rows = await _authorized(binding, headers, scope, now,
        [binding.prepare("SELECT * FROM ledger_projects WHERE id=?").bind(item_id or "")],
        "project" if item_id else None, item_id)
    existing = rows[0][0] if rows[0] and rows[0][0]["owner_id"] == user["id"] else None
    if item_id and existing is None:
        return _error(404, "NOT_FOUND")
    if method == "POST":
        repos = await _live_repos(user, gateway)
        repo_id = body["githubRepoId"]
        if repo_id not in repos:
            return _error(403, "GITHUB_ACCESS_REQUIRED")
        project_id = "prj_" + uuid.uuid4().hex
        if not target_allowed(restrictions, "project", project_id):
            return _error(403, "TARGET_FORBIDDEN")
        stamp = _timestamp(now)
        commands = [_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,
                github_full_name,description,status,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,'active',1,?,?)""").bind(project_id, user["id"], repo_id,
                    repos[repo_id], body.get("description", ""), stamp, stamp),
            binding.prepare("DELETE FROM d1_command_guard")]
        try:
            await binding.batch(commands)
        except PlanLimitError as error:
            return error.response()
        except Exception:
            return _error(409, "PROJECT_CONFLICT")
        return 201, _project({"id": project_id, "github_repo_id": repo_id,
            "description": body.get("description", ""), "status": "active", "revision": 1}, repos, [])
    expected = _revision(headers)
    if expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if expected < 0:
        return _error(422, "INVALID_INPUT")
    if expected != existing["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    status = body.get("status", existing["status"])
    if status == "active" and existing["status"] == "archived":
        repos = await _live_repos(user, gateway)
        if existing["github_repo_id"] not in repos:
            return _error(403, "GITHUB_ACCESS_REQUIRED")
    commands = [_write_guard(binding, proof, user["id"], now),
        binding.prepare("""UPDATE ledger_projects SET description=?,status=?,revision=revision+1,
            updated_at=? WHERE id=? AND owner_id=? AND revision=?""").bind(
            body.get("description", existing["description"]), status, _timestamp(now), item_id,
            user["id"], expected),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(412, "PRECONDITION_FAILED")
    updated = {**existing, "description": body.get("description", existing["description"]),
               "status": status, "revision": expected + 1}
    return 200, _project(updated, await _live_repos(user, gateway), [])


async def _categories(binding, method, item_id, headers, body, now, scope):
    if method == "GET" and item_id:
        return _error(405, "METHOD_NOT_ALLOWED")
    if method == "GET":
        user, _, auth, validate = await ledger_principal(
            binding=binding, headers=headers, scope=scope, now=now)
        parts = await binding.batch([*auth, binding.prepare(
            "SELECT * FROM expense_categories WHERE owner_id=? ORDER BY name,id").bind(user["id"])])
        validate([part.results for part in parts[:len(auth)]])
        return 200, [_category(row) for row in parts[-1].results]
    if method in {"POST", "PATCH"}:
        if (not isinstance(body, dict) or set(body) - {"name", "color"}
                or not isinstance(body.get("name"), str) or not 1 <= len(body["name"].strip()) <= 80
                or (body.get("color") is not None and (not isinstance(body["color"], str)
                    or len(body["color"]) > 32))):
            return _error(422, "INVALID_INPUT")
    user, _, proof, rows = await _authorized(binding, headers, scope, now,
        [binding.prepare("SELECT * FROM expense_categories WHERE id=?").bind(item_id or "")])
    existing = rows[0][0] if rows[0] and rows[0][0]["owner_id"] == user["id"] else None
    if item_id and existing is None:
        return _error(404, "NOT_FOUND")
    stamp = _timestamp(now)
    if method == "POST":
        category_id = "cat_" + uuid.uuid4().hex
        name = body["name"].strip()
        commands = [_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO expense_categories(id,owner_id,name,color,archived_at,
                revision,created_at,updated_at) VALUES(?,?,?,?,NULL,1,?,?)""").bind(
                category_id, user["id"], name, body.get("color"), stamp, stamp),
            binding.prepare("DELETE FROM d1_command_guard")]
        try:
            await binding.batch(commands)
        except PlanLimitError as error:
            return error.response()
        except Exception:
            return _error(409, "CATEGORY_CONFLICT")
        return 201, {"id": category_id, "name": name, "color": body.get("color"),
                     "revision": 1, "archivedAt": None}
    expected = _revision(headers)
    if expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if expected < 0:
        return _error(422, "INVALID_INPUT")
    if expected != existing["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if existing["archived_at"] is not None:
        return _error(409, "CATEGORY_ARCHIVED")
    name = body["name"].strip() if method == "PATCH" else existing["name"]
    color = body.get("color") if method == "PATCH" else existing["color"]
    archived = stamp if method == "DELETE" else None
    commands = [_write_guard(binding, proof, user["id"], now),
        binding.prepare("""UPDATE expense_categories SET name=?,color=?,archived_at=?,
            revision=revision+1,updated_at=? WHERE id=? AND owner_id=? AND revision=?
            AND archived_at IS NULL""").bind(name, color, archived, stamp, item_id, user["id"], expected),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return _error(409 if method == "PATCH" else 412,
                      "CATEGORY_CONFLICT" if method == "PATCH" else "PRECONDITION_FAILED")
    return (204, None) if method == "DELETE" else (200, {
        "id": item_id, "name": name, "color": color, "revision": expected + 1,
        "archivedAt": None})
