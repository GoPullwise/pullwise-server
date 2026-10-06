"""Escaped invalid Unicode must not stop the persistent preview meter."""
import asyncio
import json

import pytest

import test_jev_preview_payload as payload_fixture
import test_worker_application_security as application_fixture
from test_cloudflare_github_identity_http import GitHubStub
from test_jev_preview_admission import MeteredSQLite, preview
from test_ledger_automatic_assistance import expense
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1


def wire_request(client, body, *, path='/api/v1/expenses'):
    # This is the actual HTTP decoding boundary: ASCII JSON escapes can encode
    # a lone surrogate although raw wire bytes are valid and below the cap.
    raw = json.dumps(body, separators=(',', ':')).encode('ascii')
    assert len(raw) < 8192
    body = json.loads(raw)

    async def run():
        ticket = client.journal.begin_product(now=10)
        meter = ProductMeteredD1(MeteredSQLite(client.preview.fixture.store),
            client.journal, ticket, clock=lambda: 11)
        await meter.refresh()
        binding = PlanLimitedD1(meter, policy=client.preview.policy, now=client.preview.now)
        headers = {**client.preview.fixture.headers, 'Idempotency-Key': 'preview-unicode',
            'Content-Length': str(len(raw))}
        if path == '/api-keys':
            async def read_body():
                return raw
            result = await handle_http_request(binding=binding, method='POST', path=path,
                headers=headers, read_body=read_body, now=client.preview.now,
                creem_secret='', configured_products={}, trusted_origins={'https://app.example.test'})
        else:
            result = await handle_ledger_request(binding=binding, gateway=GitHubStub(),
                suggestion_gateway=client.provider, method='POST', path=path, params={},
                body=body, now=client.preview.now, headers=headers)
        client.journal.finish(ticket, now=12)
        return result
    return asyncio.run(run())


@pytest.mark.parametrize('field', ['note', 'purpose', 'unit', 'quantity'])
@pytest.mark.parametrize('surrogate', ['\ud800', '\udfff', '\x00'], ids=['high', 'low', 'nul'])
def test_escaped_lone_surrogate_rejected_before_jev_and_persistent_meter(preview, field, surrogate):
    client = payload_fixture.session(preview)
    status, response = wire_request(client, expense('cat_host', **{field: surrogate}))
    assert client.journal.snapshot()['stopped'] is None
    assert (status, response) == (422, {'error': {'code': 'INVALID_INPUT'}})
    assert client.provider.calls == []
    with preview.fixture.store.connect() as database:
        for table in ('expenses', 'expense_suggestion_budget', 'expense_suggestion_events', 'ledger_plan_usage'):
            assert database.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_escaped_valid_emoji_round_trips_without_stopping_preview(preview):
    client = payload_fixture.session(preview)
    status, response = wire_request(client, expense('cat_host', note='Hosting 😀'))
    assert status == 201 and response['note'] == 'Hosting 😀'
    payload_fixture.assert_accounting(client, writes=1, reservations=1)


@pytest.mark.parametrize('path,body,expected', [
    ('/api/v1/categories', {'name': '\ud800'}, (422, {'error': {'code': 'INVALID_INPUT'}})),
    ('/api/v1/categories', {'name': '\x00Hosting'}, (422, {'error': {'code': 'INVALID_INPUT'}})),
    ('/api-keys', {'name': '\udfff'}, (400, {'error': {'code': 'INVALID_REQUEST'}})),
    ('/api-keys', {'name': '\x00Key'}, (400, {'error': {'code': 'INVALID_REQUEST'}})),
])
def test_invalid_unicode_in_other_write_adapters_cannot_stop_preview(preview, path, body, expected):
    client = payload_fixture.session(preview)
    assert wire_request(client, body, path=path) == expected
    assert client.journal.snapshot()['stopped'] is None
    assert client.provider.calls == []
    with preview.fixture.store.connect() as database:
        assert database.execute('SELECT COUNT(*) FROM expense_categories').fetchone()[0] == 1
        assert database.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0] == 0
        assert database.execute('SELECT COUNT(*) FROM ledger_plan_usage').fetchone()[0] == 0


@pytest.mark.parametrize('path', ['/api/v1/categories', '/api-keys'])
def test_valid_emoji_name_round_trips_in_other_write_adapters(preview, path):
    client = payload_fixture.session(preview)
    status, response = wire_request(client, {'name': 'Hosting 😀'}, path=path)
    assert status == 201 and response['name'] == 'Hosting 😀'
    assert client.journal.snapshot()['stopped'] is None
    assert client.provider.calls == []


@pytest.mark.parametrize('path,code', [('/api/v1/expenses', 'INVALID_INPUT'),
    ('/billing/checkout-sessions', 'INVALID_REQUEST')])
@pytest.mark.parametrize('body', [{'nested': ['\ud800']}, {'\udfff': 'value'},
    {'nested': [{'\ud800': 'value'}]}], ids=['nested-value', 'key', 'nested-key'])
def test_worker_json_ingress_rejects_nested_or_key_surrogates_before_dispatch(path, code, body):
    app, calls = application_fixture.application()
    response = asyncio.run(app.fetch(application_fixture.request(path,
        headers={'authorization': 'Bearer pwk_synthetic'},
        raw=json.dumps(body).encode('ascii'))))
    assert response.status == 422 and response.payload == {'error': {'code': code}}
    assert calls == []


@pytest.mark.parametrize('path,expected', [('/api/v1/expenses', 'ledger'),
    ('/billing/checkout-sessions', 'billing')])
def test_worker_json_ingress_accepts_paired_emoji_in_nested_values_and_keys(path, expected):
    app, calls = application_fixture.application()
    body = {'😀': {'nested': ['汉字', '😀']}}
    response = asyncio.run(app.fetch(application_fixture.request(path,
        headers={'authorization': 'Bearer pwk_synthetic'},
        raw=json.dumps(body).encode('ascii'))))
    assert response.status in {200, 204} and calls == [expected]
