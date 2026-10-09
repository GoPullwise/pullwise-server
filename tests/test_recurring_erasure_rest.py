"""REST grants survive erasure bookkeeping without renewing consumed periods."""
import asyncio
import json
from contextlib import closing
from datetime import datetime, timezone

import pytest

from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from test_ledger_recurring import NOW, NoGitHub, api_auth, app, draft


def route(app, method, path, headers, body=None, *, revision=None, params=None):
    chosen = dict(headers)
    if revision is not None:
        chosen["If-Match"] = f'"{revision}"'
    return asyncio.run(handle_ledger_request(
        binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW), gateway=NoGitHub(),
        method=method, path=path, headers=chosen, params=params or {}, body=body, now=NOW))


def cookie():
    return {"Cookie": "pw_session=owner", "Origin": "https://app.example.test"}


def day(month, date):
    return int(datetime(2026, month, date, 12, tzinfo=timezone.utc).timestamp())


def rule_call(app, method, identifier=None, body=None, **options):
    return app.call(method, identifier, body, handler=handle_ledger_request, **options)


def stored_rule(app):
    return app.rows("expense_recurring_rules")[0]


def grant(app):
    return json.loads(stored_rule(app)["template_json"]).get("_api_key_hash")


@pytest.mark.parametrize("source_scope", ["shared", "other-project"])
def test_key_rule_nullable_consumed_period_survives_key_and_cookie_adoption(app, source_scope):
    owner_key = api_auth(app, scopes=["expenses:read", "expenses:write", "projects:write"])
    editor_key = api_auth(app, actor="editor", suffix="replacement")
    target = {"kind": "shared"}
    if source_scope == "other-project":
        status, retained = route(app, "POST", "/api/v1/projects", owner_key,
                                 {"name": "Retained source project"})
        assert status == 201, retained
        target = {"kind": "project", "projectId": retained["id"]}
    body = draft(target=target, schedule={"frequency": "monthly", "day": 1,
        "timezone": "UTC", "startOn": "2026-10-01"})
    status, rule = rule_call(app, "POST", body=body, headers=owner_key)
    assert status == 201
    original_grant = grant(app)
    assert original_grant and "_api_key_hash" not in json.dumps(rule)
    assert app.tick() == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    expense, occurrence = app.rows("expenses")[0], app.rows("expense_recurring_occurrences")[0]
    move = {**{name: value for name, value in body.items() if name != "schedule"},
            "target": {"kind": "project", "projectId": "prj_1"},
            "occurredOn": expense["occurred_on"]}
    assert route(app, "PATCH", "/api/v1/expenses/" + expense["id"], owner_key,
                 move, revision=expense["revision"])[0] == 200
    before_erasure = stored_rule(app)
    assert route(app, "DELETE", "/api/v1/projects/prj_1", owner_key, revision=1) == (204, None)
    assert app.rows("expenses") == app.rows("expense_events") == []
    assert stored_rule(app) == before_erasure
    assert app.rows("expense_recurring_occurrences") == [{**occurrence, "expense_id": None}]
    assert app.rows("ledger_plan_usage")[0]["records"] == 0

    # A different real actor/key can edit the rule, but cannot recreate October.
    status, changed = rule_call(app, "PATCH", rule["id"],
        {**body, "schedule": {**body["schedule"], "day": 20}},
        revision=before_erasure["revision"], headers=editor_key)
    assert status == 200 and changed["nextOccurrenceOn"] == "2026-10-20"
    assert grant(app) != original_grant and stored_rule(app)["actor_user_id"] == "editor"
    assert app.tick(now=day(10, 20)) == {"scanned": 1, "created": 0, "blocked": 0, "replayed": 1}
    assert app.rows("expenses") == []
    assert app.rows("expense_recurring_occurrences") == [{**occurrence, "expense_id": None}]

    # Cookie edit/resume removes the revocable key grant, keeping the same fence.
    revision = stored_rule(app)["revision"]
    status, paused = rule_call(app, "PATCH", rule["id"], {"status": "paused"},
                               revision=revision, headers=editor_key)
    assert status == 200 and grant(app)
    status, edited = rule_call(app, "PATCH", rule["id"],
        {**body, "schedule": {**body["schedule"], "day": 25}}, revision=paused["revision"])
    assert status == 200 and edited["status"] == "paused"
    assert grant(app) is None and stored_rule(app)["actor_user_id"] == "owner"
    status, resumed = rule_call(app, "PATCH", rule["id"], {"status": "active"},
                                revision=edited["revision"])
    assert status == 200 and resumed["nextOccurrenceOn"] == "2026-10-25"
    assert "_api_key_hash" not in json.loads(stored_rule(app)["template_json"])
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=?", (NOW,))
    assert app.tick(now=day(10, 25)) == {"scanned": 1, "created": 0, "blocked": 0, "replayed": 1}
    assert app.rows("expenses") == []
    assert app.rows("expense_recurring_occurrences") == [{**occurrence, "expense_id": None}]
    assert app.tick(now=day(11, 25)) == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    generated = app.rows("expenses")[0]
    assert generated["occurred_on"] == "2026-11-25" and generated["target_kind"] == target["kind"]
    assert generated["project_id"] == target.get("projectId")
    assert {row["period_key"] for row in app.rows("expense_recurring_occurrences")} == {"M2026-10", "M2026-11"}
    assert app.rows("expense_events")[0]["actor_id"] == rule["id"] + ":owner"
    with closing(app.store.connect()) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_key_rule_category_tombstone_blocks_then_replacement_adopts_revocable_member_key(app):
    owner_key = api_auth(app, scopes=["expenses:read", "expenses:write", "categories:read", "categories:write"])
    editor_key = api_auth(app, actor="editor", suffix="replacement")
    body = draft(schedule={"frequency": "monthly", "day": 1,
        "timezone": "UTC", "startOn": "2026-10-01"})
    status, rule = rule_call(app, "POST", body=body, headers=owner_key)
    assert status == 201
    original = stored_rule(app)
    original_grant = grant(app)
    assert route(app, "POST", "/api/v1/categories/cat_1/remove", owner_key, {}, revision=1) == (204, None)
    assert stored_rule(app) == original
    removed = app.rows("expense_categories")[0]
    assert removed["id"] == "cat_1" and removed["name"] == "Hosting"
    assert removed["deleted_at"] is not None and removed["archived_at"] is not None
    assert route(app, "GET", "/api/v1/categories", owner_key) == (200, [])
    status, labels = route(app, "GET", "/api/v1/categories", owner_key,
                           params={"includeRemoved": "true"})
    assert status == 200 and labels[0]["id"] == "cat_1" and labels[0]["removedAt"]
    assert app.tick() == {"scanned": 1, "created": 0, "blocked": 1, "replayed": 0}
    blocked = stored_rule(app)
    assert blocked["blocked_code"] == "INVALID_CATEGORY"
    assert blocked["next_occurrence_on"] == original["next_occurrence_on"] and grant(app) == original_grant
    assert app.rows("expenses") == app.rows("expense_events") == app.rows("expense_recurring_occurrences") == []
    assert app.tick() == {"scanned": 0, "created": 0, "blocked": 0, "replayed": 0}
    status, replacement = route(app, "POST", "/api/v1/categories", owner_key,
                                {"name": "Replacement hosting"})
    assert status == 201
    status, edited = rule_call(app, "PATCH", rule["id"],
        {**body, "categoryId": replacement["id"]}, headers=editor_key, revision=blocked["revision"])
    assert status == 200 and edited["status"] == "blocked"
    assert grant(app) != original_grant and stored_rule(app)["actor_user_id"] == "editor"
    status, resumed = rule_call(app, "PATCH", rule["id"], {"status": "active"},
                                headers=editor_key, revision=edited["revision"])
    assert status == 200 and resumed["nextOccurrenceOn"] == "2026-11-01"
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key_owner_one'", (NOW,))
    assert app.tick(now=day(11, 1)) == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    expense = app.rows("expenses")[0]
    assert expense["category_id"] == replacement["id"]
    assert app.rows("expense_events")[0]["actor_id"] == rule["id"] + ":editor"
    before = {table: app.rows(table) for table in (
        "expenses", "expense_events", "expense_recurring_occurrences", "ledger_plan_usage")}
    with app.store._immediate() as db:
        db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key_editor_replacement'", (NOW,))
    assert app.tick(now=day(12, 1)) == {"scanned": 1, "created": 0, "blocked": 1, "replayed": 0}
    assert stored_rule(app)["blocked_code"] == "SCHEDULE_AUTHORIZATION_CHANGED"
    assert {table: app.rows(table) for table in before} == before
    assert app.tick(now=day(12, 1))["scanned"] == 0
    assert next(row for row in app.rows("expense_categories") if row["id"] == "cat_1") == removed
