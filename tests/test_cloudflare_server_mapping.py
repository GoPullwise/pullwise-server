"""Run the candidate D1 batches against the actual ProductStore schema first."""
import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from pullwise_server.product_jobs import ProductJobScheduler, TrustedTrigger
from pullwise_server.entitlements import entitlements_for_user
from pullwise_server import db as server_db
from pullwise_server import cloudflare_d1_mapping
from test_pr_thread_semantics import ThreadFixture


def mapping():
    return cloudflare_d1_mapping


def execute(store, statements):
    with store._immediate() as db:
        for sql, params in statements:
            db.execute(sql, params)


def seed(path, *, now=None):
    f = ThreadFixture(path)
    if now is not None:
        f.now = now
    f.source("1", "Please change")
    f.source("2", "Question", reply="1")
    account = dict(id="owner", createdAt=f.now - 864000,
        billing=dict(plan="pro", status="active", subscriptionId="sub_fixture",
                     currentPeriodStart=f.now - 864000, currentPeriodEnd=f.now + 864000),
        githubId="author", githubAccessToken="synthetic_user_token",
        githubIdentities=[{"id": "synthetic_identity", "accessToken": "synthetic_identity_token"}])
    entitlement = entitlements_for_user(account, timestamp=f.now)
    reservation = f.store.reserve_processing_unit(charge_key="charge", billing_owner_id="owner",
        period=entitlement["period"], module="pr", limit=entitlement["entitlements"]["monthlyProcessingLimit"])
    job = ProductJobScheduler(f.store).schedule_analysis(source_context_key="first", source_id="1",
        context_id="repo:repo:pr", reservation_id=reservation["reservationId"], trigger=TrustedTrigger.SCHEDULED_DISCOVERY)
    with f.store._immediate() as db:
        db.execute("CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL)")
        # Synthetic preserved account facts; the mapping never interprets or rewrites these.
        key_path = path.parent / "synthetic-state.key"
        key_path.write_bytes(bytes([0x42]) * 32)
        with patch.dict(os.environ, {server_db.STATE_ENCRYPTION_KEY_PATH_ENV: str(key_path)}):
            storage_users = server_db.state_for_storage({"users": {"owner": account}})["users"]
        db.execute("INSERT INTO app_state VALUES('users',?,?)", (json.dumps(storage_users), f.now))
        db.execute("INSERT INTO app_state VALUES('billingEvents',?,?)", ('{"event_fixture":{"status":"processed"}}', f.now))
        db.execute("INSERT INTO app_state VALUES('billingPendingUpdates',?,?)", ('[]', f.now))
        frozen = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
    m = mapping()
    execute(f.store, m.schema())
    execute(f.store, m.initialize_account(owner_id="owner", account_snapshot=frozen, now=f.now))
    return f, job, frozen


def claim_args(f, job, frozen):
    return dict(job_id=job["id"], token="claim-fixture", now=f.now, account_snapshot=frozen,
                account_revision=1,
                owner_monthly_limit=10, global_monthly_limit=10, owner_rolling_limit=6, global_rolling_limit=60)


def test_combined_claim_budget_rejects_stale_account_without_partial_writes(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    with f.store._immediate() as db:
        db.execute("UPDATE app_state SET payload='{}' WHERE name='users'")
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**claim_args(f, job, frozen)))
    assert f.store.get_background_job(job["id"])["status"] == "queued"
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == '{"event_fixture":{"status":"processed"}}'


def test_budget_denial_rolls_back_claim_and_replay_never_readmits(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    args = claim_args(f, job, frozen)
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**{**args, "global_monthly_limit": 0}))
    assert f.store.get_background_job(job["id"])["status"] == "queued"
    execute(f.store, m.claim(**args))
    assert f.store.get_background_job(job["id"])["status"] == "running"
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**args))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 1


def publication_args(f, job, frozen):
    item = f.store.create_item(context_id="repo:repo:pr", unit_type="pr_thread", unit_key="thread1")
    sources = [dict(sourceId=sid, sourceVersion=r["sourceVersion"], sourceRevision=r["sourceRevision"])
               for sid, r in f.records.items()]
    fences = [dict(sourceId=s["sourceId"], contextId="repo:repo:pr", contextVersion=1,
        configurationRevision=1, authorizationRevision=1) for s in sources]
    assessment = dict(id="assessment-fixture", billing_owner_id="owner", source_version_id=sources[0]["sourceVersion"],
        context_hash="pr", evaluated_context_version=1, question_version="pr-followup/v3", extractor_version="pr-segments/v1",
        model="jev-1.13.0", input_hash="input", dependencies_json=json.dumps(sources[1:]),
        answers_json='{"s0_change_request":{"choice":"present"}}', usage_json="{}", status="succeeded", created_at=f.now)
    public = dict(source_id="1", context_id="repo:repo:pr", source_version_id=sources[0]["sourceVersion"],
        context_version=1, authorization_revision=1, billing_owner_id="owner", assessment_json=json.dumps({"id": assessment["id"]}),
        evidence_json="[]", sources_json=json.dumps(sources), fences_json=json.dumps(fences), coverage_json="{}")
    version = dict(item_id=item["id"], item_version=1, snapshot_hash="synthetic", sources_json=json.dumps(sources),
        context_fences_json=json.dumps(fences), snapshot_json='{"module":"pr","actionTypes":["change_requested"]}', observed_at=f.now)
    return dict(job_id=job["id"], token="claim-fixture", now=f.now, account_snapshot=frozen,
        account_revision=1,
        sources=sources, fences=fences, assessment=assessment, source_publication=public,
        item=item, expected_item_revision=item["revision"], version=version)


@pytest.mark.parametrize("fault", ["dependency", "account", "reservation", "item", "lease"])
def test_publication_rolls_back_all_tables_on_any_fence_or_final_usage_failure(tmp_path, fault):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    args = publication_args(f, job, frozen)
    execute(f.store, m.claim(**claim_args(f, job, frozen)))
    with f.store._immediate() as db:
        sql = {"dependency": "UPDATE source_records SET source_revision=source_revision+1 WHERE source_id='2'",
               "account": "UPDATE app_state SET payload='{}' WHERE name='users'",
               "reservation": "UPDATE processing_usage_buckets SET reserved=0",
               "item": "UPDATE items SET revision=revision+1",
               "lease": "UPDATE background_jobs SET claimed_until=0"}[fault]
        db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.publication(**args))
    with closing(f.store.connect()) as db:
        for table in ("assessments", "source_assessment_publications", "item_versions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "reserved"
        assert db.execute("SELECT state FROM background_jobs").fetchone()[0] == "running"


def test_publication_is_once_and_preserves_account_and_creem_facts(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    args = publication_args(f, job, frozen)
    with closing(f.store.connect()) as db:
        before = list(db.execute("SELECT * FROM app_state ORDER BY name"))
    execute(f.store, m.claim(**claim_args(f, job, frozen)))
    execute(f.store, m.publication(**args))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.publication(**args))
    assert f.store.processing_usage(billing_owner_id="owner", period=entitlements_for_user(json.loads(frozen), timestamp=f.now)["period"])["used"] == 1
    with closing(f.store.connect()) as db:
        assert list(db.execute("SELECT * FROM app_state ORDER BY name")) == before


def test_account_event_and_reprojection_fence_claim_across_aba(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    args = claim_args(f, job, frozen)
    changed = json.dumps(dict(id="owner", billing=dict(plan="free", status="active"), githubId="author"),
                         separators=(",", ":"))
    execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=1,
        account_snapshot=frozen, next_account_json=changed, event_id="event-a",
        event_record_json='{"applied":true}', now=f.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**args))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT json_extract(payload,'$.\"event-a\".applied') FROM app_state WHERE name='billingEvents'").fetchone()[0] == 1
    execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=2,
        account_snapshot=changed, next_account_json=frozen, event_id="event-b",
        event_record_json='{"applied":true}', now=f.now))
    execute(f.store, m.refresh_account_entitlement(owner_id="owner", expected_revision=3,
        account_snapshot=frozen, now=f.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**args))
    assert f.store.get_background_job(job["id"])["status"] == "queued"


def test_account_time_expiry_and_accepted_event_block_publication(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    execute(f.store, m.schema())
    # A stored projection can expire before its underlying paid account period.
    expires = f.now + 60
    with f.store._immediate() as db:
        db.execute("UPDATE account_entitlement_authority SET valid_until=?", (expires,))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**{**claim_args(f, job, frozen), "now": expires}))
    assert f.store.get_background_job(job["id"])["status"] == "queued"
    execute(f.store, m.claim(**claim_args(f, job, frozen)))
    args = publication_args(f, job, frozen)
    args["now"] = expires
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.publication(**args))
    assert f.store.processing_usage(billing_owner_id="owner", period=entitlements_for_user(json.loads(frozen), timestamp=f.now)["period"])["used"] == 0
    execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=1,
        account_snapshot=frozen, next_account_json=frozen, event_id="event-c",
        event_record_json='{"applied":false,"stale":true}', now=f.now + 1))
    args["now"] = f.now + 2
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.publication(**args))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "reserved"


def test_projection_uses_persisted_paid_boundary_and_preserves_used_on_upgrade(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    with closing(f.store.connect()) as db:
        original = db.execute("SELECT plan,period,monthly_processing_limit,valid_until FROM account_entitlement_authority").fetchone()
    expected = entitlements_for_user(json.loads(frozen), timestamp=f.now)
    assert tuple(original) == ("pro", expected["period"], 5000, expected["resetAt"])
    with f.store._immediate() as db:
        db.execute("UPDATE processing_usage_buckets SET used=3 WHERE billing_owner_id='owner'")
    upgraded = json.loads(frozen)
    upgraded["billing"]["plan"] = "max"
    upgraded_json = json.dumps(upgraded, separators=(",", ":"))
    execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=1,
        account_snapshot=frozen, next_account_json=upgraded_json, event_id="upgrade",
        event_record_json='{"applied":true}', now=f.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**{**claim_args(f, job, upgraded_json), "account_revision": 2}))
    execute(f.store, m.refresh_account_entitlement(owner_id="owner", expected_revision=2,
        account_snapshot=upgraded_json, now=f.now))
    with closing(f.store.connect()) as db:
        after = db.execute("SELECT plan,period,monthly_processing_limit,valid_until FROM account_entitlement_authority").fetchone()
        usage = db.execute("SELECT used,reserved FROM processing_usage_buckets WHERE billing_owner_id='owner'").fetchone()
    assert tuple(after) == ("max", expected["period"], 25000, expected["resetAt"])
    assert tuple(usage) == (3, 1)


def test_projection_rolls_monthly_period_and_paid_expiry_from_snapshot():
    m = mapping()
    from datetime import datetime, timezone
    def stamp(month, day):
        return int(datetime(2026, month, day, tzinfo=timezone.utc).timestamp())
    anchor = stamp(1, 15)
    end = stamp(4, 15)
    user = dict(id="owner", createdAt=anchor,
        billing=dict(plan="pro", status="active", currentPeriodStart=anchor, currentPeriodEnd=end))
    frozen = json.dumps(user)
    january = m._projection("owner", frozen, stamp(1, 20))
    february = m._projection("owner", frozen, stamp(2, 20))
    assert january == ("pro", f"cycle:{anchor}", anchor, 5000, stamp(2, 15))
    assert february == ("pro", f"cycle:{stamp(2, 15)}", stamp(2, 15), 5000, stamp(3, 15))
    expired = m._projection("owner", frozen, end)
    assert expired[0] == "free" and expired[3] == 200 and expired[4] > end
    with pytest.raises(ValueError):
        m._projection("other", frozen, end)


def test_initial_projection_requires_matching_persisted_account(tmp_path):
    m = mapping()
    f, _, frozen = seed(tmp_path / "domain.db")
    with pytest.raises(ValueError):
        execute(f.store, m.initialize_account(owner_id="other", account_snapshot=frozen, now=f.now))
    with f.store._immediate() as db:
        db.execute("DELETE FROM account_entitlement_authority WHERE owner_id='owner'")
        db.execute("UPDATE app_state SET payload='{}' WHERE name='users'")
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.initialize_account(owner_id="owner", account_snapshot=frozen, now=f.now))


def test_accepted_event_rejects_account_identity_swap_before_any_write(tmp_path):
    m = mapping()
    f, _, frozen = seed(tmp_path / "domain.db")
    wrong = json.dumps({**json.loads(frozen), "id": "other"})
    with pytest.raises(ValueError):
        execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=1,
            account_snapshot=frozen, next_account_json=wrong, event_id="swap",
            event_record_json='{"applied":true}', now=f.now))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0] == frozen
        assert db.execute("SELECT json_type(payload,'$.swap') FROM app_state WHERE name='billingEvents'").fetchone()[0] is None
        assert db.execute("SELECT revision FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()[0] == 1


def test_non_billing_account_write_invalidates_projection_and_fences_aba(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    changed = json.dumps({**json.loads(frozen), "githubLogin": "renamed"}, separators=(",", ":"))
    execute(f.store, m.stage_account_write(owner_id="owner", expected_revision=1,
        account_snapshot=frozen, next_account_json=changed, now=f.now))
    with closing(f.store.connect()) as db:
        row = db.execute("SELECT revision,dirty FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()
        assert tuple(row) == (2, 1)
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == '{"event_fixture":{"status":"processed"}}'
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**{**claim_args(f, job, changed), "account_revision": 2}))
    execute(f.store, m.stage_account_write(owner_id="owner", expected_revision=2,
        account_snapshot=changed, next_account_json=frozen, now=f.now))
    execute(f.store, m.refresh_account_entitlement(owner_id="owner", expected_revision=3,
        account_snapshot=frozen, now=f.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**claim_args(f, job, frozen)))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.stage_account_write(owner_id="owner", expected_revision=1,
            account_snapshot=frozen, next_account_json=changed, now=f.now))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT revision FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()[0] == 4


def test_pending_event_association_commits_user_events_and_pending_together(tmp_path):
    m = mapping()
    f, _, frozen = seed(tmp_path / "domain.db")
    pending = '[{"eventId":"later","customerId":"customer"}]'
    execute(f.store, m.stage_pending_billing_updates(expected_pending_json='[]',
        next_pending_json=pending, now=f.now))
    changed = json.dumps({**json.loads(frozen), "billing": {**json.loads(frozen)["billing"],
        "customerId": "customer"}}, separators=(",", ":"))
    next_events = '{"event_fixture":{"status":"processed"},"later":{"applied":true}}'
    args = dict(owner_id="owner", expected_revision=1, account_snapshot=frozen,
        next_account_json=changed, expected_events_json='{"event_fixture":{"status":"processed"}}',
        next_events_json=next_events, expected_pending_json=pending,
        next_pending_json='[]', now=f.now)
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.stage_billing_reconciliation(**{**args, "expected_pending_json": "[]"}))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == args["expected_events_json"]
    execute(f.store, m.stage_billing_reconciliation(**args))
    with closing(f.store.connect()) as db:
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == next_events
        assert db.execute("SELECT payload FROM app_state WHERE name='billingPendingUpdates'").fetchone()[0] == '[]'
        assert db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0] == changed


def test_probe_fixture_preserves_encrypted_account_fields_without_plaintext(tmp_path):
    f, _, frozen = seed(tmp_path / "domain.db")
    stored = json.loads(frozen)
    assert stored["githubAccessToken"]["__encrypted"] == server_db.STATE_ENCRYPTION_MARKER
    assert stored["githubIdentities"][0]["accessToken"]["__encrypted"] == server_db.STATE_ENCRYPTION_MARKER
    assert "synthetic_user_token" not in frozen
    assert "synthetic_identity_token" not in frozen
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == '{"event_fixture":{"status":"processed"}}'


def test_owner_monthly_attempt_budget_uses_billing_cycle_across_utc_month(tmp_path):
    from datetime import datetime, timezone
    now = int(datetime(2027, 2, 1, 12, tzinfo=timezone.utc).timestamp())
    previous_month_attempt = int(datetime(2027, 1, 25, tzinfo=timezone.utc).timestamp())
    f, job, frozen = seed(tmp_path / "domain.db", now=now)
    m = mapping()
    with f.store._immediate() as db:
        for index in range(10):
            db.execute("""INSERT INTO provider_attempts
                (attempt_id,billing_owner_id,input_key,period_utc,occurred_at,created_at)
                VALUES (?,?,?,?,?,?)""", (f"older-{index}", "owner", f"old-{index}",
                "2027-01", previous_month_attempt, previous_month_attempt))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**claim_args(f, job, frozen)))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 10
        assert f.store.get_background_job(job["id"])["status"] == "queued"


def test_first_d1_reservation_uses_projected_limit_without_resetting_usage(tmp_path):
    m = mapping()
    f, _, frozen = seed(tmp_path / "domain.db")
    with f.store._immediate() as db:
        db.execute("UPDATE processing_usage_buckets SET used=3 WHERE billing_owner_id='owner'")
    changed = json.dumps({**json.loads(frozen), "billing": {**json.loads(frozen)["billing"],
        "plan": "max"}}, separators=(",", ":"))
    execute(f.store, m.stage_account_event(owner_id="owner", expected_revision=1,
        account_snapshot=frozen, next_account_json=changed, event_id="upgrade-reserve",
        event_record_json='{"applied":true}', now=f.now))
    execute(f.store, m.refresh_account_entitlement(owner_id="owner", expected_revision=2,
        account_snapshot=changed, now=f.now))
    args = dict(owner_id="owner", account_snapshot=changed, account_revision=3,
        charge_key="new-charge", reservation_id="new-reservation", module="ci", now=f.now)
    execute(f.store, m.reserve_first_processing_unit(**args))
    with closing(f.store.connect()) as db:
        bucket = db.execute("SELECT used,reserved,limit_value FROM processing_usage_buckets WHERE billing_owner_id='owner'").fetchone()
        assert tuple(bucket) == (3, 2, 25000)
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='new-charge'").fetchone()[0] == "reserved"
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.reserve_first_processing_unit(**args))
    with closing(f.store.connect()) as db:
        assert tuple(db.execute("SELECT used,reserved FROM processing_usage_buckets WHERE billing_owner_id='owner'").fetchone()) == (3, 2)


@pytest.mark.parametrize("fault", ["quota", "dirty", "stale_snapshot"])
def test_first_d1_reservation_rejection_has_no_partial_ledger_or_usage(tmp_path, fault):
    m = mapping()
    f, _, frozen = seed(tmp_path / "domain.db")
    args = dict(owner_id="owner", account_snapshot=frozen, account_revision=1,
        charge_key="new-charge", reservation_id="new-reservation", module="ci", now=f.now)
    if fault == "quota":
        with f.store._immediate() as db:
            db.execute("UPDATE processing_usage_buckets SET used=4999 WHERE billing_owner_id='owner'")
    elif fault == "dirty":
        changed = json.dumps({**json.loads(frozen), "githubLogin": "new"}, separators=(",", ":"))
        execute(f.store, m.stage_account_write(owner_id="owner", expected_revision=1,
            account_snapshot=frozen, next_account_json=changed, now=f.now))
        args.update(account_snapshot=changed, account_revision=2)
    else:
        args["account_snapshot"] = "{}"
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.reserve_first_processing_unit(**args))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM processing_usage_ledger WHERE charge_key='new-charge'").fetchone()[0] == 0
        assert db.execute("SELECT reserved FROM processing_usage_buckets WHERE billing_owner_id='owner'").fetchone()[0] == 1


def test_d1_retry_and_terminal_failure_persist_deadline_and_release_once(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(f, job, frozen)
    execute(f.store, m.claim(**args))
    execute(f.store, m.record_claim_failure(job_id=job["id"], token=args["token"],
        now=f.now + 1, retryable=True, next_attempt_at=f.now + 40))
    with closing(f.store.connect()) as db:
        assert tuple(db.execute("SELECT state,next_attempt_at FROM background_jobs").fetchone()) == ("retry_wait", f.now + 40)
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 1
    next_claim = {**args, "token": "claim-two", "now": f.now + 39}
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.claim(**next_claim))
    next_claim["now"] = f.now + 40
    execute(f.store, m.claim(**next_claim))
    execute(f.store, m.record_claim_failure(job_id=job["id"], token="claim-two",
        now=f.now + 41, retryable=False, next_attempt_at=None))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.record_claim_failure(job_id=job["id"], token="claim-two",
            now=f.now + 42, retryable=False, next_attempt_at=None))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs").fetchone()[0] == "failed"
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "released"
        assert tuple(db.execute("SELECT used,reserved FROM processing_usage_buckets").fetchone()) == (0, 0)
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 2


def test_d1_expired_claim_cannot_release_or_retry(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(f, job, frozen)
    execute(f.store, m.claim(**args))
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.record_claim_failure(job_id=job["id"], token=args["token"],
            now=f.now + 120, retryable=False, next_attempt_at=None))
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs").fetchone()[0] == "running"
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "reserved"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 1


def test_d1_attempt_three_is_terminal_even_when_failure_is_retryable(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(f, job, frozen)
    for attempt in range(3):
        token = f"claim-{attempt}"
        now = f.now + attempt * 40
        execute(f.store, m.claim(**{**args, "token": token, "now": now}))
        execute(f.store, m.record_claim_failure(job_id=job["id"], token=token,
            now=now + 1, retryable=True, next_attempt_at=now + 40))
    with closing(f.store.connect()) as db:
        assert tuple(db.execute("SELECT state,attempt,next_attempt_at FROM background_jobs").fetchone()) == ("failed", 3, None)
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "released"
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 3


def test_d1_released_charge_key_can_reserve_again_once_without_losing_usage(tmp_path):
    m = mapping()
    f, job, frozen = seed(tmp_path / "domain.db")
    reservation_id = f.store.get_background_job(job["id"])["reservationId"]
    f.store.finish_processing_unit(reservation_id, succeeded=False)
    with closing(f.store.connect()) as db:
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "released"
    args = dict(owner_id="owner", account_snapshot=frozen, account_revision=1,
        charge_key="charge", reservation_id="replacement", module="pr", now=f.now)
    execute(f.store, m.reserve_released_processing_unit(**args))
    with closing(f.store.connect()) as db:
        row = db.execute("SELECT reservation_id,state,period FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()
        assert tuple(row) == ("replacement", "reserved", entitlements_for_user(json.loads(frozen), timestamp=f.now)["period"])
        assert tuple(db.execute("SELECT used,reserved FROM processing_usage_buckets").fetchone()) == (0, 1)
    with pytest.raises(sqlite3.IntegrityError):
        execute(f.store, m.reserve_released_processing_unit(**args))
    with closing(f.store.connect()) as db:
        assert tuple(db.execute("SELECT used,reserved FROM processing_usage_buckets").fetchone()) == (0, 1)
