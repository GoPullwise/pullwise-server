"""Join applications retain global index accounting and commercial allowances."""
import asyncio

import pytest

from ledger_d1_fixture import D1ShapedSQLite
from test_recurring_meter_plan_limits import recurring, fence, usage
from test_ledger_plan_limits import setup
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError
from pullwise_server.cloudflare_preview_budget import sql_write_bound
from pullwise_server.cloudflare_preview_schema import INDEX_COUNTS
from pullwise_server.ledger_plan_policy import default_policy


def seed_invite(fixture):
    with fixture.store.connect() as db:
        db.execute("INSERT INTO workspace_invites(id,workspace_id,token_hash,role,expires_at,"
                   "created_by_user_id,created_by_revision,created_at,updated_at) "
                   "VALUES('link','owner',?,'viewer',1000,'owner',1,'local','local')", ('a' * 64,))


def request(binding, frozen, number):
    return asyncio.run(binding.batch([
        fence(binding, frozen),
        binding.prepare("INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,created_at,updated_at) "
                        "VALUES(?,?,'link',?,'local','local')").bind('request-' + str(number), 'owner', 'applicant-' + str(number)),
        binding.prepare('DELETE FROM d1_command_guard'),
    ]))


def test_request_charges_one_business_write_without_financial_capacity(recurring):
    fixture, frozen = recurring
    seed_invite(fixture)
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    request(limited, frozen, 1)
    assert usage(fixture) == (0, 0, 1)


def test_join_requests_cannot_bypass_exhausted_commercial_write_allowance(recurring):
    fixture, frozen = recurring
    seed_invite(fixture)
    policy = default_policy()
    policy['pro'].update(writesPerMinute=1, writesPerMonth=1)
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    request(limited, frozen, 1)
    with pytest.raises(PlanLimitError, match='WRITE_RATE_LIMIT'):
        request(limited, frozen, 2)
    with fixture.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workspace_join_requests').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM d1_command_guard').fetchone()[0] == 0
    assert usage(fixture) == (0, 0, 1)


def test_global_meter_includes_join_uniqueness_indexes_and_inviter_inbox_index():
    assert INDEX_COUNTS['workspace_invites'] == 4
    assert INDEX_COUNTS['workspace_join_requests'] == 5
    assert sql_write_bound('INSERT INTO workspace_invites(id) VALUES(?)') == 5
    assert sql_write_bound('UPDATE workspace_invites SET status=? WHERE id=?') == 9
    assert sql_write_bound('INSERT INTO workspace_join_requests(id) VALUES(?)') == 6
    assert sql_write_bound('UPDATE workspace_join_requests SET status=? WHERE id=? AND revision=?') == 11
    for sql in (
        'UPDATE workspace_join_requests SET status=? WHERE invite_id=?',
        'DELETE FROM workspace_join_requests WHERE applicant_user_id=?',
        'INSERT INTO workspace_join_requests(id) SELECT id FROM workspace_invites',
    ):
        with pytest.raises(ValueError):
            sql_write_bound(sql)
