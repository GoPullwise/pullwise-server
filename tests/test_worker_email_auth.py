"""Real Worker email routing with local-only mail and SQLite boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from test_cloudflare_email_auth import Binding, Store, Gateway, NOW, SECRET, EMAIL, seed_user, records
from test_worker_cost_pause import load_entry
from pullwise_server.cloudflare_preview_rate import EmailRateLimit


ORIGIN = "https://app.example.test"


def incoming(path, *, method="POST", raw=None, body=None, cookie="", origin=ORIGIN):
    async def read():
        return raw if raw is not None else json.dumps(body).encode()
    return SimpleNamespace(url="https://api.example.test" + path, method=method,
        headers={"origin": origin, "cookie": cookie}, bytes=read)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    entry = load_entry()
    binding = Binding(Store(tmp_path / "identity.sqlite"))
    gateway = Gateway()
    env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED="1", PULLWISE_MODE="local",
        PULLWISE_APP_URL=ORIGIN, PULLWISE_EMAIL_CODE_SECRET=SECRET,
        PULLWISE_EMAIL_AUTH_ENABLED="1", PULLWISE_COOKIE_SAME_SITE="None")
    monkeypatch.setattr(entry, "WorkerEmailGateway", lambda _: gateway)
    monkeypatch.setattr(entry.time, "time", lambda: NOW)
    app = entry._Application(env, binding)
    admitted = []
    async def admit(kind, email):
        admitted.append((kind, email))
        return True
    app.email_admission = admit
    return SimpleNamespace(entry=entry, binding=binding, gateway=gateway, app=app, admitted=admitted)


def test_worker_registers_email_account_with_existing_session_contract(runtime):
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": "Alice@example.test", "purpose": "login"})))
    assert options["status"] == 202 and options["headers"]["Cache-Control"] == "no-store"
    nonce = options["headers"]["Set-Cookie"].split(";", 1)[0]
    assert "HttpOnly" in options["headers"]["Set-Cookie"]
    assert "SameSite=None" in options["headers"]["Set-Cookie"]
    email, code, _ = runtime.gateway.sent[0]
    result, verified = asyncio.run(runtime.app.fetch(incoming("/auth/email/verify-code",
        body={"email": email, "challengeId": payload["challengeId"], "code": code}, cookie=nonce)))
    assert verified["status"] == 200 and verified["headers"]["Set-Cookie"].startswith("pw_session=")
    assert result["user"]["emailVerified"] and result["user"]["providers"] == ["email"]
    assert result["github"] == {"identityConnected": False, "repositoriesConnected": False}
    assert [kind for kind, _ in runtime.admitted] == ["send", "verify"]
    assert all(len(subject) == 64 for _, subject in runtime.admitted)
    replay, replay_options = asyncio.run(runtime.app.fetch(incoming("/auth/email/verify-code",
        body={"email": email, "challengeId": payload["challengeId"], "code": code}, cookie=nonce)))
    assert replay_options["status"] == 400 and replay["error"]["code"] == "EMAIL_CODE_INVALID"


def test_worker_link_send_reports_another_mailbox_owner_before_admission_and_preserves_challenge(runtime):
    seed_user(runtime, email=EMAIL, verified=True, user_id="usr_mailbox_owner", session_id="ses-mailbox-owner")
    _, cookie = seed_user(runtime)
    _, issued = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code", body={"email": EMAIL, "purpose": "login"})))
    assert issued["status"] == 202
    before = {kind: records(runtime, kind) for kind in ("users", "sessions", "emailIdentities", "emailChallenges")}
    admitted, sent, groups, reads = list(runtime.admitted), list(runtime.gateway.sent), list(runtime.binding.groups), runtime.binding.reads
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": " ALICE@EXAMPLE.TEST ", "purpose": "link"}, cookie=cookie)))
    assert options["status"] == 409 and payload == {"error": {"code": "EMAIL_ALREADY_LINKED"}}
    assert options["headers"] == {"Cache-Control": "no-store", "Vary": "Cookie"}
    assert {kind: records(runtime, kind) for kind in before} == before
    assert runtime.admitted == admitted and runtime.gateway.sent == sent
    assert runtime.binding.groups == groups and runtime.binding.reads == reads + 3


@pytest.mark.parametrize("raw,status", [(b"bad-json", 422), (b"\xff", 422),
    (b'{"email":"\\ud800","purpose":"login"}', 422), (b"x" * 8193, 413)])
def test_email_body_rejected_before_mail_admission_or_state(runtime, raw, status):
    _, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code", raw=raw)))
    assert options["status"] == status
    assert not runtime.admitted and not runtime.gateway.sent and runtime.binding.reads == 0


@pytest.mark.parametrize("origin", ["", "https://hostile.example.test"])
def test_anonymous_email_requests_require_same_trusted_origin(runtime, origin):
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": "alice@example.test", "purpose": "login"}, origin=origin)))
    assert options["status"] == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert not runtime.admitted and not runtime.gateway.sent and runtime.binding.reads == 0


@pytest.mark.parametrize("path,body", [
    ("/auth/email/request-code", {"email": "alice@example.test", "purpose": "login"}),
    ("/auth/email/verify-code", {"email": "alice@example.test", "challengeId": "a" * 43, "code": "001234"}),
])
@pytest.mark.parametrize("origin", [None, ""])
def test_trusted_referer_does_not_replace_required_email_origin(runtime, path, body, origin):
    request = incoming(path, body=body, origin=origin)
    if origin is None:
        request.headers.pop("origin")
    request.headers["referer"] = ORIGIN + "/sign-in"
    payload, options = asyncio.run(runtime.app.fetch(request))
    assert options["status"] == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert not runtime.admitted and not runtime.gateway.sent
    assert runtime.binding.reads == 0 and runtime.binding.groups == []


def test_email_rate_limit_preserves_retry_after_and_has_no_identity_side_effects(runtime):
    async def reject(*_):
        raise EmailRateLimit(47)
    runtime.app.email_admission = reject
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": "alice@example.test", "purpose": "login"})))
    assert options["status"] == 429 and options["headers"]["Retry-After"] == "47"
    assert payload["error"]["code"] == "EMAIL_RATE_LIMIT"
    assert not runtime.gateway.sent and runtime.binding.reads == 0


def test_missing_admission_fails_closed_and_rpc_failures_are_redacted(runtime):
    runtime.app.email_admission = None
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": "alice@example.test", "purpose": "login"})))
    assert options["status"] == 503 and payload["error"]["code"] == "EMAIL_AUTH_NOT_CONFIGURED"
    async def failed(*_):
        raise RuntimeError("email@example.test private-code secret")
    runtime.app.email_admission = failed
    payload, options = asyncio.run(runtime.app.fetch(incoming("/auth/email/request-code",
        body={"email": "alice@example.test", "purpose": "login"})))
    assert options["status"] == 503 and payload == {"error": {"code": "EMAIL_AUTH_UNAVAILABLE"}}
    assert not runtime.gateway.sent and runtime.binding.reads == 0


def test_email_ip_subject_ignores_forwarded_for_and_never_contains_raw_address():
    entry = load_entry()
    one = entry._email_ip_subject(SimpleNamespace(headers={"cf-connecting-ip": "192.0.2.1",
        "x-forwarded-for": "198.51.100.1"}))
    two = entry._email_ip_subject(SimpleNamespace(headers={"cf-connecting-ip": "192.0.2.1",
        "x-forwarded-for": "198.51.100.2"}))
    assert one == two and len(one) == 64 and "192.0.2.1" not in one


def test_paused_email_rate_rpc_does_not_touch_coordinator_sql():
    entry = load_entry()
    class PausedStorage:
        @property
        def sql(self):
            raise AssertionError("paused rate RPC accessed storage")
    coordinator = entry.ValidationBudget(SimpleNamespace(storage=PausedStorage()),
        SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED="0", PULLWISE_EMAIL_AUTH_ENABLED="1"))
    result = asyncio.run(coordinator.admitEmail("send", "a" * 64, "b" * 64))
    assert not result["ok"] and coordinator.journal is None


@pytest.mark.parametrize("scheme,expected", [("http", 403), ("https", 202)])
def test_iphone_safari_email_send_requires_configured_https_page_origin(runtime, scheme, expected):
    preview_origin = "https://preview.pull-wise.com"
    runtime.app.env.PULLWISE_APP_URL = preview_origin
    request = incoming("/auth/email/request-code", body={"email": EMAIL, "purpose": "login"},
                       origin=scheme + "://preview.pull-wise.com")
    request.headers.update({
        "referer": scheme + "://preview.pull-wise.com/sign-in",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    })
    payload, options = asyncio.run(runtime.app.fetch(request))
    assert options["status"] == expected
    if scheme == "http":
        assert payload["error"]["code"] == "UNTRUSTED_ORIGIN"
        assert not runtime.admitted and not runtime.gateway.sent
        assert runtime.binding.reads == 0 and runtime.binding.groups == []
    else:
        assert len(runtime.admitted) == len(runtime.gateway.sent) == 1
        assert "challengeId" in payload and payload["expiresIn"] == 600
        assert options["headers"]["Set-Cookie"].startswith("pw_email_challenge=")
        assert "Secure" in options["headers"]["Set-Cookie"]
