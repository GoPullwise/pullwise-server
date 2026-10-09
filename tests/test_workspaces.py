"""Workspace governance through real SQL transactions and persisted credentials."""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from ledger_d1_fixture import D1ShapedSQLite, Store
from pullwise_server.cloudflare_github_gateway import GitHubFailure
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000
OWNER = "usr_github_1"
RECIPIENT = "usr_github_5"


class Gateway:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def unseal(self, value):
        self.calls.append(("unseal", value))
        return "synthetic-token"

    async def _json(self, url, *, token):
        self.calls.append((url, token))
        if self.failure:
            raise self.failure
        return {"id": 5, "login": "recipient", "type": "User"}


class RaceD1(D1ShapedSQLite):
    race = None

    async def batch(self, statements):
        if self.race and self.race[0](statements):
            _, mutation = self.race
            self.race = None
            with self.store._immediate() as db:
                mutation(db)
        return await super().batch(statements)


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    with store.connect() as db:
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
            db.executescript(path.read_text())
        users = {f"usr_github_{number}": {"id": f"usr_github_{number}",
            "githubId": str(number), "githubLogin": f"user{number}", "name": f"User {number}",
            "githubAccessToken": f"sealed:token-{number}", "createdAt": NOW - 1000,
            "billing": {"plan": "pro" if number == 1 else "free", "status": "active",
                "subscriptionId": "sub_owner" if number == 1 else None,
                "currentPeriodStart": NOW - 1000, "currentPeriodEnd": NOW + 1000}}
            for number in range(1, 7)}
        sessions = {f"session-{number}": {"userId": f"usr_github_{number}", "expiresAt": NOW + 1000}
                    for number in range(1, 7)}
        for kind, records in (("users", users), ("sessions", sessions)):
            for identifier, value in records.items():
                db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                    (record_name(kind, identifier), encode_record(kind, identifier, value), NOW))
        for number, role in ((2, "admin"), (3, "editor"), (4, "viewer")):
            db.execute("INSERT INTO workspace_members VALUES(?,?,?,1,'joined','updated',NULL,?)",
                       (OWNER, f"usr_github_{number}", role, OWNER))
    binding, gateway = RaceD1(store), Gateway()

    def call(method, path, body=None, *, actor=1, headers=None, now=NOW, plan=False):
        auth = {"Cookie": f"pw_session=session-{actor}", "Origin": "https://app.example.test"}
        auth.update(headers or {})
        return asyncio.run(handle_ledger_request(binding=PlanLimitedD1(binding, now=now) if plan else binding,
            gateway=gateway, method=method, path=path, headers=auth, params={}, body=body, now=now))

    return store, binding, gateway, call


def path(kind="invites", identifier=None):
    return f"/api/v1/workspaces/{OWNER}/{kind}" + ("/" + identifier if identifier else "")


def invite(app, role="viewer", actor=1, plan=False):
    status, payload = app[3]("POST", path(), {"role": role}, actor=actor, plan=plan)
    assert status == 201, payload
    return payload


def apply_to_invitation(app, issued, actor=5, method="accept", **kwargs):
    return app[3]("POST", "/api/v1/workspace-invitations/" + method,
                  {"token": issued["token"]}, actor=actor, **kwargs)


def review(app, issued, request, action="approve", actor=None, **kwargs):
    actor = actor or int(issued["createdByUserId"].rsplit("_", 1)[1])
    headers = {"If-Match": '"' + str(request["revision"]) + '"'}
    headers.update(kwargs.pop("headers", {}))
    return app[3]("POST", path(identifier=issued["id"]) + "/requests/" + request["id"] + "/" + action,
                  {}, actor=actor, headers=headers, **kwargs)


def accept(app, issued, actor=5, method="accept", **kwargs):
    """Complete the new two-party workflow for existing membership scenarios."""
    status, payload = apply_to_invitation(app, issued, actor=actor, method=method, **kwargs)
    if method == "preview" or status not in {200, 202}:
        return status, payload
    return review(app, issued, payload["request"], **kwargs)


def count(app, table):
    with app[0].connect() as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def member_key(app, *, actor=1, scopes=None, workspace=OWNER, restrictions=None):
    actor_id = f"usr_github_{actor}"
    limits = {"shared": False}
    if workspace is not None:
        revision = 1
        if workspace != actor_id:
            with app[0].connect() as db:
                revision = db.execute("SELECT revision FROM workspace_members WHERE workspace_id=? AND user_id=?",
                                      (workspace, actor_id)).fetchone()[0]
        limits.update(workspaceId=workspace, workspaceMemberRevision=revision)
    limits.update(restrictions or {})
    key_id = "member_key_" + str(count(app, "api_keys"))
    token = "pwk_synthetic_" + key_id
    with app[0]._immediate() as db:
        db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key_id, actor_id, "Synthetic members",
            token[:16], hashlib.sha256(token.encode()).hexdigest(),
            json.dumps(scopes or ["members:read", "members:write"]), None,
            json.dumps(limits), NOW, None, None))
    return {"Cookie": "", "Authorization": "Bearer " + token}


def test_workspace_list_ignores_selector_and_exposes_only_joined_ledgers(app):
    status, page = app[3]("GET", "/api/v1/workspaces", actor=4,
                         headers={"X-Pullwise-Workspace": "usr_github_6"})
    assert status == 200
    assert {(item["id"], item["role"]) for item in page["items"]} == {
        ("usr_github_4", "owner"), (OWNER, "viewer")}
    assert all("token" not in json.dumps(item) for item in page["items"])
    assert count(app, "ledger_plan_usage") == count(app, "workspace_events") == 0


def test_named_user_records_scale_without_global_json_maps_or_get_writes(app):
    with app[0]._immediate() as db:
        for number in range(100, 300):
            identifier = "usr_github_" + str(number)
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("users", identifier), encode_record("users", identifier,
                    {"id": identifier, "name": "Scale fixture " + str(number), "githubId": str(number)}), NOW))
        total = db.execute("SELECT SUM(length(payload)) FROM app_state WHERE name GLOB 'record:users:*'").fetchone()[0]
        assert total > 8192
    with app[0].connect() as db:
        before = list(db.iterdump())
    used, original = [], app[1].prepare
    def observe(sql):
        used.append(sql)
        return original(sql)
    app[1].prepare = observe
    status, ledgers = app[3]("GET", "/api/v1/workspaces", actor=4)
    assert status == 200 and {item["id"] for item in ledgers["items"]} == {"usr_github_4", OWNER}
    status, members = app[3]("GET", path("members"), actor=4)
    assert status == 200 and len(members["items"]) == 4
    assert not any("json_each" in sql or "name='users'" in sql or "name='sessions'" in sql for sql in used)
    with app[0].connect() as db:
        assert list(db.iterdump()) == before
        joins = [sql for sql in used if "JOIN app_state" in sql]
        assert len(joins) == 2
        for sql in joins:
            parameters = ("usr_github_4",) if "WHERE m.user_id=?" in sql else (OWNER,)
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
            assert any("SEARCH a USING INDEX sqlite_autoindex_app_state_1 (name=?)" in step for step in plan)


def test_large_retained_owner_record_keeps_invitation_fences_and_payer(app):
    with app[0]._immediate() as db:
        saved = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", OWNER),)).fetchone()[0])
        saved["retainedAccountHistory"] = "synthetic retained field " * 1000
        encoded = encode_record("users", OWNER, saved)
        assert len(encoded.encode("utf-8")) > 8192
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encoded, record_name("users", OWNER)))
    issued = invite(app, plan=True)
    status, joined = accept(app, issued, plan=True)
    assert status == 200 and joined["workspace"]["id"] == OWNER
    status, recovered = accept(app, issued, method="preview", plan=True)
    assert status == 200 and recovered["workspace"]["role"] == "viewer"
    with app[0].connect() as db:
        assert [tuple(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")] == [(OWNER, 3)]
        assert db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("users", OWNER),)).fetchone()[0] == encoded


def test_members_viewer_can_read_but_not_govern(app):
    status, page = app[3]("GET", path("members"), actor=4)
    assert status == 200 and len(page["items"]) == 4
    assert page["items"][0]["role"] == "owner"
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
                  actor=4, headers={"If-Match": '"1"'})[0] == 403
    assert app[3]("GET", path(), actor=4)[0] == 403
    assert count(app, "workspace_events") == 0


def linked_email_recipient(app, *, github=True, joined=False):
    identifier = "usr_email_recipient"
    user = {"id": identifier, "name": "Email recipient", "email": "recipient@example.test",
            "emailVerified": True, "emailVerifiedAt": NOW - 100, "providers": ["email"]}
    if github:
        user.update(githubId="5", githubLogin="recipient", providers=["email", "github"])
    with app[0]._immediate() as db:
        db.execute("DELETE FROM app_state WHERE name=?", (record_name("users", RECIPIENT),))
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users", identifier), encode_record("users", identifier, user), NOW))
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
            (encode_record("sessions", "session-5", {"userId": identifier, "expiresAt": NOW + 1000}),
             record_name("sessions", "session-5")))
        if github:
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("githubIdentities", "5"), encode_record("githubIdentities", "5",
                 {"githubId": "5", "userId": identifier, "createdAt": NOW - 100}), NOW))
        if joined:
            db.execute("INSERT INTO workspace_members VALUES(?,?,'viewer',1,'joined','updated',NULL,?)",
                (OWNER, identifier, OWNER))
    return identifier


def test_unassigned_invitation_recognizes_already_joined_linked_email_identity(app):
    identifier = linked_email_recipient(app, joined=True)
    issued = invite(app)
    status, payload = apply_to_invitation(app, issued)
    assert (status, payload["error"]["code"]) == (409, "ALREADY_MEMBER")
    assert count(app, "workspace_invites") == count(app, "workspace_events") == 1
    assert count(app, "workspace_join_requests") == 0
    assert app[3]("GET", path("members"))[1]["items"][-1]["userId"] != RECIPIENT
    with app[0].connect() as db:
        assert db.execute("SELECT user_id FROM workspace_members WHERE user_id=?", (identifier,)).fetchone()


def test_linked_email_account_accepts_exact_github_recipient_without_legacy_user_id(app):
    identifier = linked_email_recipient(app)
    issued = invite(app)
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET github_recipient_id=5,github_login='old-login'")
    status, result = accept(app, issued)
    assert status == 200 and result["workspace"]["id"] == OWNER
    with app[0].connect() as db:
        assert db.execute("SELECT accepted_by_user_id FROM workspace_invites").fetchone()[0] == identifier
        assert db.execute("SELECT user_id FROM workspace_members WHERE user_id=?", (identifier,)).fetchone()
        assert db.execute("SELECT user_id FROM workspace_members WHERE user_id=?", (RECIPIENT,)).fetchone() is None


def test_verified_email_alone_does_not_grant_github_invitation_recipient_authority(app):
    linked_email_recipient(app, github=False)
    issued = invite(app)
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET github_recipient_id=5,github_login='old-login'")
    status, payload = accept(app, issued)
    assert (status, payload["error"]["code"]) == (403, "INVITATION_RECIPIENT_MISMATCH")
    assert count(app, "workspace_members") == 3


@pytest.mark.parametrize("email_only_inviter", [False, True])
def test_email_only_account_requests_and_original_inviter_approves_unassigned_link(app, email_only_inviter):
    identifier = linked_email_recipient(app, github=False)
    if email_only_inviter:
        with app[0]._immediate() as db:
            owner = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                (record_name("users", OWNER),)).fetchone()[0])
            for field in ("githubId", "githubLogin", "githubAccessToken"):
                owner.pop(field, None)
            owner.update(email="owner@example.test", emailVerified=True,
                emailVerifiedAt=NOW - 100, providers=["email"])
            db.execute("UPDATE app_state SET payload=? WHERE name=?",
                (encode_record("users", OWNER, owner), record_name("users", OWNER)))
    with app[0].connect() as db:
        applicant_before = db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", identifier),)).fetchone()[0]
        owner_before = db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", OWNER),)).fetchone()[0]
    issued = invite(app, role="editor")
    assert issued["recipient"] is None and issued["createdByUserId"] == OWNER
    status, preview = apply_to_invitation(app, issued, method="preview")
    assert status == 200 and preview["request"] is None
    assert not any(preview["workspace"]["permissions"].values())
    status, application = apply_to_invitation(app, issued,
        headers={"X-Pullwise-Workspace": OWNER})
    assert status == 202 and application["request"]["status"] == "pending"
    assert application["request"]["applicant"] == {
        "userId": identifier, "name": "Email recipient", "githubLogin": None}
    assert app[3]("GET", "/api/v1/expenses", actor=5,
        headers={"X-Pullwise-Workspace": OWNER})[0] == 404
    assert count(app, "workspace_members") == 3
    assert review(app, issued, application["request"], actor=2)[0] == 403
    assert review(app, issued, application["request"], actor=5)[0] == 404
    inbox = app[3]("GET", "/api/v1/workspace-invitation-requests")[1]
    assert [item["applicant"]["userId"] for item in inbox["items"]] == [identifier]
    status, approved = review(app, issued, application["request"])
    assert status == 200 and approved["request"]["status"] == "approved"
    assert approved["workspace"]["id"] == OWNER
    assert approved["workspace"]["permissions"]["writeExpenses"] is True
    assert app[3]("GET", "/api/v1/expenses", actor=5,
        headers={"X-Pullwise-Workspace": OWNER})[0] == 200
    assert review(app, issued, approved["request"])[0] == 410
    assert app[2].calls == []
    with app[0].connect() as db:
        member = db.execute("SELECT * FROM workspace_members WHERE user_id=?", (identifier,)).fetchone()
        assert (member["workspace_id"], member["role"], member["invited_by_user_id"]) == (OWNER, "editor", OWNER)
        assert db.execute("SELECT accepted_by_user_id FROM workspace_invites").fetchone()[0] == identifier
        assert db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", identifier),)).fetchone()[0] == applicant_before
        assert db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", OWNER),)).fetchone()[0] == owner_before
        assert db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", RECIPIENT),)).fetchone() is None


@pytest.mark.parametrize("actor,target,new_role", [(2, 2, "viewer"), (2, 3, "admin"), (3, 4, "editor"), (4, 3, "viewer")])
def test_roles_cannot_self_promote_or_modify_peer_admin(app, actor, target, new_role):
    assert app[3]("PATCH", path("members", f"usr_github_{target}"), {"role": new_role},
                  actor=actor, headers={"If-Match": '"1"'})[0] == 403
    assert count(app, "workspace_events") == 0


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_implicit_owner_cannot_be_changed_or_removed(app, method):
    status, payload = app[3](method, path("members", OWNER), {"role": "viewer"},
                             headers={"If-Match": '"1"'})
    assert (status, payload["error"]["code"]) == (403, "OWNER_IMMUTABLE")


@pytest.mark.parametrize("header,status", [({}, 428), ({"If-Match": '"2"'}, 412), ({"If-Match": "1"}, 422)])
def test_role_change_requires_current_revision(app, header, status):
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"}, headers=header)[0] == status
    assert count(app, "workspace_events") == 0


def test_role_and_removal_advance_revision_and_audit_actual_actor(app):
    status, updated = app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
                             actor=2, headers={"If-Match": '"1"'})
    assert status == 200 and updated["revision"] == 2
    assert app[3]("DELETE", path("members", "usr_github_3"), actor=2,
                  headers={"If-Match": '"2"'}) == (204, None)
    with app[0].connect() as db:
        row = db.execute("SELECT revision,removed_at FROM workspace_members WHERE user_id='usr_github_3'").fetchone()
        events = [dict(row) for row in db.execute("SELECT * FROM workspace_events")]
    assert row["revision"] == 3 and row["removed_at"]
    assert {event["actor_user_id"] for event in events} == {"usr_github_2"}
    assert "session-" not in json.dumps(events)
    assert app[3]("GET", path("members"), actor=3)[0] == 404


def test_selector_conflict_and_unjoined_workspace_fail_closed(app):
    assert app[3]("GET", path("members"), headers={"X-Pullwise-Workspace": "usr_github_6"})[0] == 422
    assert app[3]("GET", path("members"), actor=6)[0] == 404


def test_invitation_hash_only_single_use_link_preview_no_writes(app):
    issued = invite(app)
    with app[0].connect() as db:
        saved = dict(db.execute("SELECT * FROM workspace_invites").fetchone())
        audit = db.execute("SELECT after_json FROM workspace_events").fetchone()[0]
    assert saved["token_hash"] == hashlib.sha256(issued["token"].encode()).hexdigest()
    assert issued["token"] not in json.dumps(saved) + audit
    status, listed = app[3]("GET", path())
    assert status == 200 and "token" not in json.dumps(listed)
    assert accept(app, issued, actor=6, method="preview")[0] == 200
    before = count(app, "workspace_events")
    status, preview = accept(app, issued, method="preview")
    assert status == 200 and preview["workspace"]["id"] == OWNER
    assert count(app, "workspace_events") == before
    status, accepted = accept(app, issued)
    assert status == 200 and accepted["workspace"]["role"] == "viewer"
    assert accept(app, issued)[0] == 410
    assert count(app, "workspace_events") == 3
    assert app[2].calls == []  # no recipient/provider lookup


def test_invitation_acceptance_ignores_current_personal_workspace_selector(app):
    issued = invite(app)
    assert apply_to_invitation(app, issued, headers={"X-Pullwise-Workspace": "usr_github_5"})[0] == 202


def test_accepted_invitation_preview_recovers_current_membership_without_writing(app):
    issued = invite(app)
    assert accept(app, issued)[0] == 200
    assert app[3]("PATCH", path("members", RECIPIENT), {"role": "editor"},
        headers={"If-Match": '"1"'})[0] == 200
    before = count(app, "workspace_events")
    # Recovery remains useful after the original invitation's 24-hour expiry.
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET expires_at=?", (NOW - 1,))
    status, recovered = accept(app, issued, method="preview")
    assert status == 200 and recovered["status"] == "accepted"
    assert recovered["workspace"]["id"] == OWNER
    assert recovered["workspace"]["role"] == "editor" and recovered["workspace"]["revision"] == 2
    assert count(app, "workspace_events") == before
    assert accept(app, issued) == (410, {"error": {"code": "INVITATION_ACCEPTED"}})
    assert accept(app, issued, actor=6, method="preview")[0] == 410


def test_accepted_invitation_preview_cannot_restore_removed_membership(app):
    issued = invite(app)
    assert accept(app, issued)[0] == 200
    assert app[3]("DELETE", path("members", RECIPIENT), headers={"If-Match": '"1"'})[0] == 204
    before = count(app, "workspace_events")
    assert accept(app, issued, method="preview") == (410, {"error": {"code": "INVITATION_ACCEPTED"}})
    assert count(app, "workspace_events") == before
    assert app[3]("GET", path("members"), actor=5)[0] == 404


def test_accepted_membership_recovery_does_not_depend_on_former_inviter_role(app):
    issued = invite(app, actor=2)
    assert accept(app, issued)[0] == 200
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "editor"},
        headers={"If-Match": '"1"'})[0] == 200
    before = count(app, "workspace_events")
    status, recovered = accept(app, issued, method="preview")
    assert status == 200 and recovered["workspace"]["role"] == "viewer"
    assert count(app, "workspace_events") == before


def test_revoked_invitation_is_distinct_from_expiry_and_recipient_bound(app):
    issued = invite(app)
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={"If-Match": '"1"'})[0] == 204
    assert accept(app, issued, method="preview") == (410, {"error": {"code": "INVITATION_REVOKED"}})
    assert accept(app, issued, actor=6, method="preview")[0] == 410


def test_acceptance_charges_ledger_owner_not_recipient(app):
    issued = invite(app, plan=True)
    status, _ = accept(app, issued, plan=True)
    assert status == 200
    with app[0].connect() as db:
        usage = [dict(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")]
    assert usage == [{"owner_id": OWNER, "writes": 3}]


def test_removed_member_rejoins_with_new_revision(app):
    assert app[3]("DELETE", path("members", "usr_github_3"), headers={"If-Match": '"1"'})[0] == 204
    issued = invite(app, role="editor")
    status, response = accept(app, issued, actor=3)
    assert status == 200 and response["workspace"]["revision"] == 3


def test_admin_invite_loses_authority_after_demotion(app):
    issued = invite(app, actor=2)
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "editor"},
                  headers={"If-Match": '"1"'})[0] == 200
    assert accept(app, issued)[0] == 403
    assert count(app, "workspace_members") == 3


def test_admin_invitation_does_not_revive_after_demotion_and_repromotion(app):
    issued = invite(app, actor=2)
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "editor"},
                  headers={"If-Match": '"1"'})[0] == 200
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "admin"},
                  headers={"If-Match": '"2"'})[0] == 200
    assert accept(app, issued)[0] == 403
    assert count(app, "workspace_members") == 3


def test_admin_invitation_does_not_revive_after_removal_and_rejoin(app):
    issued = invite(app, actor=2)
    assert app[3]("DELETE", path("members", "usr_github_2"), headers={"If-Match": '"1"'})[0] == 204
    rejoin = invite(app, role="admin")
    status, joined = accept(app, rejoin, actor=2)
    assert status == 200 and joined["workspace"]["revision"] == 3
    assert accept(app, issued)[0] == 403
    assert count(app, "workspace_members") == 3


def test_admin_cannot_issue_or_revoke_admin_invitation(app):
    assert app[3]("POST", path(), {"role": "admin"}, actor=2)[0] == 403
    assert app[2].calls == []
    issued = invite(app, role="admin")
    assert app[3]("DELETE", path(identifier=issued["id"]), actor=2,
                  headers={"If-Match": '"1"'})[0] == 403
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={"If-Match": '"1"'})[0] == 204
    assert accept(app, issued)[0] == 410


def test_unassigned_links_are_independent_and_expired_tokens_are_rejected(app):
    issued = invite(app)
    assert app[3]("POST", path(), {"role": "editor"})[0] == 201
    assert accept(app, issued, now=NOW + 86401)[0] == 401  # session also expired
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET expires_at=?", (NOW - 1,))
    assert accept(app, issued)[0] == 410


@pytest.mark.parametrize("body", [{"githubLogin": "../users/root", "role": "viewer"},
    {"githubLogin": "recipient", "role": []}, {"githubLogin": "recipient", "role": "owner"},
    {"githubLogin": "recipient", "role": "viewer", "expiresAt": 9999999999}])
def test_invalid_invites_have_no_provider_or_write_effects(app, body):
    assert app[3]("POST", path(), body)[0] == 422
    assert app[2].calls == [] and count(app, "workspace_invites") == 0


def test_invitation_creation_does_not_depend_on_github_provider(app):
    app[2].failure = GitHubFailure("GITHUB_UNAVAILABLE")
    status, issued = app[3]("POST", path(), {"role": "viewer"})
    assert status == 201 and issued["recipient"] is None
    assert app[2].calls == []
    assert count(app, "workspace_events") == count(app, "workspace_invites") == 1


def test_member_scopes_are_required_for_api_key_governance(app):
    token = "pwk_synthetic_workspace_governance"
    with app[0]._immediate() as db:
        db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("key1", OWNER, "Synthetic",
            token[:16], hashlib.sha256(token.encode()).hexdigest(), '["profile:read"]', None,
            '{"shared":false}', NOW, None, None))
    assert app[3]("GET", "/api/v1/workspaces", headers={"Cookie": "", "Authorization": "Bearer " + token})[0] == 403
    assert app[3]("GET", path("members"), headers={"Cookie": "", "Authorization": "Bearer session-1"})[0] == 200


def test_member_mutation_race_rolls_back_audit_and_usage(app):
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements),
        lambda db: db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='usr_github_2'"))
    status, response = app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
        actor=2, headers={"If-Match": '"1"'}, plan=True)
    assert status == 503 and response["error"]["code"] == "WORKSPACE_WRITE_UNAVAILABLE"
    with app[0].connect() as db:
        assert db.execute("SELECT role FROM workspace_members WHERE user_id='usr_github_3'").fetchone()[0] == "editor"
    assert count(app, "workspace_events") == count(app, "ledger_plan_usage") == count(app, "d1_command_guard") == 0


def test_acceptance_revocation_race_has_no_membership_or_audit(app):
    issued = invite(app)
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements),
        lambda db: db.execute("UPDATE workspace_invites SET status='revoked',revision=revision+1"))
    assert accept(app, issued, plan=True)[0] == 503
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 1
    assert count(app, "ledger_plan_usage") == 0


def test_acceptance_rechecks_exact_owner_record_before_grant(app):
    issued = invite(app)
    def change_owner(db):
        row = db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("users", OWNER),)).fetchone()
        owner = json.loads(row[0])
        owner["name"] = "Owner changed after acceptance read"
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
            (encode_record("users", OWNER, owner), record_name("users", OWNER)))
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements), change_owner)
    assert accept(app, issued, plan=True) == (503, {"error": {"code": "WORKSPACE_WRITE_UNAVAILABLE"}})
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 1
    assert count(app, "ledger_plan_usage") == 0
    with app[0].connect() as db:
        assert db.execute("SELECT status FROM workspace_invites").fetchone()[0] == "pending"


def test_acceptance_member_limit_denies_before_write(app):
    issued = invite(app)
    with app[0]._immediate() as db:
        db.executemany("INSERT INTO workspace_members VALUES(?,?,'viewer',1,'joined','updated',NULL,?)",
            [(OWNER, "usr_extra_" + str(number), OWNER) for number in range(96)])
    assert accept(app, issued)[0] == 403
    assert count(app, "workspace_members") == 99 and count(app, "workspace_events") == 2


def test_governance_workflow_uses_admitted_scalar_sql_and_parameter_envelopes(app):
    from pullwise_server.cloudflare_preview_budget import _input_bound, sql_write_bound
    original, costs = app[1].batch, []

    async def bounded(statements):
        guards = 0
        for statement in statements:
            _input_bound(statement.params, sql=statement.sql)
            cost = sql_write_bound(statement.sql, guards)
            if statement.sql.startswith("INSERT INTO d1_command_guard"):
                guards += 1
            if cost:
                costs.append(cost)
        return await original(statements)

    app[1].batch = bounded
    issued = invite(app)
    assert accept(app, issued, method="preview")[0] == 200
    assert accept(app, issued)[0] == 200
    assert app[3]("PATCH", path("members", RECIPIENT), {"role": "editor"},
                  headers={"If-Match": '"1"'})[0] == 200
    assert app[3]("DELETE", path("members", RECIPIENT), headers={"If-Match": '"2"'})[0] == 204
    assert len(costs) >= 20 and max(costs) <= 11
    assert count(app, "d1_command_guard") == 0


@pytest.mark.parametrize("exhausted,code", [("monthly", "MONTHLY_WRITE_LIMIT"), ("minute", "WRITE_RATE_LIMIT")])
def test_emergency_revocations_work_at_exhausted_quota_but_grants_and_finance_do_not(app, exhausted, code):
    issued = invite(app, plan=True)
    status, category = app[3]("POST", "/api/v1/categories", {"name": "Synthetic QA"}, plan=True)
    assert status == 201
    with app[0]._immediate() as db:
        db.execute("UPDATE ledger_plan_usage SET writes=?,minute_writes=? WHERE owner_id=?",
                   (10000 if exhausted == "monthly" else 2, 60 if exhausted == "minute" else 2, OWNER))
        before = dict(db.execute("SELECT * FROM ledger_plan_usage WHERE owner_id=?", (OWNER,)).fetchone())
    assert app[3]("POST", path(), {"role": "viewer"}, plan=True) == (
        429, {"error": {"code": code}})
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
        headers={"If-Match": '"1"'}, plan=True) == (429, {"error": {"code": code}})
    expense = {"target": {"kind": "shared"}, "categoryId": category["id"], "occurredOn": "2026-10-06",
               "currency": "USD", "amount": "0.01", "purpose": "Synthetic QA"}
    assert app[3]("POST", "/api/v1/expenses", expense, headers={"Idempotency-Key": "quota-qa"}, plan=True) == (
        429, {"error": {"code": code}})
    assert app[3]("DELETE", path("members", "usr_github_3"), headers={"If-Match": '"1"'}, plan=True) == (204, None)
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={"If-Match": '"1"'}, plan=True) == (204, None)
    with app[0].connect() as db:
        assert dict(db.execute("SELECT * FROM ledger_plan_usage WHERE owner_id=?", (OWNER,)).fetchone()) == before
    assert count(app, "expenses") == 0 and count(app, "workspace_invites") == 1


def test_emergency_revocation_retains_current_session_atomic_fence(app):
    issued = invite(app, plan=True)
    with app[0]._immediate() as db:
        db.execute("UPDATE ledger_plan_usage SET writes=10000")
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements),
        lambda db: db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "session-1"),)))
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={"If-Match": '"1"'}, plan=True)[0] == 503
    with app[0].connect() as db:
        assert db.execute("SELECT status FROM workspace_invites").fetchone()[0] == "pending"
    assert count(app, "workspace_events") == 1


def test_migration_preserves_project_identity_history_and_backfills_only_links():
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript((ROOT / "cloudflare/server/migrations/0001_ledger.sql").read_text())
    db.execute("INSERT INTO ledger_projects VALUES('proj_old','owner',123,'secret/repo','history','active',7,'created','updated')")
    db.execute("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) VALUES('cat','owner','Tools','c','u')")
    db.execute("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,amount_minor,
        currency,purpose,created_at,updated_at) VALUES('expense','owner','project','proj_old','cat','2026-10-06',1,'USD','history','c','u')""")
    db.executescript((ROOT / "cloudflare/server/migrations/0005_workspaces_repositories.sql").read_text())
    assert db.execute("SELECT project_id,github_repo_id FROM ledger_project_repositories").fetchall() == [("proj_old", 123)]
    assert db.execute("SELECT name,revision FROM ledger_projects").fetchone() == ("", 7)
    assert db.execute("SELECT project_id,amount_minor FROM expenses").fetchone() == ("proj_old", 1)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("workspace_members", "workspace_invites", "workspace_events"):
        assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    db.close()


def test_unassigned_link_identifies_applicant_but_only_approval_grants_access(app):
    issued = invite(app, role="editor")
    assert issued["recipient"] is None and issued["createdByUserId"] == OWNER
    assert issued["canReview"] is True
    status, preview = apply_to_invitation(app, issued, method="preview")
    assert status == 200 and preview["request"] is None
    assert not any(preview["workspace"]["permissions"].values())
    status, application = apply_to_invitation(app, issued)
    assert status == 202 and application["request"]["status"] == "pending"
    assert application["request"]["applicant"] == {
        "userId": RECIPIENT, "name": "User 5", "githubLogin": "user5"}
    assert app[3]("GET", path("members"), actor=5)[0] == 404
    assert app[3]("GET", "/api/v1/expenses", actor=5, headers={"X-Pullwise-Workspace": OWNER})[0] == 404
    assert count(app, "workspace_members") == 3
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests")
    assert status == 200 and inbox["hasMore"] is False
    assert len(inbox["items"]) == 1 and inbox["items"][0]["applicant"]["userId"] == RECIPIENT
    assert inbox["items"][0]["workspace"]["id"] == OWNER
    assert issued["token"] not in json.dumps(inbox)
    assert "githubAccessToken" not in json.dumps(inbox) and "session-" not in json.dumps(inbox)
    status, approved = review(app, issued, application["request"])
    assert status == 200 and approved["request"]["status"] == "approved"
    assert approved["request"]["revision"] == 2 and approved["workspace"]["role"] == "editor"
    assert app[3]("GET", path("members"), actor=5)[0] == 200
    assert app[3]("GET", "/api/v1/workspace-invitation-requests")[1]["items"] == []
    status, recovered = apply_to_invitation(app, issued, method="preview")
    assert status == 200 and recovered["request"]["status"] == "approved"
    assert recovered["workspace"]["permissions"]["writeExpenses"] is True


def test_applicant_retries_and_preview_are_read_only_without_duplicate_requests(app):
    issued = invite(app)
    status, application = apply_to_invitation(app, issued, plan=True)
    assert status == 202
    with app[0].connect() as db:
        before = list(db.iterdump())
    for method in ("accept", "preview", "accept"):
        status, response = apply_to_invitation(app, issued, method=method, plan=True)
        assert status == 200 and response["request"] == application["request"]
    with app[0].connect() as db:
        assert list(db.iterdump()) == before
    assert count(app, "workspace_join_requests") == 1 and count(app, "workspace_events") == 2


def test_rejection_is_final_for_one_applicant_and_another_applicant_can_join(app):
    issued = invite(app)
    first = apply_to_invitation(app, issued)[1]["request"]
    second = apply_to_invitation(app, issued, actor=6)[1]["request"]
    status, rejected = review(app, issued, first, "reject")
    assert status == 200 and rejected["request"]["status"] == "rejected"
    before = count(app, "workspace_events")
    status, retry = apply_to_invitation(app, issued)
    assert status == 200 and retry["request"]["status"] == "rejected"
    assert count(app, "workspace_events") == before and count(app, "workspace_join_requests") == 2
    assert count(app, "workspace_members") == 3
    assert review(app, issued, rejected["request"])[0] == 409
    status, accepted = review(app, issued, second)
    assert status == 200 and accepted["request"]["applicant"]["userId"] == "usr_github_6"
    assert app[3]("GET", path("members"), actor=5)[0] == 404
    assert app[3]("GET", path("members"), actor=6)[0] == 200


def test_first_approval_consumes_link_and_hides_other_pending_requests(app):
    issued = invite(app)
    first = apply_to_invitation(app, issued)[1]["request"]
    second = apply_to_invitation(app, issued, actor=6)[1]["request"]
    assert review(app, issued, first)[0] == 200
    before = count(app, "workspace_events")
    assert review(app, issued, second)[0] == 410
    assert apply_to_invitation(app, issued, actor=6, method="preview")[0] == 410
    assert app[3]("GET", "/api/v1/workspace-invitation-requests")[1]["items"] == []
    assert app[3]("GET", path(identifier=issued["id"]) + "/requests")[1]["items"] == []
    assert count(app, "workspace_events") == before and count(app, "workspace_members") == 4


@pytest.mark.parametrize("actor", [2, 3, 4, 5, 6])
def test_only_original_inviter_can_review_not_other_admin_or_applicant(app, actor):
    issued = invite(app)
    application = apply_to_invitation(app, issued)[1]["request"]
    before = count(app, "workspace_events")
    assert review(app, issued, application, actor=actor)[0] in {403, 404}
    assert review(app, issued, application, "reject", actor=actor)[0] in {403, 404}
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", actor=actor)
    assert status == 200 and inbox["items"] == []
    assert count(app, "workspace_events") == before and count(app, "workspace_members") == 3


def test_owner_cannot_review_an_admins_invitation_on_their_behalf(app):
    issued = invite(app, actor=2)
    application = apply_to_invitation(app, issued)[1]["request"]
    assert review(app, issued, application, actor=1)[0] == 403
    assert app[3]("GET", path(identifier=issued["id"]) + "/requests", actor=1)[0] == 403
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", actor=2)
    assert status == 200 and inbox["items"][0]["invitation"]["canReview"] is True
    assert review(app, issued, application, actor=2)[0] == 200


@pytest.mark.parametrize("headers,expected", [({"If-Match": ""}, 428), ({"If-Match": '"2"'}, 412),
    ({"If-Match": "1"}, 422)])
def test_review_requires_current_request_revision(app, headers, expected):
    issued = invite(app)
    application = apply_to_invitation(app, issued)[1]["request"]
    assert review(app, issued, application, headers=headers)[0] == expected
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 2


def test_admin_request_review_loses_authority_after_demotion_and_repromotion(app):
    issued = invite(app, actor=2)
    application = apply_to_invitation(app, issued)[1]["request"]
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "editor"}, headers={"If-Match": '"1"'})[0] == 200
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "admin"}, headers={"If-Match": '"2"'})[0] == 200
    assert review(app, issued, application, actor=2)[0] == 403
    assert app[3]("GET", "/api/v1/workspace-invitation-requests", actor=2)[1]["items"] == []
    assert count(app, "workspace_members") == 3


@pytest.mark.parametrize("change", ["revoked", "request", "applicant", "session", "owner", "inviter"])
def test_approval_races_rollback_membership_request_decision_audit_and_quota(app, change):
    issued = invite(app, actor=2)
    application = apply_to_invitation(app, issued)[1]["request"]
    def race(db):
        if change == "revoked":
            db.execute("UPDATE workspace_invites SET status='revoked',revision=revision+1")
        elif change == "request":
            db.execute("UPDATE workspace_join_requests SET revision=revision+1")
        elif change == "session":
            db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "session-2"),))
        elif change == "inviter":
            db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='usr_github_2'")
        else:
            identifier = RECIPIENT if change == "applicant" else OWNER
            saved = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("users", identifier),)).fetchone()[0])
            saved["name"] = "Changed after authorization snapshot"
            db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", identifier, saved), record_name("users", identifier)))
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements), race)
    assert review(app, issued, application, plan=True) == (503, {"error": {"code": "WORKSPACE_WRITE_UNAVAILABLE"}})
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 2
    assert count(app, "ledger_plan_usage") == count(app, "d1_command_guard") == 0
    with app[0].connect() as db:
        assert db.execute("SELECT status FROM workspace_join_requests").fetchone()[0] == "pending"


def test_legacy_targeted_link_keeps_recipient_restriction_and_requires_approval(app):
    issued = invite(app)
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET github_recipient_id=5,github_login='old-login'")
    assert apply_to_invitation(app, issued, actor=6, method="preview")[0] == 403
    assert apply_to_invitation(app, issued, actor=6)[0] == 403
    status, application = apply_to_invitation(app, issued)
    assert status == 202 and application["recipient"] == {"githubId": "5", "login": "old-login"}
    assert count(app, "workspace_members") == 3
    assert review(app, issued, application["request"])[0] == 200


def test_legacy_accepted_link_recovers_without_a_join_request_or_write(app):
    issued = invite(app)
    with app[0]._immediate() as db:
        db.execute("UPDATE workspace_invites SET github_recipient_id=5,github_login='old-login',status='accepted',revision=2,accepted_by_user_id=?,accepted_at='earlier'", (RECIPIENT,))
        db.execute("INSERT INTO workspace_members VALUES(?,?,'viewer',1,'earlier','earlier',NULL,?)", (OWNER, RECIPIENT, OWNER))
        before = list(db.iterdump())
    status, preview = apply_to_invitation(app, issued, method="preview")
    assert status == 200 and preview["request"] is None and preview["workspace"]["role"] == "viewer"
    with app[0].connect() as db:
        assert list(db.iterdump()) == before


def test_inbox_is_bounded_read_only_and_reveals_next_requests_after_review(app):
    first, second = invite(app), invite(app)
    with app[0]._immediate() as db:
        db.executemany("""INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,
            created_at,updated_at) VALUES(?,?,?,?,?,?)""", [(f"wjr_{number:03}", OWNER,
            first["id"] if number < 100 else second["id"], f"usr_request_{number}", "created", "updated")
            for number in range(101)])
        for number in range(101):
            identifier = f"usr_request_{number}"
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)", (record_name("users", identifier), encode_record("users", identifier, {"id": identifier, "name": f"Applicant {number}"}), NOW))
        before = list(db.iterdump())
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", headers={"X-Pullwise-Workspace": "some_other_ledger"})
    assert status == 200 and len(inbox["items"]) == 100 and inbox["hasMore"] is True
    assert [item["id"] for item in inbox["items"]] == [f"wjr_{number:03}" for number in range(100)]
    with app[0].connect() as db:
        assert list(db.iterdump()) == before
    assert review(app, first, inbox["items"][0], "reject")[0] == 200
    status, refreshed = app[3]("GET", "/api/v1/workspace-invitation-requests")
    assert status == 200 and refreshed["hasMore"] is False
    assert refreshed["items"][-1]["id"] == "wjr_100"


def test_application_cap_is_lifetime_per_link_and_denies_before_write(app):
    issued = invite(app)
    with app[0]._immediate() as db:
        db.executemany("""INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,
            status,created_at,updated_at,reviewed_by_user_id,reviewed_at) VALUES(?,?,?,?,'rejected','c','u',?,'r')""",
            [(f"wjr_cap_{number}", OWNER, issued["id"], f"usr_cap_{number}", OWNER) for number in range(100)])
    assert apply_to_invitation(app, issued) == (403, {"error": {"code": "INVITATION_REQUEST_LIMIT"}})
    assert count(app, "workspace_events") == 1 and count(app, "workspace_join_requests") == 100


def test_application_requires_cookie_identity_while_review_accepts_authenticated_session(app):
    issued = invite(app)
    assert apply_to_invitation(app, issued, headers={"Cookie": "", "Authorization": "Bearer session-5"})[0] == 403
    application = apply_to_invitation(app, issued)[1]["request"]
    assert app[3]("GET", "/api/v1/workspace-invitation-requests", headers={"Cookie": "", "Authorization": "Bearer session-1"})[0] == 200
    assert review(app, issued, application, headers={"Cookie": "", "Authorization": "Bearer session-1"})[0] == 200
    assert count(app, "workspace_members") == 4


def test_workspace_join_requests_are_scoped_separately_from_global_inbox_cap(app):
    status, personal = app[3]("POST", "/api/v1/workspaces/usr_github_2/invites", {"role": "viewer"}, actor=2)
    assert status == 201
    shared = invite(app, actor=2)
    shared_request = apply_to_invitation(app, shared)[1]["request"]
    personal_request = apply_to_invitation(app, personal)[1]["request"]
    status, global_inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", actor=2)
    assert status == 200 and {item["workspaceId"] for item in global_inbox["items"]} == {OWNER, "usr_github_2"}
    status, shared_inbox = app[3]("GET", f"/api/v1/workspaces/{OWNER}/join-requests", actor=2)
    assert status == 200 and [item["id"] for item in shared_inbox["items"]] == [shared_request["id"]]
    assert shared_inbox["items"][0]["invitation"]["id"] == shared["id"]
    status, personal_inbox = app[3]("GET", "/api/v1/workspaces/usr_github_2/join-requests", actor=2)
    assert status == 200 and [item["id"] for item in personal_inbox["items"]] == [personal_request["id"]]
    assert app[3]("GET", f"/api/v1/workspaces/{OWNER}/join-requests", actor=1)[1]["items"] == []
    assert app[3]("GET", f"/api/v1/workspaces/{OWNER}/join-requests", actor=4)[0] == 403
    assert count(app, "workspace_events") == 4


def test_member_key_reads_and_writes_require_only_their_explicit_scope(app):
    reader = member_key(app, scopes=["members:read"])
    writer = member_key(app, scopes=["members:write"])
    assert app[3]("GET", "/api/v1/workspaces", headers=reader)[1]["items"][0]["id"] == OWNER
    assert app[3]("GET", path("members"), headers=reader)[0] == 200
    assert app[3]("GET", path(), headers=reader)[0] == 200
    assert app[3]("GET", path("members"), headers=writer) == (403, {"error": {"code": "INSUFFICIENT_SCOPE"}})
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
        headers={**reader, "If-Match": '"1"'}) == (403, {"error": {"code": "INSUFFICIENT_SCOPE"}})
    status, changed = app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
                            headers={**writer, "If-Match": '"1"'})
    assert status == 200 and changed["role"] == "viewer" and changed["revision"] == 2
    assert app[3]("DELETE", path("members", "usr_github_3"),
                  headers={**writer, "If-Match": '"2"'}) == (204, None)


@pytest.mark.parametrize("project_ids", [[], ["prj_one"]])
@pytest.mark.parametrize("method,endpoint,body", [
    ("GET", "/api/v1/workspaces", None),
    ("GET", "/api/v1/workspace-invitation-requests", None),
    ("GET", path("members"), None),
    ("GET", path(), None),
    ("POST", path(), {"role": "viewer"}),
])
def test_target_restricted_keys_cannot_govern_even_with_member_scopes(app, project_ids, method, endpoint, body):
    key = member_key(app, restrictions={"projectIds": project_ids})
    assert app[3](method, endpoint, body, headers=key) == (403, {"error": {"code": "TARGET_FORBIDDEN"}})
    assert count(app, "workspace_events") == count(app, "workspace_invites") == 0


def test_member_key_workspace_list_is_bound_and_does_not_enumerate_issuer_ledgers(app):
    key = member_key(app, actor=2)
    status, listed = app[3]("GET", "/api/v1/workspaces", headers={**key, "X-Pullwise-Workspace": "usr_github_6"})
    assert status == 200 and [(item["id"], item["role"]) for item in listed["items"]] == [(OWNER, "admin")]
    status, listed = app[3]("GET", "/api/v1/workspaces", actor=2)
    assert status == 200 and {item["id"] for item in listed["items"]} == {OWNER, "usr_github_2"}
    assert app[3]("GET", "/api/v1/workspaces/usr_github_2/members", headers=key) == (
        403, {"error": {"code": "WORKSPACE_FORBIDDEN"}})


def test_member_key_global_inbox_is_bound_and_reviews_keep_original_inviter(app):
    shared_key = member_key(app, actor=2)
    personal_key = member_key(app, actor=2, workspace=None)
    owner_key = member_key(app)
    status, shared = app[3]("POST", path(), {"role": "viewer"}, headers=shared_key)
    assert status == 201 and shared["createdByUserId"] == "usr_github_2"
    status, personal = app[3]("POST", "/api/v1/workspaces/usr_github_2/invites", {"role": "viewer"}, headers=personal_key)
    assert status == 201
    shared_request = apply_to_invitation(app, shared)[1]["request"]
    personal_request = apply_to_invitation(app, personal)[1]["request"]
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", headers=shared_key)
    assert status == 200 and [(item["workspaceId"], item["id"]) for item in inbox["items"]] == [(OWNER, shared_request["id"])]
    status, inbox = app[3]("GET", "/api/v1/workspace-invitation-requests", headers=personal_key)
    assert status == 200 and [(item["workspaceId"], item["id"]) for item in inbox["items"]] == [("usr_github_2", personal_request["id"])]
    reader = member_key(app, actor=2, scopes=["members:read"])
    assert app[3]("GET", f"/api/v1/workspaces/{OWNER}/join-requests", headers=reader)[1]["items"][0]["id"] == shared_request["id"]
    assert app[3]("GET", path(identifier=shared["id"]) + "/requests", headers=reader)[1]["items"][0]["id"] == shared_request["id"]
    assert app[3]("GET", "/api/v1/workspaces/usr_github_2/join-requests", headers=reader) == (
        403, {"error": {"code": "WORKSPACE_FORBIDDEN"}})
    assert app[3]("GET", "/api/v1/workspace-invitation-requests", headers=owner_key)[1]["items"] == []
    assert review(app, shared, shared_request, headers=owner_key) == (
        403, {"error": {"code": "INVITATION_REVIEW_FORBIDDEN"}})
    assert review(app, shared, shared_request, headers=shared_key)[0] == 200
    assert app[3]("GET", path("members"), actor=5)[0] == 200
    assert app[3]("GET", "/api/v1/workspace-invitation-requests", headers=shared_key)[1]["items"] == []


def test_member_keys_preserve_role_boundaries_and_never_change_owner(app):
    viewer = member_key(app, actor=4)
    admin = member_key(app, actor=2)
    owner = member_key(app)
    assert app[3]("GET", path("members"), headers=viewer)[0] == 200
    assert app[3]("GET", path(), headers=viewer) == (403, {"error": {"code": "ROLE_FORBIDDEN"}})
    assert app[3]("POST", path(), {"role": "editor"}, headers=viewer) == (
        403, {"error": {"code": "ROLE_FORBIDDEN"}})
    assert app[3]("POST", path(), {"role": "admin"}, headers=admin) == (
        403, {"error": {"code": "ROLE_FORBIDDEN"}})
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "admin"},
        headers={**admin, "If-Match": '"1"'}) == (403, {"error": {"code": "ROLE_FORBIDDEN"}})
    assert app[3]("DELETE", path("members", OWNER), headers={**owner, "If-Match": '"1"'}) == (
        403, {"error": {"code": "OWNER_IMMUTABLE"}})


def test_member_key_can_reject_requests_and_revoke_invitation_with_cas(app):
    key = member_key(app)
    status, issued = app[3]("POST", path(), {"role": "viewer"}, headers=key)
    assert status == 201
    request = apply_to_invitation(app, issued)[1]["request"]
    assert review(app, issued, request, action="reject", headers=key)[1]["request"]["status"] == "rejected"
    assert review(app, issued, request, action="reject", headers=key)[0] == 412
    assert app[3]("DELETE", path(identifier=issued["id"]), headers=key)[0] == 428
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={**key, "If-Match": '"1"'}) == (204, None)
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 4


@pytest.mark.parametrize("method", ["preview", "accept"])
def test_invitation_applicant_identity_stays_cookie_only_for_member_keys(app, method):
    issued = invite(app)
    key = member_key(app, actor=5, workspace=None, scopes=["profile:read", "members:read", "members:write"])
    assert apply_to_invitation(app, issued, method=method, headers=key) == (
        403, {"error": {"code": "COOKIE_SESSION_REQUIRED"}})
    assert count(app, "workspace_join_requests") == 0


def test_member_key_is_invalidated_by_issuer_membership_revision(app):
    key = member_key(app, actor=2)
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "editor"},
        headers={"If-Match": '"1"'})[0] == 200
    assert app[3]("GET", path("members"), headers=key) == (
        403, {"error": {"code": "WORKSPACE_MEMBERSHIP_CHANGED"}})
    assert app[3]("PATCH", path("members", "usr_github_2"), {"role": "admin"},
        headers={"If-Match": '"2"'})[0] == 200
    assert app[3]("GET", path("members"), headers=key)[0] == 403


@pytest.mark.parametrize("change", ["revoke", "scope", "restriction", "membership"])
def test_member_key_write_races_rollback_mutation_audit_and_usage(app, change):
    key = member_key(app, actor=2)
    def invalidate(db):
        if change == "revoke":
            db.execute("UPDATE api_keys SET revoked_at=?", (NOW,))
        elif change == "scope":
            db.execute("UPDATE api_keys SET scopes='[\"members:read\"]'")
        elif change == "restriction":
            db.execute("UPDATE api_keys SET restrictions='{}'")
        else:
            db.execute("UPDATE workspace_members SET revision=2 WHERE user_id='usr_github_2'")
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements), invalidate)
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
        headers={**key, "If-Match": '"1"'}, plan=True) == (
        503, {"error": {"code": "WORKSPACE_WRITE_UNAVAILABLE"}})
    with app[0].connect() as db:
        assert tuple(db.execute("SELECT role,revision FROM workspace_members WHERE user_id='usr_github_3'").fetchone()) == ("editor", 1)
    assert count(app, "workspace_events") == count(app, "ledger_plan_usage") == count(app, "d1_command_guard") == 0


def test_member_key_approval_revocation_race_rolls_back_all_decision_effects(app):
    key = member_key(app, actor=2)
    status, issued = app[3]("POST", path(), {"role": "viewer"}, headers=key)
    assert status == 201
    request = apply_to_invitation(app, issued)[1]["request"]
    app[1].race = (lambda statements: any("INSERT INTO d1_command_guard" in item.sql for item in statements),
                   lambda db: db.execute("UPDATE api_keys SET revoked_at=?", (NOW,)))
    assert review(app, issued, request, headers=key, plan=True)[0] == 503
    with app[0].connect() as db:
        assert db.execute("SELECT status FROM workspace_invites").fetchone()[0] == "pending"
        assert tuple(db.execute("SELECT status,revision FROM workspace_join_requests").fetchone()) == ("pending", 1)
    assert count(app, "workspace_members") == 3 and count(app, "workspace_events") == 2
    assert count(app, "ledger_plan_usage") == count(app, "d1_command_guard") == 0
