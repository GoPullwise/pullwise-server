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
    RESOURCE, handle_recurring_request, run_due_recurring,
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
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("000*.sql")):
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


def test_api_key_cannot_create_persistent_schedule_authority(app):
    token = "pwk_synthetic_recurrence"
    with app.store._immediate() as db:
        db.execute("INSERT INTO api_keys VALUES('key','owner','key','prefix',?,?,NULL,'{\"shared\":true}',?,NULL,NULL)", (
            hashlib.sha256(token.encode()).hexdigest(), json.dumps(["expenses:read", "expenses:write"]), NOW))
    status, payload = app.call("POST", body=draft(), headers={"Cookie": "", "Authorization": "Bearer " + token})
    assert status == 403 and payload["error"]["code"] == "RECURRING_SESSION_REQUIRED"
    assert app.rows("expense_recurring_rules") == []


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
