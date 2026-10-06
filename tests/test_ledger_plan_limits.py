import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed, seed_auth
from pullwise_server.ledger_plan_policy import default_policy, parse_policy, entitlements
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError


def test_defaults_override_and_max_only_jev():
    policy = default_policy()
    assert [policy[p]["projects"] for p in ("free", "pro", "max")] == [3, 100, 100]
    assert policy["pro"]["records"] == policy["max"]["records"] == 20_000
    assert policy["free"]["records"] == 500
    assert policy["max"]["jevMonthlyBudgetUsd"] == "5.00"
    changed = parse_policy(json.dumps({"free": {"projects": 4}}))
    assert changed["free"]["projects"] == 4 and default_policy()["free"]["projects"] == 3
    for raw in ({"free": {"jevMonthlyBudgetUsd": "1.00"}}, {"pro": {"jevMonthlyBudgetUsd": "1"}},
                {"max": {"records": True}}, {"max": {"recrods": 10}}):
        with pytest.raises(ValueError):
            parse_policy(json.dumps(raw))
    with pytest.raises(ValueError):
        parse_policy(json.dumps({"pro": {"records": 30000}}))
    expired = {"billing": {"plan": "max", "status": "active", "currentPeriodEnd": 9}}
    assert entitlements(expired, now=10, policy=policy)["plan"] == "free"


@pytest.fixture
def setup(tmp_path):
    fixture, _, frozen = seed(tmp_path / "plans.db")
    seed_auth(fixture)
    with fixture.store._immediate() as db:
        db.executescript((Path(__file__).resolve().parents[1] /
                          "cloudflare/server/migrations/0004_ledger_plan_usage.sql").read_text())
    return fixture, frozen


def write(binding, frozen, fixture, number, *, kind="project", now=None):
    fence = binding.prepare("""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
        EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?) THEN 1 ELSE 0 END)""").bind("owner", frozen)
    if kind == "project":
        change = binding.prepare("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,
            github_full_name,created_at,updated_at) VALUES(?,?,?,?,?,?)""").bind(
                "p" + str(number), "owner", number + 1, "o/repo" + str(number), "local", "local")
    elif kind == "record":
        change = binding.prepare("""INSERT INTO expenses(id,owner_id,target_kind,category_id,
            occurred_on,amount_minor,currency,purpose,created_at,updated_at)
            VALUES(?,?,'shared','category','2026-09-28',1,'USD','test','local','local')""").bind(
                "e" + str(number), "owner")
    elif kind == "jev":
        change = binding.prepare("INSERT INTO expense_suggestion_budget(owner_id,day,attempts) VALUES(?,?,1)").bind(
            "owner", str(number))
    else:
        change = binding.prepare("UPDATE ledger_projects SET description=? WHERE id=?").bind(str(number), "p1")
    binding.now = fixture.now if now is None else now
    return asyncio.run(binding.batch([fence, change, binding.prepare("DELETE FROM d1_command_guard")]))


def test_atomic_project_limit_and_config_change_do_not_reset_usage(setup):
    fixture, frozen = setup
    policy = default_policy()
    policy["pro"]["projects"] = 1
    raw = D1ShapedSQLite(fixture.store)
    limited = PlanLimitedD1(raw, policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1)
    with pytest.raises(PlanLimitError, match="PROJECT_LIMIT"):
        write(limited, frozen, fixture, 2)
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_projects").fetchone()[0] == 1
        assert db.execute("SELECT writes FROM ledger_plan_usage").fetchone()[0] == 1
    policy["pro"]["projects"] = 2
    write(PlanLimitedD1(raw, policy=policy, now=fixture.now), frozen, fixture, 3)
    with fixture.store.connect() as db:
        assert db.execute("SELECT projects,writes FROM ledger_plan_usage").fetchone()[:] == (2, 2)


def test_account_rate_and_monthly_cap_are_shared_and_reset_lazily(setup):
    fixture, frozen = setup
    policy = default_policy()
    policy["pro"].update(writesPerMinute=1, writesPerMonth=2)
    write(PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now), frozen, fixture, 1)
    with pytest.raises(PlanLimitError, match="WRITE_RATE_LIMIT"):
        write(PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now), frozen, fixture, 2)
    write(PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now+60), frozen, fixture, 3, now=fixture.now+60)
    with pytest.raises(PlanLimitError, match="MONTHLY_WRITE_LIMIT"):
        write(PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now+120), frozen, fixture, 4, now=fixture.now+120)


def test_concurrent_creations_cannot_overfill_project_slots(setup):
    fixture, frozen = setup
    policy = default_policy()
    policy["pro"]["projects"] = 1
    def attempt(number):
        try:
            write(PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now), frozen, fixture, number)
            return True
        except PlanLimitError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [1, 2])) == [False, True]


def test_jev_is_max_only_and_failed_transaction_does_not_spend(setup):
    fixture, frozen = setup
    with fixture.store._immediate() as db:
        db.executescript((Path(__file__).resolve().parents[1] /
                          "cloudflare/server/migrations/0003_ledger_suggestions.sql").read_text())
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=default_policy(), now=fixture.now)
    with pytest.raises(PlanLimitError, match="MAX_REQUIRED"):
        write(limited, frozen, fixture, 1, kind="jev")
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expense_suggestion_budget").fetchone()[0] == 0


def test_monthly_jev_reservation_is_no_rollover_and_survives_failure(setup):
    fixture, _ = setup
    with fixture.store._immediate() as db:
        db.executescript((Path(__file__).resolve().parents[1] /
                          "cloudflare/server/migrations/0003_ledger_suggestions.sql").read_text())
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])["owner"]
        user["billing"] = {"plan": "max", "status": "active"}
        frozen = json.dumps(user, separators=(",", ":"))
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps({"owner": user}),))
    policy = default_policy()
    policy["max"]["jevMonthlyBudgetUsd"] = "0.002753"
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1, kind="jev")
    with pytest.raises(PlanLimitError, match="JEV_BUDGET_LIMIT"):
        write(limited, frozen, fixture, 2, kind="jev")
    # Calendar month change, not invoice interval or a maintenance cron.
    from datetime import datetime, timezone
    now = int(datetime(2027, 2, 1, tzinfo=timezone.utc).timestamp())
    write(limited, frozen, fixture, 3, kind="jev", now=now)
    with fixture.store.connect() as db:
        row = db.execute("SELECT month,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(row) == ("2027-02", 2753)  # Not unused old budget + new budget.


def test_deleted_records_count_and_failed_audit_rolls_back_quota(setup):
    fixture, frozen = setup
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) VALUES('category','owner','c','local','local')")
    policy = default_policy()
    policy["pro"]["records"] = 1
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1, kind="record")
    with fixture.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='local' WHERE id='e1'")
    with pytest.raises(PlanLimitError, match="RECORD_LIMIT"):
        write(limited, frozen, fixture, 2, kind="record")
    with fixture.store.connect() as db:
        assert db.execute("SELECT records,writes FROM ledger_plan_usage").fetchone()[:] == (1, 1)

    # Failed credential proof must roll back the quota and the mutation.
    with pytest.raises(Exception):
        write(limited, frozen.replace('"owner"', '"intruder"'), fixture, 3)
    with fixture.store.connect() as db:
        assert db.execute("SELECT projects,records,writes FROM ledger_plan_usage").fetchone()[:] == (0, 1, 1)


def test_late_request_cannot_roll_counters_back_to_an_old_window(setup):
    fixture, frozen = setup
    policy = default_policy()
    policy["pro"]["writesPerMinute"] = 1
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1, now=fixture.now+60)
    with pytest.raises(PlanLimitError, match="QUOTA_WINDOW_CHANGED"):
        write(limited, frozen, fixture, 2, now=fixture.now)
    with pytest.raises(PlanLimitError, match="WRITE_RATE_LIMIT"):
        write(limited, frozen, fixture, 3, now=fixture.now+60)


def test_http_quota_errors_and_idempotent_replays_preserve_account_usage(setup):
    from pullwise_server.cloudflare_ledger_api import handle_ledger_request
    fixture, _ = setup
    policy = default_policy()
    policy["pro"]["records"] = 1
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    def call(method, path, body=None, **headers):
        return asyncio.run(handle_ledger_request(binding=limited, gateway=None, method=method, path=path,
            body=body, headers={"Cookie": "pw_session=session-local", **headers}, params={}, now=fixture.now))
    status, category = call("POST", "/api/v1/categories", {"name": "c"})
    assert status == 201
    draft = {"target": {"kind": "shared"}, "occurredOn": "2026-09-28", "amount": "1",
             "currency": "USD", "categoryId": category["id"], "purpose": "test"}
    status, record = call("POST", "/api/v1/expenses", draft, **{"Idempotency-Key": "first"})
    assert status == 201
    assert call("POST", "/api/v1/expenses", draft, **{"Idempotency-Key": "first"})[1]["id"] == record["id"]
    assert call("POST", "/api/v1/expenses", draft, **{"Idempotency-Key": "second"}) == (
        403, {"error": {"code": "RECORD_LIMIT"}})
    assert call("DELETE", "/api/v1/expenses/"+record["id"], **{"If-Match": '"1"'})[0] == 204
    assert call("DELETE", "/api/v1/expenses/"+record["id"], **{"If-Match": '"1"'})[0] == 204
    with fixture.store.connect() as db:
        assert tuple(db.execute("SELECT records,writes FROM ledger_plan_usage").fetchone()) == (1, 3)


def test_key_revocation_is_not_blocked_by_an_exhausted_write_allowance(setup):
    from pullwise_server.cloudflare_api_key_write import revoke_api_key
    fixture, frozen = setup
    policy = default_policy()
    policy["pro"]["writesPerMonth"] = 1
    limited = PlanLimitedD1(D1ShapedSQLite(fixture.store), policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1)
    assert asyncio.run(revoke_api_key(binding=limited, key_id="key-local",
        headers={"Cookie": "pw_session=session-local"}, now=fixture.now))[0] == 200
    with fixture.store.connect() as db:
        assert db.execute("SELECT revoked_at FROM api_keys WHERE id='key-local'").fetchone()[0] == fixture.now
        assert db.execute("SELECT writes FROM ledger_plan_usage").fetchone()[0] == 1


@pytest.mark.parametrize("allowance,kind,code", [
    ("projects", "project", "PROJECT_LIMIT"),
    ("records", "record", "RECORD_LIMIT"),
    ("writesPerMinute", "project", "WRITE_RATE_LIMIT"),
    ("writesPerMonth", "project", "MONTHLY_WRITE_LIMIT"),
])
def test_known_commercial_exhaustion_never_dispatches_a_failing_batch(setup, allowance, kind, code):
    fixture, frozen = setup
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) VALUES('category','owner','c','local','local')")
    policy = default_policy()
    policy["pro"][allowance] = 1
    raw = D1ShapedSQLite(fixture.store)
    limited = PlanLimitedD1(raw, policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1, kind=kind)
    batches = raw.batch_count
    with pytest.raises(PlanLimitError, match=code):
        write(limited, frozen, fixture, 2, kind=kind)
    assert raw.batch_count == batches


def test_initial_capacity_check_includes_legacy_archived_projects(setup):
    fixture, frozen = setup
    with fixture.store._immediate() as db:
        db.execute("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,
            status,created_at,updated_at) VALUES('old','owner',100,'o/old','archived','local','local')""")
    policy = default_policy()
    policy["pro"]["projects"] = 1
    raw = D1ShapedSQLite(fixture.store)
    with pytest.raises(PlanLimitError, match="PROJECT_LIMIT"):
        write(PlanLimitedD1(raw, policy=policy, now=fixture.now), frozen, fixture, 1)
    assert raw.batch_count == 0
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0


def test_internal_jev_reservation_survives_lowered_business_write_caps(setup):
    fixture, _ = setup
    with fixture.store._immediate() as db:
        db.executescript((Path(__file__).resolve().parents[1] /
                          "cloudflare/server/migrations/0003_ledger_suggestions.sql").read_text())
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])["owner"]
        user["billing"] = {"plan": "max", "status": "active"}
        frozen = json.dumps(user, separators=(",", ":"))
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps({"owner": user}),))
    policy = default_policy()
    raw = D1ShapedSQLite(fixture.store)
    limited = PlanLimitedD1(raw, policy=policy, now=fixture.now)
    write(limited, frozen, fixture, 1)
    write(limited, frozen, fixture, 2)
    policy["max"].update(writesPerMinute=1, writesPerMonth=1)
    write(limited, frozen, fixture, 3, kind="jev")
    with fixture.store.connect() as db:
        assert tuple(db.execute("SELECT writes,minute_writes,jev_reserved_microusd FROM ledger_plan_usage").fetchone()) == (2, 2, 2753)
    batches = raw.batch_count
    with pytest.raises(PlanLimitError, match="WRITE_RATE_LIMIT"):
        write(limited, frozen, fixture, 4)
    assert raw.batch_count == batches
