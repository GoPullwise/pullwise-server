"""Automatic edits preserve monetary history, owner authority and active categories."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import test_ledger_automatic_assistance as automatic
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD

ledger = automatic.ledger


def set_plan(ledger, plan):
    with ledger.store.connect() as db:
        name = record_name("users", "usr_github_77")
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        user["billing"]["plan"] = plan
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", user["id"], user), name))


def setup_expense(ledger, target="shared", *, manual_name="Other"):
    manual = automatic.category(ledger, manual_name)
    chosen = manual if "Hosting" in manual_name else automatic.category(ledger, "Hosting")
    scope = {"kind": "shared"}
    if target == "project":
        status, project = automatic.call(ledger, {"name": "Standalone local project"}, path="/api/v1/projects")
        assert status == 201
        scope = {"kind": "project", "projectId": project["id"]}
    body = automatic.expense(manual, target=scope, note="Original note", quantity="2", unit="months")
    status, saved = automatic.call(ledger, body)
    assert status == 201
    return body, saved, chosen


def edit(ledger, saved, body, provider=None, *, revision=1, headers=None):
    return automatic.call(ledger, body, provider, method="PATCH",
        path=f"/api/v1/expenses/{saved['id']}", headers={**(headers or ledger.headers), "If-Match": f'"{revision}"'})


def financial_rows(ledger):
    tables = ("expenses", "expense_events", "ledger_activity_events", "expense_create_idempotency")
    with ledger.store.connect() as db:
        return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in tables}


@pytest.mark.parametrize("plan", ["pro", "max"])
@pytest.mark.parametrize("target", ["shared", "project"])
def test_paid_automatic_edit_selects_category_keeps_explicit_fields_and_excludes_self(ledger, plan, target):
    set_plan(ledger, plan)
    body, saved, chosen = setup_expense(ledger, target)
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    provider = automatic.Provider()
    status, updated = edit(ledger, saved, draft, provider)
    assert status == 200 and updated["revision"] == 2 and updated["categoryId"] == chosen != saved["categoryId"]
    assert updated["assistance"]["categorySource"] == "jev" and len(provider.calls) == 1
    assert "duplicateExpenseId" not in updated["assistance"]["suggestions"]
    assert updated["assistance"]["suggestions"]["targetKind"] == "shared"
    for key in ("target", "amount", "currency", "occurredOn", "purpose", "note", "quantity", "unit"):
        assert updated[key] == saved[key]
    assert automatic.call(ledger, method="GET", path=f"/api/v1/expenses/{saved['id']}")[1]["categoryId"] == chosen
    with ledger.store.connect() as db:
        rows = db.execute("SELECT before_json,after_json FROM expense_events WHERE action='update'").fetchall()
        assert len(rows) == 1 and json.loads(rows[0][0])["categoryId"] == saved["categoryId"]
        assert json.loads(rows[0][1]) == updated
        usage = db.execute("SELECT records,writes,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert usage["records"] == 1 and usage["writes"] == (5 if target == "project" else 4)
        assert usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
        assert db.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1


@pytest.mark.parametrize("same", [False, True])
def test_automatic_edit_requires_inferred_category_to_remain_active_even_when_unchanged(ledger, same):
    body, saved, chosen = setup_expense(ledger, manual_name="Hosting" if same else "Other")
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    before = financial_rows(ledger)
    with ledger.store.connect() as db:
        usage_before = dict(db.execute("SELECT * FROM ledger_plan_usage").fetchone())
    def archive_during_inference():
        with ledger.store.connect() as db:
            db.execute("UPDATE expense_categories SET archived_at='local-race' WHERE id=?", (chosen,))
    status, failure = edit(ledger, saved, draft, automatic.Provider(on_call=archive_during_inference))
    assert status == 412 and failure["error"]["code"] == "PRECONDITION_FAILED"
    assert financial_rows(ledger) == before
    with ledger.store.connect() as db:
        usage_after = dict(db.execute("SELECT * FROM ledger_plan_usage").fetchone())
        for key in ("writes", "minute_writes", "records", "projects"):
            assert usage_after[key] == usage_before[key]
        assert usage_after["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
        assert db.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1


def test_explicit_previous_archived_category_remains_editable_but_automatic_needs_active_choice(ledger):
    body, saved, chosen = setup_expense(ledger, manual_name="Hosting")
    assert automatic.call(ledger, method="DELETE", path=f"/api/v1/categories/{chosen}",
        headers={**ledger.headers, "If-Match": '"1"'})[0] == 204
    status, updated = edit(ledger, saved, {**body, "note": "Historical correction"}, automatic.Provider())
    assert status == 200 and updated["categoryId"] == chosen and updated["assistance"]["categorySource"] == "user"
    before = financial_rows(ledger)
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    provider = automatic.Provider()
    status, failure = edit(ledger, updated, draft, provider, revision=2)
    assert status == 422 and failure["error"]["code"] == "CATEGORY_REQUIRED"
    assert provider.calls == [] and financial_rows(ledger) == before


@pytest.mark.parametrize("provider", [None, automatic.Provider(confidence=0.6), automatic.Provider(failure=TimeoutError())])
def test_uncertain_or_unavailable_edit_preserves_saved_financial_rows(ledger, provider):
    body, saved, _ = setup_expense(ledger)
    before = financial_rows(ledger)
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    status, failure = edit(ledger, saved, {**draft, "amount": "99.00", "note": "Unsaved draft"}, provider)
    assert status == 422 and failure["error"]["code"] == "CATEGORY_REQUIRED"
    assert financial_rows(ledger) == before
    with ledger.store.connect() as db:
        usage = db.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(usage) == (3, 1, JEV_RESERVATION_MICROUSD if provider else 0)


@pytest.mark.parametrize("revision,status", [(None, 428), ('"invalid"', 422), ('"0"', 422), ('"2"', 412)])
def test_automatic_edit_validates_revision_before_any_model_admission(ledger, revision, status):
    body, saved, _ = setup_expense(ledger)
    before = financial_rows(ledger)
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    headers = {**ledger.headers, **({"If-Match": revision} if revision is not None else {})}
    provider = automatic.Provider()
    assert automatic.call(ledger, draft, provider, method="PATCH", path=f"/api/v1/expenses/{saved['id']}", headers=headers)[0] == status
    assert provider.calls == [] and financial_rows(ledger) == before
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0


def test_expense_write_key_can_request_automatic_edit_without_suggestions_scope(ledger):
    body, saved, chosen = setup_expense(ledger)
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "bounded local editor", "scopes": ["expenses:write"],
            "restrictions": {"shared": True}}, now=ledger.now+3))
    assert status == 201
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    status, updated = edit(ledger, saved, draft, automatic.Provider(), headers={"Authorization": "Bearer " + token["key"]})
    assert status == 200 and updated["categoryId"] == chosen
    with ledger.store.connect() as db:
        actor = db.execute("SELECT actor_kind,actor_id FROM expense_events WHERE action='update'").fetchone()
        assert tuple(actor) == ("api_key", "usr_github_77:sha256:" + hashlib.sha256(token["key"].encode()).hexdigest())


@pytest.mark.parametrize("forbidden", ["current", "new"])
def test_automatic_edit_checks_key_authority_on_current_and_new_targets_before_provider(ledger, forbidden):
    body, saved, _ = setup_expense(ledger, "project" if forbidden == "current" else "shared")
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"scopes": ["expenses:write"], "restrictions": {"projectIds": [], "shared": True}}, now=ledger.now+3))
    assert status == 201
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    if forbidden == "new":
        status, project = automatic.call(ledger, {"name": "Forbidden new target"}, path="/api/v1/projects")
        assert status == 201
        draft["target"] = {"kind": "project", "projectId": project["id"]}
    before = financial_rows(ledger)
    provider = automatic.Provider()
    status, failure = edit(ledger, saved, draft, provider, headers={"Authorization": "Bearer " + token["key"]})
    assert status == 403 and failure["error"]["code"] == "TARGET_FORBIDDEN"
    assert provider.calls == [] and financial_rows(ledger) == before


@pytest.mark.parametrize("changed", ["session", "key", "owner_plan"])
def test_automatic_edit_retains_credential_and_owner_snapshot_fences_during_provider_call(ledger, changed):
    body, saved, _ = setup_expense(ledger)
    headers = ledger.headers
    key_id = None
    if changed == "key":
        status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
            body={"scopes": ["expenses:write"], "restrictions": {"shared": True}}, now=ledger.now+3))
        assert status == 201
        headers, key_id = {"Authorization": "Bearer " + token["key"]}, token["id"]
    before = financial_rows(ledger)
    def invalidate():
        if changed == "owner_plan":
            set_plan(ledger, "free")
        else:
            with ledger.store.connect() as db:
                if changed == "key":
                    db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (ledger.now+3, key_id))
                else:
                    session_id = ledger.headers["Cookie"].split("=", 1)[1]
                    db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", session_id),))
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    provider = automatic.Provider(on_call=invalidate)
    status, failure = edit(ledger, saved, draft, provider, headers=headers)
    assert status == (403 if changed == "owner_plan" else 401)
    assert failure["error"]["code"] == ("AUTHORIZATION_CHANGED" if changed == "owner_plan" else "UNAUTHENTICATED")
    assert "assistance" not in failure
    assert len(provider.calls) == 1 and financial_rows(ledger) == before


@pytest.mark.parametrize("owner_plan,actor_plan,allowed", [("pro", "free", True), ("max", "free", True), ("free", "max", False)])
def test_automatic_edit_uses_workspace_owner_plan_and_audits_the_actual_editor(ledger, owner_plan, actor_plan, allowed):
    set_plan(ledger, owner_plan)
    body, saved, chosen = setup_expense(ledger)
    actor_id, session_id = "usr_email_local_editor", "ses-local-editor"
    actor = {"id": actor_id, "name": "Local editor", "providers": ["email"],
        "email": "editor@example.test", "emailVerified": True, "emailVerifiedAt": ledger.now,
        "billing": {"plan": actor_plan, "status": "active", "currentPeriodEnd": ledger.now+86400}}
    with ledger.store.connect() as db:
        for kind, identity, value in [("users", actor_id, actor),
                ("sessions", session_id, {"userId": actor_id, "expiresAt": ledger.now+3600})]:
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name(kind, identity), encode_record(kind, identity, value), ledger.now))
        db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,joined_at,updated_at,invited_by_user_id)
            VALUES('usr_github_77',?,'editor',1,'local','local','usr_github_77')""", (actor_id,))
    before = financial_rows(ledger)
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    provider = automatic.Provider()
    status, result = edit(ledger, saved, draft, provider, headers={"Cookie": "pw_session="+session_id,
        "Origin": "https://app.example.test", "X-Pullwise-Workspace": "usr_github_77"})
    assert status == (200 if allowed else 422)
    if not allowed:
        assert result["error"]["code"] == "CATEGORY_REQUIRED"
        assert provider.calls == [] and financial_rows(ledger) == before
    else:
        assert result["categoryId"] == chosen and len(provider.calls) == 1
        with ledger.store.connect() as db:
            audit = db.execute("SELECT actor_kind,actor_id FROM expense_events WHERE action='update'").fetchone()
            activity = db.execute("SELECT actor_json FROM ledger_activity_events WHERE action='update' AND resource_kind='expense'").fetchone()
            assert tuple(audit) == ("session", actor_id) and json.loads(activity[0])["userId"] == actor_id


@pytest.mark.parametrize("quota", ["daily", "monthly"])
@pytest.mark.parametrize("plan", ["pro", "max"])
def test_paid_edit_exhaustion_preserves_automatic_draft_and_allows_explicit_save(ledger, plan, quota):
    set_plan(ledger, plan)
    body, saved, _ = setup_expense(ledger)
    before = financial_rows(ledger)
    with ledger.store.connect() as db:
        if quota == "daily":
            day = datetime.fromtimestamp(ledger.now+3, timezone.utc).date().isoformat()
            db.execute("INSERT INTO expense_suggestion_budget VALUES('usr_github_77',?,20)", (day,))
        else:
            budget = 3_000_000 if plan == "pro" else 5_000_000
            db.execute("UPDATE ledger_plan_usage SET jev_reserved_microusd=? WHERE owner_id='usr_github_77'", (budget,))
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    provider = automatic.Provider()
    status, failure = edit(ledger, saved, draft, provider)
    assert status == 422 and failure["error"]["code"] == "CATEGORY_REQUIRED"
    assert failure["assistance"]["reason"] == ("SUGGESTION_LIMIT" if quota == "daily" else "JEV_BUDGET_LIMIT")
    assert provider.calls == [] and financial_rows(ledger) == before
    status, updated = edit(ledger, saved, {**body, "note": "Explicit save remains available"}, provider)
    assert status == 200 and updated["categoryId"] == saved["categoryId"]
    assert updated["assistance"]["categorySource"] == "user" and provider.calls == []


def test_expense_edit_contract_and_recurring_inputs_keep_distinct_category_rules():
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi/ledger-v1.yaml").read_text())
    schemas = contract["components"]["schemas"]
    patch = schemas["ExpensePatchInput"]
    assert patch["allOf"] == [{"$ref": "#/components/schemas/ExpenseInput"}]
    assert schemas["ExpenseInput"]["allOf"] == [{"$ref": "#/components/schemas/ExpenseFields"}]
    assert schemas["ExpenseInput"]["unevaluatedProperties"] is False
    assert "categoryId" not in schemas["ExpenseFields"]["required"]
    assert "categoryId" in schemas["RecurringExpenseFields"]["required"]
    assert "explicit" in schemas["RecurringExpenseFields"]["properties"]["categoryId"]["description"].lower()
