"""SQLite proves bounded inbox visibility and single-attempt mail delivery."""
import asyncio
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ledger_d1_fixture import D1ShapedSQLite, Prepared, Store
from pullwise_server.cloudflare_email_gateway import WorkerEmailGateway
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_recurring_notifications import (
    RESOURCE, handle_recurring_notification_request, notify_recurring_failure,
)
from pullwise_server.cloudflare_state_records import encode_record, record_name

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_791_632_000
TEMPLATE = {"target_kind": "shared", "project_id": None, "amount_minor": 1234,
    "currency": "USD", "category_id": "cat_1", "purpose": "云服务月费",
    "note": None, "quantity_decimal": None, "unit": None}


class SafePrepared(Prepared):
    async def first(self):
        with closing(self.binding.store.connect()) as db:
            row = db.execute(self.sql, self.params).fetchone()
            return dict(row) if row else None


class SafeD1(D1ShapedSQLite):
    def prepare(self, sql):
        return SafePrepared(self, sql)


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "notifications.db")
    raw = SafeD1(store)

    def user(identifier, **fields):
        value = {"id": identifier, "name": identifier, **fields}
        with closing(store.connect()) as db:
            db.execute("INSERT OR REPLACE INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("users", identifier), encode_record("users", identifier, value), NOW))
            db.commit()

    with closing(store.connect()) as db:
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
            db.executescript(path.read_text())
        for identifier in ("owner", "editor", "other"):
            value = {"userId": identifier, "expiresAt": NOW + 1000}
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("sessions", identifier), encode_record("sessions", identifier, value), NOW))
        db.execute("INSERT INTO workspace_members VALUES('owner','editor','editor',1,'joined','updated',NULL,'owner')")
        db.commit()
    for identifier in ("owner", "editor", "other"):
        user(identifier)

    def pending(*, rule_id="rr_1", period="m:2026-10", recipient="editor", state="pending", project=False):
        template = {**TEMPLATE, **({"target_kind": "project", "project_id": "prj_1"} if project else {})}
        with closing(store.connect()) as db:
            if project:
                db.execute("""INSERT OR IGNORE INTO ledger_projects(id,owner_id,name,github_repo_id,
                    github_full_name,description,status,revision,created_at,updated_at)
                    VALUES('prj_1','owner','Standalone',NULL,NULL,'','active',1,'created','updated')""")
            db.execute("""INSERT OR IGNORE INTO expense_recurring_rules(id,owner_id,actor_user_id,
                target_kind,project_id,template_json,schedule_json,status,revision,created_at,
                updated_at,create_key,create_sha256,create_response_json)
                VALUES(?,'owner','editor',?,?,?,'{}','active',1,'created','updated',?,?, '{}')""",
                (rule_id, template["target_kind"], template["project_id"], json.dumps(template), rule_id, "f" * 64))
            db.execute("""INSERT INTO expense_recurring_pending(rule_id,owner_id,period_key,
                scheduled_on,template_json,failed_code,rule_revision,created_at,recipient_user_id,
                notification_state) VALUES(?,'owner',?,'2026-10-01',?,'PLAN_EXPENSE_LIMIT',1,?,?,?)""",
                (rule_id, period, json.dumps(template), rule_id + period, recipient, state))
            db.commit()
        return {"id": rule_id, "owner_id": "owner"}

    def query(sql, values=()):
        with closing(store.connect()) as db:
            return [dict(row) for row in db.execute(sql, values)]

    def mutate(sql, values=()):
        with closing(store.connect()) as db:
            db.execute(sql, values)
            db.commit()

    def inbox(actor="editor", **headers):
        return asyncio.run(handle_recurring_notification_request(binding=raw, method="GET",
            path=RESOURCE, headers={"Cookie": "pw_session=" + actor, **headers}, now=NOW))

    def notify(row, mail=None, period="m:2026-10"):
        return asyncio.run(notify_recurring_failure(binding=raw, row=row,
            period_key=period, now=NOW, email_gateway=mail))

    return SimpleNamespace(raw=raw, user=user, pending=pending, inbox=inbox,
        notify=notify, query=query, mutate=mutate)


def gateway():
    return WorkerEmailGateway(SimpleNamespace(
        EMAIL=SimpleNamespace(send=AsyncMock(return_value={"messageId": "local"})),
        PULLWISE_EMAIL_FROM="login@auth.pull-wise.com", PULLWISE_EMAIL_AUTH_ENABLED="1"))


def test_current_verified_mail_once_and_manual_history_stays_available(app):
    app.user("editor", providers=["email"], email="editor@example.com", emailVerifiedAt=NOW - 1)
    row, mail = app.pending(), gateway()
    assert app.notify(row, mail) == "email"
    assert app.notify(row, mail) is None
    mail.binding.send.assert_awaited_once()
    payload = mail.binding.send.await_args.args[0]
    assert payload["to"] == "editor@example.com"
    assert all(payload[field].isascii() for field in ("subject", "text", "html"))
    assert app.inbox()[1] == {"items": [], "hasMore": False}
    saved = app.query("SELECT * FROM expense_recurring_pending")[0]
    assert saved["notification_state"] == "email"
    assert json.loads(saved["template_json"])["purpose"] == "云服务月费"


@pytest.mark.parametrize("identity", [
    {"email": "public-profile@example.com", "providers": ["github"],
        "githubAccessToken": "sealed:local-fixture"},
    {"email": "unverified@example.com", "providers": ["email", "github"],
        "emailVerifiedAt": 0, "githubAccessToken": "sealed:local-fixture"},
    {"billing": {"email": "billing@example.com"}},
])
def test_unverified_and_billing_addresses_use_only_the_responsible_users_inbox(app, identity):
    app.user("editor", **identity)
    row, mail = app.pending(), gateway()
    assert app.notify(row, mail) == "inbox"
    mail.binding.send.assert_not_awaited()
    status, inbox = app.inbox()
    assert status == 200 and inbox["items"][0]["amount"] == "12.34"
    assert inbox["items"][0]["purpose"] == "云服务月费"
    assert inbox["items"][0]["target"] == {"kind": "shared"}
    assert app.inbox("owner")[1]["items"] == app.inbox("other")[1]["items"] == []
    assert "email" not in json.dumps(inbox)


def test_delivery_failure_falls_back_without_retry_or_private_provider_error(app):
    app.user("editor", providers=["email"], email="editor@example.com", emailVerifiedAt=NOW - 1)
    row, mail = app.pending(), gateway()
    mail.binding.send.side_effect = RuntimeError("secret recipient/provider response")
    assert app.notify(row, mail) == "inbox"
    assert app.notify(row, mail) is None
    mail.binding.send.assert_awaited_once()
    assert len(app.inbox()[1]["items"]) == 1
    assert "secret" not in json.dumps(app.inbox()[1])


def test_plan_actor_change_does_not_redirect_original_failure(app):
    row = app.pending()
    app.mutate("UPDATE expense_recurring_rules SET actor_user_id='owner'")
    assert app.notify(row) == "inbox"
    assert len(app.inbox()[1]["items"]) == 1
    assert app.inbox("owner")[1]["items"] == []


@pytest.mark.parametrize("change", [
    "UPDATE workspace_members SET removed_at='removed' WHERE user_id='editor'",
    "UPDATE expense_recurring_rules SET status='canceled'",
    "UPDATE ledger_projects SET deleted_at='2026-10-10T00:00:00Z',status='archived'",
])
def test_lost_membership_canceled_plan_and_removed_target_hide_saved_history(app, change):
    row = app.pending(project=True)
    mail = gateway()
    app.mutate(change)
    assert app.notify(row, mail) is None
    assert app.inbox()[1]["items"] == []
    mail.binding.send.assert_not_awaited()


def test_claim_rechecks_recipient_snapshot_before_contacting_provider(app):
    app.user("editor", providers=["email"], email="editor@example.com", emailVerifiedAt=NOW - 1)
    row, mail = app.pending(), gateway()
    def change_identity():
        app.raw.before_batch = None
        app.user("editor", providers=["email"], email="new@example.com", emailVerifiedAt=NOW)
    app.raw.before_batch = change_identity
    assert app.notify(row, mail) is None
    mail.binding.send.assert_not_awaited()
    assert app.query("SELECT notification_state FROM expense_recurring_pending")[0]["notification_state"] == "pending"


@pytest.mark.parametrize("state", ["pending", "sending", "inbox"])
def test_interrupted_email_claim_remains_in_durable_inbox_and_get_does_not_write(app, state):
    app.pending(state=state)
    before = app.query("SELECT * FROM expense_recurring_pending")
    assert len(app.inbox()[1]["items"]) == 1
    assert app.query("SELECT * FROM expense_recurring_pending") == before
    if state != "pending":
        assert app.notify({"id": "rr_1", "owner_id": "owner"}, gateway()) is None


def test_selected_workspace_cannot_hide_account_inbox_and_resolving_removes_notification(app):
    app.pending()
    assert len(app.inbox(**{"X-Pullwise-Workspace": "other"})[1]["items"]) == 1
    app.mutate("DELETE FROM expense_recurring_pending")
    assert app.inbox()[1]["items"] == []


def test_account_inbox_is_bounded_and_uses_oldest_first(app):
    for rule_number in range(11):
        for period_number in range(10):
            app.pending(rule_id=f"rr_{rule_number:02}", period=f"m:2026-{period_number + 1:02}")
    status, inbox = app.inbox()
    assert status == 200 and len(inbox["items"]) == 100 and inbox["hasMore"] is True
    assert inbox["items"][0]["ruleId"] == "rr_00" and inbox["items"][-1]["ruleId"] == "rr_09"


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer editor"},
    {"Authorization": "Bearer pwk_secret"}, {"X-Pullwise-Api-Key": "pwk_secret"},
])
def test_notifications_require_browser_cookie_and_do_not_accept_keys(app, headers):
    result = asyncio.run(handle_recurring_notification_request(binding=app.raw,
        method="GET", path=RESOURCE, headers=headers, now=NOW))
    assert result == (403, {"error": {"code": "COOKIE_SESSION_REQUIRED"}})


def test_malformed_and_stale_cookies_are_normal_authentication_errors(app):
    assert app.inbox(**{"Cookie": "pw_session=stale"}) == (
        401, {"error": {"code": "UNAUTHENTICATED"}})
    excessive = "; ".join(f"pw_session=session{number}" for number in range(9))
    assert app.inbox(**{"Cookie": excessive}) == (
        400, {"error": {"code": "AMBIGUOUS_AUTH"}})


def test_public_ledger_router_dispatches_durable_inbox(app):
    app.pending()
    status, inbox = asyncio.run(handle_ledger_request(binding=app.raw, gateway=None,
        method="GET", path=RESOURCE, headers={"Cookie": "pw_session=editor"},
        params={}, body=None, now=NOW))
    assert status == 200 and len(inbox["items"]) == 1


@pytest.mark.parametrize("headers,status,count", [
    ({"cookie": "pw_session=editor"}, 200, 1),
    ({"cookie": "pw_session=other"}, 200, 0),
    ({"authorization": "Bearer pwk_secret"}, 403, None),
])
def test_real_worker_application_routes_the_notification_inbox(app, monkeypatch, headers, status, count):
    from test_worker_application_security import application, request
    app.pending()
    worker, _ = application()
    worker.binding = app.raw
    monkeypatch.setitem(worker.fetch.__globals__, "handle_ledger_request", handle_ledger_request)
    monkeypatch.setattr(worker.fetch.__globals__["time"], "time", lambda: NOW)
    incoming = request(RESOURCE, method="GET", headers=headers)
    async def forbidden_body():
        raise AssertionError("Notification GET attempted to read a request body")
    incoming.bytes = forbidden_body
    response = asyncio.run(worker.fetch(incoming))
    assert response.status == status
    assert response.headers["Cache-Control"] == "no-store"
    assert "Cookie" in response.headers["Vary"]
    if count is not None:
        assert len(response.payload["items"]) == count
    else:
        assert response.payload == {"error": {"code": "COOKIE_SESSION_REQUIRED"}}
