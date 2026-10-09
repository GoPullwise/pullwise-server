"""Real SQLite transactions prove schedule identity, authority and expense atomicity."""
import asyncio
import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger_d1_fixture import D1ShapedSQLite, Prepared, Store
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_recurring import (
    RESOURCE, generate_occurrence, handle_recurring_request, run_due_recurring,
)
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import default_policy

ROOT = Path(__file__).resolve().parents[1]
NOW = int(datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp())


class SafePrepared(Prepared):
    async def first(self):
        with closing(self.binding.store.connect()) as db:
            row = db.execute(self.sql, self.params).fetchone()
            return dict(row) if row else None

    async def all(self):
        with closing(self.binding.store.connect()) as db:
            return SimpleNamespace(results=[dict(row) for row in db.execute(self.sql, self.params)])


class SafeD1(D1ShapedSQLite):
    def prepare(self, sql):
        return SafePrepared(self, sql)


class NoGitHub:
    async def unseal(self, _):
        raise AssertionError("A standalone/shared schedule must not request GitHub")


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "recurring.db")
    with closing(store.connect()) as db:
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
            db.executescript(path.read_text())
        for identifier in ("owner", "editor", "viewer", "other"):
            value = {"id": identifier, "createdAt": NOW - 10000,
                     "billing": {"plan": "pro", "status": "active", "subscriptionId": "synthetic",
                                 "currentPeriodStart": NOW - 10000, "currentPeriodEnd": NOW + 10000000}}
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)", (
                record_name("users", identifier), encode_record("users", identifier, value), NOW))
            session = {"userId": identifier, "expiresAt": NOW + 1000}
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)", (
                record_name("sessions", identifier), encode_record("sessions", identifier, session), NOW))
        for identifier, role in (("editor", "editor"), ("viewer", "viewer")):
            db.execute("INSERT INTO workspace_members VALUES(?,?,?,1,'joined','updated',NULL,'owner')", ("owner", identifier, role))
        db.execute("""INSERT INTO ledger_projects(id,owner_id,name,github_repo_id,github_full_name,
            description,status,revision,created_at,updated_at) VALUES('prj_1','owner','Standalone',NULL,NULL,'','active',1,'created','updated')""")
        db.execute("INSERT INTO expense_categories VALUES('cat_1','owner','Hosting',NULL,NULL,1,'created','updated')")
        db.commit()
    raw, policy = SafeD1(store), default_policy()

    def call(method, identifier=None, body=None, *, actor="owner", revision=None,
             key="rule-one", params=None, now=NOW, headers=None, gateway=None,
             handler=handle_recurring_request):
        auth = {"Cookie": "pw_session=" + actor, "Origin": "https://app.example.test"}
        if actor != "owner":
            auth["X-Pullwise-Workspace"] = "owner"
        if revision is not None:
            auth["If-Match"] = f'"{revision}"'
        auth["Idempotency-Key"] = key
        auth.update(headers or {})
        return asyncio.run(handler(binding=PlanLimitedD1(raw, policy=policy, now=now),
            gateway=gateway or NoGitHub(), method=method, path=RESOURCE + ("/" + identifier if identifier else ""),
            headers=auth, params=params or {}, body=body, now=now))

    def tick(*, now=NOW, limit=10, gateway=None):
        return asyncio.run(run_due_recurring(binding=PlanLimitedD1(raw, policy=policy, now=now),
            maintenance_binding=raw, gateway=gateway or NoGitHub(), now=now, occurrence_limit=limit))

    def rows(table):
        with closing(store.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    return SimpleNamespace(store=store, raw=raw, policy=policy, call=call, tick=tick, rows=rows)


def draft(*, project=False, start="2026-07-01", **changes):
    return {"target": {"kind": "project", "projectId": "prj_1"} if project else {"kind": "shared"},
            "amount": "12.34", "currency": "USD", "categoryId": "cat_1", "purpose": "Hosting",
            "schedule": {"frequency": "monthly", "day": 31, "timezone": "Asia/Shanghai", "startOn": start},
            **changes}


def create(app, **kwargs):
    status, rule = app.call("POST", body=draft(**kwargs))
    assert status == 201, rule
    return rule


def test_public_ledger_router_manages_rules_used_by_internal_expense_runner(app):
    status, rule = app.call("POST", body=draft(start="2026-09-01"), handler=handle_ledger_request)
    assert status == 201
    status, page = app.call("GET", params={"target": "shared"}, handler=handle_ledger_request)
    assert status == 200 and page["items"] == [rule]
    assert app.tick()["created"] == 1
    status, current = app.call("GET", rule["id"], handler=handle_ledger_request)
    assert status == 200 and current["revision"] == 2
    status, paused = app.call("PATCH", rule["id"], {"status": "paused"}, revision=2,
                              handler=handle_ledger_request)
    assert status == 200 and paused["status"] == "paused"
    assert app.call("DELETE", rule["id"], revision=3, handler=handle_ledger_request)[0] == 204
    assert len(app.rows("expenses")) == len(app.rows("expense_recurring_occurrences")) == 1


@pytest.mark.parametrize("project", [False, True])
def test_due_rules_create_real_target_expenses_exact_money_and_atomic_audit_quota(app, project):
    rule = create(app, project=project, amount="90071992547409.91")
    assert rule["amount"] == "90071992547409.91"
    assert rule["nextOccurrenceOn"] == "2026-07-31"
    assert app.rows("expenses") == []
    result = app.tick()
    assert result == {"scanned": 1, "created": 3, "blocked": 0, "replayed": 0}
    expenses = app.rows("expenses")
    assert [expense["occurred_on"] for expense in expenses] == ["2026-07-31", "2026-08-31", "2026-09-30"]
    assert {expense["amount_minor"] for expense in expenses} == {9007199254740991}
    assert {expense["target_kind"] for expense in expenses} == {"project" if project else "shared"}
    assert {expense["project_id"] for expense in expenses} == {"prj_1" if project else None}
    events, occurrences = app.rows("expense_events"), app.rows("expense_recurring_occurrences")
    assert len(events) == len(occurrences) == 3
    assert {event["actor_kind"] for event in events} == {"schedule"}
    assert {event["actor_id"] for event in events} == {rule["id"] + ":owner"}
    assert app.rows("ledger_plan_usage")[0]["records"] == 3
    assert app.rows("ledger_plan_usage")[0]["writes"] == 4
    assert app.call("GET", rule["id"])[1]["nextOccurrenceOn"] == "2026-10-31"
    assert app.tick()["created"] == 0


def test_create_replay_is_exact_after_template_edit_and_cancel_and_rejects_changed_intent(app):
    body = draft()
    original = create(app)
    status, updated = app.call("PATCH", original["id"], {**body, "amount": "99.00"}, revision=1)
    assert status == 200
    assert app.call("DELETE", original["id"], revision=updated["revision"])[0] == 204
    assert app.call("POST", body=body) == (201, original)
    assert app.call("POST", body={**body, "amount": "99.00"})[0] == 409
    assert len(app.rows("expense_recurring_rules")) == 1
    assert app.rows("expenses") == []


def test_pause_resume_preserves_history_and_skips_paused_periods_with_cas(app):
    rule = create(app)
    assert app.call("PATCH", rule["id"], {"status": "paused"})[0] == 428
    assert app.call("PATCH", rule["id"], {"status": "paused"}, revision=2)[0] == 412
    status, paused = app.call("PATCH", rule["id"], {"status": "paused"}, revision=1)
    assert status == 200 and paused["status"] == "paused"
    assert app.tick()["created"] == 0
    status, edited = app.call("PATCH", rule["id"], draft(amount="13.00"), revision=2)
    assert status == 200 and edited["status"] == "paused"
    status, resumed = app.call("PATCH", rule["id"], {"status": "active"}, revision=3)
    assert status == 200 and resumed["nextOccurrenceOn"] == "2026-10-31"
    assert app.tick()["created"] == 0


@pytest.mark.parametrize("method,body", [
    ("PATCH", {"status": "paused"}),
    ("PATCH", {"status": "active"}),
    ("DELETE", None),
])
def test_revision_limit_rejects_mutation_without_partial_state_or_quota(app, method, body):
    rule = create(app)
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET revision=? WHERE id=?", (9007199254740991, rule["id"]))
    previous = app.rows("expense_recurring_rules")
    usage = app.rows("ledger_plan_usage")
    status, payload = app.call(method, rule["id"], body, revision=9007199254740991)
    assert status == 409 and payload["error"]["code"] == "REVISION_LIMIT"
    assert app.rows("expense_recurring_rules") == previous
    assert app.rows("ledger_plan_usage") == usage


def test_record_deletion_and_stale_pointer_never_regenerate_an_occurrence(app):
    rule = create(app, start="2026-09-01")
    assert app.tick()["created"] == 1
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted'")
        # Simulate a stale persisted pointer after successful expense publication.
        first = app.rows("expense_recurring_occurrences")[0]
        db.execute("""UPDATE expense_recurring_rules SET next_occurrence_on=?,next_period_key=?,next_run_at=0
            WHERE id=?""", (first["scheduled_on"], first["period_key"], rule["id"]))
    assert app.tick()["replayed"] == 1
    assert len(app.rows("expenses")) == len(app.rows("expense_recurring_occurrences")) == 1
    assert app.rows("ledger_plan_usage")[0]["records"] == 1
    assert app.rows("ledger_plan_usage")[0]["writes"] == 2


@pytest.mark.parametrize("mutation,code", [
    ("UPDATE expense_categories SET archived_at='archived'", "INVALID_CATEGORY"),
    ("UPDATE ledger_projects SET status='archived'", "GITHUB_ACCESS_REQUIRED"),
])
def test_archived_target_or_category_blocks_once_without_advancing_or_hot_loop(app, mutation, code):
    rule = create(app, project=True)
    with app.store._immediate() as db:
        db.execute(mutation)
    result = app.tick()
    assert result["blocked"] == 1 and result["created"] == 0
    stored = app.call("GET", rule["id"])[1]
    assert stored["status"] == "blocked" and stored["blockedCode"] == code
    assert stored["nextOccurrenceOn"] == "2026-07-31"
    assert app.tick()["scanned"] == 0
    assert app.rows("expenses") == []


def test_persistent_grant_survives_session_expiry_but_not_member_downgrade(app):
    status, rule = app.call("POST", body=draft(), actor="editor")
    assert status == 201
    with app.store._immediate() as db:
        db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "editor"),))
    assert app.tick()["created"] == 3
    assert {event["actor_id"] for event in app.rows("expense_events")} == {rule["id"] + ":editor"}
    with app.store._immediate() as db:
        db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='editor'")
        db.execute("UPDATE expense_recurring_rules SET next_run_at=0")
    assert app.tick()["blocked"] == 1
    assert app.call("GET", rule["id"])[1]["blockedCode"] == "SCHEDULE_AUTHORIZATION_CHANGED"
    assert len(app.rows("expenses")) == 3


def test_viewer_can_read_but_cannot_create_and_other_workspace_has_no_access(app):
    rule = create(app)
    status, page = app.call("GET", actor="viewer", params={"target": "shared"})
    assert status == 200 and page["items"][0]["id"] == rule["id"]
    assert app.call("POST", body=draft(), actor="viewer")[0] == 403
    assert app.call("GET", rule["id"], actor="other")[0] == 404


def api_auth(app, *, actor="owner", scopes=None, restrictions=None, expires=None, suffix="one"):
    token = "pwk_synthetic_recurrence_" + actor + "_" + suffix
    restriction = {"shared": True} if restrictions is None else restrictions
    if actor != "owner":
        restriction = {"workspaceId": "owner", "workspaceMemberRevision": 1, **restriction}
    with app.store._immediate() as db:
        db.execute("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,scopes,expires_at,
            restrictions,created_at) VALUES(?,?,?,?,?,?,?,?,?)""", (
                "key_" + actor + "_" + suffix, actor, "Synthetic recurring test", "prefix",
                hashlib.sha256(token.encode()).hexdigest(),
                json.dumps(["expenses:read", "expenses:write"] if scopes is None else scopes),
                expires, json.dumps(restriction), NOW))
    return {"Cookie": "", "Origin": "", "Authorization": "Bearer " + token}


@pytest.mark.parametrize("project", [False, True])
def test_api_key_manages_project_and_shared_rules_with_revocable_hidden_grants(app, project):
    auth = api_auth(app)
    body = draft(project=project, start="2026-09-01")
    status, rule = app.call("POST", body=body, headers=auth, handler=handle_ledger_request)
    assert status == 201
    assert app.call("POST", body=body, headers=auth) == (201, rule)
    saved = json.loads(app.rows("expense_recurring_rules")[0]["template_json"])
    assert saved["_api_key_hash"] == hashlib.sha256(auth["Authorization"].split()[1].encode()).hexdigest()
    assert "_api_key_hash" not in json.dumps(rule)
    assert app.tick()["created"] == 1
    status, current = app.call("GET", rule["id"], headers=auth)
    assert status == 200 and current["revision"] == 2
    status, edited = app.call("PATCH", rule["id"], {**body, "amount": "99.00"},
                              headers=auth, revision=2)
    assert status == 200 and edited["amount"] == "99.00"
    status, paused = app.call("PATCH", rule["id"], {"status": "paused"}, headers=auth, revision=3)
    assert status == 200 and paused["status"] == "paused"
    assert app.tick()["created"] == 0
    status, resumed = app.call("PATCH", rule["id"], {"status": "active"}, headers=auth, revision=4)
    assert status == 200 and resumed["nextOccurrenceOn"] == "2026-10-31"
    assert json.loads(app.rows("expense_recurring_rules")[0]["template_json"])["_api_key_hash"] == saved["_api_key_hash"]
    assert app.call("DELETE", rule["id"], headers=auth, revision=5)[0] == 204
    assert app.call("GET", headers=auth)[1]["items"] == []
    assert len(app.rows("expenses")) == len(app.rows("expense_recurring_occurrences")) == 1
    assert {row["actor_id"] for row in app.rows("expense_events")} == {rule["id"] + ":owner"}


def test_api_lists_filter_target_restrictions_before_pagination_and_replays(app):
    shared = create(app)
    status, project = app.call("POST", body=draft(project=True), key="project")
    assert status == 201
    status, second = app.call("POST", body=draft(project=True), key="second")
    assert status == 201
    auth = api_auth(app, restrictions={"shared": False, "projectIds": ["prj_1"]})
    cursor, collected = None, []
    while True:
        status, page = app.call("GET", headers=auth, params={"limit": "1", "cursor": cursor or ""})
        assert status == 200
        collected.extend(row["id"] for row in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert set(collected) == {project["id"], second["id"]}
    assert shared["id"] not in collected
    assert app.call("GET", shared["id"], headers=auth)[0] == 404
    assert app.call("GET", headers=auth, params={"target": "shared"})[1]["error"]["code"] == "TARGET_FORBIDDEN"
    assert app.call("GET", headers=auth, params={"projectId": "prj_other"})[0] == 403
    # Same actor and raw idempotency key cannot reveal a prior restricted target.
    assert app.call("POST", body=draft(project=True), headers=auth)[0] == 404
    empty = api_auth(app, suffix="empty", restrictions={"shared": False, "projectIds": []})
    assert app.call("GET", headers=empty)[1] == {"items": [], "nextCursor": None}
    shared_only = api_auth(app, suffix="shared", restrictions={"shared": True, "projectIds": []})
    assert app.call("GET", headers=shared_only)[1]["items"] == [shared]


def test_recurring_keys_require_expense_scope_and_explicit_target_grants(app):
    rule = create(app)
    readonly = api_auth(app, scopes=["expenses:read"])
    assert app.call("GET", rule["id"], headers=readonly)[0] == 200
    assert app.call("POST", body=draft(), headers=readonly)[1]["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert app.call("PATCH", rule["id"], {"status": "paused"}, revision=1, headers=readonly)[0] == 403
    assert app.call("DELETE", rule["id"], revision=1, headers=readonly)[0] == 403
    writeonly = api_auth(app, suffix="write", scopes=["expenses:write"])
    assert app.call("GET", headers=writeonly)[0] == 403
    restricted = api_auth(app, suffix="restricted", restrictions={"shared": False, "projectIds": []})
    assert app.call("POST", body=draft(), key="new-shared", headers=restricted)[1]["error"]["code"] == "TARGET_FORBIDDEN"
    assert app.call("POST", body=draft(project=True), key="new-project", headers=restricted)[0] == 403
    assert len(app.rows("expense_recurring_rules")) == 1


@pytest.mark.parametrize("actor,project,mutation", [
    ("owner", False, "UPDATE api_keys SET revoked_at=1"),
    ("owner", False, "UPDATE api_keys SET expires_at=0"),
    ("owner", False, "UPDATE api_keys SET scopes='[\"expenses:read\"]'"),
    ("owner", False, "UPDATE api_keys SET restrictions='{\"shared\":false}'"),
    ("owner", True, "UPDATE api_keys SET restrictions='{\"shared\":true,\"projectIds\":[]}'"),
    ("owner", False, "UPDATE api_keys SET restrictions='{\"shared\":true,\"workspaceId\":\"other\"}'"),
    ("owner", False, "UPDATE api_keys SET user_id='other'"),
    ("editor", False, "UPDATE workspace_members SET revision=revision+1 WHERE user_id='editor'"),
    ("editor", False, "UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='editor'"),
    ("editor", False, "UPDATE workspace_members SET removed_at='removed',revision=revision+1 WHERE user_id='editor'"),
])
def test_scheduled_api_grant_rechecks_key_and_member_authority_and_blocks_once(app, actor, project, mutation):
    auth = api_auth(app, actor=actor)
    status, rule = app.call("POST", body=draft(project=project), actor=actor, headers=auth)
    assert status == 201
    writes = app.rows("ledger_plan_usage")[0]["writes"]
    with app.store._immediate() as db:
        db.execute(mutation)
    assert app.tick() == {"scanned": 1, "created": 0, "blocked": 1, "replayed": 0}
    stored = app.rows("expense_recurring_rules")[0]
    assert stored["id"] == rule["id"] and stored["blocked_code"] == "SCHEDULE_AUTHORIZATION_CHANGED"
    assert stored["next_occurrence_on"] == "2026-07-31"
    assert app.rows("expenses") == app.rows("expense_events") == app.rows("expense_recurring_occurrences") == []
    assert app.rows("ledger_plan_usage")[0]["writes"] == writes
    assert app.tick()["scanned"] == 0


def test_api_rule_grant_expires_at_execution_and_owner_can_reauthorize_with_cookie(app):
    auth = api_auth(app, expires=NOW + 60)
    status, rule = app.call("POST", body=draft(start="2026-10-01"), headers=auth)
    assert status == 201
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["blocked"] == 1
    blocked = app.call("GET", rule["id"])[1]
    status, resumed = app.call("PATCH", rule["id"], {"status": "active"}, revision=blocked["revision"])
    assert status == 200
    assert "_api_key_hash" not in json.loads(app.rows("expense_recurring_rules")[0]["template_json"])
    assert app.tick(now=october)["created"] == 1
    assert len(app.rows("expenses")) == 1


def test_replay_and_pause_keep_original_key_but_resume_transfers_to_new_key(app):
    first, second = api_auth(app), api_auth(app, suffix="second")
    body = draft(start="2026-09-01")
    status, rule = app.call("POST", body=body, headers=first)
    assert status == 201
    grant = json.loads(app.rows("expense_recurring_rules")[0]["template_json"])["_api_key_hash"]
    assert app.call("POST", body=body, headers=second) == (201, rule)
    assert app.call("PATCH", rule["id"], {"status": "paused"}, revision=1)[0] == 200
    assert json.loads(app.rows("expense_recurring_rules")[0]["template_json"])["_api_key_hash"] == grant
    status, resumed = app.call("PATCH", rule["id"], {"status": "active"}, headers=second, revision=2)
    assert status == 200
    assert json.loads(app.rows("expense_recurring_rules")[0]["template_json"])["_api_key_hash"] != grant
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=1 WHERE id='key_owner_one'")
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["created"] == 1
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=1 WHERE id='key_owner_second'")
    november = int(datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=november)["blocked"] == 1
    assert len(app.rows("expenses")) == 1


def test_member_api_edit_replaces_cookie_grant_and_runs_without_session(app):
    rule = create(app, start="2026-09-01")
    auth = api_auth(app, actor="editor")
    status, changed = app.call("PATCH", rule["id"], draft(start="2026-09-01", amount="88.00"),
                               revision=1, actor="editor", headers=auth)
    assert status == 200
    with app.store._immediate() as db:
        db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "editor"),))
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["created"] == 1
    assert app.rows("expenses")[0]["amount_minor"] == 8800
    assert app.rows("expense_events")[0]["actor_id"] == rule["id"] + ":editor"
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=1")
    november = int(datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=november)["blocked"] == 1
    assert len(app.rows("expenses")) == 1


@pytest.mark.parametrize("grant", [None, "", "incorrect", "G" * 64])
def test_malformed_persisted_key_grant_blocks_instead_of_becoming_session_authority(app, grant):
    rule = create(app)
    with app.store._immediate() as db:
        stored = app.rows("expense_recurring_rules")[0]
        template = json.loads(stored["template_json"])
        template["_api_key_hash"] = grant
        db.execute("UPDATE expense_recurring_rules SET template_json=? WHERE id=?", (json.dumps(template), rule["id"]))
    assert app.tick()["blocked"] == 1
    assert app.rows("expense_recurring_rules")[0]["blocked_code"] == "SCHEDULE_AUTHORIZATION_CHANGED"
    assert app.rows("expenses") == []


@pytest.mark.parametrize("mutation", [
    "UPDATE api_keys SET revoked_at=1",
    "UPDATE api_keys SET expires_at=0",
    "UPDATE api_keys SET scopes='[]'",
    "UPDATE api_keys SET restrictions='{\"shared\":false}'",
])
def test_key_change_between_schedule_read_and_commit_rolls_back_expense_audit_occurrence_and_quota(app, mutation):
    auth = api_auth(app)
    assert app.call("POST", body=draft(), headers=auth)[0] == 201
    before = {table: app.rows(table) for table in (
        "expense_recurring_rules", "expenses", "expense_events", "expense_recurring_occurrences",
        "ledger_activity_events", "ledger_plan_usage")}
    original_batch = app.raw.batch
    async def raced_batch(statements):
        if any("INSERT INTO expenses(" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            with app.store._immediate() as db:
                db.execute(mutation)
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.tick()
    assert {table: app.rows(table) for table in before} == before


def test_revocation_before_http_rule_creation_rolls_back_rule_grant_activity_and_quota(app):
    auth = api_auth(app)
    original_batch = app.raw.batch
    async def raced_batch(statements):
        if any("INSERT INTO expense_recurring_rules(" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            with app.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=1")
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.call("POST", body=draft(), headers=auth)
    assert app.rows("expense_recurring_rules") == app.rows("ledger_activity_events") == app.rows("ledger_plan_usage") == []


def test_quota_block_keeps_pending_period_and_pause_cancel_remain_available(app):
    app.policy["pro"]["records"] = 1
    app.policy["max"]["records"] = 1
    rule = create(app)
    result = app.tick()
    assert result["created"] == 1 and result["blocked"] == 1
    _, blocked = app.call("GET", rule["id"])
    assert blocked["blockedCode"] == "RECORD_LIMIT" and blocked["nextOccurrenceOn"] == "2026-08-31"
    app.policy["pro"]["writesPerMinute"] = 1
    assert app.call("PATCH", rule["id"], {"status": "paused"}, revision=blocked["revision"])[0] == 200
    assert app.call("DELETE", rule["id"], revision=blocked["revision"] + 1)[0] == 204
    assert len(app.rows("expenses")) == 1


def test_backlog_over_twelve_periods_requires_review_without_starving_newer_rule(app):
    oldest = create(app, start="2024-01-01")
    status, fresh = app.call("POST", body=draft(start="2026-09-01"), key="fresh")
    assert status == 201
    result = app.tick()
    assert result["blocked"] == result["created"] == 1
    assert app.call("GET", oldest["id"])[1]["blockedCode"] == "CATCHUP_REVIEW_REQUIRED"
    assert app.call("GET", fresh["id"])[1]["nextOccurrenceOn"] == "2026-10-31"


def test_global_tick_and_per_rule_catchup_are_finite(app):
    for number in range(5):
        assert app.call("POST", body=draft(start="2026-06-01"), key=f"bounded-{number}")[0] == 201
    result = app.tick(limit=4)
    assert result["created"] == 4
    assert len(app.rows("expenses")) == 4
    assert max(sum(row["rule_id"] == rule["id"] for row in app.rows("expense_recurring_occurrences"))
               for rule in app.rows("expense_recurring_rules")) <= 3
    with pytest.raises(ValueError):
        app.tick(limit=11)


def test_member_change_during_atomic_generation_rolls_back_every_financial_write(app):
    assert app.call("POST", body=draft(), actor="editor")[0] == 201
    writes_before = app.rows("ledger_plan_usage")[0]["writes"]
    def revoke_before_expense(statements):
        if any("INSERT INTO expenses(" in statement.sql for statement in statements):
            app.raw.before_batch = None
            with app.store._immediate() as db:
                db.execute("UPDATE workspace_members SET removed_at='removed',revision=revision+1 WHERE user_id='editor'")
    original_batch = app.raw.batch
    async def raced_batch(statements):
        revoke_before_expense(statements)
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.tick()
    assert app.rows("expenses") == app.rows("expense_events") == app.rows("expense_recurring_occurrences") == []
    assert app.rows("ledger_plan_usage")[0]["writes"] == writes_before
    assert app.rows("expense_recurring_rules")[0]["revision"] == 1


def test_fixed_rule_target_cannot_be_changed_and_cancel_never_resumes(app):
    rule = create(app)
    assert app.call("PATCH", rule["id"], draft(project=True), revision=1)[1]["error"]["code"] == "RECURRING_TARGET_IMMUTABLE"
    assert app.call("DELETE", rule["id"], revision=1)[0] == 204
    assert app.call("PATCH", rule["id"], {"status": "active"}, revision=2)[1]["error"]["code"] == "RECURRING_CANCELED"
    assert app.call("GET")[1]["items"] == []
    assert app.tick()["created"] == 0


def test_actor_reauthorization_keeps_creator_namespaces_and_exact_create_replay(app):
    body = draft(start="2026-09-01")
    status, original = app.call("POST", body=body, actor="editor")
    assert status == 201
    status, own_rule = app.call("POST", body=body, actor="owner")
    assert status == 201 and own_rule["id"] != original["id"]
    assert app.call("PATCH", original["id"], {"status": "paused"}, revision=1)[0] == 200
    status, transferred = app.call("PATCH", original["id"], {"status": "active"}, revision=2)
    assert status == 200 and transferred["nextOccurrenceOn"] == "2026-10-31"
    # Neither a grant transfer nor another creator's same raw key changes replay.
    assert app.call("POST", body=body, actor="editor") == (201, original)
    assert app.call("POST", body=body, actor="owner") == (201, own_rule)
    with app.store._immediate() as db:
        db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='editor'")
    assert app.call("POST", body=body, actor="editor")[0] == 403
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["created"] == 3
    assert app.call("GET", original["id"], now=NOW)[1]["status"] == "active"
    assert {row["actor_id"] for row in app.rows("expense_events") if row["actor_id"].startswith(original["id"])} == {original["id"] + ":owner"}


def test_project_and_shared_rules_stay_separate_across_paginated_lists(app):
    first = create(app)
    _, project_rule = app.call("POST", body=draft(project=True), key="project")
    _, second = app.call("POST", body=draft(), key="second")
    status, page = app.call("GET", params={"target": "shared", "limit": "1"})
    assert status == 200 and len(page["items"]) == 1 and page["nextCursor"]
    status, following = app.call("GET", params={"target": "shared", "limit": "1", "cursor": page["nextCursor"]})
    assert status == 200 and len(following["items"]) == 1 and following["nextCursor"] is None
    assert {page["items"][0]["id"], following["items"][0]["id"]} == {first["id"], second["id"]}
    assert app.call("GET", params={"target": "project", "projectId": "prj_1"})[1]["items"][0]["id"] == project_rule["id"]


def test_linked_rules_use_grant_actors_live_github_access_and_owner_may_reauthorize(app):
    class Gateway:
        def __init__(self):
            self.allowed = {"owner": True, "editor": True}
            self.tokens = []
        async def unseal(self, value):
            token = value.removeprefix("sealed:")
            self.tokens.append(token)
            return token
        async def installations(self, token):
            return [{"id": 501, "account": {"id": 1, "login": token, "type": "User"}}]
        async def repositories(self, token, installation):
            return [{"id": 202, "full_name": "team/private"}] if self.allowed[token] else []
    gateway = Gateway()
    with app.store._immediate() as db:
        for actor in ("owner", "editor"):
            name = record_name("users", actor)
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
            user["githubAccessToken"] = "sealed:" + actor
            db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", actor, user), name))
        db.execute("UPDATE ledger_projects SET github_repo_id=202,github_full_name='team/private' WHERE id='prj_1'")
        db.execute("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
            github_full_name,created_at) VALUES('owner','prj_1',202,'team/private','created')""")
    status, rule = app.call("POST", body=draft(project=True), actor="editor", gateway=gateway)
    assert status == 201
    gateway.allowed["editor"] = False
    assert app.tick(gateway=gateway)["blocked"] == 1
    assert gateway.tokens == ["editor", "editor"]
    _, blocked = app.call("GET", rule["id"])
    assert blocked["blockedCode"] == "GITHUB_ACCESS_REQUIRED"
    status, resumed = app.call("PATCH", rule["id"], {"status": "active"}, revision=blocked["revision"], gateway=gateway)
    assert status == 200 and resumed["nextOccurrenceOn"] == "2026-10-31"
    assert gateway.tokens[-1] == "owner"
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october, gateway=gateway)["created"] == 1
    assert app.rows("expense_events")[0]["actor_id"] == rule["id"] + ":owner"
    assert gateway.tokens[-1] == "owner"


def remove_project(app):
    with app.store._immediate() as db:
        db.execute("UPDATE ledger_projects SET deleted_at='2026-10-08T12:00:00Z',status='archived',revision=revision+1 WHERE id='prj_1'")


def test_removed_project_rules_and_replays_are_hidden_without_erasing_financial_history(app):
    body = draft(project=True, start="2026-09-01")
    rule = create(app, project=True, start="2026-09-01")
    assert app.tick()["created"] == 1
    status, shared = app.call("POST", body=draft(start="2026-09-01"), key="shared")
    assert status == 201
    remove_project(app)
    before = {table: app.rows(table) for table in (
        "expense_recurring_rules", "expense_recurring_occurrences", "expenses",
        "expense_events", "ledger_activity_events", "ledger_plan_usage")}
    assert app.call("GET", rule["id"])[0] == 404
    assert app.call("GET")[1]["items"] == [shared]
    assert app.call("GET", params={"target": "project", "projectId": "prj_1"})[1]["items"] == []
    assert app.call("POST", body=body)[0] == 404
    assert app.call("POST", body=draft())[0] == 404  # Same key cannot expose the hidden rule either.
    assert app.call("POST", body=body, key="fresh-removed-target")[0] == 404
    for method, payload in (("PATCH", {"status": "paused"}), ("PATCH", {"status": "active"}),
                            ("PATCH", body), ("DELETE", None)):
        assert app.call(method, rule["id"], payload, revision=2)[0] == 404
    assert {table: app.rows(table) for table in before} == before
    # Its due row remains as history, while unrelated shared schedules keep working.
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET next_run_at=0 WHERE id=?", (rule["id"],))
    assert app.tick() == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    assert len(app.rows("expenses")) == 2
    assert len(app.rows("expense_recurring_occurrences")) == 2
    assert app.tick() == {"scanned": 0, "created": 0, "blocked": 0, "replayed": 0}


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "completed", "canceled"])
def test_removed_project_hides_every_preserved_rule_state(app, status):
    rule = create(app, project=True)
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET status=? WHERE id=?", (status, rule["id"]))
    remove_project(app)
    assert app.call("GET", rule["id"])[0] == 404
    assert app.call("GET")[1]["items"] == []
    assert app.call("POST", body=draft(project=True))[0] == 404
    assert app.tick() == {"scanned": 0, "created": 0, "blocked": 0, "replayed": 0}
    assert app.rows("expense_recurring_rules")[0]["status"] == status


@pytest.mark.parametrize("existing_occurrence", [False, True])
def test_stale_selected_rule_for_removed_project_skips_generation_and_replay_maintenance(app, existing_occurrence):
    rule = create(app, project=True, start="2026-09-01")
    if existing_occurrence:
        assert app.tick()["created"] == 1
        first = app.rows("expense_recurring_occurrences")[0]
        with app.store._immediate() as db:
            db.execute("""UPDATE expense_recurring_rules SET next_occurrence_on=?,next_period_key=?,next_run_at=0
                WHERE id=?""", (first["scheduled_on"], first["period_key"], rule["id"]))
    stale_row = app.rows("expense_recurring_rules")[0]
    remove_project(app)
    before = {table: app.rows(table) for table in (
        "expense_recurring_rules", "expense_recurring_occurrences", "expenses",
        "expense_events", "ledger_activity_events", "ledger_plan_usage")}
    updated, result = asyncio.run(generate_occurrence(
        binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW), maintenance_binding=app.raw,
        gateway=NoGitHub(), row=stale_row, now=NOW))
    assert updated == stale_row and result is None
    assert {table: app.rows(table) for table in before} == before


def test_project_removal_during_atomic_generation_rolls_back_even_without_revision_change(app):
    create(app, project=True)
    before = {table: app.rows(table) for table in (
        "expense_recurring_rules", "expense_recurring_occurrences", "expenses",
        "expense_events", "ledger_activity_events", "ledger_plan_usage")}
    original_batch = app.raw.batch
    async def raced_batch(statements):
        if any("INSERT INTO expenses(" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            with app.store._immediate() as db:
                db.execute("UPDATE ledger_projects SET deleted_at='2026-10-08T12:00:00Z',status='archived' WHERE id='prj_1'")
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.tick()
    assert app.rows("ledger_projects")[0]["deleted_at"] == "2026-10-08T12:00:00Z"
    assert app.rows("ledger_projects")[0]["revision"] == 1
    assert {table: app.rows(table) for table in before} == before


@pytest.mark.parametrize("method,body", [("PATCH", {"status": "paused"}), ("DELETE", None)])
def test_project_removal_between_rule_read_and_pause_cancel_rolls_back(app, method, body):
    rule = create(app, project=True)
    before = {table: app.rows(table) for table in (
        "expense_recurring_rules", "ledger_activity_events", "ledger_plan_usage")}
    original_batch = app.raw.batch
    async def raced_batch(statements):
        if any("UPDATE expense_recurring_rules SET status='" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            remove_project(app)
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.call(method, rule["id"], body, revision=1)
    assert app.rows("ledger_projects")[0]["deleted_at"] == "2026-10-08T12:00:00Z"
    assert {table: app.rows(table) for table in before} == before


def test_archiving_preserves_exact_rule_replay_and_visibility(app):
    original = create(app, project=True)
    with app.store._immediate() as db:
        db.execute("UPDATE ledger_projects SET status='archived',revision=revision+1 WHERE id='prj_1'")
    assert app.call("POST", body=draft(project=True)) == (201, original)
    assert app.call("GET", original["id"]) == (200, original)
    assert app.call("GET")[1]["items"] == [original]


def _shanghai_instant(value):
    return int(datetime.fromisoformat(value + "+08:00").timestamp())


@pytest.mark.parametrize("project", [False, True])
def test_monthly_start_boundary_is_saved_without_changing_the_selected_day(app, project):
    body = draft(project=project, schedule={"frequency": "monthly", "day": 9,
        "timezone": "Asia/Shanghai", "startOn": "2026-10-01"})
    status, original = app.call("POST", body=body)
    assert status == 201 and original["nextOccurrenceOn"] == "2026-10-09"
    edited = {**body, "schedule": {**body["schedule"], "startOn": "2026-10-10"}}
    before = app.rows("expense_recurring_rules")
    # Constructing a form draft sends no write. The saved row and list remain
    # the original plan until an explicit versioned PATCH succeeds.
    assert app.call("GET", original["id"]) == (200, original)
    assert app.call("GET", params={"target": "project" if project else "shared"})[1]["items"] == [original]
    assert app.rows("expense_recurring_rules") == before
    status, saved = app.call("PATCH", original["id"], edited, revision=original["revision"])
    assert status == 200 and saved["revision"] == 2
    assert saved["schedule"]["startOn"] == "2026-10-10" and saved["schedule"]["day"] == 9
    # October 9 is before the new boundary; the next selected monthly day is
    # November 9, rather than interpreting startOn as a new monthly anchor.
    assert saved["nextOccurrenceOn"] == "2026-11-09"
    assert saved["nextRunAt"] == _shanghai_instant("2026-11-09T00:00:00")
    assert app.call("GET", original["id"]) == (200, saved)
    assert app.call("GET", params={"target": "project" if project else "shared"})[1]["items"] == [saved]
    stored = app.rows("expense_recurring_rules")[0]
    assert json.loads(stored["schedule_json"]) == saved["schedule"]
    assert stored["next_occurrence_on"] == "2026-11-09" and stored["next_period_key"] == "M2026-11"
    assert app.rows("expenses") == app.rows("expense_events") == app.rows("expense_recurring_occurrences") == []


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("edit_date,candidate", [
    ("2026-10-09", "2026-10-10"),
    ("2026-10-10", "2026-11-10"),
])
def test_monthly_day_edit_keeps_recorded_october_period_and_applies_in_november(app, project, edit_date, candidate):
    body = draft(project=project, schedule={"frequency": "monthly", "day": 9,
        "timezone": "Asia/Shanghai", "startOn": "2026-10-01"})
    status, original = app.call("POST", body=body)
    assert status == 201
    # Keep only this synthetic local Owner cookie valid through the explicitly
    # bounded edit/read journey. The scheduled grant does not rely on a cookie.
    with app.store._immediate() as db:
        session = {"userId": "owner", "expiresAt": _shanghai_instant("2026-11-10T01:00:00")}
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (
            encode_record("sessions", "owner", session), record_name("sessions", "owner")))
    october = _shanghai_instant("2026-10-09T00:00:00")
    assert app.tick(now=october) == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    recorded = {table: app.rows(table) for table in ("expenses", "expense_events", "expense_recurring_occurrences")}
    assert recorded["expenses"][0]["occurred_on"] == "2026-10-09"
    assert recorded["expense_recurring_occurrences"][0]["period_key"] == "M2026-10"
    edit_now = _shanghai_instant(edit_date + "T00:05:00")
    current = app.call("GET", original["id"], now=edit_now)[1]
    edited = {**body, "schedule": {**body["schedule"], "day": 10, "startOn": "2026-10-10"}}
    status, saved = app.call("PATCH", original["id"], edited, revision=current["revision"], now=edit_now)
    assert status == 200 and saved["schedule"]["day"] == 10
    assert saved["schedule"]["startOn"] == "2026-10-10"
    assert saved["nextOccurrenceOn"] == candidate
    assert saved["nextRunAt"] == _shanghai_instant(candidate + "T00:00:00")
    assert app.call("GET", original["id"], now=edit_now) == (200, saved)
    assert app.call("GET", params={"target": "project" if project else "shared"}, now=edit_now)[1]["items"] == [saved]
    assert json.loads(app.rows("expense_recurring_rules")[0]["schedule_json"]) == saved["schedule"]
    assert {table: app.rows(table) for table in recorded} == recorded
    usage_after_save = app.rows("ledger_plan_usage")
    # On local October 9, tomorrow is a valid candidate date. Its monthly
    # identity is already recorded: the runner advances the pointer without
    # publishing another expense/audit/occurrence or charging a business write.
    tick_now = _shanghai_instant("2026-10-10T00:06:00")
    result = app.tick(now=tick_now)
    assert result == {"scanned": int(edit_date == "2026-10-09"), "created": 0,
        "blocked": 0, "replayed": int(edit_date == "2026-10-09")}
    assert {table: app.rows(table) for table in recorded} == recorded
    assert app.rows("ledger_plan_usage") == usage_after_save
    next_rule = app.call("GET", original["id"], now=tick_now)[1]
    assert next_rule["nextOccurrenceOn"] == "2026-11-10"
    stored = app.rows("expense_recurring_rules")[0]
    assert stored["next_occurrence_on"] == "2026-11-10" and stored["next_period_key"] == "M2026-11"
    # The next calendar month creates exactly one new record on the edited day;
    # the old October expense and its durable financial audit stay unchanged.
    assert app.tick(now=_shanghai_instant("2026-11-10T00:00:00"))["created"] == 1
    assert [row["occurred_on"] for row in app.rows("expenses")] == ["2026-10-09", "2026-11-10"]
    assert [row["period_key"] for row in app.rows("expense_recurring_occurrences")] == ["M2026-10", "M2026-11"]
    for table in recorded:
        assert app.rows(table)[0] == recorded[table][0]
