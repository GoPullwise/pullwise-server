"""Local, synthetic email auth acceptance; no mail provider or remote D1 calls."""
import asyncio
import base64
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pullwise_server import cloudflare_email_auth as email_auth
from pullwise_server.cloudflare_email_auth import (
    BROWSER_COOKIE, CODE_AGE, email_key, handle_email_request, normalize_email,
)
from pullwise_server.cloudflare_state_records import encode_record, record_name

NOW = 1_800_000_000
SECRET = "synthetic-email-code-pepper-at-least-32-characters"
EMAIL = "alice@example.test"
ORIGIN = "https://app.example.test"
REQUEST = "/auth/email/request-code"
VERIFY = "/auth/email/verify-code"


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL);
                CREATE TABLE d1_command_guard(ok INTEGER NOT NULL CHECK(ok=1));
                CREATE TABLE account_entitlement_authority(owner_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
                    plan TEXT NOT NULL,period TEXT NOT NULL,period_start INTEGER NOT NULL,valid_until INTEGER NOT NULL,dirty INTEGER NOT NULL);""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class Statement:
    def __init__(self, binding, sql, params=()):
        self.binding, self.sql, self.params = binding, sql, params

    def bind(self, *params):
        return Statement(self.binding, self.sql, params)

    async def first(self):
        self.binding.reads += 1
        with self.binding.store.connect() as db:
            row = db.execute(self.sql, self.params).fetchone()
            return dict(row) if row else None


class Binding:
    def __init__(self, store):
        self.store, self.groups, self.reads, self.fail_sql, self.before_group = store, [], 0, None, None

    def prepare(self, sql):
        return Statement(self, sql)

    async def batch(self, statements):
        self.groups.append([item.sql for item in statements])
        if self.before_group:
            callback, self.before_group = self.before_group, None
            callback()
        with self.store.connect() as db:
            results = []
            for item in statements:
                if self.fail_sql and self.fail_sql in item.sql:
                    raise RuntimeError("synthetic uncertain native failure")
                cursor = db.execute(item.sql, item.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(SimpleNamespace(success=True, results=rows))
            return results


class Gateway:
    configured = True

    def __init__(self):
        self.sent, self.failure = [], False

    async def send_code(self, email, code, *, expires_in):
        self.sent.append((email, code, expires_in))
        if self.failure:
            raise RuntimeError("synthetic provider response must never reach HTTP")


class Admission:
    def __init__(self):
        self.calls, self.allowed = [], True

    async def __call__(self, kind, subject):
        self.calls.append((kind, subject))
        return self.allowed


@pytest.fixture
def runtime(tmp_path):
    return SimpleNamespace(binding=Binding(Store(tmp_path / "email-auth.sqlite")),
                           gateway=Gateway(), admit=Admission())


def call(runtime, path, body, *, now=NOW, cookie="", origin=ORIGIN, secret=SECRET,
         method="POST", admit="default"):
    return asyncio.run(handle_email_request(binding=runtime.binding, gateway=runtime.gateway,
        secret=secret, admit=runtime.admit if admit == "default" else admit,
        method=method, path=path, body=body, headers={"Cookie": cookie, "Origin": origin},
        now=now, cookie_same_site="None", trusted_origins={ORIGIN}))


def request(runtime, *, email=EMAIL, purpose="login", now=NOW, cookie="", secret=SECRET):
    status, payload, headers = call(runtime, REQUEST, {"email": email, "purpose": purpose},
                                  now=now, cookie=cookie, secret=secret)
    assert status == 202
    browser_cookie = headers["Set-Cookie"].split(";", 1)[0]
    return payload["challengeId"], runtime.gateway.sent[-1][1], browser_cookie


def verify(runtime, issued, *, email=EMAIL, now=NOW + 1, session="", secret=SECRET):
    challenge, code, browser = issued
    return call(runtime, VERIFY, {"email": email, "challengeId": challenge, "code": code},
                now=now, cookie="; ".join(filter(None, [session, browser])), secret=secret)


def records(runtime, kind):
    with runtime.binding.store.connect() as db:
        return [json.loads(row["payload"]) for row in db.execute(
            "SELECT payload FROM app_state WHERE name GLOB ? ORDER BY name", (f"record:{kind}:*",))]


def seed_user(runtime, *, email=None, verified=False, user_id="usr_github_77", session_id="ses-existing", include_id=True):
    user = {"id": user_id, "name": "Existing account", "createdAt": NOW - 100,
        "providers": ["github"], "githubId": "77", "githubAccessToken": "sealed:synthetic",
        "billing": {"plan": "pro", "subscriptionId": "sub-existing", "status": "active",
                    "currentPeriodStart": NOW - 100, "currentPeriodEnd": NOW + 3600}}
    if email:
        user["email"] = email
    if verified:
        user.update(emailVerifiedAt=NOW - 50, emailVerified=True, providers=["github", "email"])
    session = {"userId": user_id, "createdAt": NOW - 20, "expiresAt": NOW + 3600}
    if include_id:
        session["id"] = session_id
    with runtime.binding.store.connect() as db:
        for kind, identity, value in [("users", user_id, user), ("sessions", session_id, session)]:
            db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name(kind, identity), encode_record(kind, identity, value), NOW))
        db.execute("INSERT INTO account_entitlement_authority VALUES(?,1,'pro','fixture',?,?,0)",
                   (user_id, NOW - 100, NOW + 3600))
        if verified:
            key = email_key(email)
            identity = {"email": email, "userId": user_id, "createdAt": NOW - 50, "verifiedAt": NOW - 50}
            db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("emailIdentities", key), encode_record("emailIdentities", key, identity), NOW))
    return user, f"pw_session={session_id}"


def test_first_verified_mailbox_registers_and_issues_one_atomic_cookie_session(runtime):
    issued = request(runtime, email=" Alice@Example.Test ")
    assert runtime.gateway.sent[0][0] == EMAIL
    assert runtime.gateway.sent[0][2] == 600
    assert len(issued[1]) == 6 and issued[1].isascii() and issued[1].isdigit()
    assert not records(runtime, "users") and not records(runtime, "emailIdentities")
    challenge = records(runtime, "emailChallenges")[0]
    assert challenge["attempts"] == 0 and challenge["expiresAt"] == NOW + 600
    stored = json.dumps(challenge)
    assert SECRET not in stored and issued[1] not in challenge.values() and issued[2].split("=", 1)[1] not in stored
    status, payload, headers = verify(runtime, issued)
    assert status == 200 and payload["authenticated"] is True
    user = payload["user"]
    assert user["id"].startswith("usr_email_") and EMAIL not in user["id"]
    assert user["email"] == EMAIL and user["emailVerified"] is True and user["providers"] == ["email"]
    assert payload["github"] == {"identityConnected": False, "repositoriesConnected": False}
    assert headers["Set-Cookie"].startswith("pw_session=ses-")
    assert all(part in headers["Set-Cookie"] for part in ["HttpOnly", "Secure", "SameSite=None", "Max-Age=604800"])
    assert not records(runtime, "emailChallenges")
    assert records(runtime, "emailIdentities")[0]["userId"] == user["id"]
    assert records(runtime, "sessions")[0]["userId"] == user["id"]
    final_group = runtime.binding.groups[-1]
    assert sum("INSERT INTO account_entitlement_authority" in sql for sql in final_group) == 1
    assert sum("INSERT INTO app_state" in sql for sql in final_group) == 3
    assert "DELETE FROM app_state" in final_group[0]
    with runtime.binding.store.connect() as db:
        authority = dict(db.execute("SELECT * FROM account_entitlement_authority").fetchone())
        assert authority["owner_id"] == user["id"] and authority["plan"] == "free"
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_repeated_login_and_pepper_rotation_keep_the_same_account_and_payment_facts(runtime):
    issued = request(runtime)
    first = verify(runtime, issued)[1]["user"]["id"]
    original = records(runtime, "users")[0]
    preserved = {**original, "billing": {"plan": "max", "subscriptionId": "sub-preserved"}, "privateFixture": "retained"}
    with runtime.binding.store.connect() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", first, preserved), record_name("users", first)))
    rotated = "different-synthetic-code-pepper-at-least-32-characters"
    issued_again = request(runtime, email="ALICE@EXAMPLE.TEST", now=NOW + 120, secret=rotated)
    status, payload, _ = verify(runtime, issued_again, now=NOW + 121, secret=rotated)
    assert status == 200 and payload["user"]["id"] == first
    assert records(runtime, "users") == [preserved]
    assert len(records(runtime, "emailIdentities")) == 1 and len(records(runtime, "sessions")) == 2


def test_an_unverified_github_or_billing_email_does_not_merge_accounts(runtime):
    github_user, _ = seed_user(runtime, email=EMAIL)
    issued = request(runtime)
    status, payload, _ = verify(runtime, issued)
    assert status == 200 and payload["user"]["id"] != github_user["id"]
    assert github_user in records(runtime, "users")
    assert len(records(runtime, "users")) == 2


@pytest.mark.parametrize("include_id", [True, False])
def test_explicit_email_link_keeps_github_id_billing_providers_and_current_session(runtime, include_id):
    original, cookie = seed_user(runtime, include_id=include_id)
    issued = request(runtime, purpose="link", cookie=cookie)
    status, payload, headers = verify(runtime, issued, session=cookie)
    assert status == 200 and payload["user"]["id"] == original["id"]
    assert headers["Set-Cookie"].startswith(BROWSER_COOKIE + "=;")
    assert "pw_session" not in headers["Set-Cookie"]
    assert len(records(runtime, "sessions")) == 1
    linked = records(runtime, "users")[0]
    assert linked["billing"] == original["billing"] and linked["githubAccessToken"] == original["githubAccessToken"]
    assert linked["providers"] == ["github", "email"] and linked["emailVerifiedAt"] == NOW + 1
    assert records(runtime, "emailIdentities")[0]["userId"] == original["id"]
    assert not any("account_entitlement_authority" in sql for sql in runtime.binding.groups[-1])


def test_link_requires_the_original_authenticated_cookie_session(runtime):
    _, cookie = seed_user(runtime)
    status, _, _ = call(runtime, REQUEST, {"email": EMAIL, "purpose": "link"})
    assert status == 401 and not runtime.gateway.sent
    issued = request(runtime, purpose="link", cookie=cookie)
    _, other_cookie = seed_user(runtime, user_id="usr_second", session_id="ses-second")
    assert verify(runtime, issued, session=other_cookie)[0] == 401
    with runtime.binding.store.connect() as db:
        db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "ses-existing"),))
    assert verify(runtime, issued, session=cookie)[0] == 401
    assert not records(runtime, "emailIdentities")


def test_verified_existing_mailbox_conflict_is_rejected_without_merging_or_new_sessions(runtime):
    issued = request(runtime)
    existing = verify(runtime, issued)[1]["user"]["id"]
    original, cookie = seed_user(runtime)
    linking = request(runtime, purpose="link", cookie=cookie, now=NOW + 120)
    before = records(runtime, "users")
    status, payload, _ = verify(runtime, linking, session=cookie, now=NOW + 121)
    assert status == 409 and payload["error"]["code"] == "EMAIL_ALREADY_LINKED"
    assert records(runtime, "users") == before
    assert records(runtime, "emailIdentities")[0]["userId"] == existing != original["id"]
    assert len(records(runtime, "sessions")) == 2 and not records(runtime, "emailChallenges")


def test_a_verified_login_email_cannot_be_changed_through_the_link_flow(runtime):
    _, cookie = seed_user(runtime, email="old@example.test", verified=True)
    status, payload, _ = call(runtime, REQUEST, {"email": EMAIL, "purpose": "link"}, cookie=cookie)
    assert status == 409 and payload["error"]["code"] == "EMAIL_CHANGE_NOT_SUPPORTED"
    assert not runtime.gateway.sent and not records(runtime, "emailChallenges")


@pytest.mark.parametrize("browser", ["", BROWSER_COOKIE + "=" + "A" * 43, BROWSER_COOKIE + "=short"])
def test_a_code_cannot_be_verified_from_another_or_unbound_browser(runtime, browser):
    challenge, code, _ = request(runtime)
    status, _, _ = call(runtime, VERIFY, {"email": EMAIL, "challengeId": challenge, "code": code}, cookie=browser)
    assert status == 400 and not records(runtime, "users")
    assert records(runtime, "emailChallenges")[0]["attempts"] == 0


def test_success_consumes_the_code_and_replay_does_not_issue_another_session(runtime):
    issued = request(runtime)
    assert verify(runtime, issued)[0] == 200
    assert verify(runtime, issued, now=NOW + 2)[0] == 400
    assert len(records(runtime, "sessions")) == 1


def test_competing_successful_verifications_commit_only_one_account_and_session(runtime, monkeypatch):
    challenge, code, browser = request(runtime)
    original_first = Statement.first
    async def interleaved_first(statement):
        result = await original_first(statement)
        await asyncio.sleep(0)
        return result
    monkeypatch.setattr(Statement, "first", interleaved_first)
    async def compete():
        async def attempt():
            return await handle_email_request(binding=runtime.binding, gateway=runtime.gateway,
                secret=SECRET, admit=runtime.admit, method="POST", path=VERIFY,
                body={"email": EMAIL, "challengeId": challenge, "code": code},
                headers={"Cookie": browser, "Origin": ORIGIN}, now=NOW + 1,
                trusted_origins={ORIGIN})
        return await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    results = asyncio.run(compete())
    assert sum(isinstance(result, tuple) and result[0] == 200 for result in results) == 1
    assert sum(isinstance(result, sqlite3.IntegrityError) for result in results) == 1
    assert len(records(runtime, "users")) == len(records(runtime, "emailIdentities")) == len(records(runtime, "sessions")) == 1
    assert not records(runtime, "emailChallenges")


def test_five_wrong_codes_lock_the_challenge_and_resend_invalidates_old_code(runtime):
    challenge, code, browser = request(runtime)
    wrong = "000000" if code != "000000" else "000001"
    for index in range(5):
        assert call(runtime, VERIFY, {"email": EMAIL, "challengeId": challenge, "code": wrong},
                    cookie=browser, now=NOW + index + 1)[0] == 400
    assert records(runtime, "emailChallenges")[0]["attempts"] == 5
    assert verify(runtime, (challenge, code, browser), now=NOW + 6)[0] == 429
    next_issue = request(runtime, now=NOW + 60)
    assert len(records(runtime, "emailChallenges")) == 1
    assert verify(runtime, (challenge, code, next_issue[2]), now=NOW + 61)[0] == 400
    assert verify(runtime, next_issue, now=NOW + 61)[0] == 200


def test_expiration_is_enforced_at_the_exact_deadline(runtime):
    issued = request(runtime)
    status, payload, _ = verify(runtime, issued, now=NOW + CODE_AGE)
    assert status == 400 and payload["error"]["code"] == "EMAIL_CODE_EXPIRED"
    assert not records(runtime, "users")


def test_unknown_delivery_stays_bounded_and_is_never_automatically_retried(runtime, capsys):
    runtime.gateway.failure = True
    status, payload, _ = call(runtime, REQUEST, {"email": EMAIL, "purpose": "login"})
    assert status == 503 and payload == {"error": {"code": "EMAIL_SEND_UNAVAILABLE"}}
    assert len(runtime.gateway.sent) == 1 and records(runtime, "emailChallenges")
    status, _, headers = call(runtime, REQUEST, {"email": EMAIL, "purpose": "login"}, now=NOW + 1)
    assert status == 429 and headers["Retry-After"] == "59"
    assert len(runtime.gateway.sent) == 1
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("case", ["provider", "secret", "admission"])
@pytest.mark.parametrize("path", [REQUEST, VERIFY])
def test_missing_auth_configuration_fails_closed_before_identity_storage(runtime, case, path):
    if case == "provider":
        runtime.gateway.configured = False
    body = {"email": EMAIL, "purpose": "login"} if path == REQUEST else {"email": EMAIL, "challengeId": "A" * 43, "code": "123456"}
    status, _, _ = call(runtime, path, body, secret="short" if case == "secret" else SECRET,
                        admit=None if case == "admission" else "default")
    assert status == 503 and not runtime.binding.groups and runtime.binding.reads == 0
    assert not runtime.gateway.sent and not runtime.admit.calls


@pytest.mark.parametrize("origin", ["", "https://attacker.example", "https://app.example.test.attacker.example", "https://[invalid"])
def test_email_post_requires_a_trusted_origin_even_without_existing_session(runtime, origin):
    status, payload, _ = call(runtime, REQUEST, {"email": EMAIL, "purpose": "login"}, origin=origin)
    assert status == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert not runtime.gateway.sent and not runtime.admit.calls and not runtime.binding.groups


@pytest.mark.parametrize("body", [
    {"email": EMAIL}, {"email": EMAIL, "purpose": "register"},
    {"email": EMAIL, "purpose": "login", "userId": "victim"},
    {"email": "alice@example.test\r\nBcc: victim@example.test", "purpose": "login"},
    {"email": "alice..test@example.test", "purpose": "login"},
    {"email": "\ud800@example.test", "purpose": "login"},
])
def test_request_input_is_strict_and_validated_before_admission_or_delivery(runtime, body):
    assert call(runtime, REQUEST, body)[0] == 422
    assert not runtime.admit.calls and not runtime.gateway.sent and not runtime.binding.groups


def test_oversized_standalone_body_and_unsupported_methods_do_not_reach_storage(runtime):
    assert call(runtime, REQUEST, {"email": "a" * 8193, "purpose": "login"})[0] == 413
    assert call(runtime, REQUEST, {}, method="GET")[0] == 405
    assert call(runtime, "/other", {}) is None
    assert not runtime.binding.groups and not runtime.admit.calls


def test_delivery_and_verification_require_rate_admission(runtime):
    runtime.admit.allowed = False
    assert call(runtime, REQUEST, {"email": EMAIL, "purpose": "login"})[0] == 429
    runtime.admit.allowed = True
    issued = request(runtime)
    runtime.admit.allowed = False
    assert verify(runtime, issued)[0] == 429
    assert not records(runtime, "users") and records(runtime, "emailChallenges")[0]["attempts"] == 0
    assert runtime.admit.calls[-1] == ("verify", email_key(EMAIL))


@pytest.mark.parametrize("result", [None, 0, 1, {}, "true"])
def test_ambiguous_rate_admission_fails_closed_without_accessing_identity_storage(runtime, result):
    async def ambiguous(_kind, _subject):
        return result
    status, payload, _ = call(runtime, REQUEST, {"email": EMAIL, "purpose": "login"}, admit=ambiguous)
    assert status == 503 and payload["error"]["code"] == "EMAIL_AUTH_UNAVAILABLE"
    assert not runtime.gateway.sent and not runtime.binding.groups and runtime.binding.reads == 0


@pytest.mark.parametrize("change", ["session", "billing"])
def test_link_atomic_fences_reject_concurrent_revocation_or_account_changes(runtime, change):
    original, cookie = seed_user(runtime)
    issued = request(runtime, purpose="link", cookie=cookie)
    changed_user = {**original, "billing": {**original["billing"], "subscriptionId": "sub-new-fact"}}
    def concurrent_change():
        with runtime.binding.store.connect() as db:
            if change == "session":
                db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", "ses-existing"),))
            else:
                db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", original["id"], changed_user), record_name("users", original["id"])))
    runtime.binding.before_group = concurrent_change
    with pytest.raises(sqlite3.IntegrityError):
        verify(runtime, issued, session=cookie)
    assert not records(runtime, "emailIdentities") and len(records(runtime, "emailChallenges")) == 1
    assert records(runtime, "users") == [changed_user if change == "billing" else original]


def test_atomic_native_failure_rolls_back_consumption_account_mapping_and_session(runtime):
    issued = request(runtime)
    runtime.binding.fail_sql = "INSERT INTO account_entitlement_authority"
    with pytest.raises(RuntimeError, match="synthetic uncertain native failure"):
        verify(runtime, issued)
    assert not records(runtime, "users") and not records(runtime, "emailIdentities") and not records(runtime, "sessions")
    assert len(records(runtime, "emailChallenges")) == 1
    with runtime.binding.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM account_entitlement_authority").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_expired_email_challenge_cleanup_is_finite_and_keeps_current_reissue_atomic(runtime):
    issued = request(runtime)
    for index in range(10):
        stale = f"stale{index}@example.test"
        key = email_key(stale)
        value = {**records(runtime, "emailChallenges")[0], "email": stale}
        with runtime.binding.store.connect() as db:
            db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("emailChallenges", key), encode_record("emailChallenges", key, value), NOW))
    new_issue = request(runtime, now=NOW + 601)
    assert len(records(runtime, "emailChallenges")) >= 3
    assert len(records(runtime, "emailChallenges")) <= 4
    assert verify(runtime, (issued[0], issued[1], new_issue[2]), now=NOW + 602)[0] == 400
    assert verify(runtime, new_issue, now=NOW + 602)[0] == 200


def test_uniform_code_sampling_rejects_the_uint32_remainder(monkeypatch):
    values = iter([0xffffffff, 0])
    monkeypatch.setattr(email_auth, "_random_urlsafe", lambda _size: base64.urlsafe_b64encode(next(values).to_bytes(4, "big")).decode().rstrip("="))
    assert email_auth._new_code() == "000000"


def test_provider_aliases_and_plus_tags_are_not_merged():
    assert normalize_email(" Alice+tag@Example.Test ") == "alice+tag@example.test"
    assert email_key("alice+tag@example.test") != email_key("alice@example.test")
