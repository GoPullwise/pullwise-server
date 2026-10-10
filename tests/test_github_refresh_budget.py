"""Admission regressions for the real refresh CAS and typed user envelope."""
import asyncio
from types import SimpleNamespace

import pytest

from pullwise_server.cloudflare_github_refresh import _publish_user
from pullwise_server.cloudflare_preview_budget import _input_bound, _tokens, _where_keys, sql_write_bound
from pullwise_server.cloudflare_preview_schema import INDEX_COUNTS
from pullwise_server.cloudflare_state_records import USER_RECORD_BYTES, encode_record


class Capture:
    def prepare(self, sql):
        class Statement:
            def bind(self, *params):
                self.sql, self.params = sql, params
                return self

        return Statement()

    async def batch(self, statements):
        self.statements = statements
        return [SimpleNamespace(results=[{"name": "record:users:owner"}])]


def captured_refresh_cas():
    binding = Capture()
    existing = {"id": "owner", "githubId": "81", "githubAccessToken": "gcm1:synthetic-access",
        "githubRefreshToken": "gcm1:synthetic-refresh", "githubAccessTokenExpiresAt": 90,
        "githubRefreshTokenExpiresAt": 200,
        "githubRepositoryAccess": {"repositoryItems": [
            {"id": str(index), "fullName": "synthetic/repository-" + str(index)} for index in range(1000)]}}
    session = {"id": "session", "userId": "owner", "expiresAt": 300}
    assert asyncio.run(_publish_user(binding, user={**existing,
        "githubTokenRefresh": {"id": "synthetic-claim", "startedAt": 100}},
        expected_json=encode_record("users", "owner", existing), session=session,
        session_json=encode_record("sessions", "session", session), now=100))
    return binding.statements[0]


def test_refresh_claim_cas_accepts_retained_large_user_and_independent_session_snapshot():
    statement = captured_refresh_cas()
    assert len(statement.params[0].encode()) > 8192
    assert len(statement.params[0].encode()) < USER_RECORD_BYTES
    assert _input_bound(statement.params, sql=statement.sql) == 1000
    assert sql_write_bound(statement.sql) == 1 + 2 * INDEX_COUNTS["app_state"]
    assert {"name", "payload"} <= _where_keys(_tokens(statement.sql))
    assert "RETURNING name" in statement.sql and len(statement.params) == 8


def test_refresh_budget_keeps_exact_typed_user_target_and_scalar_sql_fences():
    statement = captured_refresh_cas()
    params = list(statement.params)
    params[2] = "record:users:another-owner"
    with pytest.raises(ValueError, match="typed record"):
        _input_bound(params, sql=statement.sql)
    with pytest.raises(ValueError, match="unique key"):
        sql_write_bound("UPDATE app_state SET payload=? WHERE payload=? RETURNING name")
    with pytest.raises(ValueError, match="disjunctive"):
        sql_write_bound("UPDATE app_state SET payload=? WHERE name=? OR name=? RETURNING name")
