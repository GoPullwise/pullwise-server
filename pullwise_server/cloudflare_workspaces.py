"""Cookie-only workspace governance over existing, immutable ledger owners.

Invitations grant finance access only. GitHub identity is resolved once to its
stable numeric ID; accepting never grants GitHub repository or organization rights.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from .cloudflare_github_gateway import GitHubFailure
from .cloudflare_github_identity_http import _random_urlsafe
from .cloudflare_ledger_auth import ledger_principal, workspace_payload
from .cloudflare_plan_limits import PlanLimitError
from .cloudflare_principal import PrincipalAuthError, _cookie_sessions, _header

ROLES = {"admin", "editor", "viewer"}
MAX_MEMBERS = 99  # plus the implicit owner
MAX_INVITES = 100
INVITATION_AGE = 86400
MAX_REVISION = 9007199254740991
_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_TOKEN = re.compile(r"pwi_[A-Za-z0-9_-]{43}")


def _error(status, code):
    return status, {"error": {"code": code}}


def _helpers():
    # Imported lazily because ledger_api dispatches to this module.
    from .cloudflare_ledger_api import _revision, _timestamp, _write_guard
    return _revision, _timestamp, _write_guard


def _cookie_only(headers, proof):
    if (proof.get("key") is not None or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or proof.get("session_id") not in _cookie_sessions(headers)):
        raise PrincipalAuthError(403, "COOKIE_SESSION_REQUIRED", "Use your browser session.")


def _selected(headers, workspace_id=None):
    selected = _header(headers, "X-Pullwise-Workspace")
    if workspace_id and selected and selected != workspace_id:
        raise PrincipalAuthError(422, "WORKSPACE_SELECTION_CONFLICT", "Ledger selection differs.")
    result = {key: value for key, value in headers.items()
              if str(key).casefold() != "x-pullwise-workspace"}
    if workspace_id:
        result["X-Pullwise-Workspace"] = workspace_id
    return result


async def _auth(binding, headers, now, workspace_id=None):
    proof = {}
    user, _, auth, validate = await ledger_principal(binding=binding,
        headers=_selected(headers, workspace_id), scope="profile:read", now=now, proof=proof)
    return user, proof, auth, validate


async def _read(binding, headers, now, commands, workspace_id=None):
    user, proof, auth, validate = await _auth(binding, headers, now, workspace_id)
    results = await binding.batch([*auth, *commands])
    validate([part.results for part in results[:len(auth)]])
    _cookie_only(headers, proof)
    return user, proof, [part.results for part in results[len(auth):]]


def _manage(user, target_role=None):
    role = user["_workspace"]["role"]
    if role not in {"owner", "admin"} or (target_role == "admin" and role != "owner"):
        raise PrincipalAuthError(403, "ROLE_FORBIDDEN", "Your role cannot manage this member.")


def _member(row, user=None):
    user = user or (json.loads(row["snapshot"]) if row.get("snapshot") else {})
    return {"userId": row["user_id"], "name": user.get("name"),
            "githubLogin": user.get("githubLogin"), "role": row["role"],
            "revision": row["revision"], "joinedAt": row.get("joined_at")}


def _invite(row):
    _, timestamp, _ = _helpers()
    return {"id": row["id"], "recipient": {"githubId": str(row["github_recipient_id"]),
            "login": row["github_login"]}, "role": row["role"], "status": row["status"],
            "revision": row["revision"], "expiresAt": timestamp(row["expires_at"]),
            "createdAt": row["created_at"]}


def _guard(binding, checks, values):
    return binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN "
                           + " AND ".join(checks) + " THEN 1 ELSE 0 END)").bind(*values)


def _event(binding, workspace_id, actor_id, action, subject_id, before, after, now):
    _, timestamp, _ = _helpers()
    encode = lambda item: json.dumps(item, separators=(",", ":"), ensure_ascii=False) if item is not None else None
    return binding.prepare("""INSERT INTO workspace_events
        (id,workspace_id,actor_user_id,action,subject_id,before_json,after_json,created_at)
        VALUES(?,?,?,?,?,?,?,?)""").bind("wev_" + uuid.uuid4().hex, workspace_id, actor_id,
                                       action, subject_id, encode(before), encode(after), timestamp(now))


async def _write(binding, commands):
    try:
        await binding.batch([*commands, binding.prepare("DELETE FROM d1_command_guard")])
    except PlanLimitError:
        raise
    except Exception:
        # Atomic fences must fail closed. Do not expose native/provider details.
        raise PrincipalAuthError(503, "WORKSPACE_WRITE_UNAVAILABLE", "The write could not be confirmed.") from None


def _expected(headers, row):
    revision, _, _ = _helpers()
    expected = revision(headers)
    if expected is None:
        raise PrincipalAuthError(428, "REVISION_REQUIRED", "If-Match is required.")
    if expected < 1 or expected > MAX_REVISION:
        raise PrincipalAuthError(422, "INVALID_INPUT", "Invalid revision.")
    if expected != row["revision"]:
        raise PrincipalAuthError(412, "REVISION_CONFLICT", "Reload the current member.")
    if expected == MAX_REVISION:
        raise PrincipalAuthError(409, "REVISION_EXHAUSTED", "The revision cannot advance.")
    return expected


async def _list(binding, headers, now):
    # Selectors do not change the identity whose accessible workspaces are listed.
    user, proof, auth, validate = await _auth(binding, headers, now)
    result = await binding.batch([*auth, binding.prepare("""SELECT
        m.workspace_id,m.role,m.revision,u.value AS snapshot
        FROM workspace_members m,app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=m.workspace_id AND m.user_id=?
        AND m.removed_at IS NULL ORDER BY m.workspace_id LIMIT 100""").bind(user["id"])])
    validate([part.results for part in result[:len(auth)]])
    _cookie_only(headers, proof)
    rows = [result[-1].results]
    if len(rows[0]) > MAX_MEMBERS:
        return _error(503, "WORKSPACE_LIMIT")
    items = [user["_workspace"]]
    for row in rows[0]:
        owner = json.loads(row["snapshot"])
        if (isinstance(owner, dict) and owner.get("id") == row["workspace_id"]
                and row["role"] in ROLES):
            items.append(workspace_payload(owner, row["role"], row["revision"]))
    return 200, {"items": items}


async def _members(binding, headers, body, now, workspace_id, user_id, method):
    user, proof, auth, validate = await _auth(binding, headers, now, workspace_id)
    sql = """SELECT m.*,u.value AS snapshot FROM workspace_members m,
        app_state a,json_each(a.payload) u WHERE a.name='users' AND u.key=m.user_id
        AND m.workspace_id=? AND m.removed_at IS NULL"""
    values = [workspace_id]
    sql += " AND m.user_id=?" if user_id else " ORDER BY m.user_id LIMIT 100"
    if user_id:
        values.append(user_id)
    parts = await binding.batch([*auth, binding.prepare(sql).bind(*values)])
    validate([part.results for part in parts[:len(auth)]])
    _cookie_only(headers, proof)
    rows = parts[-1].results
    if method == "GET" and not user_id:
        if len(rows) > MAX_MEMBERS:
            return _error(503, "WORKSPACE_LIMIT")
        owner = _member({"user_id": workspace_id, "role": "owner", "revision": 1}, user)
        return 200, {"items": [owner, *[_member(row) for row in rows]], "workspace": user["_workspace"]}
    _manage(user)
    if user_id == workspace_id:
        return _error(403, "OWNER_IMMUTABLE")
    if len(rows) != 1:
        return _error(404, "MEMBER_NOT_FOUND")
    row = rows[0]
    _manage(user, row["role"])
    if method == "PATCH":
        if (not isinstance(body, dict) or set(body) != {"role"}
                or not isinstance(body["role"], str) or body["role"] not in ROLES):
            return _error(422, "INVALID_INPUT")
        _manage(user, body["role"])
    expected = _expected(headers, row)
    _, timestamp, write_guard = _helpers()
    stamp = timestamp(now)
    before = _member(row)
    after = {**before, "role": body["role"], "revision": expected + 1} if method == "PATCH" else None
    fence = _guard(binding, ["""EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=?
        AND user_id=? AND role=? AND revision=? AND removed_at IS NULL)"""],
        [workspace_id, user_id, row["role"], expected])
    mutation = binding.prepare("""UPDATE workspace_members SET role=?,revision=revision+1,updated_at=?
        WHERE workspace_id=? AND user_id=? AND revision=?""").bind(body["role"], stamp,
        workspace_id, user_id, expected) if method == "PATCH" else binding.prepare("""UPDATE
        workspace_members SET removed_at=?,updated_at=?,revision=revision+1
        WHERE workspace_id=? AND user_id=? AND revision=?""").bind(stamp, stamp, workspace_id, user_id, expected)
    await _write(binding, [write_guard(binding, proof, workspace_id, now), fence, mutation,
        _event(binding, workspace_id, proof["actor_user_id"],
               "change_role" if method == "PATCH" else "remove_member", user_id, before, after, now)])
    return (200, after) if method == "PATCH" else (204, None)


async def _recipient(gateway, actor, login):
    if not actor.get("githubAccessToken"):
        raise GitHubFailure("GITHUB_REAUTHORIZATION_REQUIRED")
    token = await gateway.unseal(actor["githubAccessToken"])
    # The validated username cannot influence the host, path hierarchy or query.
    recipient = await gateway._json("https://api.github.com/users/" + login, token=token)
    if not isinstance(recipient, dict):
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    identifier, canonical = recipient.get("id"), recipient.get("login")
    if (type(identifier) is not int or not 1 <= identifier <= MAX_REVISION
            or not isinstance(canonical, str) or not _LOGIN.fullmatch(canonical)
            or recipient.get("type") != "User"):
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    return identifier, canonical


async def _invites(binding, gateway, headers, body, now, workspace_id, invite_id, method):
    user, proof, auth, validate = await _auth(binding, headers, now, workspace_id)
    commands = [binding.prepare("SELECT * FROM workspace_invites WHERE workspace_id=?"
        + (" AND id=?" if invite_id else " AND status='pending' AND expires_at>=? ORDER BY created_at,id LIMIT 101"))
        .bind(workspace_id, invite_id if invite_id else now)]
    parts = await binding.batch([*auth, *commands])
    validate([part.results for part in parts[:len(auth)]])
    _cookie_only(headers, proof)
    _manage(user)
    rows = parts[-1].results
    _, timestamp, write_guard = _helpers()
    if method == "GET":
        if len(rows) > MAX_INVITES:
            return _error(503, "INVITATION_LIMIT")
        return 200, {"items": [_invite(row) for row in rows[:MAX_INVITES]]}
    if method == "DELETE":
        if len(rows) != 1 or rows[0]["status"] != "pending":
            return _error(404, "INVITATION_NOT_FOUND")
        row = rows[0]
        _manage(user, row["role"])
        expected = _expected(headers, row)
        await _write(binding, [write_guard(binding, proof, workspace_id, now),
            _guard(binding, ["""EXISTS(SELECT 1 FROM workspace_invites WHERE id=? AND workspace_id=?
                AND revision=? AND status='pending')"""], [invite_id, workspace_id, expected]),
            binding.prepare("""UPDATE workspace_invites SET status='revoked',revision=revision+1,updated_at=?
                WHERE id=? AND workspace_id=? AND revision=?""").bind(timestamp(now), invite_id, workspace_id, expected),
            _event(binding, workspace_id, proof["actor_user_id"], "revoke_invite", invite_id,
                   _invite(row), {"status": "revoked", "revision": expected + 1}, now)])
        return 204, None
    if (not isinstance(body, dict) or set(body) != {"githubLogin", "role"}
            or not isinstance(body["githubLogin"], str) or not _LOGIN.fullmatch(body["githubLogin"])
            or not isinstance(body["role"], str) or body["role"] not in ROLES):
        return _error(422, "INVALID_INPUT")
    _manage(user, body["role"])
    if len(rows) >= MAX_INVITES:
        return _error(403, "INVITATION_LIMIT")
    github_id, login = await _recipient(gateway, user["_actor"], body["githubLogin"])
    if str(github_id) == str(user.get("githubId")):
        return _error(403, "OWNER_IMMUTABLE")
    # Stable GitHub ID, not a mutable login, determines the eventual recipient.
    recipient_id = "usr_github_" + str(github_id)
    snapshots = await binding.batch([*auth, binding.prepare("""SELECT user_id FROM workspace_members
        WHERE workspace_id=? AND user_id=? AND removed_at IS NULL""").bind(workspace_id, recipient_id),
        binding.prepare("""SELECT id FROM workspace_invites WHERE workspace_id=?
          AND github_recipient_id=? AND status='pending' AND expires_at>=? LIMIT 1""").bind(workspace_id, github_id, now)])
    validate([part.results for part in snapshots[:len(auth)]])
    if snapshots[-2].results:
        return _error(409, "ALREADY_MEMBER")
    if snapshots[-1].results:
        return _error(409, "INVITATION_EXISTS")
    token, invite_id = "pwi_" + _random_urlsafe(32), "win_" + uuid.uuid4().hex
    row = {"id": invite_id, "workspace_id": workspace_id, "github_recipient_id": github_id,
        "github_login": login, "role": body["role"], "status": "pending", "revision": 1,
        "expires_at": now + INVITATION_AGE, "created_at": timestamp(now)}
    await _write(binding, [write_guard(binding, proof, workspace_id, now),
        _guard(binding, ["""(SELECT COUNT(*) FROM workspace_invites WHERE workspace_id=?
                AND status='pending' AND expires_at>=?)<?""",
            """NOT EXISTS(SELECT 1 FROM workspace_invites WHERE workspace_id=? AND github_recipient_id=?
                AND status='pending' AND expires_at>=?)""",
            """NOT EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND removed_at IS NULL)"""],
            [workspace_id, now, MAX_INVITES, workspace_id, github_id, now, workspace_id, recipient_id]),
        binding.prepare("""INSERT INTO workspace_invites(id,workspace_id,github_recipient_id,github_login,
            token_hash,role,status,revision,expires_at,created_by_user_id,created_by_revision,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',1,?,?,?,?,?)""").bind(invite_id, workspace_id, github_id, login,
                hashlib.sha256(token.encode()).hexdigest(), body["role"], row["expires_at"],
                proof["actor_user_id"], proof["workspace_revision"], row["created_at"], row["created_at"]),
        _event(binding, workspace_id, proof["actor_user_id"], "invite", invite_id, None, _invite(row), now)])
    return 201, {**_invite(row), "token": token}


def _github_id(user):
    value = user.get("githubId")
    return int(value) if (isinstance(value, str) and value.isdigit()
                         or type(value) is int) and 1 <= int(value) <= MAX_REVISION else None


async def _accept(binding, headers, body, now, method):
    if not isinstance(body, dict) or set(body) != {"token"} or not isinstance(body["token"], str) or not _TOKEN.fullmatch(body["token"]):
        return _error(422, "INVALID_INPUT")
    token_hash = hashlib.sha256(body["token"].encode()).hexdigest()
    actor, proof, rows = await _read(binding, _selected(headers), now, [binding.prepare(
        "SELECT * FROM workspace_invites WHERE token_hash=?").bind(token_hash)])
    if len(rows[0]) != 1:
        return _error(404, "INVITATION_NOT_FOUND")
    invite = rows[0][0]
    if _github_id(actor) != invite["github_recipient_id"]:
        return _error(403, "INVITATION_RECIPIENT_MISMATCH")
    if invite["status"] == "revoked":
        return _error(410, "INVITATION_REVOKED")
    accepted_preview = invite["status"] == "accepted" and method == "preview"
    if invite["status"] == "accepted" and not accepted_preview:
        return _error(410, "INVITATION_ACCEPTED")
    if not accepted_preview and (invite["status"] != "pending" or invite["expires_at"] < now):
        return _error(410, "INVITATION_EXPIRED")
    workspace_id, inviter_id = invite["workspace_id"], invite["created_by_user_id"]
    # Revalidate the actor alongside all preview/accept resource data.
    current_actor, current_proof, auth, validate = await _auth(binding, headers, now)
    found = await binding.batch([*auth, binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(workspace_id),
        binding.prepare("SELECT role,revision,removed_at FROM workspace_members WHERE workspace_id=? AND user_id=?")
            .bind(workspace_id, inviter_id),
        binding.prepare("SELECT * FROM workspace_members WHERE workspace_id=? AND user_id=?")
            .bind(workspace_id, actor["id"]),
        binding.prepare("""SELECT
          (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=? AND removed_at IS NULL) AS members,
          (SELECT COUNT(*) FROM workspace_members WHERE user_id=? AND removed_at IS NULL) AS workspaces""")
            .bind(workspace_id, actor["id"])])
    validate([part.results for part in found[:len(auth)]])
    if current_actor != actor:
        return _error(403, "AUTHORIZATION_CHANGED")
    proof = current_proof
    owners, inviters, members, counts = (part.results for part in found[len(auth):])
    if len(owners) != 1:
        return _error(404, "WORKSPACE_NOT_FOUND")
    owner = json.loads(owners[0]["snapshot"])
    if not isinstance(owner, dict) or owner.get("id") != workspace_id or actor["id"] == workspace_id:
        return _error(403, "OWNER_IMMUTABLE")
    member = members[0] if len(members) == 1 else None
    if accepted_preview:
        # A lost acceptance response can be recovered without consuming another
        # token or writing. Current membership owns authority, never the old
        # invitation role; removed members cannot be resurrected by rechecking.
        if (invite["accepted_by_user_id"] != actor["id"] or not member
                or member["removed_at"] is not None or member["role"] not in ROLES):
            return _error(410, "INVITATION_ACCEPTED")
        return 200, {**_invite(invite), "workspace": workspace_payload(
            owner, member["role"], member["revision"])}
    inviter = inviters[0] if len(inviters) == 1 else None
    if (inviter_id == workspace_id and invite["created_by_revision"] != 1
            or inviter_id != workspace_id and (not inviter or inviter["removed_at"] is not None
            or inviter["role"] != "admin" or inviter["revision"] != invite["created_by_revision"]
            or invite["role"] == "admin")):
        return _error(403, "INVITATION_AUTHORITY_LOST")
    if member and member["removed_at"] is None:
        return _error(409, "ALREADY_MEMBER")
    revision = member["revision"] + 1 if member else 1
    if revision > MAX_REVISION:
        return _error(409, "REVISION_EXHAUSTED")
    if method == "preview":
        return 200, {**_invite(invite), "workspace": workspace_payload(owner, invite["role"], revision)}
    if counts[0]["members"] >= MAX_MEMBERS or counts[0]["workspaces"] >= MAX_MEMBERS:
        return _error(403, "WORKSPACE_LIMIT")
    if invite["revision"] == MAX_REVISION:
        return _error(409, "REVISION_EXHAUSTED")
    _, timestamp, write_guard = _helpers()
    checks = ["""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?)""",
        """EXISTS(SELECT 1 FROM workspace_invites WHERE id=? AND workspace_id=? AND token_hash=?
          AND github_recipient_id=? AND role=? AND created_by_user_id=? AND created_by_revision=? AND revision=?
          AND status='pending' AND expires_at>=?)""",
        "(SELECT COUNT(*) FROM workspace_members WHERE workspace_id=? AND removed_at IS NULL)<?",
        "(SELECT COUNT(*) FROM workspace_members WHERE user_id=? AND removed_at IS NULL)<?"]
    values = [workspace_id, owners[0]["snapshot"], invite["id"], workspace_id, token_hash,
        invite["github_recipient_id"], invite["role"], inviter_id, invite["created_by_revision"], invite["revision"], now,
        workspace_id, MAX_MEMBERS, actor["id"], MAX_MEMBERS]
    if inviter_id != workspace_id:
        checks.append("""EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?
            AND role='admin' AND revision=? AND removed_at IS NULL)""")
        values.extend([workspace_id, inviter_id, inviter["revision"]])
    if member:
        checks.append("""EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?
            AND revision=? AND removed_at=?)""")
        values.extend([workspace_id, actor["id"], member["revision"], member["removed_at"]])
        mutation = binding.prepare("""UPDATE workspace_members SET role=?,revision=?,joined_at=?,
            updated_at=?,removed_at=NULL,invited_by_user_id=? WHERE workspace_id=? AND user_id=? AND revision=?""")
        mutation = mutation.bind(invite["role"], revision, timestamp(now), timestamp(now), inviter_id,
                                 workspace_id, actor["id"], member["revision"])
    else:
        checks.append("NOT EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?)")
        values.extend([workspace_id, actor["id"]])
        mutation = binding.prepare("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,
            joined_at,updated_at,removed_at,invited_by_user_id) VALUES(?,?,?,1,?,?,NULL,?)""")
        mutation = mutation.bind(workspace_id, actor["id"], invite["role"], timestamp(now), timestamp(now), inviter_id)
    # First guard identifies the ledger payer; recipient's personal credential
    # guard authenticates them without requiring the membership being created.
    await _write(binding, [_guard(binding, checks, values), write_guard(binding, proof, actor["id"], now),
        mutation, binding.prepare("""UPDATE workspace_invites SET status='accepted',revision=revision+1,
            accepted_by_user_id=?,accepted_at=?,updated_at=? WHERE id=? AND revision=?""")
            .bind(actor["id"], timestamp(now), timestamp(now), invite["id"], invite["revision"]),
        _event(binding, workspace_id, actor["id"], "accept_invite", actor["id"],
               {"role": member["role"], "revision": member["revision"], "removed": True} if member else None,
               {"role": invite["role"], "revision": revision}, now)])
    return 200, {"workspace": workspace_payload(owner, invite["role"], revision)}


async def handle_workspace_request(*, binding, gateway, method, path, headers, body, now):
    try:
        if path == "/api/v1/workspaces":
            return await _list(binding, headers, now) if method == "GET" else _error(405, "METHOD_NOT_ALLOWED")
        if path in {"/api/v1/workspace-invitations/preview", "/api/v1/workspace-invitations/accept"}:
            return await _accept(binding, headers, body, now, path.rsplit("/", 1)[1]) if method == "POST" else _error(405, "METHOD_NOT_ALLOWED")
        parts = path.strip("/").split("/")
        if (len(parts) not in {5, 6} or parts[:3] != ["api", "v1", "workspaces"]
                or not _ID.fullmatch(parts[3]) or parts[4] not in {"members", "invites"}
                or len(parts) == 6 and not _ID.fullmatch(parts[5])):
            return _error(404, "NOT_FOUND")
        workspace_id, kind = parts[3:5]
        item_id = parts[5] if len(parts) == 6 else None
        allowed = {"GET"} if not item_id else {"PATCH", "DELETE"}
        if kind == "invites":
            allowed = {"GET", "POST"} if not item_id else {"DELETE"}
        if method not in allowed:
            return _error(405, "METHOD_NOT_ALLOWED")
        if kind == "members":
            return await _members(binding, headers, body, now, workspace_id, item_id, method)
        return await _invites(binding, gateway, headers, body, now, workspace_id, item_id, method)
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    except GitHubFailure as error:
        return _error(error.status, error.code)
    except PlanLimitError as error:
        return error.response()
