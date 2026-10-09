"""Local multi-user authorization, transactional fences and shared quota proofs."""
import asyncio
import json
from pathlib import Path

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed, seed_auth
from pullwise_server.cloudflare_ledger_auth import ROLE_SCOPES, ledger_principal
from pullwise_server.cloudflare_ledger_api import _write_guard, handle_ledger_request
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_principal import PrincipalAuthError
from pullwise_server.cloudflare_api_key_read import list_api_keys
from pullwise_server.cloudflare_api_key_write import create_api_key, revoke_api_key
from pullwise_server.cloudflare_state_records import encode_record, record_name


@pytest.fixture
def workspace_db(tmp_path):
    fixture, _, _ = seed(tmp_path / "workspace.sqlite")
    seed_auth(fixture)
    with fixture.store.connect() as db:
        sessions = {"session-local": {"userId": "owner", "expiresAt": fixture.now + 3600}}
        for index, role in enumerate(("admin", "editor", "viewer"), 1):
            user = {"id": role, "name": role, "githubId": str(index), "createdAt": fixture.now}
            db.execute("INSERT OR REPLACE INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("users", role), encode_record("users", role, user), fixture.now))
            sessions[role] = {"userId": role, "expiresAt": fixture.now + 3600}
            db.execute("""INSERT INTO workspace_members VALUES(?,?,?,1,?,?,NULL,?)""",
                       ("owner", role, role, "2026-10-06", "2026-10-06", "owner"))
        for identifier, session in sessions.items():
            db.execute("INSERT OR REPLACE INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                (record_name("sessions", identifier), encode_record("sessions", identifier, session), fixture.now))
    return fixture, D1ShapedSQLite(fixture.store)


def headers(actor, workspace="owner"):
    return {"Cookie": "pw_session=" + ("session-local" if actor == "owner" else actor),
            "X-Pullwise-Workspace": workspace}


async def principal(binding, fixture, actor, scope):
    proof = {}
    user, restrictions, commands, validate = await ledger_principal(
        binding=binding, headers=headers(actor), scope=scope, now=fixture.now, proof=proof)
    result = await binding.batch(commands)
    validate([part.results for part in result])
    return user, restrictions, proof


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
@pytest.mark.parametrize("scope", ["profile:read", "projects:read", "categories:read", "expenses:read", "reports:read", "projects:write", "categories:write", "expenses:write", "suggestions:use"])
def test_role_matrix_is_enforced_before_mutations(workspace_db, role, scope):
    fixture, binding = workspace_db
    if scope in ROLE_SCOPES[role]:
        user, _, proof = asyncio.run(principal(binding, fixture, role, scope))
        assert user["id"] == "owner"
        assert user["_actor"]["id"] == role
        assert proof["actor_user_id"] == role
        assert user["_workspace"]["role"] == role
    else:
        with pytest.raises(PrincipalAuthError) as error:
            asyncio.run(principal(binding, fixture, role, scope))
        assert (error.value.status, error.value.code) == (403, "ROLE_FORBIDDEN")


def test_default_workspace_is_personal_and_unknown_ledger_is_hidden(workspace_db):
    fixture, binding = workspace_db
    async def run():
        user, _, commands, validate = await ledger_principal(binding=binding,
            headers={"Cookie": "pw_session=editor"}, scope="expenses:read", now=fixture.now)
        validate([part.results for part in await binding.batch(commands)])
        assert user["id"] == "editor" and user["_workspace"]["role"] == "owner"
        with pytest.raises(PrincipalAuthError) as error:
            await ledger_principal(binding=binding, headers=headers("editor", "unrelated"),
                                   scope="expenses:read", now=fixture.now)
        assert (error.value.status, error.value.code) == (404, "WORKSPACE_NOT_FOUND")
    asyncio.run(run())


def test_read_batch_rechecks_membership_revision(workspace_db):
    fixture, binding = workspace_db
    async def run():
        _, _, commands, validate = await ledger_principal(binding=binding,
            headers=headers("editor"), scope="expenses:read", now=fixture.now)
        with fixture.store.connect() as db:
            db.execute("UPDATE workspace_members SET revision=2 WHERE workspace_id='owner' AND user_id='editor'")
        with pytest.raises(PrincipalAuthError) as error:
            validate([part.results for part in await binding.batch(commands)])
        assert error.value.code == "AUTHORIZATION_CHANGED"
    asyncio.run(run())


def test_shared_writes_charge_owner_plan_and_audit_actual_member(workspace_db):
    fixture, raw = workspace_db
    binding = PlanLimitedD1(raw, now=fixture.now)
    async def run():
        status, category = await handle_ledger_request(binding=binding, gateway=None,
            method="POST", path="/api/v1/categories", headers=headers("admin"), params={},
            body={"name": "Team"}, now=fixture.now)
        assert status == 201
        body = {"target": {"kind": "shared"}, "categoryId": category["id"],
                "occurredOn": "2026-10-06", "amount": "1.00", "currency": "USD", "purpose": "Shared"}
        results = []
        for actor in ("owner", "admin", "editor"):
            result = await handle_ledger_request(binding=binding, gateway=None,
                method="POST", path="/api/v1/expenses", headers={**headers(actor), "Idempotency-Key": "same-client-key"},
                params={}, body=body, now=fixture.now)
            assert result[0] == 201
            results.append(result[1]["id"])
        assert len(set(results)) == 3
        replay = await handle_ledger_request(binding=binding, gateway=None,
            method="POST", path="/api/v1/expenses", headers={**headers("editor"), "Idempotency-Key": "same-client-key"},
            params={}, body=body, now=fixture.now)
        assert replay[1]["id"] == results[-1]
        status, me = await read_ledger_me(binding=binding, headers=headers("viewer"), now=fixture.now)
        assert status == 200 and me["id"] == "viewer" and "expenses:write" not in me["scopes"]
        assert me["entitlements"]["plan"] == "pro"
    asyncio.run(run())
    with fixture.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT owner_id,writes,records FROM ledger_plan_usage")] == [("owner", 4, 3)]
        assert {row[0] for row in db.execute("SELECT actor_id FROM expense_events")} == {"owner", "admin", "editor"}


@pytest.mark.parametrize("changed", ["member", "owner", "actor", "session"])
def test_write_batch_cannot_use_stale_authority(workspace_db, changed):
    fixture, binding = workspace_db
    _, _, proof = asyncio.run(principal(binding, fixture, "admin", "categories:write"))
    with fixture.store.connect() as db:
        if changed == "member":
            db.execute("UPDATE workspace_members SET removed_at='now',revision=2 WHERE workspace_id='owner' AND user_id='admin'")
        elif changed == "session":
            db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "admin"),))
        else:
            identifier = "owner" if changed == "owner" else "admin"
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                (record_name("users", identifier),)).fetchone()[0])
            user["name"] = "changed"
            db.execute("UPDATE app_state SET payload=? WHERE name=?",
                (encode_record("users", identifier, user), record_name("users", identifier)))
    commands = [_write_guard(binding, proof, "owner", fixture.now),
                binding.prepare("INSERT INTO expense_categories(id,owner_id,name,color,archived_at,revision,created_at,updated_at) "
                                "VALUES('cat_guard','owner','Guard',NULL,NULL,1,'now','now')"),
                binding.prepare("DELETE FROM d1_command_guard")]
    with pytest.raises(Exception):
        asyncio.run(binding.batch(commands))
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expense_categories WHERE id='cat_guard'").fetchone()[0] == 0


def test_team_key_is_bound_to_issuer_workspace_current_role_and_generation(workspace_db):
    fixture, raw = workspace_db
    binding = PlanLimitedD1(raw, now=fixture.now)
    status, key = asyncio.run(create_api_key(binding=binding, headers=headers("editor"),
        body={"scopes": ["profile:read", "expenses:read", "expenses:write"],
              "restrictions": {"workspaceId": "owner", "shared": True}}, now=fixture.now))
    assert status == 201 and key["userId"] == "editor"
    assert key["restrictions"]["workspaceMemberRevision"] == 1
    token_headers = {"Authorization": "Bearer " + key["key"]}
    async def authorize():
        user, _, commands, validate = await ledger_principal(binding=binding,
            headers=token_headers, scope="expenses:write", now=fixture.now)
        validate([part.results for part in await binding.batch(commands)])
        return user
    assert asyncio.run(authorize())["id"] == "owner"
    token_headers["X-Pullwise-Workspace"] = "editor"
    with pytest.raises(PrincipalAuthError) as denied:
        asyncio.run(authorize())
    assert denied.value.code == "WORKSPACE_FORBIDDEN"
    token_headers.pop("X-Pullwise-Workspace")
    with fixture.store.connect() as db:
        db.execute("UPDATE workspace_members SET role='viewer',revision=2 WHERE workspace_id='owner' AND user_id='editor'")
    with pytest.raises(PrincipalAuthError) as denied:
        asyncio.run(authorize())
    assert denied.value.code == "WORKSPACE_MEMBERSHIP_CHANGED"
    with fixture.store.connect() as db:
        db.execute("UPDATE workspace_members SET role='editor',revision=3 WHERE workspace_id='owner' AND user_id='editor'")
    with pytest.raises(PrincipalAuthError) as denied:
        asyncio.run(authorize())
    assert denied.value.code == "WORKSPACE_MEMBERSHIP_CHANGED"
    with fixture.store.connect() as db:
        db.execute("UPDATE workspace_members SET removed_at='now',revision=4 WHERE workspace_id='owner' AND user_id='editor'")
        assert [tuple(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")] == [("owner", 1)]
    assert asyncio.run(revoke_api_key(binding=binding, key_id=key["id"],
        headers={"Cookie": "pw_session=editor"}, now=fixture.now))[0] == 200


@pytest.mark.parametrize("role,scope", [("viewer", "expenses:write"), ("editor", "projects:write"), ("editor", "categories:write")])
def test_key_creation_cannot_exceed_issuing_members_role(workspace_db, role, scope):
    fixture, binding = workspace_db
    status, payload = asyncio.run(create_api_key(binding=binding, headers=headers(role),
        body={"scopes": [scope], "restrictions": {"workspaceId": "owner", "workspaceMemberRevision": 1}}, now=fixture.now))
    assert (status, payload["error"]["code"]) == (403, "ROLE_FORBIDDEN")
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=?", (role,)).fetchone()[0] == 0


def test_key_lists_are_partitioned_and_personal_defaults_do_not_grant_shared_ledgers(workspace_db):
    fixture, binding = workspace_db
    status, personal = asyncio.run(create_api_key(binding=binding, headers={"Cookie": "pw_session=editor"},
        body={"scopes": ["expenses:read"]}, now=fixture.now))
    assert status == 201
    status, team = asyncio.run(create_api_key(binding=binding, headers=headers("editor"),
        body={"scopes": ["expenses:read"], "restrictions": {"workspaceId": "owner"}}, now=fixture.now))
    assert status == 201
    for selected, wanted in (("owner", team["id"]), ("editor", personal["id"])):
        status, page = asyncio.run(list_api_keys(binding=binding, headers={"Cookie": "pw_session=editor"},
            workspace_id=selected, now=fixture.now))
        assert status == 200 and [row["id"] for row in page["items"]] == [wanted]
    with pytest.raises(PrincipalAuthError) as denied:
        asyncio.run(ledger_principal(binding=binding,
            headers={"Authorization": "Bearer " + personal["key"], "X-Pullwise-Workspace": "owner"},
            scope="expenses:read", now=fixture.now))
    assert denied.value.code == "WORKSPACE_FORBIDDEN"
