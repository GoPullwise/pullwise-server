"""Cookie-only workspace governance over existing, immutable ledger owners.

Invitation links create identity-backed join requests. Only the original
inviter's approval grants finance access; GitHub rights are never shared.
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
from .cloudflare_state_records import record_name

ROLES = {"admin", "editor", "viewer"}
MAX_MEMBERS = 99  # plus the implicit owner
MAX_INVITES = 100
MAX_JOIN_REQUESTS = 100
INVITATION_AGE = 86400
MAX_REVISION = 9007199254740991
_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
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


def _invite(row, reviewer_id=None, reviewer_role=None, reviewer_revision=None):
    _, timestamp, _ = _helpers()
    recipient = {"githubId": str(row["github_recipient_id"]), "login": row["github_login"]} if row["github_recipient_id"] is not None else None
    return {"id": row["id"], "recipient": recipient,
            "createdByUserId": row["created_by_user_id"],
            "canReview": reviewer_id == row["created_by_user_id"] and
                (reviewer_role == "owner" and row["created_by_revision"] == 1 or
                 reviewer_role == "admin" and reviewer_revision == row["created_by_revision"] and row["role"] != "admin"),
            "role": row["role"], "status": row["status"],
            "revision": row["revision"], "expiresAt": timestamp(row["expires_at"]),
            "createdAt": row["created_at"]}


def _join_request(row, applicant):
    return {"id": row["id"], "invitationId": row["invite_id"], "workspaceId": row["workspace_id"],
            "applicant": {"userId": row["applicant_user_id"], "name": applicant.get("name"),
                          "githubLogin": applicant.get("githubLogin")},
            "status": row["status"], "revision": row["revision"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "reviewedAt": row.get("reviewed_at")}


def _inviter_authorized(invite, inviter):
    if invite["created_by_user_id"] == invite["workspace_id"]:
        return invite["created_by_revision"] == 1
    return (inviter is not None and inviter["removed_at"] is None and inviter["role"] == "admin"
            and inviter["revision"] == invite["created_by_revision"] and invite["role"] != "admin")


def _invitation_checks(invite, owner_snapshot, now):
    checks = ["EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?)",
        """EXISTS(SELECT 1 FROM workspace_invites WHERE id=? AND workspace_id=?
            AND token_hash=? AND github_recipient_id IS ? AND role=? AND created_by_user_id=?
            AND created_by_revision=? AND revision=? AND status='pending' AND expires_at>=?)"""]
    values = [record_name("users", invite["workspace_id"]), owner_snapshot, invite["id"],
        invite["workspace_id"], invite["token_hash"], invite["github_recipient_id"], invite["role"],
        invite["created_by_user_id"], invite["created_by_revision"], invite["revision"], now]
    if invite["created_by_user_id"] != invite["workspace_id"]:
        checks.append("""EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?
            AND role='admin' AND revision=? AND removed_at IS NULL)""")
        values.extend([invite["workspace_id"], invite["created_by_user_id"], invite["created_by_revision"]])
    return checks, values


def _pending_workspace(owner, role, revision):
    workspace = workspace_payload(owner, role, revision)
    workspace["permissions"] = {name: False for name in workspace["permissions"]}
    return workspace


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
        m.workspace_id,m.role,m.revision,a.payload AS snapshot
        FROM workspace_members m JOIN app_state a ON a.name='record:users:'||m.workspace_id
        WHERE m.user_id=?
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
    sql = """SELECT m.*,a.payload AS snapshot FROM workspace_members m
        JOIN app_state a ON a.name='record:users:'||m.user_id
        WHERE m.workspace_id=? AND m.removed_at IS NULL"""
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
        return 200, {"items": [_invite(row, proof["actor_user_id"], proof["workspace_role"],
            proof["workspace_revision"]) for row in rows[:MAX_INVITES]]}
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
    if (not isinstance(body, dict) or set(body) != {"role"}
            or not isinstance(body["role"], str) or body["role"] not in ROLES):
        return _error(422, "INVALID_INPUT")
    _manage(user, body["role"])
    if len(rows) >= MAX_INVITES:
        return _error(403, "INVITATION_LIMIT")
    token, invite_id = "pwi_" + _random_urlsafe(32), "win_" + uuid.uuid4().hex
    row = {"id": invite_id, "workspace_id": workspace_id, "github_recipient_id": None,
        "github_login": None, "role": body["role"], "status": "pending", "revision": 1,
        "created_by_user_id": proof["actor_user_id"], "created_by_revision": proof["workspace_revision"],
        "expires_at": now + INVITATION_AGE, "created_at": timestamp(now)}
    await _write(binding, [write_guard(binding, proof, workspace_id, now),
        _guard(binding, ["""(SELECT COUNT(*) FROM workspace_invites WHERE workspace_id=?
                AND status='pending' AND expires_at>=?)<?"""], [workspace_id, now, MAX_INVITES]),
        binding.prepare("""INSERT INTO workspace_invites(id,workspace_id,github_recipient_id,github_login,
            token_hash,role,status,revision,expires_at,created_by_user_id,created_by_revision,created_at,updated_at)
            VALUES(?,?,NULL,NULL,?,?,'pending',1,?,?,?,?,?)""").bind(invite_id, workspace_id,
                hashlib.sha256(token.encode()).hexdigest(), body["role"], row["expires_at"],
                proof["actor_user_id"], proof["workspace_revision"], row["created_at"], row["created_at"]),
        _event(binding, workspace_id, proof["actor_user_id"], "invite", invite_id, None, _invite(row), now)])
    return 201, {**_invite(row, proof["actor_user_id"], proof["workspace_role"],
                          proof["workspace_revision"]), "token": token}


def _github_id(user):
    value = user.get("githubId")
    return int(value) if (isinstance(value, str) and value.isdigit()
                         or type(value) is int) and 1 <= int(value) <= MAX_REVISION else None


async def _accept(binding, headers, body, now, method):
    if (not isinstance(body, dict) or set(body) != {"token"}
            or not isinstance(body["token"], str) or not _TOKEN.fullmatch(body["token"])):
        return _error(422, "INVALID_INPUT")
    token_hash = hashlib.sha256(body["token"].encode()).hexdigest()
    actor, _, rows = await _read(binding, _selected(headers), now, [binding.prepare(
        "SELECT * FROM workspace_invites WHERE token_hash=?").bind(token_hash)])
    if len(rows[0]) != 1:
        return _error(404, "INVITATION_NOT_FOUND")
    invite = rows[0][0]
    if invite["github_recipient_id"] is not None and _github_id(actor) != invite["github_recipient_id"]:
        return _error(403, "INVITATION_RECIPIENT_MISMATCH")
    if invite["status"] == "revoked":
        return _error(410, "INVITATION_REVOKED")
    accepted_preview = invite["status"] == "accepted" and method == "preview"
    if invite["status"] == "accepted" and not accepted_preview:
        return _error(410, "INVITATION_ACCEPTED")
    if not accepted_preview and (invite["status"] != "pending" or invite["expires_at"] < now):
        return _error(410, "INVITATION_EXPIRED")
    workspace_id, inviter_id = invite["workspace_id"], invite["created_by_user_id"]
    # Ignore the applicant's current ledger selector, preserving actual identity.
    current_actor, proof, auth, validate = await _auth(binding, _selected(headers), now)
    found = await binding.batch([*auth, binding.prepare(
        "SELECT payload AS snapshot FROM app_state WHERE name=?").bind(record_name("users", workspace_id)),
        binding.prepare("SELECT role,revision,removed_at FROM workspace_members WHERE workspace_id=? AND user_id=?")
            .bind(workspace_id, inviter_id),
        binding.prepare("SELECT * FROM workspace_members WHERE workspace_id=? AND user_id=?")
            .bind(workspace_id, actor["id"]),
        binding.prepare("SELECT * FROM workspace_join_requests WHERE invite_id=? AND applicant_user_id=?")
            .bind(invite["id"], actor["id"]),
        binding.prepare("SELECT COUNT(*) AS total FROM workspace_join_requests WHERE invite_id=?")
            .bind(invite["id"])])
    validate([part.results for part in found[:len(auth)]])
    if current_actor != actor:
        return _error(403, "AUTHORIZATION_CHANGED")
    owners, inviters, members, requests, counts = (part.results for part in found[len(auth):])
    if len(owners) != 1:
        return _error(404, "WORKSPACE_NOT_FOUND")
    owner = json.loads(owners[0]["snapshot"])
    if not isinstance(owner, dict) or owner.get("id") != workspace_id or actor["id"] == workspace_id:
        return _error(403, "OWNER_IMMUTABLE")
    member = members[0] if len(members) == 1 else None
    request = requests[0] if len(requests) == 1 else None
    request_dto = _join_request(request, actor) if request else None
    if accepted_preview:
        # Existing accepted invitations remain recoverable, including legacy
        # acceptances that predate requests. Current membership owns authority.
        if (invite["accepted_by_user_id"] != actor["id"] or not member
                or member["removed_at"] is not None or member["role"] not in ROLES):
            return _error(410, "INVITATION_ACCEPTED")
        return 200, {**_invite(invite), "request": request_dto, "workspace": workspace_payload(
            owner, member["role"], member["revision"])}
    inviter = inviters[0] if len(inviters) == 1 else None
    if not _inviter_authorized(invite, inviter):
        return _error(403, "INVITATION_AUTHORITY_LOST")
    if member and member["removed_at"] is None:
        return _error(409, "ALREADY_MEMBER")
    revision = member["revision"] + 1 if member else 1
    if revision > MAX_REVISION:
        return _error(409, "REVISION_EXHAUSTED")
    preview = {**_invite(invite), "request": request_dto,
               "workspace": _pending_workspace(owner, invite["role"], revision)}
    if method == "preview" or request:
        # Retrying an application never duplicates audit, changes the invite,
        # or overrides an inviter's rejection.
        return 200, preview
    if counts[0]["total"] >= MAX_JOIN_REQUESTS:
        return _error(403, "INVITATION_REQUEST_LIMIT")
    _, timestamp, write_guard = _helpers()
    stamp = timestamp(now)
    request = {"id": "wjr_" + uuid.uuid4().hex, "workspace_id": workspace_id,
        "invite_id": invite["id"], "applicant_user_id": actor["id"], "status": "pending", "revision": 1,
        "created_at": stamp, "updated_at": stamp, "reviewed_at": None}
    checks, values = _invitation_checks(invite, owners[0]["snapshot"], now)
    checks.extend(["(SELECT COUNT(*) FROM workspace_join_requests WHERE invite_id=?)<?",
        "NOT EXISTS(SELECT 1 FROM workspace_join_requests WHERE invite_id=? AND applicant_user_id=?)",
        "NOT EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND removed_at IS NULL)"])
    values.extend([invite["id"], MAX_JOIN_REQUESTS, invite["id"], actor["id"], workspace_id, actor["id"]])
    await _write(binding, [_guard(binding, checks, values), write_guard(binding, proof, actor["id"], now),
        binding.prepare("""INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,
            status,revision,created_at,updated_at) VALUES(?,?,?,?,'pending',1,?,?)""")
            .bind(request["id"], workspace_id, invite["id"], actor["id"], stamp, stamp),
        _event(binding, workspace_id, actor["id"], "request_join", request["id"], None,
               _join_request(request, actor), now)])
    return 202, {**preview, "request": _join_request(request, actor)}


async def _request_inbox(binding, headers, now, workspace_id=None, invite_id=None):
    user, proof, auth, validate = await _auth(binding, headers, now, workspace_id)
    actor_id = user["_actor"]["id"]
    invite_columns = ("id", "workspace_id", "github_recipient_id", "github_login", "role", "status",
        "revision", "expires_at", "created_by_user_id", "created_by_revision", "created_at")
    sql = "SELECT r.*, " + ",".join("i." + field + " AS invite_" + field for field in invite_columns)
    sql += """,a.payload AS applicant_snapshot,o.payload AS owner_snapshot,
        m.role AS inviter_role,m.revision AS inviter_revision,m.removed_at AS inviter_removed_at
        FROM workspace_invites i JOIN workspace_join_requests r ON r.invite_id=i.id
        JOIN app_state a ON a.name='record:users:'||r.applicant_user_id
        JOIN app_state o ON o.name='record:users:'||i.workspace_id
        LEFT JOIN workspace_members m ON m.workspace_id=i.workspace_id AND m.user_id=i.created_by_user_id
        WHERE i.created_by_user_id=? AND i.status='pending' AND i.expires_at>=? AND r.status='pending'
        AND ((i.created_by_user_id=i.workspace_id AND i.created_by_revision=1) OR
            (m.role='admin' AND m.revision=i.created_by_revision AND m.removed_at IS NULL AND i.role!='admin'))"""
    values = [actor_id, now]
    commands = []
    if workspace_id and invite_id:
        commands.append(binding.prepare("SELECT * FROM workspace_invites WHERE id=? AND workspace_id=?")
                        .bind(invite_id, workspace_id))
        sql += " AND i.workspace_id=? AND i.id=?"
        values.extend([workspace_id, invite_id])
    elif workspace_id:
        sql += " AND i.workspace_id=?"
        values.append(workspace_id)
    sql += " ORDER BY r.created_at,r.id LIMIT 101"
    parts = await binding.batch([*auth, *commands, binding.prepare(sql).bind(*values)])
    validate([part.results for part in parts[:len(auth)]])
    _cookie_only(headers, proof)
    if workspace_id:
        _manage(user)
    if workspace_id and invite_id:
        invitations = parts[len(auth)].results
        if len(invitations) != 1:
            return _error(404, "INVITATION_NOT_FOUND")
        if invitations[0]["created_by_user_id"] != actor_id:
            return _error(403, "INVITATION_REVIEW_FORBIDDEN")
        _manage(user, invitations[0]["role"])
    rows = parts[-1].results
    items = []
    for row in rows[:MAX_JOIN_REQUESTS]:
        applicant, owner = json.loads(row["applicant_snapshot"]), json.loads(row["owner_snapshot"])
        if (not isinstance(applicant, dict) or applicant.get("id") != row["applicant_user_id"]
                or not isinstance(owner, dict) or owner.get("id") != row["workspace_id"]):
            continue
        request = _join_request(row, applicant)
        if not invite_id:
            invitation = {field: row["invite_" + field] for field in invite_columns}
            role = "owner" if actor_id == row["workspace_id"] else row["inviter_role"]
            revision = 1 if role == "owner" else row["inviter_revision"]
            request = {**request, "invitation": _invite(invitation, actor_id, role, revision),
                       "workspace": workspace_payload(owner, role, revision)}
        items.append(request)
    return 200, {"items": items, "hasMore": len(rows) > MAX_JOIN_REQUESTS}


async def _review(binding, headers, body, now, workspace_id, invite_id, request_id, action):
    if body is not None and (not isinstance(body, dict) or body):
        return _error(422, "INVALID_INPUT")
    user, proof, auth, validate = await _auth(binding, headers, now, workspace_id)
    found = await binding.batch([*auth, binding.prepare("SELECT * FROM workspace_invites WHERE id=? AND workspace_id=?")
        .bind(invite_id, workspace_id),
        binding.prepare("""SELECT r.*,a.payload AS applicant_snapshot FROM workspace_join_requests r
            JOIN app_state a ON a.name='record:users:'||r.applicant_user_id
            WHERE r.id=? AND r.invite_id=? AND r.workspace_id=?""").bind(request_id, invite_id, workspace_id)])
    validate([part.results for part in found[:len(auth)]])
    _cookie_only(headers, proof)
    _manage(user)
    invites, requests = (part.results for part in found[len(auth):])
    if len(invites) != 1 or len(requests) != 1:
        return _error(404, "INVITATION_REQUEST_NOT_FOUND")
    invite, request = invites[0], requests[0]
    if invite["created_by_user_id"] != proof["actor_user_id"]:
        return _error(403, "INVITATION_REVIEW_FORBIDDEN")
    _manage(user, invite["role"])
    inviter = proof.get("workspace_member")
    if not _inviter_authorized(invite, inviter):
        return _error(403, "INVITATION_AUTHORITY_LOST")
    if invite["status"] == "revoked":
        return _error(410, "INVITATION_REVOKED")
    if invite["status"] == "accepted":
        return _error(410, "INVITATION_ACCEPTED")
    if invite["status"] != "pending" or invite["expires_at"] < now:
        return _error(410, "INVITATION_EXPIRED")
    expected = _expected(headers, request)
    if request["status"] != "pending":
        return _error(409, "INVITATION_REQUEST_REVIEWED")
    applicant = json.loads(request["applicant_snapshot"])
    if not isinstance(applicant, dict) or applicant.get("id") != request["applicant_user_id"]:
        return _error(404, "INVITATION_REQUEST_NOT_FOUND")
    if (request["applicant_user_id"] == workspace_id or invite["github_recipient_id"] is not None
            and _github_id(applicant) != invite["github_recipient_id"]):
        return _error(403, "INVITATION_RECIPIENT_MISMATCH")
    _, timestamp, write_guard = _helpers()
    checks, values = _invitation_checks(invite, proof["owner_user"], now)
    checks.extend(["EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?)",
        """EXISTS(SELECT 1 FROM workspace_join_requests WHERE id=? AND workspace_id=? AND invite_id=?
            AND applicant_user_id=? AND status='pending' AND revision=?)"""])
    values.extend([record_name("users", applicant["id"]), request["applicant_snapshot"], request_id,
                   workspace_id, invite_id, applicant["id"], expected])
    stamp, mutations = timestamp(now), []
    workspace = user["_workspace"]
    if action == "approve":
        if invite["revision"] == MAX_REVISION:
            return _error(409, "REVISION_EXHAUSTED")
        # Revalidate reviewer plus applicant membership/capacities in one read.
        current, current_proof, auth, validate = await _auth(binding, headers, now, workspace_id)
        resources = await binding.batch([*auth,
            binding.prepare("SELECT * FROM workspace_members WHERE workspace_id=? AND user_id=?")
                .bind(workspace_id, applicant["id"]),
            binding.prepare("""SELECT
              (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=? AND removed_at IS NULL) AS members,
              (SELECT COUNT(*) FROM workspace_members WHERE user_id=? AND removed_at IS NULL) AS workspaces""")
                .bind(workspace_id, applicant["id"])])
        validate([part.results for part in resources[:len(auth)]])
        if current != user:
            return _error(403, "AUTHORIZATION_CHANGED")
        proof = current_proof
        members, counts = (part.results for part in resources[len(auth):])
        member = members[0] if len(members) == 1 else None
        if member and member["removed_at"] is None:
            return _error(409, "ALREADY_MEMBER")
        revision = member["revision"] + 1 if member else 1
        if revision > MAX_REVISION:
            return _error(409, "REVISION_EXHAUSTED")
        if counts[0]["members"] >= MAX_MEMBERS or counts[0]["workspaces"] >= MAX_MEMBERS:
            return _error(403, "WORKSPACE_LIMIT")
        checks.extend(["(SELECT COUNT(*) FROM workspace_members WHERE workspace_id=? AND removed_at IS NULL)<?",
                       "(SELECT COUNT(*) FROM workspace_members WHERE user_id=? AND removed_at IS NULL)<?"])
        values.extend([workspace_id, MAX_MEMBERS, applicant["id"], MAX_MEMBERS])
        if member:
            checks.append("""EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?
                AND revision=? AND removed_at=?)""")
            values.extend([workspace_id, applicant["id"], member["revision"], member["removed_at"]])
            mutations.append(binding.prepare("""UPDATE workspace_members SET role=?,revision=?,joined_at=?,
                updated_at=?,removed_at=NULL,invited_by_user_id=? WHERE workspace_id=? AND user_id=? AND revision=?""")
                .bind(invite["role"], revision, stamp, stamp, proof["actor_user_id"], workspace_id,
                      applicant["id"], member["revision"]))
        else:
            checks.append("NOT EXISTS(SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?)")
            values.extend([workspace_id, applicant["id"]])
            mutations.append(binding.prepare("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,
                joined_at,updated_at,removed_at,invited_by_user_id) VALUES(?,?,?,1,?,?,NULL,?)""")
                .bind(workspace_id, applicant["id"], invite["role"], stamp, stamp, proof["actor_user_id"]))
        mutations.append(binding.prepare("""UPDATE workspace_invites SET status='accepted',revision=revision+1,
            accepted_by_user_id=?,accepted_at=?,updated_at=? WHERE id=? AND revision=?""")
            .bind(applicant["id"], stamp, stamp, invite_id, invite["revision"]))
        workspace = workspace_payload(user, invite["role"], revision)
    state = "approved" if action == "approve" else "rejected"
    after = {**request, "status": state, "revision": expected + 1, "updated_at": stamp, "reviewed_at": stamp}
    mutations.append(binding.prepare("""UPDATE workspace_join_requests SET status=?,revision=revision+1,
        updated_at=?,reviewed_by_user_id=?,reviewed_at=? WHERE id=? AND revision=?""")
        .bind(state, stamp, proof["actor_user_id"], stamp, request_id, expected))
    await _write(binding, [write_guard(binding, proof, workspace_id, now), _guard(binding, checks, values),
        *mutations, _event(binding, workspace_id, proof["actor_user_id"],
            "approve_join" if action == "approve" else "reject_join", request_id,
            _join_request(request, applicant), _join_request(after, applicant), now)])
    return 200, {"request": _join_request(after, applicant), "workspace": workspace}


async def handle_workspace_request(*, binding, gateway, method, path, headers, body, now):
    try:
        if path == "/api/v1/workspaces":
            return await _list(binding, headers, now) if method == "GET" else _error(405, "METHOD_NOT_ALLOWED")
        if path == "/api/v1/workspace-invitation-requests":
            return await _request_inbox(binding, headers, now) if method == "GET" else _error(405, "METHOD_NOT_ALLOWED")
        if path in {"/api/v1/workspace-invitations/preview", "/api/v1/workspace-invitations/accept"}:
            return await _accept(binding, headers, body, now, path.rsplit("/", 1)[1]) if method == "POST" else _error(405, "METHOD_NOT_ALLOWED")
        parts = path.strip("/").split("/")
        if (len(parts) == 5 and parts[:3] == ["api", "v1", "workspaces"]
                and _ID.fullmatch(parts[3]) and parts[4] == "join-requests"):
            return await _request_inbox(binding, headers, now, parts[3]) if method == "GET" else _error(405, "METHOD_NOT_ALLOWED")
        if (len(parts) in {7, 9} and parts[:3] == ["api", "v1", "workspaces"]
                and parts[4] == "invites" and parts[6] == "requests"
                and _ID.fullmatch(parts[3]) and _ID.fullmatch(parts[5])):
            if len(parts) == 7:
                return await _request_inbox(binding, headers, now, parts[3], parts[5]) if method == "GET" else _error(405, "METHOD_NOT_ALLOWED")
            if not _ID.fullmatch(parts[7]) or parts[8] not in {"approve", "reject"}:
                return _error(404, "NOT_FOUND")
            return await _review(binding, headers, body, now, parts[3], parts[5], parts[7], parts[8]) if method == "POST" else _error(405, "METHOD_NOT_ALLOWED")
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
