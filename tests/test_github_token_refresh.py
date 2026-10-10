"""Synthetic credential renewal and rotation fences; no provider/network calls.

The sealing stub only marks values. AES-GCM and Cloudflare native behavior have
their own runtime checks; these tests prove validation, publication and races.
"""
import asyncio
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import AsyncMock, patch

import pytest

from pullwise_server.cloudflare_github_gateway import (
    GitHubFailure, GitHubTokenBundle, WorkerGitHubGateway, token_bundle,
)
from pullwise_server.cloudflare_github_identity_http import handle_identity_request
from pullwise_server.cloudflare_state_records import encode_record, record_name
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub, call, login, seed


ACCESS = "synthetic-access-token"
REFRESH = "synthetic-refresh-token"
ROTATED_ACCESS = "synthetic-rotated-access-token"
ROTATED_REFRESH = "synthetic-rotated-refresh-token"
ACCESS_SECONDS = 28800
REFRESH_SECONDS = 15552000
USER_ID = "usr_github_77"
ORIGIN = "https://app.example.test"


def provider_pair(**changes):
    return {"access_token": ACCESS, "token_type": "bearer",
            "expires_in": ACCESS_SECONDS, "refresh_token": REFRESH,
            "refresh_token_expires_in": REFRESH_SECONDS, **changes}


class ExpiringGitHub(GitHubStub):
    def __init__(self):
        self.refresh_calls = []
        self.repository_calls = []
        self.refresh_wait = None
        self.refresh_failure = None
        self.configuration_failure = None
        self.refresh_result = GitHubTokenBundle(
            ROTATED_ACCESS, ACCESS_SECONDS, ROTATED_REFRESH, REFRESH_SECONDS)

    async def exchange(self, code, redirect_uri, verifier):
        await super().exchange(code, redirect_uri, verifier)
        return GitHubTokenBundle(ACCESS, ACCESS_SECONDS, REFRESH, REFRESH_SECONDS)

    async def validate_refresh_configuration(self):
        if self.configuration_failure is not None:
            raise self.configuration_failure

    async def refresh(self, token):
        self.refresh_calls.append(token)
        assert token == REFRESH
        if self.refresh_wait is not None:
            await self.refresh_wait()
        if self.refresh_failure is not None:
            raise self.refresh_failure
        return self.refresh_result

    async def installations(self, token):
        self.repository_calls.append(("installations", token))
        assert token in {ACCESS, ROTATED_ACCESS}
        return [{"id": 501, "account": {"id": 88, "login": "synthetic-org", "type": "Organization"}}]

    async def repositories(self, token, installation_id):
        self.repository_calls.append(("repositories", token))
        assert token in {ACCESS, ROTATED_ACCESS} and installation_id == 501
        return [{"id": 202, "full_name": "synthetic-org/project"}]


def stored_user(store):
    with store.connect() as db:
        return json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                                    (record_name("users", USER_ID),)).fetchone()[0])


def replace_user(store, user, now):
    with store.connect() as db:
        db.execute("UPDATE app_state SET payload=?,updated_at=? WHERE name=?",
                   (encode_record("users", USER_ID, user), now, record_name("users", USER_ID)))


def database_snapshot(store):
    with store.connect() as db:
        return list(db.iterdump())


def setup_login(tmp_path, gateway=None):
    fixture, _, _ = seed(tmp_path / "refresh.db")
    gateway = gateway or ExpiringGitHub()
    binding = D1ShapedSQLite(fixture.store)
    status, payload, headers = login(binding, gateway, fixture.now)
    assert status == 302
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return fixture, binding, gateway, cookie, fixture.now + 1 + ACCESS_SECONDS


def refresh_request(binding, gateway, now, cookie, headers=None):
    return call(binding, gateway, now, "POST", "/integrations/github/refresh",
                headers={"Cookie": cookie, "Origin": ORIGIN, **(headers or {})})


async def async_refresh_request(binding, gateway, now, cookie):
    return await handle_identity_request(
        binding=binding, gateway=gateway, now=now, method="POST",
        path="/integrations/github/refresh", params={},
        headers={"Cookie": cookie, "Origin": ORIGIN}, app_url=ORIGIN,
        callback_url=ORIGIN + "/api/auth/github/callback", cookie_same_site="None",
        trusted_origins={ORIGIN})


def assert_no_credentials(payload):
    encoded = json.dumps(payload)
    for value in (ACCESS, REFRESH, ROTATED_ACCESS, ROTATED_REFRESH,
                  "githubAccessToken", "githubRefreshToken", "githubTokenRefresh"):
        assert value not in encoded


def test_provider_bundle_accepts_complete_expiring_pair_and_legacy_access_token():
    assert token_bundle(provider_pair()) == GitHubTokenBundle(
        ACCESS, ACCESS_SECONDS, REFRESH, REFRESH_SECONDS)
    assert token_bundle({"access_token": ACCESS, "token_type": "Bearer"}) == GitHubTokenBundle(ACCESS)
    assert ACCESS not in repr(token_bundle(provider_pair()))
    assert REFRESH not in repr(token_bundle(provider_pair()))


@pytest.mark.parametrize("field,value", [
    ("access_token", None), ("access_token", ""), ("access_token", "white space"),
    ("access_token", "token\n"), ("access_token", "令牌"), ("access_token", "x" * 4097),
    ("refresh_token", None), ("refresh_token", ""), ("refresh_token", "token\t"),
    ("refresh_token", "令牌"), ("refresh_token", "x" * 4097),
    ("token_type", "Basic"), ("token_type", None),
    ("expires_in", True), ("expires_in", 28800.0), ("expires_in", "28800"),
    ("expires_in", 0), ("expires_in", -1), ("expires_in", 366 * 86400 + 1),
    ("refresh_token_expires_in", True), ("refresh_token_expires_in", 15552000.0),
    ("refresh_token_expires_in", 0), ("refresh_token_expires_in", 366 * 86400 + 1),
])
def test_provider_bundle_rejects_invalid_tokens_and_lifetimes_without_exposing_values(field, value):
    with pytest.raises(GitHubFailure) as failure:
        token_bundle(provider_pair(**{field: value}))
    assert failure.value.code == "GITHUB_RESPONSE_INVALID"
    assert ACCESS not in str(failure.value) and REFRESH not in repr(failure.value)


@pytest.mark.parametrize("missing", [
    ("expires_in",), ("refresh_token",), ("refresh_token_expires_in",),
    ("expires_in", "refresh_token"), ("expires_in", "refresh_token_expires_in"),
    ("refresh_token", "refresh_token_expires_in"),
])
def test_provider_bundle_rejects_incomplete_expiry_pair(missing):
    raw = {key: value for key, value in provider_pair().items() if key not in missing}
    with pytest.raises(GitHubFailure) as failure:
        token_bundle(raw)
    assert failure.value.code == "GITHUB_RESPONSE_INVALID"


def test_gateway_exchange_and_refresh_send_distinct_grants_and_validate_actual_response():
    gateway = WorkerGitHubGateway(SimpleNamespace(
        PULLWISE_GITHUB_CLIENT_ID="synthetic-client", PULLWISE_GITHUB_CLIENT_SECRET="synthetic-secret"))
    with patch.object(gateway, "_json", AsyncMock(return_value=provider_pair())) as provider:
        exchange = asyncio.run(gateway.exchange("synthetic-code", ORIGIN + "/callback", "synthetic-verifier"))
        refreshed = asyncio.run(gateway.refresh(REFRESH))
    assert exchange == refreshed == token_bundle(provider_pair())
    first, second = provider.call_args_list
    assert first.args == second.args == ("https://github.com/login/oauth/access_token",)
    assert first.kwargs["method"] == second.kwargs["method"] == "POST"
    assert parse_qs(first.kwargs["body"]) == {
        "client_id": ["synthetic-client"], "client_secret": ["synthetic-secret"],
        "code": ["synthetic-code"], "redirect_uri": [ORIGIN + "/callback"],
        "code_verifier": ["synthetic-verifier"]}
    assert parse_qs(second.kwargs["body"]) == {
        "client_id": ["synthetic-client"], "client_secret": ["synthetic-secret"],
        "grant_type": ["refresh_token"], "refresh_token": [REFRESH]}


def test_actual_gateway_fetch_requests_json_for_oauth_and_vendor_json_for_rest_without_logging_credentials(capsys):
    requests = []
    async def fetch(url, options):
        requests.append((url, options))
        async def response_text():
            return json.dumps(provider_pair() if url == "https://github.com/login/oauth/access_token"
                              else {"installations": []})
        return SimpleNamespace(status=200, ok=True,
                               headers=SimpleNamespace(get=lambda _: None), text=response_text)
    modules = {
        "js": SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
                              AbortSignal=SimpleNamespace(timeout=lambda _: None)),
        "pyodide.ffi": SimpleNamespace(to_js=lambda value, **_: value),
    }
    gateway = WorkerGitHubGateway(SimpleNamespace(
        PULLWISE_GITHUB_CLIENT_ID="synthetic-client", PULLWISE_GITHUB_CLIENT_SECRET="synthetic-secret"))
    with patch.dict(sys.modules, modules):
        bundle = asyncio.run(gateway.refresh(REFRESH))
        assert bundle == token_bundle(provider_pair())
        assert asyncio.run(gateway.installations(ACCESS)) == []
    oauth, rest = requests
    assert oauth[1]["headers"]["Accept"] == "application/json"
    assert oauth[1]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "Authorization" not in oauth[1]["headers"]
    assert parse_qs(oauth[1]["body"])["grant_type"] == ["refresh_token"]
    assert rest[1]["headers"]["Accept"] == "application/vnd.github+json"
    assert rest[1]["headers"]["Authorization"] == "Bearer " + ACCESS
    assert oauth[1]["redirect"] == rest[1]["redirect"] == "manual"
    captured = capsys.readouterr()
    for token in (ACCESS, REFRESH, "synthetic-secret"):
        assert token not in captured.out + captured.err + repr(bundle)


@pytest.mark.parametrize("error,code", [
    ("bad_refresh_token", "GITHUB_REAUTHORIZATION_REQUIRED"),
    ("invalid_grant", "GITHUB_REAUTHORIZATION_REQUIRED"),
    ("expired_token", "GITHUB_REAUTHORIZATION_REQUIRED"),
    ("incorrect_client_credentials", "GITHUB_CONFIGURATION_ERROR"),
])
def test_actual_gateway_refresh_known_rejection_is_safe(error, code):
    gateway = WorkerGitHubGateway(SimpleNamespace(
        PULLWISE_GITHUB_CLIENT_ID="synthetic-client", PULLWISE_GITHUB_CLIENT_SECRET="synthetic-secret"))
    with patch.object(gateway, "_json", AsyncMock(return_value={
            "error": error, "error_description": REFRESH, "access_token": ACCESS})) as provider, \
            pytest.raises(GitHubFailure) as failure:
        asyncio.run(gateway.refresh(REFRESH))
    assert failure.value.code == code and failure.value.request_rejected is True
    assert provider.await_count == 1
    assert REFRESH not in str(failure.value) and ACCESS not in repr(failure.value)


def test_actual_gateway_refresh_rejects_nonrotating_success_and_bad_local_configuration():
    gateway = WorkerGitHubGateway(SimpleNamespace(
        PULLWISE_GITHUB_CLIENT_ID="synthetic-client", PULLWISE_GITHUB_CLIENT_SECRET="synthetic-secret"))
    with patch.object(gateway, "_json", AsyncMock(return_value={
            "access_token": ACCESS, "token_type": "bearer"})) as provider, \
            pytest.raises(GitHubFailure) as failure:
        asyncio.run(gateway.refresh(REFRESH))
    assert failure.value.code == "GITHUB_RESPONSE_INVALID" and not failure.value.request_rejected
    assert provider.await_count == 1
    with patch.object(gateway, "_json", AsyncMock()) as provider, pytest.raises(GitHubFailure) as failure:
        asyncio.run(gateway.refresh("invalid token"))
    assert failure.value.code == "GITHUB_CONFIGURATION_ERROR"
    assert provider.await_count == 0


@pytest.mark.parametrize("error", [None, "", True, 1, [], {}, ["bad_refresh_token"], {"code": "bad_refresh_token"}])
def test_actual_gateway_malformed_error_is_an_unknown_response_not_a_rejected_rotation(error):
    gateway = WorkerGitHubGateway(SimpleNamespace(
        PULLWISE_GITHUB_CLIENT_ID="synthetic-client", PULLWISE_GITHUB_CLIENT_SECRET="synthetic-secret"))
    with patch.object(gateway, "_json", AsyncMock(return_value=provider_pair(error=error))) as provider, \
            pytest.raises(GitHubFailure) as failure:
        asyncio.run(gateway.refresh(REFRESH))
    assert failure.value.code == "GITHUB_RESPONSE_INVALID"
    assert not failure.value.request_rejected and provider.await_count == 1
    assert REFRESH not in str(failure.value) and ACCESS not in repr(failure.value)


def test_callback_stores_both_sealed_credentials_with_provider_expiry_and_no_session_dto_leak(tmp_path):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    user = stored_user(fixture.store)
    assert user["githubAccessToken"] == "sealed:" + ACCESS
    assert user["githubRefreshToken"] == "sealed:" + REFRESH
    assert user["githubAccessTokenExpiresAt"] == expiry
    assert user["githubRefreshTokenExpiresAt"] == fixture.now + 1 + REFRESH_SECONDS
    status, payload, headers = call(binding, gateway, fixture.now + 2,
                                     "GET", "/auth/session", headers={"Cookie": cookie})
    assert status == 200 and payload["authenticated"] is True
    assert headers["Cache-Control"] == "no-store"
    assert_no_credentials(payload)


@pytest.mark.parametrize("method,path", [
    ("GET", "/repositories"), ("GET", "/integrations"),
    ("GET", "/api/v1/repositories"), ("POST", "/repositories/sync"),
])
def test_eight_hour_expiry_reads_signal_refresh_without_writes_or_provider_calls(tmp_path, method, path):
    fixture, _, gateway, cookie, expiry = setup_login(tmp_path)
    with fixture.store.connect() as db:
        db.execute("""CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,
            key_prefix TEXT,key_hash TEXT UNIQUE,scopes TEXT,expires_at INTEGER,
            restrictions TEXT,created_at INTEGER,last_used_at INTEGER,revoked_at INTEGER)""")
    class ReadOnly(D1ShapedSQLite):
        def prepare(self, sql):
            assert sql.lstrip().upper().startswith("SELECT"), sql
            return super().prepare(sql)
    before = database_snapshot(fixture.store)
    status, payload, headers = call(ReadOnly(fixture.store), gateway, expiry, method, path,
        headers={"Cookie": cookie, "Origin": ORIGIN})
    assert status == 200 and payload["githubRefreshRequired"] is True
    assert payload["githubAccess"] == "reauthorization_required"
    assert headers["Cache-Control"] == "no-store"
    assert_no_credentials(payload)
    assert not gateway.refresh_calls and not gateway.repository_calls
    assert database_snapshot(fixture.store) == before


@pytest.mark.parametrize("claim_age", [0, 59, 60, 61])
def test_reads_of_claimed_expiring_pair_distinguish_pending_and_stale_claim_without_provider(tmp_path, claim_age):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    user = stored_user(fixture.store)
    user["githubTokenRefresh"] = {"id": "synthetic-durable-claim", "startedAt": expiry}
    replace_user(fixture.store, user, expiry)
    before = database_snapshot(fixture.store)
    if claim_age < 60:
        with pytest.raises(GitHubFailure) as failure:
            call(binding, gateway, expiry + claim_age, "GET", "/repositories", headers={"Cookie": cookie})
        assert failure.value.code == "GITHUB_UNAVAILABLE"
    else:
        status, payload, _ = call(binding, gateway, expiry + claim_age, "GET", "/repositories", headers={"Cookie": cookie})
        assert status == 200 and payload == {"items": [], "githubAccess": "reauthorization_required"}
        assert_no_credentials(payload)
    assert not gateway.repository_calls and not gateway.refresh_calls
    assert database_snapshot(fixture.store) == before


def test_refresh_rotates_pair_once_and_preserves_session_repository_binding_and_financial_history(tmp_path):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    user = stored_user(fixture.store)
    repository_binding = {"status": "authorized", "installationId": "501",
                          "repositoryItems": [{"id": "202", "fullName": "synthetic-org/project"}]}
    user.update(githubRepositoryAccess=repository_binding,
                billing={"plan": "pro", "customerId": "synthetic-customer"})
    replace_user(fixture.store, user, fixture.now + 2)
    session_name = record_name("sessions", cookie.split("=", 1)[1])
    with fixture.store.connect() as db:
        session_before = db.execute("SELECT payload,updated_at FROM app_state WHERE name=?", (session_name,)).fetchone()
        session_before = tuple(session_before)
        assert json.loads(session_before[0])["expiresAt"] == fixture.now + 1 + 7 * 86400
        db.execute("CREATE TABLE synthetic_financial_history(id TEXT PRIMARY KEY,amount_minor INTEGER,project_id TEXT)")
        db.execute("INSERT INTO synthetic_financial_history VALUES('expense-history',12345,'project-existing')")
    status, payload, headers = refresh_request(binding, gateway, expiry, cookie)
    assert status == 200 and payload == {"ok": True, "refreshed": True}
    assert headers["Cache-Control"] == "no-store" and "Set-Cookie" not in headers
    assert_no_credentials(payload)
    rotated = stored_user(fixture.store)
    assert rotated["githubAccessToken"] == "sealed:" + ROTATED_ACCESS
    assert rotated["githubRefreshToken"] == "sealed:" + ROTATED_REFRESH
    assert rotated["githubAccessTokenExpiresAt"] == expiry + ACCESS_SECONDS
    assert rotated["githubRefreshTokenExpiresAt"] == expiry + REFRESH_SECONDS
    assert rotated["githubAccessTokenUpdatedAt"] == expiry
    assert rotated["githubRepositoryAccess"] == repository_binding
    assert rotated["billing"] == user["billing"]
    assert rotated["id"] == user["id"] and rotated["githubId"] == user["githubId"]
    assert "githubTokenRefresh" not in rotated
    with fixture.store.connect() as db:
        assert tuple(db.execute("SELECT payload,updated_at FROM app_state WHERE name=?", (session_name,)).fetchone()) == session_before
        assert tuple(db.execute("SELECT * FROM synthetic_financial_history").fetchone()) == (
            "expense-history", 12345, "project-existing")
    before = database_snapshot(fixture.store)
    assert refresh_request(binding, gateway, expiry + 1, cookie)[:2] == (200, {"ok": True, "refreshed": False})
    assert gateway.refresh_calls == [REFRESH]
    assert database_snapshot(fixture.store) == before
    status, repositories, _ = call(binding, gateway, expiry + 1, "GET", "/repositories", headers={"Cookie": cookie})
    assert status == 200 and repositories["githubAccess"] == "authorized"
    assert repositories["items"][0]["fullName"] == "synthetic-org/project"
    assert repositories["organizations"] == [{"id": 88, "login": "synthetic-org", "type": "Organization"}]
    assert "githubRefreshRequired" not in repositories
    assert_no_credentials(repositories)


def test_legacy_account_without_refresh_pair_requires_explicit_reconnection(tmp_path):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path, GitHubStub())
    before = database_snapshot(fixture.store)
    status, payload, _ = refresh_request(binding, gateway, expiry, cookie)
    assert status == 403 and payload["error"]["code"] == "GITHUB_REAUTHORIZATION_REQUIRED"
    assert_no_credentials(payload)
    assert database_snapshot(fixture.store) == before


@pytest.mark.parametrize("headers,method,status,code", [
    ({"Origin": ""}, "POST", 403, "UNTRUSTED_ORIGIN"),
    ({"Origin": "https://evil.example"}, "POST", 403, "UNTRUSTED_ORIGIN"),
    ({"Authorization": "Bearer synthetic-api-key"}, "POST", 401, "UNAUTHENTICATED"),
    ({"Authorization": "malformed"}, "POST", 401, "UNAUTHENTICATED"),
    ({"X-Pullwise-Api-Key": "synthetic-api-key"}, "POST", 401, "UNAUTHENTICATED"),
    ({"Cookie": ""}, "POST", 401, "UNAUTHENTICATED"),
    ({}, "GET", 405, "METHOD_NOT_ALLOWED"),
])
def test_refresh_rejects_wrong_origin_non_cookie_credentials_and_method_before_provider(tmp_path, headers, method, status, code):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    before = database_snapshot(fixture.store)
    response = call(binding, gateway, expiry, method, "/integrations/github/refresh",
                    headers={"Cookie": cookie, "Origin": ORIGIN, **headers})
    assert response[0] == status and response[1]["error"]["code"] == code
    assert not gateway.refresh_calls
    assert database_snapshot(fixture.store) == before


def test_expired_pullwise_session_cannot_refresh_provider_token(tmp_path):
    fixture, binding, gateway, cookie, _ = setup_login(tmp_path)
    before = database_snapshot(fixture.store)
    status, payload, _ = refresh_request(binding, gateway, fixture.now + 1 + 7 * 86400, cookie)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert not gateway.refresh_calls and database_snapshot(fixture.store) == before


@pytest.mark.parametrize("code", ["GITHUB_CONFIGURATION_ERROR", "GITHUB_TOKEN_UNREADABLE"])
def test_local_crypto_or_configuration_failure_does_not_claim_or_consume_refresh(tmp_path, code):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    gateway.configuration_failure = GitHubFailure(code)
    before = database_snapshot(fixture.store)
    with pytest.raises(GitHubFailure) as failure:
        refresh_request(binding, gateway, expiry, cookie)
    assert failure.value.code == code
    assert not gateway.refresh_calls and database_snapshot(fixture.store) == before


@pytest.mark.parametrize("code", ["GITHUB_RATE_LIMITED", "GITHUB_PERMISSION_DENIED", "GITHUB_REAUTHORIZATION_REQUIRED"])
def test_known_provider_rejection_releases_claim_and_keeps_distinct_recovery(tmp_path, code):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    gateway.refresh_failure = GitHubFailure(code, 403, request_rejected=True)
    with pytest.raises(GitHubFailure) as failure:
        refresh_request(binding, gateway, expiry, cookie)
    assert failure.value.code == code
    current = stored_user(fixture.store)
    assert "githubTokenRefresh" not in current
    assert current["githubAccessToken"] == "sealed:" + ACCESS
    if code == "GITHUB_REAUTHORIZATION_REQUIRED":
        assert "githubRefreshToken" not in current and "githubRefreshTokenExpiresAt" not in current
        assert refresh_request(binding, gateway, expiry + 1, cookie)[0] == 403
        assert gateway.refresh_calls == [REFRESH]
    else:
        assert current["githubRefreshToken"] == "sealed:" + REFRESH
        gateway.refresh_failure = None
        assert refresh_request(binding, gateway, expiry + 1, cookie)[:2] == (200, {"ok": True, "refreshed": True})
        assert gateway.refresh_calls == [REFRESH, REFRESH]


@pytest.mark.parametrize("outcome", ["network", "invalid_pair", "missing_pair", "publish_unknown", "claim_unknown"])
def test_unknown_refresh_outcome_retains_claim_and_never_consumes_old_pair_again(tmp_path, outcome):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    if outcome == "network":
        gateway.refresh_failure = GitHubFailure("GITHUB_UNAVAILABLE")
    elif outcome == "invalid_pair":
        gateway.refresh_result = GitHubTokenBundle(ROTATED_ACCESS, True, ROTATED_REFRESH, REFRESH_SECONDS)
    elif outcome == "missing_pair":
        gateway.refresh_result = GitHubTokenBundle(ROTATED_ACCESS)
    else:
        class UnknownD1(D1ShapedSQLite):
            async def batch(self, statements):
                rotating = [item for item in statements if item.sql.lstrip().startswith("UPDATE app_state")
                            and "RETURNING name" in item.sql]
                if rotating:
                    is_claim = "githubTokenRefresh" in json.loads(rotating[0].params[0])
                    if outcome == "claim_unknown" and is_claim:
                        await super().batch(statements)
                        raise RuntimeError("synthetic unknown native claim outcome")
                    if outcome == "publish_unknown" and not is_claim:
                        raise RuntimeError("synthetic unknown native publication outcome")
                return await super().batch(statements)
        binding = UnknownD1(fixture.store)
    with pytest.raises(GitHubFailure if outcome in {"network", "invalid_pair", "missing_pair"} else RuntimeError):
        refresh_request(binding, gateway, expiry, cookie)
    claimed = stored_user(fixture.store)
    assert claimed["githubTokenRefresh"]["startedAt"] == expiry
    assert claimed["githubAccessToken"] == "sealed:" + ACCESS
    assert claimed["githubRefreshToken"] == "sealed:" + REFRESH
    expected_calls = [] if outcome == "claim_unknown" else [REFRESH]
    assert gateway.refresh_calls == expected_calls
    before = database_snapshot(fixture.store)
    status, payload, _ = refresh_request(binding, gateway, expiry + 1, cookie)
    assert status == 409 and payload["error"]["code"] == "GITHUB_REFRESH_PENDING"
    status, payload, _ = refresh_request(binding, gateway, expiry + 60, cookie)
    assert status == 403 and payload["error"]["code"] == "GITHUB_REAUTHORIZATION_REQUIRED"
    assert_no_credentials(payload)
    assert gateway.refresh_calls == expected_calls and database_snapshot(fixture.store) == before


def test_durable_claim_allows_only_one_provider_consumer_for_concurrent_requests(tmp_path):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    async def journey():
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        gateway.refresh_wait = wait
        first = asyncio.create_task(async_refresh_request(binding, gateway, expiry, cookie))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            # A separate binding sees the claim rather than an in-memory lock.
            second = await async_refresh_request(D1ShapedSQLite(fixture.store), gateway, expiry, cookie)
            assert second[0] == 409 and second[1]["error"]["code"] == "GITHUB_REFRESH_PENDING"
            assert gateway.refresh_calls == [REFRESH]
        finally:
            release.set()
        assert (await first)[:2] == (200, {"ok": True, "refreshed": True})
    asyncio.run(journey())
    assert gateway.refresh_calls == [REFRESH]


@pytest.mark.parametrize("change", ["session_revoked", "reauthorized", "billing_changed"])
def test_provider_await_fences_session_and_token_generation_but_preserves_current_billing(tmp_path, change):
    fixture, binding, gateway, cookie, expiry = setup_login(tmp_path)
    old_user = stored_user(fixture.store)
    session_name = record_name("sessions", cookie.split("=", 1)[1])
    async def journey():
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        gateway.refresh_wait = wait
        task = asyncio.create_task(async_refresh_request(binding, gateway, expiry, cookie))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            current = stored_user(fixture.store)
            assert "githubTokenRefresh" in current
            if change == "session_revoked":
                with fixture.store.connect() as db:
                    db.execute("DELETE FROM app_state WHERE name=?", (session_name,))
            elif change == "reauthorized":
                current.pop("githubTokenRefresh")
                current.update(githubAccessToken="sealed:synthetic-reauthorized-access",
                               githubRefreshToken="sealed:synthetic-reauthorized-refresh",
                               githubAccessTokenExpiresAt=expiry + ACCESS_SECONDS,
                               githubRefreshTokenExpiresAt=expiry + REFRESH_SECONDS,
                               githubAccessTokenUpdatedAt=expiry)
                replace_user(fixture.store, current, expiry)
            else:
                current.update(billing={"plan": "max", "revision": 5}, billingEvents=[{"id": "synthetic-paid-event"}])
                replace_user(fixture.store, current, expiry)
        finally:
            release.set()
        return await task
    status, payload, _ = asyncio.run(journey())
    current = stored_user(fixture.store)
    assert gateway.refresh_calls == [REFRESH]
    if change == "session_revoked":
        assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
        assert current["githubAccessToken"] == old_user["githubAccessToken"]
        assert current["githubRefreshToken"] == old_user["githubRefreshToken"]
        assert "githubTokenRefresh" in current
    elif change == "reauthorized":
        assert status == 409 and payload["error"]["code"] == "ACCOUNT_CHANGED"
        assert current["githubAccessToken"] == "sealed:synthetic-reauthorized-access"
        assert current["githubRefreshToken"] == "sealed:synthetic-reauthorized-refresh"
        assert "githubTokenRefresh" not in current
    else:
        assert status == 200 and payload == {"ok": True, "refreshed": True}
        assert current["billing"] == {"plan": "max", "revision": 5}
        assert current["billingEvents"] == [{"id": "synthetic-paid-event"}]
        assert current["githubAccessToken"] == "sealed:" + ROTATED_ACCESS
        assert "githubTokenRefresh" not in current
    assert_no_credentials(payload)


@pytest.fixture
def expiring_ledger():
    # Reuse the canonical migrated ledger fixture without collecting its tests.
    import test_ledger_routes as ledger_fixtures
    ledger = ledger_fixtures.LedgerRoutesTests()
    ledger.setUp()
    try:
        linked_status, linked = ledger.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        standalone_status, standalone = ledger.call("POST", "/api/v1/projects", {"name": "Standalone costs"})
        category_status, category = ledger.call("POST", "/api/v1/categories", {"name": "Hosting"})
        assert linked_status == standalone_status == category_status == 201
        status, expense = ledger.call("POST", "/api/v1/expenses", {
            "target": {"kind": "project", "projectId": linked["id"]},
            "occurredOn": "2026-10-10", "amount": "12.34", "currency": "USD",
            "categoryId": category["id"], "purpose": "Preserved history"},
            {**ledger.headers, "Idempotency-Key": "synthetic-refresh-history"})
        assert status == 201
        from pullwise_server.cloudflare_api_key_write import create_api_key
        status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
            body={"name": "Synthetic read scope", "scopes": ["projects:read"]}, now=ledger.now + 3))
        assert status == 201
        expiry = ledger.now + 1 + ACCESS_SECONDS
        user = stored_user(ledger.store)
        user.update(githubAccessTokenExpiresAt=expiry, githubRefreshToken="sealed:" + REFRESH,
                    githubRefreshTokenExpiresAt=ledger.now + 1 + REFRESH_SECONDS)
        replace_user(ledger.store, user, ledger.now + 4)
        ledger.now = expiry - 3
        ledger.gateway = ExpiringGitHub()
        yield ledger, linked, standalone, expense, {"Authorization": "Bearer " + key["key"]}
    finally:
        ledger.tearDown()


@pytest.mark.parametrize("authentication", ["cookie", "key"])
def test_real_ledger_repository_and_project_reads_only_signal_cookie_refresh_and_preserve_history(expiring_ledger, authentication):
    ledger, linked, standalone, expense, key_headers = expiring_ledger
    headers = ledger.headers if authentication == "cookie" else key_headers
    before = database_snapshot(ledger.store)
    for path in ("/api/v1/repositories", "/api/v1/projects", "/api/v1/projects/" + linked["id"]):
        status, payload = ledger.call("GET", path, headers=headers, gateway=ledger.gateway)
        assert status == 200
        assert (payload.get("githubRefreshRequired") is True) == (authentication == "cookie")
        assert_no_credentials(payload)
        if path == "/api/v1/repositories":
            assert payload["items"] == [] and payload["githubAccess"] == "reauthorization_required"
        elif path == "/api/v1/projects":
            items = {item["id"]: item for item in payload["items"]}
            assert items[linked["id"]]["totals"] == [{"currency": "USD", "amountMinor": 1234}]
            assert items[linked["id"]]["githubAccess"] == "reauthorization_required"
            assert (items[linked["id"]].get("githubRefreshRequired") is True) == (authentication == "cookie")
            assert "githubRefreshRequired" not in items[standalone["id"]]
        else:
            assert payload["totals"] == [{"currency": "USD", "amountMinor": 1234}]
            assert payload["githubAccess"] == "reauthorization_required" and payload["githubFullName"] is None
    status, payload = ledger.call("GET", "/api/v1/projects/" + standalone["id"], headers=headers, gateway=ledger.gateway)
    assert status == 200 and payload["githubAccess"] == "not_linked"
    assert "githubRefreshRequired" not in payload
    assert not ledger.gateway.refresh_calls and not ledger.gateway.repository_calls
    assert database_snapshot(ledger.store) == before


def test_standalone_only_project_page_has_no_refresh_signal_even_when_later_page_is_linked(expiring_ledger):
    ledger, linked, standalone, _, _ = expiring_ledger
    with ledger.store.connect() as db:
        db.execute("UPDATE ledger_projects SET id='000-standalone' WHERE id=?", (standalone["id"],))
    before = database_snapshot(ledger.store)
    status, page = ledger.call("GET", "/api/v1/projects", params={"limit": "1"}, gateway=ledger.gateway)
    assert status == 200 and page["nextCursor"] == "000-standalone"
    assert page["items"][0]["githubAccess"] == "not_linked"
    assert "githubRefreshRequired" not in page and "githubRefreshRequired" not in page["items"][0]
    assert not ledger.gateway.refresh_calls and not ledger.gateway.repository_calls
    assert database_snapshot(ledger.store) == before


@pytest.mark.parametrize("claim_age,state,repository_status", [
    (0, "unavailable", 503), (59, "unavailable", 503),
    (60, "reauthorization_required", 200),
])
def test_claimed_project_reads_preserve_totals_without_premature_reauthorization_or_new_refresh(expiring_ledger, claim_age, state, repository_status):
    ledger, linked, _, _, _ = expiring_ledger
    expiry = ledger.now + 3
    user = stored_user(ledger.store)
    user["githubTokenRefresh"] = {"id": "synthetic-pending-refresh", "startedAt": expiry}
    replace_user(ledger.store, user, expiry)
    ledger.now += claim_age
    before = database_snapshot(ledger.store)
    status, repositories = ledger.call("GET", "/api/v1/repositories", gateway=ledger.gateway)
    assert status == repository_status and "githubRefreshRequired" not in repositories
    if repository_status == 503:
        assert repositories["error"]["code"] == "GITHUB_UNAVAILABLE"
    for path in ("/api/v1/projects", "/api/v1/projects/" + linked["id"]):
        status, payload = ledger.call("GET", path, gateway=ledger.gateway)
        assert status == 200 and "githubRefreshRequired" not in payload
        project = next(item for item in payload["items"] if item["id"] == linked["id"]) if "items" in payload else payload
        assert project["githubAccess"] == state and "githubRefreshRequired" not in project
        assert project["totals"] == [{"currency": "USD", "amountMinor": 1234}]
        assert project["githubFullName"] is None
        assert_no_credentials(payload)
    assert not ledger.gateway.refresh_calls and not ledger.gateway.repository_calls
    assert database_snapshot(ledger.store) == before
