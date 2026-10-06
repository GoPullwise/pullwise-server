"""Owner-scoped project and category REST operations for the ledger Worker."""
from __future__ import annotations

from .cloudflare_plan_limits import PlanLimitError

import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_github_gateway import GitHubFailure
from .cloudflare_principal import PrincipalAuthError, _header
from .ledger_money_totals import AGGREGATE_SQL, aggregate_minor, public_minor
from .json_input import validate_json_unicode
from .cloudflare_state_records import record_name
from .cloudflare_project_repositories import (
    MAX_REPOSITORIES, bound_repository_dto, live_repository_access, project_input,
    repository_snapshot_values,
)


_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
MAX_REVISION = 9007199254740991


def _error(status: int, code: str):
    return status, {"error": {"code": code}}


def _timestamp(now: int) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _param(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[-1] if value else ""
    return value if isinstance(value, str) else ""


def _valid_resource_id(value: object) -> bool:
    return isinstance(value, str) and _RESOURCE_ID.fullmatch(value) is not None


def _page_inputs(params: Mapping[str, object]) -> tuple[int, str]:
    """Reject malformed pagination before authentication or SQL admission."""
    raw_limit, cursor = _param(params, "limit"), _param(params, "cursor")
    if raw_limit and (len(raw_limit) > 3 or not raw_limit.isascii() or not raw_limit.isdigit()):
        raise ValueError("limit")
    limit = int(raw_limit) if raw_limit else 50
    if not 1 <= limit <= 100 or cursor and not _valid_resource_id(cursor):
        raise ValueError("pagination")
    return limit, cursor


def _revision(headers: Mapping[str, object]):
    value = _header(headers, "If-Match")
    if not value:
        return None
    match = re.fullmatch(r'"([1-9][0-9]{0,15})"', value)
    revision = int(match.group(1)) if match else -1
    return revision if revision <= MAX_REVISION else -1


def _project(row: dict, allowed_repos: dict[int, dict], totals: list[dict], github_access="lost",
             bindings: list[dict] | None = None, organizations: list[dict] | None = None):
    repo_id = row["github_repo_id"]
    ids = [item["github_repo_id"] for item in (bindings or [])]
    ids = [repo_id, *sorted(value for value in ids if value != repo_id)]
    repositories = [bound_repository_dto(value, allowed_repos, github_access) for value in ids]
    authorized = sum(item["githubAccess"] == "authorized" for item in repositories)
    state = "authorized" if authorized == len(ids) else "partial" if authorized else github_access
    organization_id = row.get("github_organization_id")
    organization = next((item for item in organizations or [] if item["id"] == organization_id), None)
    live_anchor = allowed_repos.get(repo_id)
    return {"id": row["id"], "name": row.get("name", ""), "githubRepoId": repo_id,
            "githubFullName": live_anchor["fullName"] if live_anchor else None,
            "githubRepoIds": ids, "repositories": repositories,
            "githubOrganizationId": organization_id,
            "githubOrganization": {"id": organization_id, "login": organization["login"] if organization else None,
                "type": "Organization", "githubAccess": "authorized" if organization else github_access}
                if organization_id is not None else None,
            "description": row["description"],
            "status": row["status"], "githubAccess": state,
            "canCreateExpense": row["status"] == "active" and authorized > 0,
            "revision": row["revision"], "totals": [{"currency": total["currency"],
                "amountMinor": total["amountMinor"] if "amountMinor" in total
                else public_minor(aggregate_minor(total))} for total in totals]}


def _category(row: dict):
    return {"id": row["id"], "name": row["name"], "color": row["color"],
            "revision": row["revision"], "archivedAt": row["archived_at"]}


def _write_guard(binding: Any, proof: dict, owner_id: str, now: int):
    """A failed credential/user fence aborts the D1 transaction."""
    actor_id = proof.get("actor_user_id", owner_id)
    owner_snapshot = proof.get("owner_user", proof["user"])
    user_check = "EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?)"
    checks = [user_check]
    values = [record_name("users", owner_id), owner_snapshot]
    if actor_id != owner_id:
        checks.append(user_check)
        values.extend([record_name("users", actor_id), proof["user"]])
        checks.append("""EXISTS(SELECT 1 FROM workspace_members
            WHERE workspace_id=? AND user_id=? AND role=? AND revision=? AND removed_at IS NULL)""")
        values.extend([owner_id, actor_id, proof["workspace_role"], proof["workspace_revision"]])
    if proof.get("key") is not None:
        key = proof["key"]
        checks.append("""EXISTS(SELECT 1 FROM api_keys WHERE key_hash=?
              AND user_id=? AND scopes=? AND restrictions=? AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at>=?))""")
        token_hash = hashlib.sha256(proof["token"].encode()).hexdigest()
        values.extend([token_hash, actor_id, key["scopes"], key["restrictions"], now])
    else:
        checks.append("""EXISTS(SELECT 1 FROM app_state WHERE name=?
          AND payload=? AND json_extract(payload, '$.userId') = ?
          AND CAST(json_extract(payload, '$.expiresAt') AS INTEGER)>=?)""")
        session_id = proof["session_id"]
        values.extend([record_name("sessions", session_id), proof["sessions"], actor_id, now])
    sql = "INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN " + " AND ".join(checks) + " THEN 1 ELSE 0 END)"
    return binding.prepare(sql).bind(*values)


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
    access = await live_repository_access(user, gateway)
    return {item["githubRepoId"]: item["fullName"] for item in access["items"]}


async def _project_repos(user: dict, gateway: Any):
    """History reads remain usable without claiming an outage revoked grants."""
    try:
        access = await live_repository_access(user, gateway)
        repos = {item["githubRepoId"]: item for item in access["items"]}
        state = "reauthorization_required" if access["githubAccess"] == "reauthorization_required" else "lost"
        return repos, state, access.get("organizations", [])
    except Exception:
        return {}, "unavailable", []


async def handle_ledger_request(*, binding: Any, gateway: Any, method: str, path: str,
                                headers: Mapping[str, object], params: Mapping[str, object],
                                body: object, now: int, suggestion_gateway=None) -> tuple[int, object] | None:
    """Return None for non-ledger paths; structured status and payload otherwise."""
    try:
        validate_json_unicode(body)
    except UnicodeError:
        return _error(422, "INVALID_INPUT")
    if path == "/api/v1/workspaces" or path.startswith(("/api/v1/workspaces/", "/api/v1/workspace-invitations/")):
        from .cloudflare_workspaces import handle_workspace_request
        return await handle_workspace_request(binding=binding, gateway=gateway,
            method=method, path=path, headers=headers, body=body, now=now)
    if path == "/api/v1/repositories":
        if method != "GET":
            return _error(405, "METHOD_NOT_ALLOWED")
        limit_text, cursor = _param(params, "limit"), _param(params, "cursor")
        limit = int(limit_text) if (len(limit_text) <= 3 and limit_text.isascii()
                                   and limit_text.isdigit()) else 50 if not limit_text else 0
        if (not 1 <= limit <= 100 or (cursor and (not cursor.isascii() or not cursor.isdigit()
                or len(cursor) > 16 or not 1 <= int(cursor) <= 9007199254740991))):
            return _error(422, "INVALID_INPUT")
        try:
            user, restrictions, auth, validate = await ledger_principal(
                binding=binding, headers=headers, scope="projects:read", now=now)
            snapshot = await binding.batch(auth)
            validate([part.results for part in snapshot])
            access = await live_repository_access(user, gateway)
            items = sorted((item for item in access["items"]
                            if item["githubRepoId"] > (int(cursor) if cursor else 0)),
                           key=lambda item: item["githubRepoId"])
            page = items[:limit]
            if page:
                project_ids = restrictions.get("projectIds")
                visibility = "" if project_ids is None else (
                    " AND project_id IN (" + ",".join("?" for _ in project_ids) + ")"
                    if project_ids else " AND 0")
                linked = binding.prepare("""SELECT github_repo_id FROM ledger_project_repositories
                    WHERE owner_id=? AND github_repo_id IN (""" + ",".join("?" for _ in page) + ")"
                    + visibility + " LIMIT 100").bind(user["id"], *(item["githubRepoId"] for item in page),
                                                     *(project_ids or []))
                linked_snapshot = await binding.batch([*auth, linked])
                validate([part.results for part in linked_snapshot[:len(auth)]])
                bound = {item["github_repo_id"] for item in linked_snapshot[-1].results}
                page = [{**item, "isBound": item["githubRepoId"] in bound} for item in page]
            return 200, {"items": page,
                         "nextCursor": str(page[-1]["githubRepoId"]) if len(items) > limit else None,
                         "githubAccess": access["githubAccess"],
                         "organizations": access.get("organizations", [])}
        except PrincipalAuthError as exc:
            return _error(exc.status, exc.code)
        except GitHubFailure as exc:
            return _error(exc.status, exc.code)
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
        try:
            return await handle_expense_request(binding=binding, gateway=gateway,
                method=method, path=path, headers=headers, params=params, body=body, now=now,
                suggestion_gateway=suggestion_gateway)
        except GitHubFailure as exc:
            return _error(exc.status, exc.code)
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[:2] != ["api", "v1"] or parts[2] not in {"projects", "categories"}:
        return None
    kind = parts[2]
    if len(parts) > 4 or (len(parts) == 4 and not parts[3]):
        return _error(404, "NOT_FOUND")
    item_id = parts[3] if len(parts) == 4 else None
    if item_id is not None and not _valid_resource_id(item_id):
        return _error(404, "NOT_FOUND")
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
    except GitHubFailure as exc:
        return _error(exc.status, exc.code)


async def _projects(binding, gateway, method, item_id, headers, params, body, now, scope):
    selected = None
    if method in {"POST", "PATCH"}:
        try:
            selected = project_input(body, creating=method == "POST")
        except ValueError:
            return _error(422, "INVALID_INPUT")
    if method == "GET":
        if item_id:
            user, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
                scope=scope, now=now, target_kind="project", project_id=item_id)
            commands = [binding.prepare("SELECT * FROM ledger_projects WHERE owner_id=? AND id=?").bind(user["id"], item_id),
                binding.prepare(f"""SELECT currency,{AGGREGATE_SQL} FROM expenses
                    WHERE owner_id=? AND project_id=? AND deleted_at IS NULL GROUP BY currency""").bind(user["id"], item_id),
                binding.prepare("""SELECT * FROM ledger_project_repositories
                    WHERE owner_id=? AND project_id=? ORDER BY github_repo_id LIMIT 30""").bind(user["id"], item_id)]
            rows = await binding.batch([*auth, *commands])
            validate([part.results for part in rows[:len(auth)]])
            project_rows, totals, bindings = [part.results for part in rows[len(auth):]]
            if not project_rows:
                return _error(404, "NOT_FOUND")
            repos, state, organizations = await _project_repos(user, gateway)
            return 200, _project(project_rows[0], repos, totals, state, bindings, organizations)
        try:
            limit, cursor = _page_inputs(params)
        except ValueError:
            return _error(422, "INVALID_INPUT")
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
            binding.prepare(f"""SELECT project_id,currency,{AGGREGATE_SQL} FROM expenses
            WHERE owner_id=? AND deleted_at IS NULL""" + date_where +
                " GROUP BY project_id,currency").bind(user["id"], *date_values),
            binding.prepare("""SELECT * FROM ledger_project_repositories
                WHERE owner_id=? AND project_id>?""" + visible.replace("id IN", "project_id IN") +
                " ORDER BY project_id,github_repo_id LIMIT ?").bind(
                    user["id"], cursor, *(project_ids or []), (limit + 1) * MAX_REPOSITORIES)]
        rows = await binding.batch([*auth, *commands])
        validate([part.results for part in rows[:len(auth)]])
        projects, totals, bindings = [part.results for part in rows[len(auth):]]
        page = projects[:limit]
        by_project = {}
        for total in totals:
            by_project.setdefault(total["project_id"], []).append({
                "currency": total["currency"], "amountMinor": public_minor(aggregate_minor(total))})
        by_binding = {}
        for row in bindings:
            by_binding.setdefault(row["project_id"], []).append(row)
        repos, state, organizations = await _project_repos(user, gateway)
        return 200, {"items": [_project(row, repos, by_project.get(row["id"], []), state,
            by_binding.get(row["id"], []), organizations) for row in page],
                     "nextCursor": page[-1]["id"] if len(projects) > limit else None}
    user, restrictions, proof, rows = await _authorized(binding, headers, scope, now,
        [binding.prepare("SELECT * FROM ledger_projects WHERE id=?").bind(item_id or ""),
         binding.prepare("""SELECT * FROM ledger_project_repositories
             WHERE project_id=? ORDER BY github_repo_id LIMIT 30""").bind(item_id or "")],
        "project" if item_id else None, item_id)
    existing = rows[0][0] if rows[0] and rows[0][0]["owner_id"] == user["id"] else None
    if item_id and existing is None:
        return _error(404, "NOT_FOUND")
    current_bindings = rows[1] if existing else []
    if method == "PATCH":
        expected = _revision(headers)
        if expected is None:
            return _error(428, "PRECONDITION_REQUIRED")
        if expected < 0:
            return _error(422, "INVALID_INPUT")
        if expected != existing["revision"]:
            return _error(412, "PRECONDITION_FAILED")
    project_id = "prj_" + uuid.uuid4().hex if method == "POST" else item_id
    if method == "POST" and not target_allowed(restrictions, "project", project_id):
        return _error(403, "TARGET_FORBIDDEN")
    if selected is not None and await _repository_conflict(binding, user["id"], selected, item_id):
        return _error(409, "PROJECT_CONFLICT")
    status = body.get("status", existing["status"] if existing else "active")
    access = None
    if (selected is not None or "githubOrganizationId" in body
            or (existing and status == "active" and existing["status"] == "archived")):
        access = await live_repository_access(user, gateway)
    repos = {item["githubRepoId"]: item for item in access["items"]} if access else {}
    organizations = access.get("organizations", []) if access else []
    organization_id = body.get("githubOrganizationId", existing.get("github_organization_id") if existing else None)
    if ("githubOrganizationId" in body and organization_id is not None
            and not any(item["id"] == organization_id for item in organizations)):
        return _error(403, "GITHUB_ORGANIZATION_ACCESS_REQUIRED")
    if selected is not None:
        if any(repo_id not in repos for repo_id in selected):
            return _error(403, "GITHUB_ACCESS_REQUIRED")
    elif existing and status == "active" and existing["status"] == "archived":
        if not any(row["github_repo_id"] in repos for row in current_bindings):
            return _error(403, "GITHUB_ACCESS_REQUIRED")
    stamp = _timestamp(now)
    if method == "POST":
        repo_id = selected[0]
        commands = [_write_guard(binding, proof, user["id"], now),
            binding.prepare("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,
                github_full_name,description,status,revision,created_at,updated_at,name,github_organization_id)
                VALUES(?,?,?,?,?,'active',1,?,?,?,?)""").bind(project_id, user["id"], repo_id,
                    repos[repo_id]["fullName"], body.get("description", ""), stamp, stamp,
                    body.get("name", ""), organization_id),
            *_insert_repository_bindings(binding, user["id"], project_id, selected, repos, stamp),
            binding.prepare("DELETE FROM d1_command_guard")]
        try:
            await binding.batch(commands)
        except PlanLimitError as error:
            return error.response()
        except Exception:
            return _error(409, "PROJECT_CONFLICT")
        return 201, _project({"id": project_id, "github_repo_id": repo_id,
            "name": body.get("name", ""), "github_organization_id": organization_id,
            "description": body.get("description", ""), "status": "active", "revision": 1}, repos, [],
            bindings=[{"github_repo_id": value} for value in selected], organizations=organizations)
    repo_id = selected[0] if selected is not None else existing["github_repo_id"]
    full_name = repos[repo_id]["fullName"] if selected is not None else existing["github_full_name"]
    commands = [_write_guard(binding, proof, user["id"], now),
        binding.prepare("""UPDATE ledger_projects SET name=?,github_organization_id=?,
            github_repo_id=?,github_full_name=?,description=?,status=?,revision=revision+1,
            updated_at=? WHERE id=? AND owner_id=? AND revision=?""").bind(
            body.get("name", existing["name"]), organization_id, repo_id, full_name,
            body.get("description", existing["description"]), status, stamp, item_id,
            user["id"], expected),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)")]
    if selected is not None:
        commands.extend(binding.prepare("""DELETE FROM ledger_project_repositories
            WHERE project_id=? AND github_repo_id=? AND owner_id=?""").bind(
                item_id, row["github_repo_id"], user["id"]) for row in current_bindings)
        commands.extend(_insert_repository_bindings(binding, user["id"], item_id, selected, repos, stamp))
    commands.extend([binding.prepare("DELETE FROM d1_command_guard"),
        binding.prepare(f"""SELECT currency,{AGGREGATE_SQL} FROM expenses
            WHERE owner_id=? AND project_id=? AND deleted_at IS NULL GROUP BY currency""").bind(
                user["id"], item_id)])
    try:
        result = await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception:
        if selected is not None and await _repository_conflict(binding, user["id"], selected, item_id):
            return _error(409, "PROJECT_CONFLICT")
        return _error(412, "PRECONDITION_FAILED")
    updated = {**existing, "description": body.get("description", existing["description"]),
        "name": body.get("name", existing["name"]), "github_organization_id": organization_id,
        "github_repo_id": repo_id, "status": status, "revision": expected + 1}
    bindings = [{"github_repo_id": value} for value in selected] if selected is not None else current_bindings
    repos, state, organizations = await _project_repos(user, gateway)
    return 200, _project(updated, repos, result[-1].results, state, bindings, organizations)


async def _repository_conflict(binding, owner_id, selected, project_id):
    row = await binding.prepare("""SELECT project_id FROM ledger_project_repositories
        WHERE owner_id=? AND github_repo_id IN (""" + ",".join("?" for _ in selected) +
        ") AND project_id<>? LIMIT 1").bind(owner_id, *selected, project_id or "").first()
    return row is not None


def _insert_repository_bindings(binding, owner_id, project_id, selected, repos, stamp):
    return [binding.prepare("""INSERT INTO ledger_project_repositories(owner_id,project_id,
        github_repo_id,github_full_name,installation_id,github_account_id,github_account_login,
        github_account_type,created_at) VALUES(?,?,?,?,?,?,?,?,?)""").bind(
            owner_id, project_id, repo_id, *repository_snapshot_values(repos[repo_id]), stamp)
        for repo_id in selected]


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
        duplicate = await binding.prepare("""SELECT id FROM expense_categories
            WHERE owner_id=? AND lower(name)=lower(?) AND archived_at IS NULL""").bind(user["id"], name).first()
        if duplicate is not None:
            return _error(409, "CATEGORY_CONFLICT")
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
    if method == "PATCH":
        duplicate = await binding.prepare("""SELECT id FROM expense_categories
            WHERE owner_id=? AND lower(name)=lower(?) AND archived_at IS NULL AND id<>?""").bind(
                user["id"], name, item_id).first()
        if duplicate is not None:
            return _error(409, "CATEGORY_CONFLICT")
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
