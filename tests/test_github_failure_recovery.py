"""Synthetic GitHub failure evidence; no provider, Worker or remote D1 calls."""
import asyncio
import base64
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

import pytest

from pullwise_server.cloudflare_github_gateway import WorkerGitHubGateway
from pullwise_server.cloudflare_github_identity_http import read_repository_access
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub, call, login, seed
from test_worker_cost_pause import load_entry


@pytest.mark.parametrize('status,headers,body,code', [
    (401, {}, 'synthetic-private-provider-body', 'GITHUB_REAUTHORIZATION_REQUIRED'),
    (403, {}, '{}', 'GITHUB_PERMISSION_DENIED'),
    (404, {}, '{}', 'GITHUB_PERMISSION_DENIED'),
    (403, {'x-ratelimit-remaining': '0'}, '{}', 'GITHUB_RATE_LIMITED'),
    (403, {'retry-after': '30'}, '{}', 'GITHUB_RATE_LIMITED'),
    (403, {}, '{"message":"You have exceeded a secondary rate limit."}', 'GITHUB_RATE_LIMITED'),
    (429, {}, '{}', 'GITHUB_RATE_LIMITED'),
    (500, {}, '{}', 'GITHUB_UNAVAILABLE'),
    (503, {}, '{}', 'GITHUB_UNAVAILABLE'),
    (302, {}, '{}', 'GITHUB_UNAVAILABLE'),
    (200, {}, 'not-json', 'GITHUB_RESPONSE_INVALID'),
    (200, {}, '[]', 'GITHUB_RESPONSE_INVALID'),
])
def test_gateway_preserves_safe_failure_kind_without_provider_body(status, headers, body, code):
    calls = []
    async def text():
        return body
    async def fetch(url, options):
        calls.append((url, options))
        return SimpleNamespace(status=status, ok=200 <= status < 300,
                               headers=SimpleNamespace(get=headers.get), text=text)
    modules = {'js': SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
                                     AbortSignal=SimpleNamespace(timeout=lambda _: None)),
               'pyodide.ffi': SimpleNamespace(to_js=lambda value, **_: value)}
    gateway = WorkerGitHubGateway(SimpleNamespace())
    with patch.dict(sys.modules, modules), pytest.raises(Exception) as failure:
        asyncio.run(gateway.installations('synthetic-private-token'))
    assert getattr(failure.value, 'code', None) == code
    assert 'synthetic-private' not in str(failure.value)
    assert len(calls) == 1 and calls[0][1]['redirect'] == 'manual'


def test_network_exception_is_safe_and_never_retried():
    calls = []
    async def fetch(*args):
        calls.append(args)
        raise RuntimeError('synthetic-private-token in transport error')
    modules = {'js': SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
                                     AbortSignal=SimpleNamespace(timeout=lambda _: None)),
               'pyodide.ffi': SimpleNamespace(to_js=lambda value, **_: value)}
    with patch.dict(sys.modules, modules), pytest.raises(Exception) as failure:
        asyncio.run(WorkerGitHubGateway(SimpleNamespace()).installations('synthetic-private-token'))
    assert getattr(failure.value, 'code', None) == 'GITHUB_UNAVAILABLE'
    assert 'synthetic-private' not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize('code,state', [
    ('GITHUB_REAUTHORIZATION_REQUIRED', 'reauthorization_required'),
    ('GITHUB_PERMISSION_DENIED', 'lost'),
])
def test_rejected_credentials_discard_partial_and_cached_grants(code, state):
    from pullwise_server.cloudflare_github_gateway import GitHubFailure
    class Rejected(GitHubStub):
        async def installations(self, _token):
            return [{'id': 501}, {'id': 502}]
        async def repositories(self, token, installation_id):
            if installation_id == 502:
                raise GitHubFailure(code)
            return await super().repositories(token, installation_id)
    user = {'githubAccessToken': 'sealed:synthetic-access-token',
            'githubRepositoryAccess': {'status': 'authorized', 'repositoryItems': [{'fullName': 'private/cached'}]}}
    result = asyncio.run(read_repository_access(user, Rejected()))
    assert result == {'items': [], 'githubAccess': state}


def test_expired_provider_credentials_leave_local_identity_and_database_untouched(tmp_path):
    from pullwise_server.cloudflare_github_gateway import GitHubFailure
    fixture, _, _ = seed(tmp_path / 'identity.db')
    binding = D1ShapedSQLite(fixture.store)
    _, _, response_headers = login(binding, GitHubStub(), fixture.now)
    cookie = {'Cookie': response_headers['Set-Cookie'].split(';', 1)[0]}
    with fixture.store.connect() as db:
        db.execute('''CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,
            key_prefix TEXT,key_hash TEXT UNIQUE,scopes TEXT,expires_at INTEGER,
            restrictions TEXT,created_at INTEGER,last_used_at INTEGER,revoked_at INTEGER)''')
        before = list(db.iterdump())
    class Rejected(GitHubStub):
        async def installations(self, _token):
            raise GitHubFailure('GITHUB_REAUTHORIZATION_REQUIRED')
    class ReadOnly(D1ShapedSQLite):
        def prepare(self, sql):
            assert sql.lstrip().upper().startswith('SELECT'), sql
            return super().prepare(sql)
    status, payload, _ = call(ReadOnly(fixture.store), Rejected(), fixture.now + 2,
                             'GET', '/api/v1/repositories', headers=cookie)
    assert status == 200 and payload == {'items': [], 'nextCursor': None,
                                       'githubAccess': 'reauthorization_required', 'organizations': []}
    status, session, _ = call(ReadOnly(fixture.store), Rejected(), fixture.now + 2,
                             'GET', '/auth/session', headers=cookie)
    assert status == 200 and session['authenticated'] is True
    with fixture.store.connect() as db:
        assert list(db.iterdump()) == before


def test_identity_unexpected_failure_has_safe_preview_diagnostic(capsys):
    entry = load_entry()
    env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED='1', PULLWISE_MODE='preview')
    async def broken(**_):
        raise RuntimeError('synthetic-secret-cookie-provider-body')
    request = SimpleNamespace(method='GET', url='https://preview.invalid/api/v1/repositories',
                              headers=SimpleNamespace(get=lambda _: None))
    with patch.object(entry, 'handle_ledger_request', broken):
        payload, options = asyncio.run(entry._Application(env, SimpleNamespace()).fetch(request))
    assert options['status'] == 503 and payload['error']['code'] == 'IDENTITY_UNAVAILABLE'
    assert payload['error']['diagnosticSite'].startswith('RuntimeError:broken:')
    assert 'synthetic-secret' not in json.dumps(payload) + capsys.readouterr().out


def test_bad_encryption_configuration_is_distinct_from_unreadable_ciphertext():
    from pullwise_server.cloudflare_github_gateway import _decode_key
    with pytest.raises(Exception) as failure:
        _decode_key('synthetic-private-invalid-key')
    assert getattr(failure.value, 'code', None) == 'GITHUB_CONFIGURATION_ERROR'
    assert 'synthetic-private' not in str(failure.value)


def test_decryption_failure_never_exposes_ciphertext_or_crypto_exception():
    gateway = WorkerGitHubGateway(SimpleNamespace())
    modules = {'js': SimpleNamespace(crypto=SimpleNamespace(subtle=SimpleNamespace(
                   decrypt=AsyncMock(side_effect=RuntimeError('synthetic-private-key')))),
                   Uint8Array=SimpleNamespace(), Object=SimpleNamespace(fromEntries=None)),
               'pyodide.ffi': SimpleNamespace(to_js=lambda value, **_: value)}
    with patch.dict(sys.modules, modules), patch.object(gateway, '_crypto_key', AsyncMock(return_value='key')), \
            patch('pullwise_server.cloudflare_github_gateway._bytes_view', lambda raw: raw), \
            pytest.raises(Exception) as failure:
        asyncio.run(gateway.unseal('gcm1:' + base64.urlsafe_b64encode(b'x' * 40).decode()))
    assert getattr(failure.value, 'code', None) == 'GITHUB_TOKEN_UNREADABLE'
    assert 'synthetic-private' not in str(failure.value)


@pytest.mark.parametrize('code', ['GITHUB_RATE_LIMITED', 'GITHUB_UNAVAILABLE',
                               'GITHUB_TOKEN_UNREADABLE', 'GITHUB_CONFIGURATION_ERROR'])
def test_provider_and_decryption_failures_remain_errors_not_successful_empty(code):
    from pullwise_server.cloudflare_github_gateway import GitHubFailure
    class Broken(GitHubStub):
        async def installations(self, _token):
            raise GitHubFailure(code)
    with pytest.raises(GitHubFailure) as failure:
        asyncio.run(read_repository_access({'githubAccessToken': 'sealed:synthetic-access-token'}, Broken()))
    assert failure.value.code == code


@pytest.mark.parametrize('code,status', [('GITHUB_RATE_LIMITED', 503),
                                       ('GITHUB_TOKEN_UNREADABLE', 503),
                                       ('GITHUB_RESPONSE_INVALID', 502)])
def test_worker_distinguishes_typed_identity_failures_without_raw_diagnostics(code, status, capsys):
    from pullwise_server.cloudflare_github_gateway import GitHubFailure
    entry = load_entry()
    async def broken(**_):
        raise GitHubFailure(code, 403)
    request = SimpleNamespace(method='GET', url='https://preview.invalid/api/v1/repositories',
                              headers=SimpleNamespace(get=lambda _: 'synthetic-private-cookie'))
    env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED='1', PULLWISE_MODE='preview')
    with patch.object(entry, 'handle_ledger_request', broken):
        payload, options = asyncio.run(entry._Application(env, SimpleNamespace()).fetch(request))
    assert options['status'] == status and payload['error']['code'] == code
    assert payload['error']['providerStatus'] == 403
    assert 'synthetic-private' not in json.dumps(payload) + capsys.readouterr().out


def test_production_failure_contains_only_error_code_and_does_not_log(capsys):
    entry = load_entry()
    status, payload = entry._identity_failure(SimpleNamespace(PULLWISE_MODE='production'),
                                             RuntimeError('synthetic-private-secret'))
    assert status == 503 and payload == {'error': {'code': 'IDENTITY_UNAVAILABLE'}}
    assert capsys.readouterr().out == ''


def test_repository_authentication_precedes_provider_access(tmp_path):
    fixture, _, _ = seed(tmp_path / 'unauthenticated.db')
    class NoProvider(GitHubStub):
        async def unseal(self, _token):
            raise AssertionError('Unauthenticated request must never access GitHub')
    status, payload, _ = call(D1ShapedSQLite(fixture.store), NoProvider(), fixture.now,
                              'GET', '/repositories')
    assert status == 401 and payload['error']['code'] == 'UNAUTHENTICATED'
