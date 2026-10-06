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
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("000*.sql")):
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
    status, payload = app[3]("POST", path(), {"githubLogin": "recipient", "role": role}, actor=actor, plan=plan)
    assert status == 201, payload
    return payload


def accept(app, issued, actor=5, method="accept", **kwargs):
    return app[3]("POST", "/api/v1/workspace-invitations/" + method,
                  {"token": issued["token"]}, actor=actor, **kwargs)


def count(app, table):
    with app[0].connect() as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


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
        assert [tuple(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")] == [(OWNER, 2)]
        assert db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("users", OWNER),)).fetchone()[0] == encoded


def test_members_viewer_can_read_but_not_govern(app):
    status, page = app[3]("GET", path("members"), actor=4)
    assert status == 200 and len(page["items"]) == 4
    assert page["items"][0]["role"] == "owner"
    assert app[3]("PATCH", path("members", "usr_github_3"), {"role": "viewer"},
                  actor=4, headers={"If-Match": '"1"'})[0] == 403
    assert app[3]("GET", path(), actor=4)[0] == 403
    assert count(app, "workspace_events") == 0


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


def test_invitation_hash_only_single_use_recipient_preview_no_writes(app):
    issued = invite(app)
    with app[0].connect() as db:
        saved = dict(db.execute("SELECT * FROM workspace_invites").fetchone())
        audit = db.execute("SELECT after_json FROM workspace_events").fetchone()[0]
    assert saved["token_hash"] == hashlib.sha256(issued["token"].encode()).hexdigest()
    assert issued["token"] not in json.dumps(saved) + audit
    status, listed = app[3]("GET", path())
    assert status == 200 and "token" not in json.dumps(listed)
    assert accept(app, issued, actor=6, method="preview")[0] == 403
    before = count(app, "workspace_events")
    status, preview = accept(app, issued, method="preview")
    assert status == 200 and preview["workspace"]["id"] == OWNER
    assert count(app, "workspace_events") == before
    status, accepted = accept(app, issued)
    assert status == 200 and accepted["workspace"]["role"] == "viewer"
    assert accept(app, issued)[0] == 410
    assert count(app, "workspace_events") == 2
    assert len(app[2].calls) == 2  # only the issuance lookup


def test_invitation_acceptance_ignores_current_personal_workspace_selector(app):
    issued = invite(app)
    assert accept(app, issued, headers={"X-Pullwise-Workspace": "usr_github_5"})[0] == 200


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
    assert accept(app, issued, actor=6, method="preview")[0] == 403


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
    assert accept(app, issued, actor=6, method="preview")[0] == 403


def test_acceptance_charges_ledger_owner_not_recipient(app):
    issued = invite(app, plan=True)
    status, _ = accept(app, issued, plan=True)
    assert status == 200
    with app[0].connect() as db:
        usage = [dict(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")]
    assert usage == [{"owner_id": OWNER, "writes": 2}]


def test_removed_member_rejoins_with_new_revision(app):
    assert app[3]("DELETE", path("members", "usr_github_3"), headers={"If-Match": '"1"'})[0] == 204
    # Resolve to the already registered, previously removed GitHub identity.
    async def lookup(url, *, token):
        return {"id": 3, "login": "user3", "type": "User"}
    app[2]._json = lookup
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
    async def lookup(url, *, token):
        return {"id": 2, "login": "user2", "type": "User"}
    app[2]._json = lookup
    rejoin = invite(app, role="admin")
    status, joined = accept(app, rejoin, actor=2)
    assert status == 200 and joined["workspace"]["revision"] == 3
    assert accept(app, issued)[0] == 403
    assert count(app, "workspace_members") == 3


def test_admin_cannot_issue_or_revoke_admin_invitation(app):
    assert app[3]("POST", path(), {"githubLogin": "recipient", "role": "admin"}, actor=2)[0] == 403
    assert app[2].calls == []
    issued = invite(app, role="admin")
    assert app[3]("DELETE", path(identifier=issued["id"]), actor=2,
                  headers={"If-Match": '"1"'})[0] == 403
    assert app[3]("DELETE", path(identifier=issued["id"]), headers={"If-Match": '"1"'})[0] == 204
    assert accept(app, issued)[0] == 410


def test_pending_duplicates_and_expired_tokens_are_rejected(app):
    issued = invite(app)
    assert app[3]("POST", path(), {"githubLogin": "recipient", "role": "editor"})[0] == 409
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


def test_provider_failure_is_safe_and_does_not_create_invite(app):
    app[2].failure = GitHubFailure("GITHUB_UNAVAILABLE")
    assert app[3]("POST", path(), {"githubLogin": "recipient", "role": "viewer"}) == (
        503, {"error": {"code": "GITHUB_UNAVAILABLE"}})
    assert count(app, "workspace_events") == count(app, "workspace_invites") == 0


def test_api_keys_and_bearer_sessions_cannot_govern(app):
    token = "pwk_synthetic_workspace_governance"
    with app[0]._immediate() as db:
        db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("key1", OWNER, "Synthetic",
            token[:16], hashlib.sha256(token.encode()).hexdigest(), '["profile:read"]', None,
            '{"shared":false}', NOW, None, None))
    assert app[3]("GET", "/api/v1/workspaces", headers={"Cookie": "", "Authorization": "Bearer " + token})[0] == 403
    assert app[3]("GET", path("members"), headers={"Cookie": "", "Authorization": "Bearer session-1"})[0] == 403


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
    assert count(app, "workspace_members") == 99 and count(app, "workspace_events") == 1


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
    assert len(costs) >= 15 and max(costs) <= 7
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
    async def lookup(url, *, token):
        return {"id": 6, "login": "user6", "type": "User"}
    app[2]._json = lookup
    assert app[3]("POST", path(), {"githubLogin": "user6", "role": "viewer"}, plan=True) == (
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
