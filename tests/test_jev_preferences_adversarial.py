"""Independent account-boundary and preference-preservation acceptance.

All identity, billing and model providers below are local synthetic fixtures.
"""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

import test_billing_record_storage as signed_billing
import test_cloudflare_email_auth as email
import test_cloudflare_billing_mutations as billing_mutations
import test_cloudflare_github_identity_http as github
import test_ledger_automatic_assistance as automatic
from test_ledger_automatic_edit import edit, financial_rows, set_plan, setup_expense
from test_ledger_review import review
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_creem_gateway import CreemRequestRejected
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_state_records import encode_record, record_name

ledger = automatic.ledger
runtime = email.runtime
OWNER = "usr_github_77"
PREFERENCE = {"jevEnabled": False, "jevPreferenceRevision": 7}


def stored(store, user_id):
    with store.connect() as db:
        return json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", user_id),)).fetchone()[0])


def replace_fields(store, user_id, changes):
    user = {**stored(store, user_id), **changes}
    with store.connect() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
            (encode_record("users", user_id, user), record_name("users", user_id)))


def preference_request(ledger, *, headers, enabled=None, revision=1):
    return automatic.call(ledger, None if enabled is None else {"enabled": enabled},
        method="GET" if enabled is None else "PATCH", path="/api/v1/account/jev",
        headers={**headers, "If-Match": f'"{revision}"'})


def member(ledger, *, plan="free", enabled=True):
    actor_id, session_id = "usr_email_preference_editor", "ses-preference-editor"
    actor = {"id": actor_id, "name": "Preference editor", "providers": ["email"],
        "email": "preference-editor@example.test", "emailVerified": True,
        "emailVerifiedAt": ledger.now, "jevEnabled": enabled, "jevPreferenceRevision": 7,
        "billing": {"plan": plan, "status": "active", "currentPeriodEnd": ledger.now + 86400}}
    with ledger.store.connect() as db:
        for kind, identity, value in (("users", actor_id, actor),
            ("sessions", session_id, {"userId": actor_id, "expiresAt": ledger.now + 3600})):
            db.execute("INSERT INTO app_state VALUES(?,?,?)",
                (record_name(kind, identity), encode_record(kind, identity, value), ledger.now))
        db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,
            joined_at,updated_at,invited_by_user_id) VALUES(?,?,'editor',1,'local','local',?)""",
            (OWNER, actor_id, OWNER))
    return actor_id, {"Cookie": "pw_session=" + session_id,
        "Origin": "https://app.example.test", "X-Pullwise-Workspace": OWNER}


@pytest.mark.parametrize("actor_plan,status", [("free", 403), ("pro", 200), ("max", 200)])
def test_selected_paid_workspace_never_lends_owner_preference_authority(ledger, actor_plan, status):
    actor_id, headers = member(ledger, plan=actor_plan)
    owner_before = stored(ledger.store, OWNER)
    actor_before = stored(ledger.store, actor_id)
    assert preference_request(ledger, headers=headers)[1]["revision"] == 7
    result = preference_request(ledger, headers=headers, enabled=False, revision=7)
    assert result[0] == status
    assert stored(ledger.store, OWNER) == owner_before
    if status == 200:
        assert stored(ledger.store, actor_id) == {**actor_before,
            "jevEnabled": False, "jevPreferenceRevision": 8}
        assert result[1]["monthlyBudgetUsd"] == ("3.00" if actor_plan == "pro" else "5.00")
    else:
        assert result[1] == {"error": {"code": "JEV_PLAN_REQUIRED"}}
        assert stored(ledger.store, actor_id) == actor_before


def test_free_member_personal_off_does_not_disable_paid_owner_model(ledger):
    body, saved, _ = setup_expense(ledger)
    _, headers = member(ledger, enabled=False)
    provider = automatic.Provider()
    status, result = edit(ledger, saved, {key: value for key, value in body.items()
        if key != "categoryId"}, provider, headers=headers)
    assert status == 200 and result["assistance"]["categorySource"] == "jev"
    assert len(provider.calls) == 1
    assert stored(ledger.store, OWNER).get("jevEnabled", True) is True


@pytest.mark.parametrize("entry", ["draft", "create", "edit", "review"])
def test_paid_member_personal_on_cannot_bypass_owner_off(ledger, entry):
    body, saved, _ = setup_expense(ledger)
    _, headers = member(ledger, plan="max", enabled=True)
    replace_fields(ledger.store, OWNER, PREFERENCE)
    before = financial_rows(ledger)
    with ledger.store.connect() as db:
        before_usage = [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")]
    provider = automatic.Provider()
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    if entry == "draft":
        status, result = automatic.call(ledger, {"target": body["target"], "purpose": body["purpose"]},
            provider, path="/api/v1/expense-suggestions", headers=headers)
        assert status == 200 and result["reason"] == "disabled"
    elif entry == "create":
        status, result = automatic.call(ledger, draft, provider, headers=headers, key="member-disabled")
        assert status == 422 and result["error"]["code"] == "CATEGORY_REQUIRED"
    elif entry == "edit":
        status, result = edit(ledger, saved, draft, provider, headers=headers)
        assert status == 422 and result["error"]["code"] == "CATEGORY_REQUIRED"
    else:
        status, result = review(ledger, saved, provider, headers=headers)
        assert status == 200 and result["checks"]["category"]["reason"] == "disabled"
    assert provider.calls == [] and financial_rows(ledger) == before
    with ledger.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")] == before_usage
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0


@pytest.mark.parametrize("stored_on", [False, True], ids=["legacy-missing-default-on", "explicit-saved-on"])
@pytest.mark.parametrize("entry", ["draft", "create", "edit", "review"])
def test_downgrade_to_free_cannot_use_retained_on_preference(ledger, stored_on, entry):
    body, saved, _ = setup_expense(ledger)
    if stored_on:
        replace_fields(ledger.store, OWNER, {"jevEnabled": True, "jevPreferenceRevision": 7})
    set_plan(ledger, "free")
    current = stored(ledger.store, OWNER)
    assert current.get("jevEnabled", True) is True
    status, preference = preference_request(ledger, headers=ledger.headers)
    assert status == 200 and preference["enabled"] is True
    assert preference["eligible"] is preference["available"] is False
    before = financial_rows(ledger)
    with ledger.store.connect() as db:
        usage_before = [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")]
    provider = automatic.Provider()
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    if entry == "draft":
        result = automatic.call(ledger, {"target": body["target"], "purpose": body["purpose"]},
            provider, path="/api/v1/expense-suggestions")
    elif entry == "create":
        result = automatic.call(ledger, draft, provider, key="free-retained-on")
    elif entry == "edit":
        result = edit(ledger, saved, draft, provider)
    else:
        result = review(ledger, saved, provider)
    assert result == ((403, {"error": {"code": "JEV_PLAN_REQUIRED"}})
        if entry in {"draft", "review"} else (422, {"error": {"code": "CATEGORY_REQUIRED"}}))
    assert provider.calls == [] and financial_rows(ledger) == before
    assert stored(ledger.store, OWNER) == current
    with ledger.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")] == usage_before
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0


@pytest.mark.parametrize("plan,budget", [("pro", "3.00"), ("max", "5.00")])
def test_cookie_and_restricted_key_inherit_owner_off_without_model_admission(ledger, plan, budget):
    body, saved, _ = setup_expense(ledger)
    set_plan(ledger, plan)
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Local preference acceptance key",
            "scopes": ["profile:read", "expenses:write", "suggestions:use"],
            "restrictions": {"projectIds": [], "shared": True}}, now=ledger.now + 3))
    assert status == 201
    headers = {"Authorization": "Bearer " + key["key"]}
    replace_fields(ledger.store, OWNER, PREFERENCE)
    ledger.binding.jev_available = True
    for credentials in (ledger.headers, headers):
        status, payload = asyncio.run(read_ledger_me(binding=ledger.binding,
            headers=credentials, now=ledger.now + 3))
        assert status == 200
        assert payload["entitlements"]["jev"] == {"eligible": True, "enabled": False,
            "available": False, "monthlyBudgetUsd": budget, "period": "utc-calendar-month",
            "rollover": False}
    before = financial_rows(ledger)
    with ledger.store.connect() as db:
        usage_before = [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")]
    provider = automatic.Provider()
    assert automatic.call(ledger, {"target": body["target"], "purpose": body["purpose"]},
        provider, path="/api/v1/expense-suggestions", headers=headers)[1]["reason"] == "disabled"
    draft = {key: value for key, value in body.items() if key != "categoryId"}
    assert automatic.call(ledger, draft, provider, headers=headers, key="key-owner-off")[0] == 422
    assert edit(ledger, saved, draft, provider, headers=headers)[0] == 422
    assert review(ledger, saved, provider, headers=headers)[1]["checks"]["target"]["reason"] == "disabled"
    assert provider.calls == [] and financial_rows(ledger) == before
    with ledger.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM ledger_plan_usage")] == usage_before
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0


@pytest.mark.parametrize("credentials,status,code", [
    ({"Authorization": "Bearer pwk_synthetic"}, 401, "UNAUTHENTICATED"),
    ({"Authorization": "Bearer session-synthetic"}, 401, "UNAUTHENTICATED"),
    ({"X-Pullwise-Api-Key": "pwk_synthetic"}, 401, "UNAUTHENTICATED"),
    ({"Cookie": "pw_session=ses-local", "Authorization": "Bearer pwk_synthetic"}, 401, "UNAUTHENTICATED"),
    ({"Cookie": "; ".join("pw_session=ses-" + str(index) for index in range(9))}, 400, "AMBIGUOUS_AUTH"),
])
@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_non_cookie_or_ambiguous_credentials_stop_before_any_d1(method, credentials, status, code):
    class NoDatabase:
        def prepare(self, sql):
            raise AssertionError("Rejected credentials must not read D1")
        async def batch(self, statements):
            raise AssertionError("Rejected credentials must not dispatch D1")
    result = asyncio.run(handle_ledger_request(binding=NoDatabase(), gateway=None,
        method=method, path="/api/v1/account/jev", params={}, now=email.NOW,
        headers={**credentials, "If-Match": '"1"'},
        body={"enabled": False} if method == "PATCH" else None))
    assert result == (status, {"error": {"code": code}})


def test_unclassified_native_failure_is_propagated_without_false_cas_response(ledger):
    before = stored(ledger.store, OWNER)
    original = ledger.binding.batch
    calls = []
    async def fail_preference_write(statements):
        if any(statement.sql.startswith("UPDATE app_state SET payload=?") for statement in statements):
            calls.append(statements)
            raise RuntimeError("synthetic missing native metadata")
        return await original(statements)
    ledger.binding.batch = fail_preference_write
    with pytest.raises(RuntimeError, match="missing native metadata"):
        preference_request(ledger, headers=ledger.headers, enabled=False)
    assert len(calls) == 1 and stored(ledger.store, OWNER) == before


@pytest.mark.parametrize("already_verified", [False, True])
def test_email_link_and_existing_email_login_preserve_off_and_revision(runtime, already_verified):
    email.seed_user(runtime, email=email.EMAIL if already_verified else None, verified=already_verified)
    replace_fields(runtime.binding.store, OWNER, PREFERENCE)
    session = "pw_session=ses-existing"
    issued = email.request(runtime, purpose="login" if already_verified else "link",
        cookie="" if already_verified else session)
    status, _, _ = email.verify(runtime, issued, session="" if already_verified else session)
    assert status == 200
    user = stored(runtime.binding.store, OWNER)
    assert {key: user[key] for key in PREFERENCE} == PREFERENCE
    assert user["email"] == email.EMAIL and "email" in user["providers"]
    assert user["billing"]["plan"] == "pro"


@pytest.mark.parametrize("operation", ["login", "installation"])
def test_github_identity_refresh_preserves_account_preference(tmp_path, operation):
    fixture, _, _ = github.seed(tmp_path / "github-preference.sqlite")
    binding, gateway = github.D1ShapedSQLite(fixture.store), github.GitHubStub()
    assert github.login(binding, gateway, fixture.now)[0] == 302
    replace_fields(fixture.store, OWNER, PREFERENCE)
    if operation == "login":
        assert github.login(binding, gateway, fixture.now + 2)[0] == 302
    else:
        _, _, login_headers = github.login(binding, gateway, fixture.now + 2)
        cookie = login_headers["Set-Cookie"].split(";", 1)[0]
        status, payload, _ = github.call(binding, gateway, fixture.now + 4, "GET",
            "/integrations/github/authorize", {"redirectTo": "/projects"}, {"Cookie": cookie})
        assert status == 200
        state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
        assert github.call(binding, gateway, fixture.now + 5, "GET", "/integrations/github/callback",
            {"state": state, "installation_id": "501"}, {"Cookie": cookie})[0] == 302
    user = stored(fixture.store, OWNER)
    assert {key: user[key] for key in PREFERENCE} == PREFERENCE


@pytest.mark.parametrize("operation", ["checkout", "cancel", "resume", "upgrade", "rejected_upgrade"])
def test_billing_mutations_and_rejected_stale_claim_preserve_preference(operation):
    fixture = billing_mutations.BillingMutationTests()
    fixture.setUp()
    try:
        fixture.user.update(PREFERENCE)
        fixture.user["billing"] = {"provider": "creem", "plan": "pro", "interval": "month",
            "status": "canceling" if operation == "resume" else "active", "subscriptionId": "sub_1"}
        if operation == "checkout":
            fixture.user["billing"] = {"plan": "free"}
        fixture._save_user()
        if operation == "checkout":
            path, body = "/billing/checkout-sessions", {"plan": "max", "interval": "month"}
        elif operation in {"upgrade", "rejected_upgrade"}:
            path, body = "/billing/change-interval", {"plan": "max", "interval": "month"}
        else:
            path, body = "/billing/" + operation + "-subscription", {}
        expected = PREFERENCE
        if operation == "rejected_upgrade":
            expected = {"jevEnabled": True, "jevPreferenceRevision": 8}
            async def reject_after_new_preference(path, payload):
                replace_fields(fixture.store, "owner", expected)
                raise CreemRequestRejected("synthetic definitive provider rejection")
            fixture.gateway.post = reject_after_new_preference
            with pytest.raises(CreemRequestRejected):
                fixture.call(path, body)
        else:
            assert fixture.call(path, body)[0] == 200
        user = stored(fixture.store, "owner")
        assert {key: user[key] for key in expected} == expected
        if operation == "rejected_upgrade":
            assert "billingChange" not in user
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize("kind", ["subscription.paid", "subscription.unpaid"])
def test_signed_billing_reducer_preserves_preference_and_independent_revision(tmp_path, kind):
    fixture, binding, user = signed_billing.fixture_state(tmp_path)
    user.update(PREFERENCE)
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
            (encode_record("users", "owner", user), record_name("users", "owner")))
    assert signed_billing.deliver(fixture, binding,
        signed_billing.event(fixture, "evt-preference-preserved", kind=kind, offset=1))["state"] == "applied"
    saved = signed_billing.stored_account(fixture)
    assert {key: saved[key] for key in PREFERENCE} == PREFERENCE
