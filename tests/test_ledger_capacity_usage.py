"""Billing and REST capacity share the owner's current guarded quota facts."""
import asyncio
import json
from contextlib import closing

import pytest

from ledger_d1_fixture import D1ShapedSQLite, TOKEN, seed, seed_auth
from pullwise_server.cloudflare_billing_catalog import read_public_plan
from pullwise_server.cloudflare_billing_read import read_billing
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import default_policy
from test_cloudflare_billing_read import seed_public_catalog
from test_ledger_plan_limits import account_plan, write


@pytest.fixture
def capacity(tmp_path):
    fixture, _, frozen = seed(tmp_path / "capacity.db")
    seed_auth(fixture, restrictions='{"shared":false}')
    seed_public_catalog(fixture)
    with fixture.store._immediate() as db:
        for identifier, status, deleted in (("active", "active", None),
                                             ("archived", "archived", None),
                                             ("removed", "archived", "2026-10-09T00:00:00Z")):
            db.execute("""INSERT INTO ledger_projects(id,owner_id,name,description,status,
                revision,created_at,updated_at,deleted_at) VALUES(?, 'owner',?,'',?,1,'created','updated',?)""",
                ("prj_" + identifier, identifier, status, deleted))
        db.execute("INSERT INTO expense_categories VALUES('cat_usage','owner','Hosting',NULL,NULL,1,'created','updated')")
        for identifier, kind, project, deleted in (("shared", "shared", None, None),
                                                    ("deleted", "shared", None, "deleted"),
                                                    ("project", "project", "prj_active", None)):
            db.execute("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
                occurred_on,amount_minor,currency,purpose,created_at,updated_at,deleted_at)
                VALUES(?, 'owner',?,?,'cat_usage','2026-10-09',100,'USD','Hosting','created','updated',?)""",
                ("exp_" + identifier, kind, project, deleted))
    return fixture, frozen


COOKIE = {"Cookie": "pw_session=session-local"}


@pytest.mark.parametrize("reader", [read_billing, read_public_plan, read_ledger_me])
def test_capacity_read_counts_active_expenses_without_initializing_usage(capacity, reader):
    fixture, _ = capacity
    binding = D1ShapedSQLite(fixture.store)
    status, payload = asyncio.run(reader(binding=binding, headers=COOKIE, now=fixture.now))
    assert status == 200
    assert payload["ledgerUsage"] == {"workspaceId": "owner",
        "projects": {"used": 3, "limit": 20, "remaining": 17},
        "expenseRecords": {"used": 2, "limit": 20000, "remaining": 19998}}
    assert binding.batch_count == 1
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0


def test_me_bearer_capacity_is_same_owner_projection_and_scope_guard(capacity):
    fixture, _ = capacity
    binding = D1ShapedSQLite(fixture.store)
    bearer = {"Authorization": "Bearer " + TOKEN}
    status, payload = asyncio.run(read_ledger_me(binding=binding, headers=bearer, now=fixture.now))
    assert status == 200 and payload["ledgerUsage"]["projects"]["used"] == 3
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET scopes='[\"expenses:read\"]'")
    assert asyncio.run(read_ledger_me(binding=binding, headers=bearer, now=fixture.now))[0] == 403


@pytest.mark.parametrize("reader", [read_billing, read_public_plan, read_ledger_me])
def test_capacity_ignores_legacy_record_counter_and_keeps_configured_limits(capacity, reader):
    fixture, frozen = capacity
    policy = default_policy()
    policy["pro"].update(projects=40, records=80)
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    write(binding, frozen, fixture, 1)
    with fixture.store._immediate() as db:
        db.execute("UPDATE ledger_plan_usage SET projects=30,records=90 WHERE owner_id='owner'")
    status, payload = asyncio.run(reader(binding=binding, headers=COOKIE, now=fixture.now))
    assert status == 200
    assert payload["ledgerUsage"] == {"workspaceId": "owner",
        "projects": {"used": 30, "limit": 40, "remaining": 10},
        "expenseRecords": {"used": 2, "limit": 80, "remaining": 78}}
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT projects,records FROM ledger_plan_usage").fetchone()) == (30, 90)


def test_personal_billing_stays_personal_while_me_uses_selected_workspace(capacity):
    fixture, _ = capacity
    with fixture.store._immediate() as db:
        member = {"id": "member", "billing": {"plan": "free", "status": "active"}}
        db.execute("INSERT INTO app_state VALUES(?,?,?)", (
            record_name("users", "member"), encode_record("users", "member", member), fixture.now))
        db.execute("INSERT INTO app_state VALUES(?,?,?)", (
            record_name("sessions", "member"), encode_record("sessions", "member",
                {"userId": "member", "expiresAt": fixture.now + 1000}), fixture.now))
        db.execute("INSERT INTO workspace_members VALUES('owner','member','viewer',1,'joined','updated',NULL,'owner')")
    headers = {"Cookie": "pw_session=member", "X-Pullwise-Workspace": "owner"}
    binding = D1ShapedSQLite(fixture.store)
    status, personal = asyncio.run(read_public_plan(binding=binding, headers=headers, now=fixture.now))
    assert status == 200 and personal["ledgerUsage"]["workspaceId"] == "member"
    assert personal["ledgerUsage"]["expenseRecords"] == {"used": 0, "limit": 100, "remaining": 100}
    status, selected = asyncio.run(read_ledger_me(binding=binding, headers=headers, now=fixture.now))
    assert status == 200 and selected["ledgerUsage"]["workspaceId"] == "owner"
    assert selected["ledgerUsage"]["expenseRecords"] == {"used": 2, "limit": 20000, "remaining": 19998}


@pytest.mark.parametrize("reader", [read_billing, read_public_plan, read_ledger_me])
def test_revoked_auth_snapshot_never_discloses_capacity(capacity, reader):
    fixture, _ = capacity
    binding = D1ShapedSQLite(fixture.store)
    def revoke():
        with fixture.store._immediate() as db:
            db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "session-local"),))
    binding.before_batch = revoke
    status, payload = asyncio.run(reader(binding=binding, headers=COOKIE, now=fixture.now))
    assert status == (200 if reader is read_public_plan else 401)
    assert "ledgerUsage" not in payload and "account" not in payload


def test_public_catalog_never_inherits_saved_personal_usage(capacity):
    fixture, _ = capacity
    with fixture.store._immediate() as db:
        catalog = json.loads(db.execute("SELECT payload_json FROM billing_public_catalog").fetchone()[0])
        catalog["ledgerUsage"] = {"workspaceId": "stale-private-account"}
        db.execute("UPDATE billing_public_catalog SET payload_json=?", (json.dumps(catalog),))
    for headers in ({}, {"Authorization": "Bearer " + TOKEN}):
        status, public = asyncio.run(read_public_plan(binding=D1ShapedSQLite(fixture.store),
            headers=headers, now=fixture.now))
        assert status == 200 and "ledgerUsage" not in public


def test_expired_paid_plan_reports_free_limits_without_truncating_existing_capacity(capacity):
    fixture, _ = capacity
    account_plan(fixture, "max", currentPeriodEnd=fixture.now - 1)
    status, payload = asyncio.run(read_billing(binding=D1ShapedSQLite(fixture.store),
        headers=COOKIE, now=fixture.now))
    assert status == 200
    assert payload["ledgerUsage"]["projects"] == {"used": 3, "limit": 3, "remaining": 0}
    assert payload["ledgerUsage"]["expenseRecords"] == {"used": 2, "limit": 100, "remaining": 98}
