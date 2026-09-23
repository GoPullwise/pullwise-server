"""Candidate Billing read keeps payment history and product usage in one snapshot."""
import asyncio
import json

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.entitlements import product_usage_payload
from pullwise_server.product_billing_projection import billing_account_dto
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_product_reads import TOKEN, _seed_auth
from test_cloudflare_server_mapping import seed


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
