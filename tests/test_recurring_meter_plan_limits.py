"""Commercial and global metering of ordinary and scheduled recurring writes."""
import asyncio
import json
from pathlib import Path

import pytest

from ledger_d1_fixture import D1ShapedSQLite
from test_ledger_plan_limits import setup
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError
from pullwise_server.cloudflare_preview_budget import _input_bound, sql_write_bound
from pullwise_server.cloudflare_preview_schema import INDEX_COUNTS, UPGRADE_V6_SQL, UPGRADE_V7_SQL, UPGRADE_V8_SQL
from pullwise_server.cloudflare_state_records import record_name
from pullwise_server.ledger_plan_policy import default_policy


@pytest.fixture
def recurring(setup):
    fixture, frozen = setup
    with fixture.store._immediate() as db:
        db.executescript((Path(__file__).resolve().parents[1] /
            "cloudflare/server/migrations/0005_workspaces_repositories.sql").read_text())
        db.execute("BEGIN")
        for sql in (*UPGRADE_V6_SQL, *UPGRADE_V7_SQL, *UPGRADE_V8_SQL):
            db.execute(sql)
        db.execute("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) "
                   "VALUES('category','owner','Hosting','local','local')")
    return fixture, frozen


def fence(binding, frozen):
    return binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?) THEN 1 ELSE 0 END)""").bind(
            record_name("users", "owner"), frozen)


def create_rule(binding, frozen):
    statement = binding.prepare("""INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,
        target_kind,template_json,schedule_json,status,created_at,updated_at,create_key,create_sha256,
        create_response_json) VALUES('rule','owner','owner','shared','{}','{}','active','local','local',?,?,?)""").bind(
            "rule-key", "a" * 64, "{}")
    return asyncio.run(binding.batch([fence(binding, frozen), statement,
                                     binding.prepare("DELETE FROM d1_command_guard")]))


def usage(fixture):
    with fixture.store.connect() as db:
        return db.execute("SELECT projects,records,writes FROM ledger_plan_usage WHERE owner_id='owner'").fetchone()[:]


def test_creating_rule_counts_one_write_but_no_expense_record(recurring):
    fixture, frozen = recurring
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    create_rule(limited, frozen)
    assert usage(fixture) == (0, 0, 1)


def test_generated_expense_occurrence_and_rule_progress_consume_one_business_write(recurring):
    fixture, frozen = recurring
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    create_rule(limited, frozen)
    asyncio.run(limited.batch([
        fence(limited, frozen),
        limited.prepare("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at) VALUES(
            'expense','owner','shared','category','2026-10-08',1250,'USD','Hosting','local','local')"""),
        limited.prepare("INSERT INTO expense_recurring_occurrences VALUES('rule','owner','2026-10',"
                        "'2026-10-08','expense',1,'local')"),
        limited.prepare("UPDATE expense_recurring_rules SET revision=revision+1 WHERE id='rule' AND owner_id='owner'"),
        limited.prepare("DELETE FROM d1_command_guard"),
    ]))
    assert usage(fixture) == (0, 1, 2)


@pytest.mark.parametrize("status", ["paused", "canceled"])
def test_stopping_rule_retains_quota_but_active_edits_still_require_it(recurring, status):
    fixture, frozen = recurring
    policy = default_policy()
    policy["pro"].update(writesPerMinute=1, writesPerMonth=1)
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    create_rule(limited, frozen)
    asyncio.run(limited.batch([fence(limited, frozen),
        limited.prepare("UPDATE expense_recurring_rules SET status='" + status +
                        "', revision=revision+1, updated_at='local' WHERE id='rule' AND owner_id='owner'"),
        limited.prepare("DELETE FROM d1_command_guard")]))
    assert usage(fixture) == (0, 0, 1)
    with pytest.raises(PlanLimitError, match="WRITE_RATE_LIMIT"):
        asyncio.run(limited.batch([fence(limited, frozen),
            limited.prepare("UPDATE expense_recurring_rules SET status='active', revision=revision+1 "
                            "WHERE id='rule' AND owner_id='owner'"),
            limited.prepare("DELETE FROM d1_command_guard")]))
    with fixture.store.connect() as db:
        assert db.execute("SELECT status,revision FROM expense_recurring_rules").fetchone()[:] == (status, 2)


def test_generated_record_capacity_denial_commits_no_partial_occurrence(recurring):
    fixture, frozen = recurring
    policy = default_policy()
    policy["pro"]["records"] = 1
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    create_rule(limited, frozen)
    with fixture.store.connect() as db:
        db.execute("UPDATE ledger_plan_usage SET records=1 WHERE owner_id='owner'")
    with pytest.raises(PlanLimitError, match="RECORD_LIMIT"):
        asyncio.run(limited.batch([fence(limited, frozen),
            limited.prepare("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
                amount_minor,currency,purpose,created_at,updated_at) VALUES(
                'expense','owner','shared','category','2026-10-08',1,'USD','Hosting','local','local')"""),
            limited.prepare("INSERT INTO expense_recurring_occurrences VALUES('rule','owner','2026-10',"
                            "'2026-10-08','expense',1,'local')"),
            limited.prepare("DELETE FROM d1_command_guard")]))
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM expense_recurring_occurrences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0
    assert usage(fixture) == (0, 1, 1)


def test_rule_replay_json_expands_only_the_reviewed_parameter(recurring):
    sql = "INSERT INTO expense_recurring_rules(id,create_response_json,template_json) VALUES(?,?,?)"
    response = json.dumps({"note": "x" * 9000})
    assert _input_bound(("rule", response, "{}"), sql=sql) == 0
    for params in (("rule", "[]", "{}"), ("rule", response, response),
                   ("rule", json.dumps({"note": "x" * 16384}), "{}")):
        with pytest.raises(ValueError):
            _input_bound(params, sql=sql)
    assert sql_write_bound("INSERT INTO expense_recurring_rules(id) VALUES(?)") == 1 + INDEX_COUNTS["expense_recurring_rules"] == 6
    assert sql_write_bound("UPDATE expense_recurring_rules SET status=? WHERE id=? AND owner_id=?") == 11
    assert sql_write_bound("INSERT INTO expense_recurring_occurrences(rule_id) VALUES(?)") == 3
    with pytest.raises(ValueError):
        sql_write_bound("UPDATE expense_recurring_occurrences SET expense_id=? WHERE rule_id=?")
