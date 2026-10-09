"""Independent permanent-erasure boundary tests over real isolated transactions.

No provider, native runtime or remote database is used. In particular, retained
period fences are exercised through the actual DELETE and recurring runner.
"""
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pullwise_server.cloudflare_ledger_api import _write_guard, handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitError, PlanLimitedD1
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, _input_bound, initial_data
from pullwise_server.cloudflare_project_erasure import (
    owner_key_guard_sql, recipe_identity, recipe_tail,
)
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal
from test_d1_validation_budget import LocalSql, RawD1
from test_ledger_recurring import NOW, NoGitHub, SafeD1, app, draft


BUSINESS = (
    "ledger_projects", "ledger_project_repositories", "expenses", "expense_events",
    "expense_create_idempotency", "expense_recurring_rules",
    "expense_recurring_occurrences", "expense_suggestion_events",
    "ledger_activity_events", "ledger_plan_usage", "d1_command_guard",
)


def facts(app, tables=BUSINESS):
    with closing(app.store.connect()) as db:
        return {table: sorted(tuple(row) for row in db.execute("SELECT * FROM " + table))
                for table in tables}


def call(app, method, path, body=None, *, revision=None, actor="owner", headers=None,
         binding=None, now=NOW):
    auth = {"Cookie": "pw_session=" + actor, "Origin": "https://app.example.test"}
    if actor != "owner":
        auth["X-Pullwise-Workspace"] = "owner"
    if revision is not None:
        auth["If-Match"] = f'"{revision}"'
    auth.update(headers or {})
    return asyncio.run(handle_ledger_request(
        binding=binding or PlanLimitedD1(app.raw, policy=app.policy, now=now),
        gateway=NoGitHub(), method=method, path=path, headers=auth, params={},
        body=body, now=now))


def erase(app, **options):
    return call(app, "DELETE", "/api/v1/projects/prj_1", revision=1, **options)


def save(app, identifier, *, project=None):
    body = {"target": {"kind": "project", "projectId": project} if project else {"kind": "shared"},
            "categoryId": "cat_1", "purpose": identifier, "amount": "12.34",
            "currency": "USD", "occurredOn": "2026-10-06"}
    status, expense = call(app, "POST", "/api/v1/expenses", body,
                          headers={"Idempotency-Key": identifier})
    assert status == 201, expense
    return expense, body


def seed_other_project(app):
    with closing(app.store.connect()) as db:
        db.execute("""INSERT INTO ledger_projects(id,owner_id,name,description,status,
            revision,created_at,updated_at) VALUES('prj_other','owner','Retained project',
            '','active',1,'created','updated')""")
        db.commit()


@pytest.mark.parametrize("source_scope", ["shared", "other-project"])
def test_actual_erasure_preserves_external_consumed_month_after_generated_expense_moves_in(app, source_scope):
    target = {"kind": "shared"}
    if source_scope == "other-project":
        seed_other_project(app)
        target = {"kind": "project", "projectId": "prj_other"}
    body = draft(target=target, schedule={"frequency": "monthly", "day": 1,
        "timezone": "UTC", "startOn": "2026-10-01"})
    status, original = app.call("POST", body=body)
    assert status == 201
    assert app.tick() == {"scanned": 1, "created": 1, "blocked": 0, "replayed": 0}
    expense = app.rows("expenses")[0]
    occurrence = app.rows("expense_recurring_occurrences")[0]
    assert occurrence["period_key"] == "M2026-10"
    with closing(app.store.connect()) as db:
        db.execute("UPDATE expenses SET target_kind='project',project_id='prj_1',revision=revision+1 "
                   "WHERE id=?", (expense["id"],))
        db.commit()
    rule_before = app.rows("expense_recurring_rules")[0]
    assert erase(app) == (204, None)
    assert app.rows("expenses") == []
    assert app.rows("expense_recurring_rules") == [rule_before]
    assert app.rows("expense_recurring_occurrences") == [{**occurrence, "expense_id": None}]
    status, edited = app.call("PATCH", original["id"],
        {**body, "schedule": {**body["schedule"], "day": 20}}, revision=rule_before["revision"])
    assert status == 200 and edited["nextOccurrenceOn"] == "2026-10-20"
    later = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=later) == {"scanned": 1, "created": 0, "blocked": 0, "replayed": 1}
    assert app.rows("expenses") == []
    assert app.rows("expense_recurring_rules")[0]["next_occurrence_on"] == "2026-11-20"
    assert app.rows("expense_recurring_occurrences")[0]["expense_id"] is None
    with closing(app.store.connect()) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_current_targets_define_erased_expenses_but_historical_project_snapshots_are_erased(app):
    seed_other_project(app)
    moved_out, out_body = save(app, "origin-in-P", project="prj_1")
    moved_in, in_body = save(app, "origin-in-Shared")
    unrelated, _ = save(app, "unrelated-Q", project="prj_other")
    status, moved_out = call(app, "PATCH", "/api/v1/expenses/" + moved_out["id"],
        {**out_body, "target": {"kind": "shared"}}, revision=1)
    assert status == 200
    status, moved_out = call(app, "PATCH", "/api/v1/expenses/" + moved_out["id"],
        {**out_body, "target": {"kind": "shared"}, "purpose": "Fresh Shared history"}, revision=2)
    assert status == 200
    status, moved_in = call(app, "PATCH", "/api/v1/expenses/" + moved_in["id"],
        {**in_body, "target": {"kind": "project", "projectId": "prj_1"}}, revision=1)
    assert status == 200
    before = {row["id"]: row for row in app.rows("expenses")}
    retained_replay = next(row for row in app.rows("expense_create_idempotency")
                           if row["expense_id"] == unrelated["id"])
    retained_event = next(row for row in app.rows("expense_events")
                          if row["expense_id"] == moved_out["id"] and row["after_json"]
                          and json.loads(row["after_json"])["revision"] == 3)
    assert erase(app) == (204, None)
    assert {row["id"]: row for row in app.rows("expenses")} == {
        identifier: before[identifier] for identifier in (moved_out["id"], unrelated["id"])}
    assert app.rows("expense_create_idempotency") == [retained_replay]
    assert retained_event in app.rows("expense_events")
    for table, columns in (("expense_events", ("before_json", "after_json")),
                           ("expense_create_idempotency", ("response_json",)),
                           ("ledger_activity_events", ("before_json", "after_json"))):
        for row in app.rows(table):
            assert row.get("expense_id") != moved_in["id"]
            assert row.get("project_id") != "prj_1"
            for column in columns:
                if row[column]:
                    assert json.loads(row[column]).get("target") != {"kind": "project", "projectId": "prj_1"}
    assert {row["id"] for row in app.rows("ledger_projects")} == {"prj_other"}
    assert app.rows("ledger_plan_usage")[0]["projects"] == 2
    assert app.rows("ledger_plan_usage")[0]["records"] == 2


def test_saved_review_source_attribution_erases_moved_in_shared_review_only(app):
    selected, _ = save(app, "selected", project="prj_1")
    retained, _ = save(app, "retained")
    with closing(app.store.connect()) as db:
        for identifier, owner, source, draft_project in (
            ("sg_selected", "owner", selected["id"], None),
            ("sg_retained", "owner", retained["id"], None),
            ("sg_legacy_shared", "owner", None, None),
            ("sg_foreign_same_project_string", "other", None, "prj_1"),
        ):
            db.execute("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,
                question_version,draft_target_kind,draft_project_id,outcome,recorded_expense_id)
                VALUES(?,?,'created','review','shared',?,'unavailable',?)""",
                (identifier, owner, draft_project, source))
        db.execute("INSERT INTO expense_suggestion_budget VALUES('owner','2026-10-08',7)")
        db.execute("UPDATE ledger_plan_usage SET jev_reserved_microusd=123456,jev_delta=123456")
        db.commit()
    security_before = facts(app, ("app_state", "workspace_members", "expense_categories", "expense_suggestion_budget"))
    usage_before = app.rows("ledger_plan_usage")[0]
    assert erase(app) == (204, None)
    assert {row["id"] for row in app.rows("expense_suggestion_events")} == {
        "sg_retained", "sg_legacy_shared", "sg_foreign_same_project_string"}
    assert facts(app, security_before) == security_before
    usage_after = app.rows("ledger_plan_usage")[0]
    assert usage_after["jev_reserved_microusd"] == 123456
    assert usage_after["writes"] == usage_before["writes"] + 1
    assert usage_after["minute_writes"] == usage_before["minute_writes"] + 1
    assert usage_after["project_delta"] == usage_after["record_delta"] == usage_after["jev_delta"] == 0


@pytest.mark.parametrize("limit,code", [("writesPerMinute", "WRITE_RATE_LIMIT"),
                                       ("writesPerMonth", "MONTHLY_WRITE_LIMIT")])
def test_exhausted_commercial_writes_never_partially_erase_or_initialize_usage(app, limit, code):
    app.policy["pro"][limit] = 0
    before = facts(app)
    assert erase(app) == (429, {"error": {"code": code}})
    assert facts(app) == before


def test_capacity_zero_after_paid_downgrade_still_allows_one_charged_erasure_with_lazy_usage(app):
    with closing(app.store.connect()) as db:
        key = record_name("users", "owner")
        account = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (key,)).fetchone()[0])
        account["billing"]["currentPeriodEnd"] = NOW - 1
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", "owner", account), key))
        db.commit()
    app.policy["free"]["projects"] = app.policy["free"]["records"] = 0
    assert app.rows("ledger_plan_usage") == []
    assert erase(app) == (204, None)
    usage = app.rows("ledger_plan_usage")[0]
    assert (usage["projects"], usage["records"], usage["writes"], usage["minute_writes"], usage["jev_cap"]) == (1, 0, 1, 1, 0)
    before = facts(app)
    assert erase(app) == (204, None)
    assert facts(app) == before


def test_cumulative_project_counter_below_existing_projects_fails_closed_before_cleanup(app):
    save(app, "selected", project="prj_1")
    with closing(app.store.connect()) as db:
        db.execute("UPDATE ledger_plan_usage SET projects=0")
        db.commit()
    before = facts(app)
    assert erase(app) == (503, {"error": {"code": "USAGE_GUARD_UNAVAILABLE"}})
    assert facts(app) == before


@pytest.mark.parametrize("race", ["minute", "month", "quota"])
def test_quota_or_clock_race_after_preflight_rolls_back_all_children(app, race):
    save(app, "selected", project="prj_1")

    class RaceUsage(SafeD1):
        raced = False

        async def batch(self, commands):
            commands = list(commands)
            if len(commands) == 17 and not self.raced:
                self.raced = True
                with self.store._immediate() as db:
                    if race == "minute":
                        db.execute("UPDATE ledger_plan_usage SET minute=minute+1,previous_minute=minute+1")
                    elif race == "month":
                        db.execute("UPDATE ledger_plan_usage SET month='2026-11',previous_month='2026-11'")
                    else:
                        db.execute("UPDATE ledger_plan_usage SET minute_writes=minute_cap")
                self.expected = facts(app)
            return await super().batch(commands)

    raw = RaceUsage(app.store)
    with pytest.raises(sqlite3.IntegrityError, match="plan_clock_fence|plan_write_rate"):
        erase(app, binding=PlanLimitedD1(raw, policy=app.policy, now=NOW))
    assert raw.raced
    assert facts(app) == raw.expected


@pytest.mark.parametrize("headers", [
    {"Cookie": "pw_session=other", "X-Pullwise-Workspace": "other"},
    {"X-Pullwise-Workspace": "other"},
    {"Authorization": "Bearer owner"},
    {"X-Pullwise-Api-Key": "not-an-owner-cookie"},
])
def test_selected_workspace_or_mixed_credentials_cannot_turn_actor_into_project_owner(app, headers):
    before = facts(app)
    status, _ = erase(app, headers=headers)
    assert status in (400, 401, 403, 404)
    assert facts(app) == before


def statements(app, *, user_extra=None):
    with closing(app.store.connect()) as db:
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("users", "owner"),)).fetchone()[0])
        session = db.execute("SELECT payload FROM app_state WHERE name=?", (record_name("sessions", "owner"),)).fetchone()[0]
    if user_extra:
        user.update(user_extra)
    user_json = encode_record("users", "owner", user)
    commands = [_write_guard(app.raw, {"user": user_json, "sessions": session, "session_id": "owner"}, "owner", NOW)]
    commands.extend(app.raw.prepare(sql).bind(*params) for sql, params in
                    recipe_tail("owner", "prj_1", 1, user, NOW, app.policy))
    return commands


@pytest.mark.parametrize("mutation", ["omit-owner", "omit-project", "omit-usage", "omit-changed",
    "omit-final", "omit-cleanup", "swap-child", "repeat-parent", "extra-select",
    "project-binding", "owner-binding", "revision-binding", "clock", "policy"])
def test_closed_admission_rejects_any_changed_sequence_or_fixed_binding_before_dispatch(app, mutation):
    commands = statements(app)
    assert recipe_identity(commands, policy=app.policy, now=NOW) == ("owner", "prj_1", 1)
    indices = {"omit-owner": 0, "omit-project": 1, "omit-usage": 2,
               "omit-changed": 3, "omit-final": 15, "omit-cleanup": 16}
    if mutation in indices:
        commands.pop(indices[mutation])
    elif mutation == "swap-child":
        commands[4], commands[5] = commands[5], commands[4]
    elif mutation == "repeat-parent":
        commands[12] = commands[13]
    elif mutation == "extra-select":
        commands.append(app.raw.prepare("SELECT 1"))
    elif mutation in {"project-binding", "owner-binding", "revision-binding"}:
        params = list(commands[1].params)
        index = {"owner-binding": 0, "project-binding": 1, "revision-binding": 2}[mutation]
        params[index] = {0: "other", 1: "prj_other", 2: 2}[index]
        commands[1] = app.raw.prepare(commands[1].sql).bind(*params)
    elif mutation == "clock":
        # An internally consistent recipe at another clock is still rejected
        # by the trusted PlanLimitedD1 clock rather than its submitted values.
        user = json.loads(commands[0].params[1])
        params = (*commands[0].params[:-1], NOW + 1)
        commands[0] = app.raw.prepare(commands[0].sql).bind(*params)
        commands[1:] = [app.raw.prepare(sql).bind(*values) for sql, values in
            recipe_tail("owner", "prj_1", 1, user, NOW + 1, app.policy)]
    else:
        policy = {plan: dict(limits) for plan, limits in app.policy.items()}
        policy["pro"]["writesPerMonth"] += 1
        user = json.loads(commands[0].params[1])
        commands[1:] = [app.raw.prepare(sql).bind(*params) for sql, params in
            recipe_tail("owner", "prj_1", 1, user, NOW, policy)]
    assert recipe_identity(commands, policy=app.policy, now=NOW) is None
    before = facts(app)
    calls = app.raw.batch_count
    with pytest.raises(PlanLimitError, match="USAGE_GUARD_UNAVAILABLE"):
        asyncio.run(PlanLimitedD1(app.raw, policy=app.policy, now=NOW).batch(commands))
    assert app.raw.batch_count == calls
    assert facts(app) == before


def test_raw_owner_proof_keeps_typed_user_envelope_without_widening_plain_input(app):
    commands = statements(app, user_extra={"retainedNote": "x" * 10000})
    assert recipe_identity(commands, policy=app.policy, now=NOW) == ("owner", "prj_1", 1)
    _input_bound(commands[0].params, sql=commands[0].sql)
    with pytest.raises(ValueError, match="input bound"):
        _input_bound(("x" * 10000,), sql="SELECT ?")


@pytest.mark.parametrize("mutation", ["missing-usage", "missing-cleanup", "changed-owner", "reordered-children", "extra-query"])
def test_global_meter_never_dispatches_incomplete_or_rebound_bulk_recipe(app, mutation):
    commands = statements(app)
    if mutation == "missing-usage":
        commands.pop(2)
    elif mutation == "missing-cleanup":
        commands.pop()
    elif mutation == "changed-owner":
        commands[7] = app.raw.prepare(commands[7].sql).bind(*("other" if value == "owner" else value for value in commands[7].params))
    elif mutation == "reordered-children":
        commands[4], commands[5] = commands[5], commands[4]
    else:
        commands.append(app.raw.prepare("SELECT 1"))
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=10)
        raw = RawD1()
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 11)
        with pytest.raises(BudgetError, match="UNREVIEWED_SQL"):
            asyncio.run(meter.batch([meter.prepare(item.sql).bind(*item.params) for item in commands]))
        assert raw.calls == 0
        assert journal.snapshot()["stopped"] == "UNREVIEWED_SQL_BOUND"


@pytest.mark.parametrize("failure,reason", [
    ({"meta": False}, "METERING_MISSING"),
    ({"failure": TimeoutError()}, "D1_OUTCOME_UNKNOWN"),
    ({"writes": 100000}, "USAGE_EXCEEDED_BOUND"),
])
def test_recipe_unknown_native_outcome_preserves_reservation_and_never_refreshes_or_retries(app, failure, reason):
    commands = statements(app)
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        journal = BudgetJournal(sql, preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=10)
        raw = RawD1(**failure)
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 11)
        with pytest.raises(BudgetError, match=reason):
            asyncio.run(meter.batch([meter.prepare(item.sql).bind(*item.params) for item in commands]))
        stopped = journal.snapshot()
        assert raw.calls == 1 and not meter.accounted_outcome()
        assert stopped["active"] == ticket and stopped["stopped"] == reason
        assert stopped["reserved_read"] > 0 and stopped["reserved_written"] > 0
        restarted = BudgetJournal(sql, preview_product=True, product_operations=True)
        assert restarted.snapshot() == stopped
        with pytest.raises(BudgetError, match=reason):
            restarted.begin_product(now=13)
        assert raw.calls == 1


@pytest.mark.parametrize("failure", ["guard", "unknown"])
def test_late_failure_rolls_back_every_child_and_usage_and_unknown_error_propagates(app, failure):
    selected, _ = save(app, "selected", project="prj_1")
    before = facts(app)

    class FailLate(SafeD1):
        async def batch(self, commands):
            commands = list(commands)
            if len(commands) == 17 and commands[13].sql.startswith("DELETE FROM ledger_projects"):
                # Emulate D1's atomic rollback after the full child cleanup.
                with self.store._immediate() as db:
                    for item in commands[:15]:
                        db.execute(item.sql, item.params)
                    assert db.execute("SELECT COUNT(*) FROM expenses WHERE id=?", (selected["id"],)).fetchone()[0] == 0
                    if failure == "unknown":
                        raise RuntimeError("D1_OUTCOME_UNKNOWN")
                    db.execute("INSERT INTO d1_command_guard(ok) VALUES(0)")
            return await super().batch(commands)

    binding = PlanLimitedD1(FailLate(app.store), policy=app.policy, now=NOW)
    if failure == "unknown":
        with pytest.raises(RuntimeError, match="D1_OUTCOME_UNKNOWN"):
            erase(app, binding=binding)
    else:
        assert erase(app, binding=binding) == (412, {"error": {"code": "PRECONDITION_FAILED"}})
    assert facts(app) == before


def key_statements(app, *, user_extra=None, scopes=None, restrictions=None, now=NOW):
    """Use the real Owner guard generator with a synthetic saved-key proof."""
    with closing(app.store.connect()) as db:
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", "owner"),)).fetchone()[0])
    user.update(user_extra or {})
    proof = {"user": encode_record("users", "owner", user),
        "token": "pwk_independent-owner-erasure-admission",
        "key": {"scopes": json.dumps(scopes if scopes is not None else ["projects:write"]),
            "restrictions": json.dumps(restrictions if restrictions is not None else
                {"shared": False, "workspaceId": "owner", "projectIds": ["prj_1"]})}}
    commands = [_write_guard(app.raw, proof, "owner", now)]
    commands.extend(app.raw.prepare(sql).bind(*params) for sql, params in
        recipe_tail("owner", "prj_1", 1, user, now, app.policy))
    return commands


class AdmissionSpy:
    """Record quota-wrapper admission without executing a business mutation."""
    def __init__(self):
        self.batches = []

    def prepare(self, sql):
        return SimpleNamespace(bind=lambda *params: SimpleNamespace(sql=sql, params=params))

    async def batch(self, commands):
        self.batches.append(list(commands))
        return []


@pytest.mark.parametrize("raw_note_bytes", [0, 10000])
def test_owner_bound_key_admits_only_the_complete_paid_recipe_and_preserves_raw_envelope(app, raw_note_bytes):
    extra = {"retainedNote": "x" * raw_note_bytes} if raw_note_bytes else None
    commands = key_statements(app, user_extra=extra)
    assert len(commands) == 17 and commands[0].sql == owner_key_guard_sql()
    assert len(commands[0].params) == 7
    assert commands[0].params[2] == hashlib.sha256(
        b"pwk_independent-owner-erasure-admission").hexdigest()
    assert recipe_identity(commands, policy=app.policy, now=NOW) == ("owner", "prj_1", 1)
    _input_bound(commands[0].params, sql=commands[0].sql)
    if raw_note_bytes:
        assert len(commands[0].params[1].encode()) > 8192
        with pytest.raises(ValueError, match="input bound"):
            _input_bound((commands[0].params[1],), sql="SELECT ?")
    spy = AdmissionSpy()
    asyncio.run(PlanLimitedD1(spy, policy=app.policy, now=NOW).batch(commands))
    assert len(spy.batches) == 1
    assert [(item.sql, item.params) for item in spy.batches[0]] == [
        (item.sql, item.params) for item in commands]

    # The global meter must admit this same private family and reserve it before
    # the deliberately unacknowledged spy dispatch, never infer a zero cost.
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        state["product_data"]["rows"]["api_keys"] = 1
        journal._save(state)
        ticket = journal.begin_product(now=NOW)
        raw = RawD1(failure=RuntimeError("unacknowledged admission spy"))
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: NOW)
        with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
            asyncio.run(meter.batch([meter.prepare(item.sql).bind(*item.params) for item in commands]))
        saved = journal.snapshot()
        assert raw.calls == 1 and saved["active"] == ticket
        assert saved["reserved_read"] > 0 and saved["reserved_written"] > 0


@pytest.mark.parametrize("mutation", [
    "hash", "actor-owner", "user-record-key", "missing-scope", "unknown-scope",
    "duplicate-scope", "malformed-restrictions", "noncanonical-restrictions",
    "wrong-workspace", "outside-project", "shared-only", "clock-tail",
    "missing-usage", "tail-owner", "reordered-children", "extra-sql",
])
def test_owner_key_private_recipe_rejects_invalid_authority_or_sequence_before_both_dispatches(app, mutation):
    commands = key_statements(app)
    params = list(commands[0].params)
    replacements = {
        "hash": (2, "g" * 64),
        "actor-owner": (3, "editor"),
        "user-record-key": (0, record_name("users", "other")),
        "missing-scope": (4, json.dumps(["projects:read"])),
        "unknown-scope": (4, json.dumps(["projects:write", "unsupported:write"])),
        "duplicate-scope": (4, json.dumps(["projects:write", "projects:write"])),
        "malformed-restrictions": (5, "[]"),
        "noncanonical-restrictions": (5, json.dumps({"projectIds": ["prj_1"]})),
        "wrong-workspace": (5, json.dumps({"shared": False, "workspaceId": "other",
            "projectIds": ["prj_1"]})),
        "outside-project": (5, json.dumps({"shared": False, "projectIds": ["prj_other"]})),
        "shared-only": (5, json.dumps({"shared": True, "projectIds": []})),
        "clock-tail": (6, NOW + 60),
    }
    if mutation in replacements:
        index, value = replacements[mutation]
        params[index] = value
        commands[0] = app.raw.prepare(commands[0].sql).bind(*params)
    elif mutation == "missing-usage":
        commands.pop(2)
    elif mutation == "tail-owner":
        commands[7] = app.raw.prepare(commands[7].sql).bind(*(
            "other" if value == "owner" else value for value in commands[7].params))
    elif mutation == "reordered-children":
        commands[4], commands[5] = commands[5], commands[4]
    else:
        commands.append(app.raw.prepare("SELECT 1"))
    assert recipe_identity(commands, policy=app.policy, now=NOW) is None
    spy = AdmissionSpy()
    with pytest.raises(PlanLimitError, match="USAGE_GUARD_UNAVAILABLE"):
        asyncio.run(PlanLimitedD1(spy, policy=app.policy, now=NOW).batch(commands))
    assert spy.batches == []
    with closing(sqlite3.connect(":memory:")) as connection:
        journal = BudgetJournal(LocalSql(connection), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=NOW)
        raw = RawD1()
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: NOW)
        with pytest.raises(BudgetError, match="UNREVIEWED_SQL_BOUND"):
            asyncio.run(meter.batch([meter.prepare(item.sql).bind(*item.params) for item in commands]))
        assert raw.calls == 0
        saved = journal.snapshot()
        assert saved["active"] == ticket and saved["stopped"] == "UNREVIEWED_SQL_BOUND"
        assert saved["reserved_read"] == saved["reserved_written"] == 0


@pytest.mark.parametrize("change", ["clock", "capacity"])
def test_fully_rebound_key_recipe_cannot_replace_trusted_application_clock_or_limits(app, change):
    commands = key_statements(app, now=NOW + 60 if change == "clock" else NOW)
    if change == "capacity":
        policy = {plan: dict(limits) for plan, limits in app.policy.items()}
        policy["pro"]["records"] += 1
        user = json.loads(commands[0].params[1])
        commands[1:] = [app.raw.prepare(sql).bind(*params) for sql, params in
            recipe_tail("owner", "prj_1", 1, user, NOW, policy)]
    assert recipe_identity(commands) == ("owner", "prj_1", 1)
    assert recipe_identity(commands, policy=app.policy, now=NOW) is None
    spy = AdmissionSpy()
    with pytest.raises(PlanLimitError, match="USAGE_GUARD_UNAVAILABLE"):
        asyncio.run(PlanLimitedD1(spy, policy=app.policy, now=NOW).batch(commands))
    assert spy.batches == []


@pytest.mark.parametrize("usage_mode", ["legacy-high", "legacy-low", "missing"])
@pytest.mark.parametrize("project_already_removed", [False, True])
def test_erasure_reconciles_active_records_and_retains_cumulative_projects_fees_and_owner_preferences(
        app, usage_mode, project_already_removed):
    seed_other_project(app)
    with closing(app.store.connect()) as db:
        db.execute("""INSERT INTO ledger_projects(id,owner_id,name,description,status,
            revision,created_at,updated_at) VALUES('prj_removed','owner','Hidden project',
            '','active',1,'created','updated')""")
        name = record_name("users", "owner")
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        user.update(jevEnabled=True, jevPreferenceRevision=5, autoRemoveOldestExpense=True,
                    expenseRetentionRevision=9)
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", "owner", user), name))
        db.commit()
    selected, _ = save(app, "P-active", project="prj_1")
    selected_deleted, _ = save(app, "P-soft-deleted", project="prj_1")
    shared, _ = save(app, "Shared-active")
    shared_deleted, _ = save(app, "Shared-soft-deleted")
    archived, _ = save(app, "Archived-project-active", project="prj_other")
    hidden, _ = save(app, "Removed-project-hidden", project="prj_removed")
    for expense in (selected_deleted, shared_deleted):
        assert call(app, "DELETE", "/api/v1/expenses/" + expense["id"], revision=1) == (204, None)
    with closing(app.store.connect()) as db:
        db.execute("UPDATE ledger_projects SET status='archived' WHERE id='prj_other'")
        db.execute("UPDATE ledger_projects SET status='archived',deleted_at='2026-10-08T12:00:00Z' "
                   "WHERE id='prj_removed'")
        if project_already_removed:
            db.execute("UPDATE ledger_projects SET status='archived',deleted_at='2026-10-08T12:00:00Z' "
                       "WHERE id='prj_1'")
        if usage_mode == "missing":
            db.execute("DELETE FROM ledger_plan_usage WHERE owner_id='owner'")
        else:
            db.execute("""UPDATE ledger_plan_usage SET projects=7,records=?,writes=12,
                minute_writes=11,jev_reserved_microusd=12345,project_delta=0,
                record_delta=0,jev_delta=0 WHERE owner_id='owner'""",
                (999 if usage_mode == "legacy-high" else 0,))
        db.commit()
        raw_user_before = db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0]
    expenses_before = {item["id"]: item for item in app.rows("expenses")}
    usage_before = app.rows("ledger_plan_usage")
    security_before = facts(app, ("app_state", "api_keys", "expense_suggestion_budget"))
    assert len(expenses_before) == 6

    assert erase(app) == (204, None)

    assert {item["id"]: item for item in app.rows("expenses")} == {
        expense["id"]: expenses_before[expense["id"]]
        for expense in (shared, shared_deleted, archived, hidden)}
    assert not any(item["expense_id"] in {selected["id"], selected_deleted["id"]}
                   for item in app.rows("expense_events"))
    assert {item["id"] for item in app.rows("ledger_projects")} == {"prj_other", "prj_removed"}
    assert facts(app, ("app_state", "api_keys", "expense_suggestion_budget")) == security_before
    usage = app.rows("ledger_plan_usage")[0]
    # Archived projects remain visible capacity; removed projects and soft
    # deleted Shared expenses stay physically present but consume no slot.
    assert usage["records"] == 2
    assert usage["project_delta"] == usage["record_delta"] == usage["jev_delta"] == 0
    if usage_mode == "missing":
        assert (usage["projects"], usage["writes"], usage["minute_writes"],
                usage["jev_reserved_microusd"]) == (3, 1, 1, 0)
    else:
        assert usage["projects"] == usage_before[0]["projects"] == 7
        assert usage["writes"] == usage_before[0]["writes"] + 1
        assert usage["minute_writes"] == usage_before[0]["minute_writes"] + 1
        assert usage["jev_reserved_microusd"] == usage_before[0]["jev_reserved_microusd"]
    with closing(app.store.connect()) as db:
        assert db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0] == raw_user_before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
