"""Closed one-use preview maintenance; SQLite metadata is synthetic, not native proof."""
import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_business_erasure_schema_upgrade import insert, seed
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_budget import (
    STATE_RECORD_INTEGRITY_VERSION, _COUNT_SQL, _RECORD_COUNT_SQL, _record_product_data,
    sql_write_bound,
)
from pullwise_server.cloudflare_preview_business_clear import (
    ACTION_ID, BUSINESS_TABLES, CLEAR_SQL, EXPECTED_MANIFEST, MARKER_KEY,
    _compile_plan, business_clear_pending, business_clear_receipt, clear_preview_business,
)
from pullwise_server.cloudflare_preview_schema import (
    INDEX_COUNTS, SCHEMA_FINGERPRINT, SCHEMA_SQL, SCHEMA_VERSION,
)
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal


def armed(**changes):
    return SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
        PULLWISE_PREVIEW_PRODUCT_ENABLED="1", PULLWISE_PREVIEW_BUSINESS_CLEAR_ID=ACTION_ID,
        PULLWISE_PREVIEW_BUSINESS_CLEAR_MANIFEST=EXPECTED_MANIFEST, **changes)


def facts(db):
    return {table: sorted((tuple(row) for row in db.execute("SELECT * FROM " + table)), key=repr)
            for table in INDEX_COUNTS}


def current_data(db):
    counts = SimpleNamespace(results=[dict(db.execute(_COUNT_SQL).fetchone())])
    records = SimpleNamespace(results=[dict(db.execute(_RECORD_COUNT_SQL).fetchone())])
    return _record_product_data(counts, records, counts.results[0])


class RecordingD1(SQLiteD1):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dispatched, self.admissions = [], []

    async def batch(self, statements):
        self.dispatched.append(tuple(statement.sql for statement in statements))
        state = self.journal.snapshot()
        self.admissions.append((state["active"], state["cases"].get(ACTION_ID),
            state.get(MARKER_KEY), state["product_data_verified"]))
        return await super().batch(statements)


@pytest.fixture
def fixture():
    db, storage = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        for sql in SCHEMA_SQL:
            db.execute(sql)
        seed(db)
        for owner, repo in (("owner", 202), ("other", 203)):
            insert(db, "ledger_project_repositories", owner_id=owner, project_id="prj_" + owner,
                github_repo_id=repo, github_full_name="synthetic/private", created_at="old")
        # Preserve memberships, invites and join history, not just empty tables.
        insert(db, "workspace_members", workspace_id="owner", user_id="other", role="editor",
            revision=3, joined_at="old", updated_at="old", invited_by_user_id="owner")
        insert(db, "workspace_invites", id="invite", workspace_id="owner", token_hash="e" * 64,
            role="editor", status="pending", revision=2, expires_at=1000,
            created_by_user_id="owner", created_by_revision=1, created_at="old", updated_at="old")
        insert(db, "workspace_join_requests", id="join", workspace_id="owner", invite_id="invite",
            applicant_user_id="other", created_at="old", updated_at="old")
        insert(db, "workspace_events", id="workspace-history", workspace_id="owner",
            actor_user_id="owner", action="invite", subject_id="invite", created_at="old")
        for kind, identity, payload in (
            ("sessions", "retained-session", {"userId": "owner", "expiresAt": 1000}),
            ("githubStates", "retained-state", {"state": "synthetic"}),
            ("githubIdentities", "77", {"githubId": "77", "userId": "owner", "createdAt": 1}),
            ("emailIdentities", hashlib.sha256(b"pullwise-email:fixture@example.test").hexdigest(), {"email": "fixture@example.test", "userId": "owner",
                "createdAt": 1, "verifiedAt": 1}),
            ("billingEvents", "retained-event", {"billing": "synthetic"}),
            ("billingPendingUpdates", "retained-update", {"eventId": "retained-update"}),
        ):
            insert(db, "app_state", name=record_name(kind, identity),
                payload=encode_record(kind, identity, payload), updated_at=1)
        insert(db, "expense_recurring_pending", rule_id="rule_owner", owner_id="owner",
            period_key="2026-11", scheduled_on="2026-11-09", template_json='{"purpose":"Pending history"}',
            failed_code="RECORD_LIMIT", rule_revision=7, created_at="2026-11-09T00:00:00Z", recipient_user_id="owner")
        db.commit()
        journal = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
        state = journal.snapshot()
        state.update(schema_ready=True, schema_version=SCHEMA_VERSION,
            schema_fingerprint=SCHEMA_FINGERPRINT, product_data=current_data(db), product_data_verified=True,
            state_storage_version=1, state_record_integrity_version=STATE_RECORD_INTEGRITY_VERSION,
            state_record_integrity_request=6, requests=11,
            cases={"product-schema": 1, "product": 10, "product-state-record-v1": 1},
            state_record_migration={"complete": True, "request": 6},
            schema_upgrade_v11={"complete": True, "from": 10, "to": 11, "request": 9},
            reserved_read=123, reserved_written=31, actual_read=80, actual_written=20,
            evidence=[{"request": 1, "operation": 1, "rows_read": 80, "rows_written": 20}])
        journal._save(state)
        yield db, storage, journal
    finally:
        db.close()
        storage.close()


def run(raw, journal, env=None):
    return asyncio.run(clear_preview_business(raw, journal, env or armed(), clock=lambda: 10))


def test_complete_clear_preserves_all_nonbusiness_rows_and_all_other_usage_columns(fixture):
    db, _, journal = fixture
    original, before = facts(db), journal.snapshot()
    columns = [row[1] for row in db.execute("PRAGMA table_info(ledger_plan_usage)")]
    plan = _compile_plan(before)
    raw = RecordingD1(db, journal)
    assert business_clear_pending(armed(), journal) is True
    receipt = run(raw, journal)
    after, remaining = journal.snapshot(), facts(db)
    for table, rows in original.items():
        if table in BUSINESS_TABLES:
            assert rows and remaining[table] == []
        elif table == "ledger_plan_usage":
            expected = [tuple(0 if column in {"projects", "records", "project_delta", "record_delta"}
                              else value for column, value in zip(columns, row)) for row in rows]
            assert remaining[table] == expected
        else:
            assert remaining[table] == rows
    assert raw.calls == 3 and raw.dispatched[1] == CLEAR_SQL and len(CLEAR_SQL) == 13
    assert all(ticket == after[MARKER_KEY]["request"] and used == 1 and marker["complete"] is False
               and verified is False for ticket, used, marker, verified in raw.admissions)
    assert len(set(raw.reservations)) == 1
    assert raw.reservations[0] == (before["reserved_read"] + plan.rows_read,
                                   before["reserved_written"] + plan.rows_written)
    for key in ("scope", "state_record_migration", "schema_upgrade_v11", "state_storage_version",
                "state_record_integrity_version", "state_record_integrity_request", "evidence"):
        assert after[key] == before[key]
    assert after["requests"] == before["requests"] + 1
    assert after["cases"] == {**before["cases"], "product": before["cases"]["product"] + 1, ACTION_ID: 1}
    for key in ("reserved_read", "reserved_written", "actual_read", "actual_written"):
        assert after[key] >= before[key]
    assert after["active"] is None and after["stopped"] is None and after["product_data_verified"] is True
    assert after["product_data"] == current_data(db)
    assert not list(db.execute("PRAGMA foreign_key_check")) and db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert receipt == business_clear_receipt(journal) and receipt["complete"] is True
    assert receipt["remainingBusinessRows"]["total"] == 0 and receipt["nonzeroCapacityRows"] == 0
    assert "owner" not in json.dumps(receipt) and "fixture@example.test" not in json.dumps(receipt)


def test_completed_armed_restart_never_clears_new_business_data(fixture):
    db, storage, journal = fixture
    raw = RecordingD1(db, journal)
    first = run(raw, journal)
    insert(db, "expense_categories", id="cat_new", owner_id="owner", name="New", created_at="new", updated_at="new")
    insert(db, "ledger_projects", id="prj_new", owner_id="owner", name="New", created_at="new", updated_at="new")
    insert(db, "expenses", id="exp_new", owner_id="owner", target_kind="project", project_id="prj_new",
        category_id="cat_new", occurred_on="2026-10-10", amount_minor=900, currency="USD", purpose="New", created_at="new", updated_at="new")
    db.commit()
    saved = journal.snapshot()
    saved["product_data"] = current_data(db)
    journal._save(saved)
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    original = facts(db)
    state = restarted.snapshot()
    assert business_clear_pending(armed(), restarted) is False
    assert run(raw, restarted) == first and raw.calls == 3
    assert facts(db) == original and restarted.snapshot() == state


@pytest.mark.parametrize("read_attempts", [None, 2, 3])
def test_read_retry_or_absent_attempts_retains_full_plan_margin_and_write_contract(fixture, read_attempts):
    db, _, journal = fixture
    before = journal.snapshot()
    plan = _compile_plan(before)
    raw = RecordingD1(db, journal, attempts=None, attempts_by_call={1: read_attempts, 3: read_attempts})
    assert run(raw, journal)["complete"] is True
    after = journal.snapshot()
    assert after["reserved_read"] == before["reserved_read"] + plan.rows_read
    assert after["reserved_written"] == before["reserved_written"] + plan.rows_written
    assert after[MARKER_KEY]["write_execution"]["native_attempts"] == [None] * 13
    assert after.get("read_margin_released", 0) == before.get("read_margin_released", 0)


@pytest.mark.parametrize("change", [{"PULLWISE_MODE": "production"}, {"PULLWISE_MODE": "local"},
    {"PULLWISE_PREVIEW_BUSINESS_CLEAR_ID": "other-action"}, {"PULLWISE_PREVIEW_BUSINESS_CLEAR_MANIFEST": "0" * 64},
    {"PULLWISE_D1_ACCESS_ENABLED": "0"}, {"PULLWISE_PREVIEW_PRODUCT_ENABLED": "0"}])
def test_invalid_armed_configuration_rejects_before_any_native_dispatch(fixture, change):
    db, _, journal = fixture
    env = armed()
    for key, value in change.items():
        setattr(env, key, value)
    raw, before = RecordingD1(db, journal), journal.snapshot()
    with pytest.raises(BudgetError, match="UNREVIEWED"):
        business_clear_pending(env, journal)
    with pytest.raises(BudgetError, match="UNREVIEWED"):
        run(raw, journal, env)
    assert raw.calls == 0 and journal.snapshot() == before


def test_normal_unarmed_is_noop_in_every_environment(fixture):
    db, _, journal = fixture
    raw, before = RecordingD1(db, journal), journal.snapshot()
    for mode in ("preview", "production", "local"):
        env = SimpleNamespace(PULLWISE_MODE=mode)
        assert business_clear_pending(env, journal) is False and run(raw, journal, env) is None
    assert raw.calls == 0 and journal.snapshot() == before and business_clear_receipt(journal) is None


@pytest.mark.parametrize("change", ["stopped", "active", "unverified", "schema", "typed", "scope",
                                   "v11_incomplete", "cutover_incomplete", "unknown_table", "oversized", "guard"])
def test_invalid_state_never_dispatches(fixture, change):
    db, _, journal = fixture
    state = journal.snapshot()
    if change == "stopped":
        state["stopped"] = "D1_OUTCOME_UNKNOWN"
    elif change == "active":
        state["active"] = 11
    elif change == "unverified":
        state["product_data_verified"] = False
    elif change == "schema":
        state["schema_fingerprint"] = "unreviewed"
    elif change == "typed":
        state["state_record_integrity_version"] = 0
    elif change == "scope":
        state["scope"] = "new-scope"
    elif change == "v11_incomplete":
        state["schema_upgrade_v11"]["complete"] = False
    elif change == "cutover_incomplete":
        state["state_record_migration"]["complete"] = False
    elif change == "unknown_table":
        state["product_data"]["rows"]["unreviewed"] = 0
    elif change == "oversized":
        state["product_data"]["rows"]["expenses"] = 1_000_001
    else:
        state["product_data"]["rows"]["d1_command_guard"] = 1
    journal._save(state)
    raw = RecordingD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == state


@pytest.mark.parametrize("failure", ["timeout", "missing_pre", "missing_write", "missing_post", "write_retry", "atomic_check"])
def test_unknown_outcome_keeps_entire_reservation_and_incomplete_ticket_without_retry(fixture, failure):
    db, storage, journal = fixture
    before, original = journal.snapshot(), facts(db)
    plan = _compile_plan(before)
    kwargs = {"fail_call": 2} if failure == "timeout" else {
        "missing_meta": {"missing_pre": 1, "missing_write": 2, "missing_post": 3}[failure]
    } if failure.startswith("missing_") else {"attempts_by_call": {2: 2}} if failure == "write_retry" else {}
    raw = RecordingD1(db, journal, **kwargs)
    if failure == "atomic_check":
        original_batch = raw.batch
        async def inject_native_check(statements):
            if tuple(statement.sql for statement in statements) == CLEAR_SQL:
                statements = [*statements[:5], raw.prepare("INSERT INTO d1_command_guard(ok) VALUES(0)").bind(), *statements[5:]]
            return await original_batch(statements)
        raw.batch = inject_native_check
    with pytest.raises(BudgetError):
        run(raw, journal)
    failed = journal.snapshot()
    assert failed["stopped"] is not None and failed["active"] is not None
    assert failed[MARKER_KEY]["complete"] is False and failed["cases"][ACTION_ID] == 1
    assert failed["reserved_read"] == before["reserved_read"] + plan.rows_read
    assert failed["reserved_written"] == before["reserved_written"] + plan.rows_written
    if failure in {"timeout", "missing_pre", "atomic_check"}:
        assert facts(db) == original
    count = raw.calls
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError):
        run(raw, restarted)
    assert raw.calls == count and restarted.snapshot() == failed
    assert business_clear_receipt(restarted)["complete"] is False


@pytest.mark.parametrize("damage", ["schema", "foreign_keys", "typed"])
def test_fresh_preproof_rejects_mismatch_before_deletion(fixture, damage):
    db, _, journal = fixture
    if damage == "schema":
        db.execute("CREATE TABLE unreviewed_extra(id)")
    elif damage == "foreign_keys":
        db.execute("PRAGMA foreign_keys=OFF")
    else:
        db.execute("UPDATE app_state SET payload='{}' WHERE name='record:users:owner'")
    db.commit()
    raw, original = RecordingD1(db, journal), facts(db)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 1 and facts(db) == original
    assert journal.snapshot()[MARKER_KEY]["complete"] is False and journal.snapshot()["stopped"] is not None


def test_unreviewed_complete_boolean_is_not_a_public_success_receipt(fixture):
    _, _, journal = fixture
    state = journal.snapshot()
    state[MARKER_KEY] = {"complete": True, "request": "private-account-name", "schema_version": "private"}
    journal._save(state)
    receipt = business_clear_receipt(journal)
    assert receipt["complete"] is False and receipt["request"] is None and "private" not in json.dumps(receipt)
    with pytest.raises(BudgetError, match="INCOMPLETE"):
        business_clear_pending(armed(), journal)


def test_global_sql_remains_rejected_outside_the_complete_private_plan():
    for sql in ("DELETE FROM expenses", CLEAR_SQL[-2]):
        with pytest.raises(ValueError):
            sql_write_bound(sql)
