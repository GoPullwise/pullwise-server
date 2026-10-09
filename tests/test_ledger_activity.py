"""Shared history proves real identity, atomic publication, scope and expiry."""
import asyncio
import json
from contextlib import closing

import pytest

from test_ledger_recurring import app, draft, NOW
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_activity import _stamp
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name


def request(app, method, path, body=None, *, actor="owner", now=NOW, params=None,
            revision=None, key="activity-expense", auth=None):
    headers = auth if auth is not None else {"Cookie": "pw_session=" + actor,
        "Origin": "https://app.example.test", **({"X-Pullwise-Workspace": "owner"} if actor != "owner" else {})}
    headers = {**headers, "Idempotency-Key": key}
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return asyncio.run(handle_ledger_request(binding=PlanLimitedD1(app.raw, policy=app.policy, now=now),
        gateway=None, method=method, path=path, headers=headers, params=params or {}, body=body, now=now))


def expense(**changes):
    value = draft(start="2026-10-01")
    value.pop("schedule")
    return {**value, "occurredOn": "2026-10-09", **changes}


def history(app, *, target="shared", project=None, **kwargs):
    return request(app, "GET", "/api/v1/activity", params={"target": target,
        **({"projectId": project} if project else {}), **kwargs.pop("params", {})}, **kwargs)


def identity(app, identifier, **changes):
    with closing(app.store.connect()) as db:
        saved = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", identifier),)).fetchone()[0])
        saved.update(changes)
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
            (encode_record("users", identifier, saved), record_name("users", identifier)))
        db.commit()


def key_for(app, project_ids, *, scopes=None, shared=False):
    status, key = asyncio.run(create_api_key(binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW),
        headers={"Cookie": "pw_session=owner", "Origin": "https://app.example.test"},
        body={"name": "Automation", "scopes": scopes or ["expenses:read"],
              "restrictions": {"projectIds": project_ids, "shared": shared}}, now=NOW))
    assert status == 201, key
    return {"Authorization": "Bearer " + key["key"]}, key


def seed_old(app, count, *, now=NOW, target="shared"):
    with closing(app.store.connect()) as db:
        for index in range(count):
            snapshot = {"target": {"kind": target}, "purpose": "Retired", "amount": "1.00", "currency": "USD"}
            db.execute("""INSERT INTO ledger_activity_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                "old_" + str(index), "operation_old_" + str(index), "owner", target, None,
                '{"kind":"user","userId":"owner","name":"Historical"}', "expense", "old_expense",
                "create", None, json.dumps(snapshot), _stamp(now - 86400 - index)))
        db.commit()


def test_email_member_identity_is_immutable_after_rename_removal_and_expense_deletion(app):
    identity(app, "editor", name="小李", providers=["email"], email="private@example.test",
             emailVerifiedAt=NOW - 1, emailCode="never-log-this", githubAccessToken="private-token")
    status, record = request(app, "POST", "/api/v1/expenses", expense(), actor="editor")
    assert status == 201
    assert request(app, "DELETE", "/api/v1/expenses/" + record["id"], actor="editor", revision=1)[0] == 204
    identity(app, "editor", name="New display name")
    with closing(app.store.connect()) as db:
        db.execute("UPDATE workspace_members SET removed_at='removed',revision=2 WHERE user_id='editor'")
        db.commit()
    status, page = history(app)
    assert status == 200 and len(page["items"]) == 2
    assert {row["action"] for row in page["items"]} == {"create", "delete"}
    assert all(row["actor"] == {"kind": "user", "userId": "editor", "name": "小李", "githubLogin": None}
               and row["resource"]["id"] == record["id"] and row["resource"]["label"] == "Hosting"
               for row in page["items"])
    persisted = json.dumps(app.rows("ledger_activity_events"))
    assert all(secret not in persisted for secret in ("private@example.test", "never-log-this", "private-token"))
    assert history(app, actor="editor")[0] == 404


def test_replays_stale_updates_and_noop_revisions_do_not_duplicate_activity(app):
    status, saved = request(app, "POST", "/api/v1/expenses", expense())
    assert status == 201
    assert request(app, "POST", "/api/v1/expenses", expense())[1]["id"] == saved["id"]
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"], expense(), revision=2)[0] == 412
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"], expense(), revision=1)[0] == 200
    assert len(app.rows("ledger_activity_events")) == 1
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"], expense(amount="20.10"), revision=2)[0] == 200
    assert len(app.rows("ledger_activity_events")) == 2
    assert app.rows("ledger_plan_usage")[0]["writes"] == 3


def test_currency_diff_preserves_exact_decimal_and_each_sides_currency(app):
    _, saved = request(app, "POST", "/api/v1/expenses", expense(amount="120", currency="JPY"))
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"],
        expense(amount="120", currency="KWD"), revision=1, now=NOW + 1)[0] == 200
    _, page = history(app, now=NOW + 1)
    update = next(row for row in page["items"] if row["action"] == "update")
    amount = next(change for change in update["changes"] if change["field"] == "amount")
    assert amount == {"field": "amount", "before": {"amount": "120", "currency": "JPY"},
                      "after": {"amount": "120.000", "currency": "KWD"}}


def test_moves_publish_to_both_pools_and_restricted_keys_see_only_allowed_side(app):
    identity(app, "owner", name="Owner")
    _, other = request(app, "POST", "/api/v1/projects", {"name": "Other private project"})
    old = expense(target={"kind": "project", "projectId": "prj_1"}, purpose="Visible before")
    _, saved = request(app, "POST", "/api/v1/expenses", old)
    new = {**old, "target": {"kind": "project", "projectId": other["id"]},
           "purpose": "Opposite-side private edit", "note": "Private note", "amount": "999.00"}
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"], new, revision=1)[0] == 200
    _, original = history(app, target="project", project="prj_1")
    _, destination = history(app, target="project", project=other["id"])
    origin_move = next(item for item in original["items"] if item["action"] == "move")
    dest_move = next(item for item in destination["items"] if item["action"] == "move")
    assert origin_move["operationId"] == dest_move["operationId"]
    assert origin_move["target"]["projectId"] == "prj_1" and dest_move["target"]["projectId"] == other["id"]
    auth, _ = key_for(app, ["prj_1"])
    status, protected = history(app, target="project", project="prj_1", auth=auth)
    assert status == 200
    move = next(row for row in protected["items"] if row["action"] == "move")
    assert move["resource"]["label"] == "Visible before"
    assert move["changes"] == [{"field": "target", "before": {"kind": "project", "projectId": "prj_1", "projectName": "Standalone"},
                                "after": {"kind": "restricted"}}]
    assert all(secret not in json.dumps(protected) for secret in (other["id"], "Other private project", "Private note", "Opposite-side private edit", "999.00"))
    assert history(app, target="project", project=other["id"], auth=auth)[0] == 403
    assert history(app, auth=auth)[0] == 403


def test_key_actor_marks_actual_issuer_without_credential_and_cannot_read_project_settings(app):
    identity(app, "owner", name="Issuer", githubLogin="issuer")
    _, created = request(app, "POST", "/api/v1/projects", {"name": "Key project"})
    auth, key = key_for(app, [created["id"]], scopes=["expenses:read", "expenses:write"])
    assert request(app, "POST", "/api/v1/expenses", expense(target={"kind": "project", "projectId": created["id"]}), auth=auth)[0] == 201
    status, page = history(app, target="project", project=created["id"], auth=auth)
    assert status == 200 and len(page["items"]) == 1
    assert page["items"][0]["resource"]["kind"] == "expense"
    assert page["items"][0]["actor"] == {"kind": "api_key", "userId": "owner", "name": "Issuer", "githubLogin": "issuer"}
    assert key["key"] not in json.dumps(page) and "sha256" not in json.dumps(page)


def test_project_settings_and_recurring_rule_edits_publish_specific_before_after(app):
    _, project = request(app, "POST", "/api/v1/projects", {"name": "New project", "productUrl": "https://example.test/app"})
    assert request(app, "PATCH", "/api/v1/projects/" + project["id"],
        {"description": "Changed", "status": "archived"}, revision=1)[0] == 200
    _, page = history(app, target="project", project=project["id"])
    archived = next(item for item in page["items"] if item["action"] == "archive")
    assert archived["resource"]["label"] == "New project"
    assert {change["field"] for change in archived["changes"]} == {"description", "status"}
    _, rule = app.call("POST", body=draft(start="2026-10-01"))
    assert app.call("PATCH", rule["id"], {"status": "paused"}, revision=1)[0] == 200
    assert app.call("PATCH", rule["id"], {"status": "active"}, revision=2)[0] == 200
    assert app.call("DELETE", rule["id"], revision=3)[0] == 204
    _, page = history(app)
    assert {row["action"] for row in page["items"]} == {"create", "pause", "resume", "cancel"}
    assert {row["resource"]["id"] for row in page["items"]} == {rule["id"]}


def test_generated_expense_identifies_automation_and_real_rule_creator(app):
    identity(app, "editor", name="Schedule creator")
    _, rule = app.call("POST", body=draft(start="2026-09-01"), actor="editor")
    assert app.tick()["created"] == 1
    _, page = history(app)
    generated = next(row for row in page["items"] if row["action"] == "generate")
    assert generated["actor"] == {"kind": "system", "userId": "editor", "name": "Schedule creator", "githubLogin": None}
    assert generated["resource"]["id"] != rule["id"] and generated["resource"]["kind"] == "expense"


def test_server_day_is_strictly_enforced_with_anchored_cursor_and_no_get_writes(app):
    for index in range(3):
        assert request(app, "POST", "/api/v1/expenses", expense(purpose=str(index)), key=str(index), now=NOW + index)[0] == 201
    seed_old(app, 2, now=NOW + 2)
    before = app.rows("ledger_activity_events")
    status, first = history(app, now=NOW + 2, params={"limit": "1", "from": "1900-01-01"})
    assert status == 200 and len(first["items"]) == 1 and first["nextCursor"]
    assert len(app.rows("ledger_activity_events")) == len(before)
    assert request(app, "POST", "/api/v1/expenses", expense(purpose="Later"), key="later", now=NOW + 3)[0] == 201
    _, second = history(app, now=NOW + 3, params={"limit": "100", "cursor": first["nextCursor"]})
    assert second["windowEnd"] == first["windowEnd"]
    assert {row["resource"]["label"] for row in second["items"]} == {"0", "1"}
    assert all(row["createdAt"] > second["windowStart"] for row in second["items"])


def test_retention_is_bounded_in_business_batch_and_idle_scheduler_preserves_financial_audit(app):
    _, record = request(app, "POST", "/api/v1/expenses", expense())
    financial_before = app.rows("expense_events")
    seed_old(app, 20)
    assert request(app, "PATCH", "/api/v1/expenses/" + record["id"], expense(purpose="Changed"), revision=1)[0] == 200
    old = [row for row in app.rows("ledger_activity_events") if row["id"].startswith("old_")]
    assert len(old) == 4
    assert app.tick() == {"scanned": 0, "created": 0, "blocked": 0, "replayed": 0}
    assert not any(row["id"].startswith("old_") for row in app.rows("ledger_activity_events"))
    assert len(app.rows("expense_events")) == len(financial_before) + 1
    assert app.rows("expenses")[0]["id"] == record["id"]


@pytest.mark.parametrize("params", [{}, {"target": "all"}, {"target": "shared", "projectId": "prj_1"},
    {"target": "project"}, {"target": "project", "projectId": "invalid/id"}, {"target": "shared", "limit": "101"},
    {"target": "shared", "limit": "0"}, {"target": "shared", "cursor": "invalid"}])
def test_malformed_activity_query_is_rejected_before_authentication(app, params):
    assert request(app, "GET", "/api/v1/activity", params=params, auth={}) == (422, {"error": {"code": "INVALID_INPUT"}})


def test_valid_near_ingress_limit_unicode_expense_preserves_history_without_truncating_changes(app):
    body = expense(note="😀" * 1600, purpose="中文用途" * 60)
    assert len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 8192
    status, saved = request(app, "POST", "/api/v1/expenses", body)
    assert status == 201
    changed = {**body, "note": "🐈" * 1600}
    assert request(app, "PATCH", "/api/v1/expenses/" + saved["id"], changed, revision=1)[0] == 200
    _, page = history(app)
    updated = next(row for row in page["items"] if row["action"] == "update")
    assert next(row for row in updated["changes"] if row["field"] == "note") == {
        "field": "note", "before": body["note"], "after": changed["note"]}


def test_failed_activity_insert_rolls_back_expense_audit_idempotency_usage_and_retirement(app, monkeypatch):
    import pullwise_server.cloudflare_ledger_activity as activity
    original = activity.activity_commands
    seed_old(app, 20)
    existing = app.rows("ledger_activity_events")

    async def invalid_activity(*args, **kwargs):
        statements = await original(*args, **kwargs)
        first = statements[0]
        values = list(first.params)
        values[8] = "invalid-action"
        return [app.raw.prepare(first.sql).bind(*values), *statements[1:]]

    monkeypatch.setattr(activity, "activity_commands", invalid_activity)
    status, _ = request(app, "POST", "/api/v1/expenses", expense())
    assert status == 409
    assert app.rows("expenses") == app.rows("expense_events") == app.rows("expense_create_idempotency") == []
    assert app.rows("ledger_plan_usage") == []
    assert app.rows("ledger_activity_events") == existing
    monkeypatch.setattr(activity, "activity_commands", original)
    assert request(app, "POST", "/api/v1/expenses", expense())[0] == 201


def test_late_membership_revocation_aborts_business_history_and_retention_together(app):
    _, record = request(app, "POST", "/api/v1/expenses", expense(), actor="editor")
    seed_old(app, 20)
    before = {table: app.rows(table) for table in ("expenses", "expense_events", "ledger_activity_events", "ledger_plan_usage")}
    raw = app.raw

    class RevokedDuringBatch:
        def prepare(self, sql):
            return raw.prepare(sql)

        async def batch(self, statements):
            statements = list(statements)
            if any(item.sql.lstrip().startswith("UPDATE expenses") for item in statements):
                with closing(app.store.connect()) as db:
                    db.execute("UPDATE workspace_members SET removed_at='removed',revision=2 WHERE user_id='editor'")
                    db.commit()
            return await raw.batch(statements)

    app.raw = RevokedDuringBatch()
    assert request(app, "PATCH", "/api/v1/expenses/" + record["id"],
        expense(purpose="Unauthorized new value"), actor="editor", revision=1)[0] == 412
    app.raw = raw
    assert all(app.rows(table) == values for table, values in before.items())


def test_activity_metadata_enrichment_does_not_change_expense_or_replay_contract(app):
    body = expense(target={"kind": "project", "projectId": "prj_1"})
    status, record = request(app, "POST", "/api/v1/expenses", body)
    assert status == 201 and record["target"] == {"kind": "project", "projectId": "prj_1"}
    assert "categoryName" not in record
    assert request(app, "POST", "/api/v1/expenses", body)[1] == record
    snapshot = json.loads(app.rows("ledger_activity_events")[0]["after_json"])
    assert snapshot["target"]["projectName"] == "Standalone" and snapshot["categoryName"] == "Hosting"
