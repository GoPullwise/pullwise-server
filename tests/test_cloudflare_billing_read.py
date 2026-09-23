"""Candidate Billing read keeps payment history and product usage in one snapshot."""
import asyncio
import json

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.entitlements import product_usage_payload
from pullwise_server.product_billing_projection import billing_account_dto
from pullwise_server.product_entitlement_rules import PLAN_ENTITLEMENTS
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_product_reads import TOKEN, _seed_auth
from test_cloudflare_server_mapping import seed


def seed_public_catalog(fixture):
    payload = {"provider": "disabled", "enabled": False, "currency": "USD",
        "plans": [{"id": plan, "name": plan.title(),
                   "entitlements": dict(PLAN_ENTITLEMENTS[plan]),
                   "prices": {"month": {"amount": None, "configured": False}}}
                  for plan in ("free", "pro", "max")]}
    with fixture.store._immediate() as db:
        db.execute("""CREATE TABLE billing_public_catalog(
            id INTEGER PRIMARY KEY CHECK(id=1),payload_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,source_revision INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)""")
        db.execute("INSERT INTO billing_public_catalog VALUES(1,?,?,1,?)",
            (json.dumps(payload), fixture.now + 3600, fixture.now))
    return payload


def get(binding, headers, now):
    async def no_body():
        raise AssertionError("GET must not read body")
    return asyncio.run(handle_http_request(method="GET", path="/billing",
        headers=headers, read_body=no_body, binding=binding,
        creem_secret="", configured_products={}, now=now))


def test_billing_cookie_read_matches_product_and_payment_projection(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    status, payload = get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 200 and payload["page"]["id"] == "billing"
    user = json.loads(frozen)
    expected = billing_account_dto(user,
        product_usage_payload(fixture.store, user, timestamp=fixture.now), [])
    assert payload["account"] == expected
    assert binding.batch_count == 1
    assert get(binding, {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 401


def test_billing_cookie_revocation_before_read_batch_hides_account(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_snapshot
    status, payload = get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_public_plan_uses_fresh_saved_catalog_and_cookie_account_snapshot(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    catalog = seed_public_catalog(fixture)
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("GET must not read body")

    async def read(headers):
        return await handle_http_request(method="GET", path="/billing/plan",
            headers=headers, read_body=no_body, binding=binding,
            creem_secret="", configured_products={}, now=fixture.now)

    status, public = asyncio.run(read({}))
    assert status == 200 and public["provider"] == catalog["provider"]
    assert "account" not in public
    status, personal = asyncio.run(read({"Cookie": "pw_session=session-local"}))
    assert status == 200 and personal["account"]["plan"] == "pro"
    assert personal["plans"] == catalog["plans"]
    assert binding.batch_count == 2


def test_expired_public_plan_refuses_stale_provider_price_facts(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    seed_public_catalog(fixture)
    with fixture.store._immediate() as db:
        db.execute("UPDATE billing_public_catalog SET expires_at=?", (fixture.now - 1,))
    async def no_body():
        raise AssertionError("GET must not read body")
    status, payload = asyncio.run(handle_http_request(method="GET",
        path="/billing/plan", headers={}, read_body=no_body,
        binding=D1ShapedSQLite(fixture.store), creem_secret="",
        configured_products={}, now=fixture.now))
    assert status == 503 and payload["error"]["code"] == "BILLING_CATALOG_UNAVAILABLE"


def test_public_plan_drops_account_if_cookie_revoked_before_combined_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    seed_public_catalog(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_snapshot

    async def no_body():
        raise AssertionError("GET must not read body")

    status, payload = asyncio.run(handle_http_request(method="GET",
        path="/billing/plan", headers={"Cookie": "pw_session=session-local"},
        read_body=no_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now))
    assert status == 200 and "account" not in payload
    assert payload["plans"][1]["entitlements"] == PLAN_ENTITLEMENTS["pro"]
    assert binding.batch_count == 1
