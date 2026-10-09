"""Activity companions preserve commercial limits and atomic business outcomes."""
import asyncio

import pytest

from ledger_d1_fixture import D1ShapedSQLite
from test_ledger_plan_limits import setup
from test_recurring_meter_plan_limits import recurring, fence, create_rule, usage
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError
from pullwise_server.cloudflare_preview_schema import UPGRADE_V9_SQL
from pullwise_server.ledger_plan_policy import default_policy


def audit(binding, event_id, *, actor='{}'):
    return binding.prepare("""INSERT INTO ledger_activity_events(id,operation_id,owner_id,
        target_kind,actor_json,resource_kind,resource_id,action,after_json,created_at)
        VALUES(?,?, 'owner','shared',?,'recurring_rule','rule','pause','{}','local')""").bind(
            event_id, event_id, actor)


@pytest.fixture
def activity(recurring):
    fixture, frozen = recurring
    with fixture.store._immediate() as db:
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='ledger_activity_events'").fetchone():
            for sql in UPGRADE_V9_SQL:
                db.execute(sql)
    return fixture, frozen


@pytest.mark.parametrize('status', ['paused', 'canceled'])
def test_stop_with_activity_and_expiry_remains_available_at_quota(activity, status):
    fixture, frozen = activity
    policy = default_policy()
    policy['pro'].update(writesPerMinute=1, writesPerMonth=1)
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    create_rule(binding, frozen)
    asyncio.run(binding.batch([fence(binding, frozen), audit(binding, 'old'),
                               binding.prepare('DELETE FROM d1_command_guard')]))
    asyncio.run(binding.batch([
        fence(binding, frozen),
        binding.prepare("UPDATE expense_recurring_rules SET status='" + status +
                        "',revision=revision+1 WHERE id='rule' AND owner_id='owner'"),
        audit(binding, 'current'),
        binding.prepare('DELETE FROM ledger_activity_events WHERE id=?').bind('old'),
        binding.prepare('DELETE FROM d1_command_guard'),
    ]))
    assert usage(fixture) == (0, 0, 1)
    with fixture.store.connect() as db:
        assert db.execute('SELECT id FROM ledger_activity_events').fetchall()[0][0] == 'current'
        assert db.execute('SELECT status FROM expense_recurring_rules').fetchone()[0] == status


def test_normal_mutation_with_multiple_log_rows_charges_once(activity):
    fixture, frozen = activity
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    create_rule(binding, frozen)
    asyncio.run(binding.batch([
        fence(binding, frozen),
        binding.prepare("UPDATE expense_recurring_rules SET revision=revision+1 "
                        "WHERE id='rule' AND owner_id='owner'"),
        audit(binding, 'scope-a'), audit(binding, 'scope-b'),
        binding.prepare('DELETE FROM d1_command_guard'),
    ]))
    assert usage(fixture) == (0, 0, 2)


def test_failed_activity_rolls_back_business_rule_and_usage(activity):
    fixture, frozen = activity
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    create_rule(binding, frozen)
    with pytest.raises(Exception):
        asyncio.run(binding.batch([
            fence(binding, frozen),
            binding.prepare("UPDATE expense_recurring_rules SET revision=revision+1 "
                            "WHERE id='rule' AND owner_id='owner'"),
            audit(binding, 'invalid', actor='[]'),
            binding.prepare('DELETE FROM d1_command_guard'),
        ]))
    assert usage(fixture) == (0, 0, 1)
    with fixture.store.connect() as db:
        assert db.execute('SELECT revision FROM expense_recurring_rules').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM ledger_activity_events').fetchone()[0] == 0
