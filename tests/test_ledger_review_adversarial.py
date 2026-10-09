"""Saved-record inspection protects privacy and rejects stale authority."""
import asyncio
import json

import pytest

import test_ledger_automatic_assistance as automatic
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD


ledger = automatic.ledger
OWNER = "usr_github_77"
FINANCIAL_TABLES = ("expenses", "expense_events", "expense_create_idempotency", "ledger_activity_events")


def saved_record(ledger, category_id, *, key="source", **changes):
    status, record = automatic.call(ledger, automatic.expense(category_id, **changes), key=key)
    assert status == 201
    return record


def standalone_project(ledger):
    status, project = automatic.call(ledger, {"name": "Other authorized target"}, path="/api/v1/projects")
    assert status == 201
    return project["id"]


def financial_rows(ledger):
    with ledger.store.connect() as db:
        return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in FINANCIAL_TABLES}


def usage(ledger):
    with ledger.store.connect() as db:
        return dict(db.execute("SELECT * FROM ledger_plan_usage WHERE owner_id=?", (OWNER,)).fetchone())


def review(ledger, record, provider=None, *, headers=None, body=None, path=None):
    return automatic.call(ledger, {} if body is None else body, provider,
        path=path or f"/api/v1/expenses/{record['id']}/review",
        headers={**(headers or ledger.headers), "If-Match": f'"{record["revision"]}"'})


def write_only_key(ledger):
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Local inspection key", "scopes": ["expenses:write"],
              "restrictions": {"projectIds": [], "shared": True}}, now=ledger.now + 3))
    assert status == 201
    return {"Authorization": "Bearer " + key["key"]}


def assert_no_financial_charge(before, after):
    for field in ("projects", "records", "writes", "minute_writes"):
        assert after[field] == before[field]


@pytest.mark.parametrize("field", ["purpose", "note"])
def test_invalid_model_context_preserves_local_duplicates_without_admission(ledger, field):
    category = automatic.category(ledger)
    source = saved_record(ledger, category, **{field: "data:image/png;base64,legal-saved-text"})
    candidate = saved_record(ledger, category, key="candidate", **{field: source[field]})
    before, before_usage = financial_rows(ledger), usage(ledger)
    provider = automatic.Provider()

    status, result = review(ledger, source, provider)

    assert status == 200
    for dimension in ("category", "target"):
        assert result["checks"][dimension]["status"] == "unavailable"
        assert result["checks"][dimension]["reason"] == "invalid_context"
    assert result["checks"]["duplicate"] == {
        "status": "issue", "candidate": {"id": candidate["id"], "revision": candidate["revision"]}}
    assert provider.calls == []
    assert financial_rows(ledger) == before
    assert usage(ledger) == before_usage
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0


def test_write_only_key_receives_minimal_candidate_and_model_observes_only_saved_source(ledger):
    category = automatic.category(ledger)
    source = saved_record(ledger, category, purpose="Saved shared hosting", note="Source explanation")
    candidate = saved_record(ledger, category, key="candidate", purpose=source["purpose"],
                             note="Candidate secret must stay out of the response and model")
    project_id = standalone_project(ledger)
    foreign = saved_record(ledger, category, key="foreign", purpose=source["purpose"],
                           target={"kind": "project", "projectId": project_id}, note="Foreign secret")
    headers = write_only_key(ledger)
    before, before_usage = financial_rows(ledger), usage(ledger)
    provider = automatic.Provider()

    status, result = review(ledger, source, provider, headers=headers)

    assert status == 200 and len(provider.calls) == 1
    assert provider.calls[0]["state"] == {"purpose": source["purpose"], "note": source["note"]}
    assert result["checks"]["duplicate"] == {
        "status": "issue", "candidate": {"id": candidate["id"], "revision": candidate["revision"]}}
    assert set(result) <= {"expenseId", "revision", "questionVersion", "modelVersion", "suggestionId", "checks"}
    encoded = json.dumps(result)
    assert foreign["id"] not in encoded and "secret" not in encoded
    assert not ({"purpose", "note", "amount", "amountMinor", "currency", "occurredOn", "target"} & set(result))
    assert financial_rows(ledger) == before
    after_usage = usage(ledger)
    assert_no_financial_charge(before_usage, after_usage)
    assert after_usage["jev_reserved_microusd"] - before_usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
    assert automatic.call(ledger, method="GET", path=f"/api/v1/expenses/{candidate['id']}", headers=headers)[0] == 403


@pytest.mark.parametrize("changed", ["source_move", "source_delete", "source_edit", "candidate_move", "candidate_edit"])
def test_provider_time_source_and_candidate_changes_never_publish_old_target_evidence(ledger, changed):
    category = automatic.category(ledger)
    source = saved_record(ledger, category)
    candidate = saved_record(ledger, category, key="candidate")
    project_id = standalone_project(ledger)
    headers = write_only_key(ledger)
    before_usage = usage(ledger)
    changed_rows = []

    def mutate_during_provider():
        identity = source["id"] if changed.startswith("source") else candidate["id"]
        with ledger.store.connect() as db:
            if changed.endswith("move"):
                db.execute("UPDATE expenses SET target_kind='project',project_id=?,revision=revision+1 WHERE id=?",
                           (project_id, identity))
            elif changed.endswith("delete"):
                db.execute("UPDATE expenses SET deleted_at='2026-10-09T00:00:00Z',revision=revision+1 WHERE id=?",
                           (identity,))
            else:
                db.execute("UPDATE expenses SET purpose='Changed description',revision=revision+1 WHERE id=?",
                           (identity,))
        changed_rows.append(financial_rows(ledger))

    provider = automatic.Provider(on_call=mutate_during_provider)
    status, result = review(ledger, source, provider, headers=headers)

    assert len(provider.calls) == 1 and financial_rows(ledger) == changed_rows[0]
    assert_no_financial_charge(before_usage, usage(ledger))
    with ledger.store.connect() as db:
        assert db.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0
        events = db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0]
    if changed.startswith("source"):
        assert status == 412 and result == {"error": {"code": "PRECONDITION_FAILED"}}
        assert events == 0
    else:
        assert status == 200 and result["checks"]["duplicate"] == {"status": "checked"}
        assert candidate["id"] not in json.dumps(result) and events == 1


def test_paid_owner_downgrade_during_provider_blocks_publication_but_retains_admitted_cost(ledger):
    category = automatic.category(ledger)
    source = saved_record(ledger, category)
    before, before_usage = financial_rows(ledger), usage(ledger)

    def downgrade_owner():
        with ledger.store.connect() as db:
            name = record_name("users", OWNER)
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
            user["billing"]["plan"] = "free"
            db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", OWNER, user), name))

    provider = automatic.Provider(on_call=downgrade_owner)
    status, result = review(ledger, source, provider)
    assert status == 403 and result == {"error": {"code": "JEV_PLAN_REQUIRED"}}
    assert len(provider.calls) == 1 and financial_rows(ledger) == before
    after_usage = usage(ledger)
    assert_no_financial_charge(before_usage, after_usage)
    assert after_usage["jev_reserved_microusd"] - before_usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["key", "session", "member_role"])
def test_credentials_are_rechecked_after_provider_before_any_result_is_published(ledger, changed):
    category = automatic.category(ledger)
    source = saved_record(ledger, category)
    headers = write_only_key(ledger) if changed == "key" else ledger.headers
    actor_id = "usr_email_inspection_editor"
    if changed == "member_role":
        actor = {"id": actor_id, "providers": ["email"], "email": "editor@example.test",
                 "emailVerified": True, "emailVerifiedAt": ledger.now,
                 "billing": {"plan": "free", "status": "active"}}
        with ledger.store.connect() as db:
            for kind, identity, value in (
                ("users", actor_id, actor),
                ("sessions", "inspection-editor", {"userId": actor_id, "expiresAt": ledger.now + 3600}),
            ):
                db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                           (record_name(kind, identity), encode_record(kind, identity, value), ledger.now))
            db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,joined_at,
                updated_at,invited_by_user_id) VALUES(?,?,'editor',1,'local','local',?)""",
                       (OWNER, actor_id, OWNER))
        headers = {"Cookie": "pw_session=inspection-editor", "X-Pullwise-Workspace": OWNER,
                   "Origin": "https://app.example.test"}
    before, before_usage = financial_rows(ledger), usage(ledger)

    def revoke():
        with ledger.store.connect() as db:
            if changed == "key":
                db.execute("UPDATE api_keys SET revoked_at=? WHERE name='Local inspection key'", (ledger.now + 3,))
            elif changed == "session":
                session_id = headers["Cookie"].split("=", 1)[1]
                db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", session_id),))
            else:
                db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE workspace_id=? AND user_id=?",
                           (OWNER, actor_id))

    provider = automatic.Provider(on_call=revoke)
    status, result = review(ledger, source, provider, headers=headers)
    assert status in {401, 403} and set(result) == {"error"}
    assert len(provider.calls) == 1 and financial_rows(ledger) == before
    after_usage = usage(ledger)
    assert_no_financial_charge(before_usage, after_usage)
    assert after_usage["jev_reserved_microusd"] - before_usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0


def test_same_current_category_archived_during_provider_is_uncertain_without_active_suggestion(ledger):
    category = automatic.category(ledger)
    source = saved_record(ledger, category)
    before, before_usage = financial_rows(ledger), usage(ledger)

    def archive():
        with ledger.store.connect() as db:
            db.execute("UPDATE expense_categories SET archived_at='local-race',revision=revision+1 WHERE id=?", (category,))

    provider = automatic.Provider(on_call=archive)
    status, result = review(ledger, source, provider)
    assert status == 200 and result["checks"]["category"]["status"] == "uncertain"
    assert result["checks"]["category"]["current"] == category
    assert "suggested" not in result["checks"]["category"]
    assert financial_rows(ledger) == before
    assert_no_financial_charge(before_usage, usage(ledger))
    with ledger.store.connect() as db:
        assert db.execute("SELECT category_id FROM expense_suggestion_events").fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0


@pytest.mark.parametrize("section", ["expensesx", "expenses-export", "expenses_backup"])
def test_review_endpoint_does_not_accept_neighboring_resource_prefixes(ledger, section):
    category = automatic.category(ledger)
    source = saved_record(ledger, category)
    before_usage = usage(ledger)
    provider = automatic.Provider()
    result = review(ledger, source, provider, path=f"/api/v1/{section}/{source['id']}/review")
    assert result is None  # The outer HTTP router owns unknown-route responses.
    assert provider.calls == [] and usage(ledger) == before_usage
