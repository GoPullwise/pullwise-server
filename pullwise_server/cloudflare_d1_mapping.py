"""Candidate finite D1 commands over Server tables, exercised only by the probe.

No API, credentials, network, payment interpretation or production wiring.
Each returned list must be one D1 batch; never execute it as separate awaits.
"""
import json
from datetime import datetime, timezone


def schema():
    return [
        ("CREATE TABLE IF NOT EXISTS d1_command_guard(ok INTEGER NOT NULL CHECK(ok=1))", ()),
        ("""CREATE TABLE IF NOT EXISTS account_entitlement_authority(
            owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=1),
            plan TEXT NOT NULL, period TEXT NOT NULL,
            period_start INTEGER NOT NULL CHECK(period_start>=0),
            monthly_processing_limit INTEGER NOT NULL CHECK(monthly_processing_limit>=0),
            valid_until INTEGER NOT NULL, dirty INTEGER NOT NULL CHECK(dirty IN (0,1))
        )""", ()),
        ("""CREATE TABLE IF NOT EXISTS d1_claim_authority(
            job_id TEXT PRIMARY KEY, account_revision INTEGER NOT NULL CHECK(account_revision>=1)
        )""", ()),
        ("""CREATE TABLE IF NOT EXISTS billing_webhook_receipts(
            event_id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL,
            update_json TEXT NOT NULL, received_at INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','applied'))
        )""", ()),
        ("""CREATE TABLE IF NOT EXISTS d1_enqueue_decision(
            job_id TEXT PRIMARY KEY, admit INTEGER NOT NULL CHECK(admit IN (0,1))
        )""", ()),
    ]


def _check(predicate, params=()):
    return ("INSERT INTO d1_command_guard VALUES(CASE WHEN " + predicate + " THEN 1 ELSE 0 END)", tuple(params))


def _changed():
    return _check("changes()=1")


def _account(job_id, frozen, revision, now):
    # Match the actual persisted users entry, including billing facts, not an
    # in-memory plan or checkout-return URL. Equality is deliberately conservative.
    return _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,background_jobs j
        JOIN account_entitlement_authority authority ON authority.owner_id=j.billing_owner_id
        JOIN processing_usage_ledger ledger ON ledger.reservation_id=j.reservation_id
        WHERE a.name='users' AND u.key=j.billing_owner_id AND u.value=? AND j.id=?
        AND authority.revision=? AND authority.dirty=0
        AND authority.period_start<=? AND authority.valid_until>?
        AND authority.period=ledger.period)""", (frozen, job_id, revision, now, now))


def _projection(owner_id, account_snapshot, now):
    # Imported only by the Server-side fixture/adapter. The isolated Worker
    # executes exported commands and carries no second copy of billing rules.
    from pullwise_server.product_entitlement_rules import entitlements_for_user
    from pullwise_server.account_cycle_rules import period_start_for_key

    user = json.loads(account_snapshot)
    if not isinstance(user, dict) or user.get("id") != owner_id:
        raise ValueError("account snapshot does not match owner")
    entitlement = entitlements_for_user(user, timestamp=now)
    valid_until = entitlement["resetAt"]
    if valid_until <= now:
        raise ValueError("expired entitlement projection")
    return (entitlement["plan"], entitlement["period"],
            period_start_for_key(entitlement["period"], valid_until),
            entitlement["entitlements"]["monthlyProcessingLimit"], valid_until)


def initialize_account(*, owner_id, account_snapshot, now):
    """Local synthetic seed after the account has been persisted; no API entrypoint."""
    plan, period, period_start, monthly_processing_limit, valid_until = _projection(owner_id, account_snapshot, now)
    return [_check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?)""", (owner_id, account_snapshot)),
        ("""INSERT INTO account_entitlement_authority
        (owner_id,revision,plan,period,period_start,monthly_processing_limit,valid_until,dirty)
        VALUES (?,1,?,?,?,?,?,0)""",
        (owner_id, plan, period, period_start, monthly_processing_limit, valid_until)),
        ("DELETE FROM d1_command_guard", ())]


def _json_path(identifier):
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("non-empty account/event identity required")
    return '$.' + json.dumps(identifier, ensure_ascii=False)


def record_billing_webhook_receipt(*, event_id, raw_sha256, update_json, now):
    """Store one already verified and mapped Creem update before HTTP ACK."""
    update = json.loads(update_json)
    if (not isinstance(event_id, str) or not event_id
            or not isinstance(raw_sha256, str) or len(raw_sha256) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha256)
            or not isinstance(update, dict) or update.get("eventId") != event_id):
        raise ValueError("invalid mapped webhook receipt")
    return [
        ("""INSERT OR IGNORE INTO billing_webhook_receipts
            (event_id,raw_sha256,update_json,received_at,state)
            VALUES (?,?,?,?,'pending')""", (event_id, raw_sha256, update_json, now)),
        _check("""EXISTS(SELECT 1 FROM billing_webhook_receipts WHERE event_id=?
            AND raw_sha256=? AND update_json=?)""", (event_id, raw_sha256, update_json)),
        ("DELETE FROM d1_command_guard", ()),
    ]


def stage_account_write(*, owner_id, expected_revision, account_snapshot,
                        next_account_json, now):
    """Finite CAS for a trusted non-billing account writer.

    The caller must pass state_for_storage output. Every account mutation
    dirties the entitlement projection until a trusted refresh runs.
    """
    next_account = json.loads(next_account_json)
    if not isinstance(next_account, dict) or next_account.get("id") != owner_id:
        raise ValueError("account identity is invalid")
    user_path = _json_path(owner_id)
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?)""",
            (owner_id, account_snapshot, owner_id, expected_revision)),
        ("""UPDATE app_state SET payload=json_set(payload,?,json(?)),updated_at=?
            WHERE name='users' AND (SELECT value FROM json_each(payload) WHERE key=?)=?""",
            (user_path, next_account_json, now, owner_id, account_snapshot)), _changed(),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,dirty=1,valid_until=?
            WHERE owner_id=? AND revision=?""", (now, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def stage_pending_billing_updates(*, expected_pending_json, next_pending_json, now):
    """Persist the trusted handler's unmatched update list with a whole-list CAS."""
    if (not isinstance(json.loads(expected_pending_json), list)
            or not isinstance(json.loads(next_pending_json), list)):
        raise ValueError("pending billing updates must be JSON arrays")
    return [
        ("""UPDATE app_state SET payload=?,updated_at=?
            WHERE name='billingPendingUpdates' AND payload=?""",
            (next_pending_json, now, expected_pending_json)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def stage_billing_reconciliation(*, owner_id, expected_revision, account_snapshot,
                                 next_account_json, expected_events_json,
                                 next_events_json, expected_pending_json,
                                 next_pending_json, now):
    """Persist one trusted handler result across user, events and pending facts.

    The billing handler, not this command, interprets event order and payment
    state. Whole-map CAS rejects concurrent unrelated account/event changes.
    """
    next_account = json.loads(next_account_json)
    if not isinstance(next_account, dict) or next_account.get("id") != owner_id:
        raise ValueError("account identity is invalid")
    if (not isinstance(json.loads(expected_events_json), dict)
            or not isinstance(json.loads(next_events_json), dict)
            or not isinstance(json.loads(expected_pending_json), list)
            or not isinstance(json.loads(next_pending_json), list)):
        raise ValueError("billing event/pending snapshots have invalid shape")
    user_path = _json_path(owner_id)
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?)""",
            (owner_id, account_snapshot, owner_id, expected_revision)),
        ("""UPDATE app_state SET payload=json_set(payload,?,json(?)),updated_at=?
            WHERE name='users' AND (SELECT value FROM json_each(payload) WHERE key=?)=?""",
            (user_path, next_account_json, now, owner_id, account_snapshot)), _changed(),
        ("""UPDATE app_state SET payload=?,updated_at=?
            WHERE name='billingEvents' AND payload=?""",
            (next_events_json, now, expected_events_json)), _changed(),
        ("""UPDATE app_state SET payload=?,updated_at=?
            WHERE name='billingPendingUpdates' AND payload=?""",
            (next_pending_json, now, expected_pending_json)), _changed(),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,dirty=1,valid_until=?
            WHERE owner_id=? AND revision=?""", (now, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def apply_webhook_receipt(*, receipt_event_id, owner_id, expected_revision,
                          account_snapshot, next_account_json, expected_events_json,
                          next_events_json, expected_pending_json, next_pending_json, now):
    """Settle a pending verified receipt with its trusted billing state change."""
    before_events = json.loads(expected_events_json)
    after_events = json.loads(next_events_json)
    if (not isinstance(receipt_event_id, str) or not receipt_event_id
            or not isinstance(before_events, dict) or not isinstance(after_events, dict)
            or receipt_event_id in before_events or receipt_event_id not in after_events):
        raise ValueError("receipt must add its billing event record")
    state_commands = stage_billing_reconciliation(
        owner_id=owner_id, expected_revision=expected_revision,
        account_snapshot=account_snapshot, next_account_json=next_account_json,
        expected_events_json=expected_events_json, next_events_json=next_events_json,
        expected_pending_json=expected_pending_json, next_pending_json=next_pending_json, now=now)
    return [
        _check("""EXISTS(SELECT 1 FROM billing_webhook_receipts
            WHERE event_id=? AND state='pending')""", (receipt_event_id,)),
        *state_commands[:-1],
        ("""UPDATE billing_webhook_receipts SET state='applied'
            WHERE event_id=? AND state='pending'""", (receipt_event_id,)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def stage_account_event(*, owner_id, expected_revision, account_snapshot,
                        next_account_json, event_id, event_record_json, now):
    """Persist a previously accepted Creem fact and invalidate its projection.

    The caller must supply only state_for_storage output and the validated,
    deduplicated event record from the existing billing handler.
    """
    user_path, event_path = _json_path(owner_id), _json_path(event_id)
    next_account = json.loads(next_account_json)
    event_record = json.loads(event_record_json)
    if (not isinstance(next_account, dict) or next_account.get("id") != owner_id
            or not isinstance(event_record, dict)):
        raise ValueError("account identity or event record is invalid")
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?)""",
            (owner_id, account_snapshot, owner_id, expected_revision)),
        _check("""EXISTS(SELECT 1 FROM app_state WHERE name='billingEvents'
            AND json_type(payload,?) IS NULL)""", (event_path,)),
        ("""UPDATE app_state SET payload=json_set(payload,?,json(?)),updated_at=?
            WHERE name='users' AND (SELECT value FROM json_each(payload) WHERE key=?)=?""",
            (user_path, next_account_json, now, owner_id, account_snapshot)), _changed(),
        ("""UPDATE app_state SET payload=json_set(payload,?,json(?)),updated_at=?
            WHERE name='billingEvents' AND json_type(payload,?) IS NULL""",
            (event_path, event_record_json, now, event_path)), _changed(),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,dirty=1,valid_until=?
            WHERE owner_id=? AND revision=?""", (now, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def refresh_account_entitlement(*, owner_id, expected_revision, account_snapshot, now):
    """Commit a trusted entitlement calculation over the persisted account."""
    plan, period, period_start, monthly_processing_limit, valid_until = _projection(owner_id, account_snapshot, now)
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?)""",
            (owner_id, account_snapshot, owner_id, expected_revision)),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,plan=?,period=?,
            period_start=?,monthly_processing_limit=?,valid_until=?,dirty=0
            WHERE owner_id=? AND revision=?""",
            (plan, period, period_start, monthly_processing_limit, valid_until, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def reserve_first_processing_unit(*, owner_id, account_snapshot, account_revision,
                                  charge_key, reservation_id, module, now):
    """Reserve a new charge key under the current persisted entitlement.

    Replay/released-charge semantics remain a separate transaction mapping.
    """
    if (not all(isinstance(value, str) and value for value in
                (owner_id, charge_key, reservation_id)) or module not in {"pr", "ci", "updates"}):
        raise ValueError("invalid processing reservation identity")
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?
            AND authority.dirty=0 AND authority.period_start<=? AND authority.valid_until>?)""",
            (owner_id, account_snapshot, owner_id, account_revision, now, now)),
        _check("""NOT EXISTS(SELECT 1 FROM processing_usage_ledger
            WHERE charge_key=? OR reservation_id=?)""", (charge_key, reservation_id)),
        ("""INSERT INTO processing_usage_buckets
            (billing_owner_id,period,metric,used,reserved,limit_value,updated_at)
            SELECT owner_id,period,'intelligent_processing',0,0,monthly_processing_limit,?
            FROM account_entitlement_authority WHERE owner_id=?
            ON CONFLICT(billing_owner_id,period,metric) DO UPDATE SET
            limit_value=excluded.limit_value,updated_at=excluded.updated_at""", (now, owner_id)), _changed(),
        ("""UPDATE processing_usage_buckets SET reserved=reserved+1,updated_at=?
            WHERE billing_owner_id=? AND period=(SELECT period FROM account_entitlement_authority WHERE owner_id=?)
            AND metric='intelligent_processing' AND used+reserved<limit_value""",
            (now, owner_id, owner_id)), _changed(),
        ("""INSERT INTO processing_usage_ledger
            (charge_key,reservation_id,billing_owner_id,period,module,state,reserved_at,finished_at)
            SELECT ?,?,owner_id,period,?,'reserved',?,NULL
            FROM account_entitlement_authority WHERE owner_id=?""",
            (charge_key, reservation_id, module, now, owner_id)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def reserve_released_processing_unit(*, owner_id, account_snapshot, account_revision,
                                     charge_key, reservation_id, module, now):
    """Reopen an already released charge key under the current owner period."""
    if (not all(isinstance(value, str) and value for value in
                (owner_id, charge_key, reservation_id)) or module not in {"pr", "ci", "updates"}):
        raise ValueError("invalid processing reservation identity")
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority,processing_usage_ledger ledger
            WHERE a.name='users' AND u.key=? AND u.value=?
            AND authority.owner_id=? AND authority.revision=? AND authority.dirty=0
            AND authority.period_start<=? AND authority.valid_until>?
            AND ledger.charge_key=? AND ledger.billing_owner_id=?
            AND ledger.module=? AND ledger.state='released')""",
            (owner_id, account_snapshot, owner_id, account_revision, now, now,
             charge_key, owner_id, module)),
        ("""INSERT INTO processing_usage_buckets
            (billing_owner_id,period,metric,used,reserved,limit_value,updated_at)
            SELECT owner_id,period,'intelligent_processing',0,0,monthly_processing_limit,?
            FROM account_entitlement_authority WHERE owner_id=?
            ON CONFLICT(billing_owner_id,period,metric) DO UPDATE SET
            limit_value=excluded.limit_value,updated_at=excluded.updated_at""", (now, owner_id)), _changed(),
        ("""UPDATE processing_usage_buckets SET reserved=reserved+1,updated_at=?
            WHERE billing_owner_id=? AND period=(SELECT period FROM account_entitlement_authority WHERE owner_id=?)
            AND metric='intelligent_processing' AND used+reserved<limit_value""",
            (now, owner_id, owner_id)), _changed(),
        ("""UPDATE processing_usage_ledger SET reservation_id=?,period=(SELECT period
            FROM account_entitlement_authority WHERE owner_id=?),state='reserved',
            reserved_at=?,finished_at=NULL
            WHERE charge_key=? AND billing_owner_id=? AND module=? AND state='released'""",
            (reservation_id, owner_id, now, charge_key, owner_id, module)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def confirm_existing_reservation(*, charge_key, reservation_id, owner_id, period, module, state):
    """Fence an idempotent active-charge read at a D1 transaction boundary."""
    if state not in {"reserved", "consumed"}:
        raise ValueError("only active charges can be confirmed")
    return [
        _check("""EXISTS(SELECT 1 FROM processing_usage_ledger WHERE charge_key=?
            AND reservation_id=? AND billing_owner_id=? AND period=? AND module=? AND state=?)""",
            (charge_key, reservation_id, owner_id, period, module, state)),
        ("DELETE FROM d1_command_guard", ()),
    ]


def enqueue_first_analysis_job(*, job_id, logical_key, source_id, context_id,
                               reservation_id, trigger, now,
                               global_active_limit, owner_active_limit):
    """First generation only: queue within caps or release/throttle atomically."""
    if (not all(isinstance(value, str) and value for value in
                (job_id, logical_key, source_id, context_id, reservation_id))
            or trigger not in {"github_event", "scheduled_discovery", "initial_backfill", "context_refresh"}
            or any(type(value) is not int or value < 1 for value in
                   (global_active_limit, owner_active_limit))):
        raise ValueError("invalid trusted analysis enqueue")
    admitted = "EXISTS(SELECT 1 FROM d1_enqueue_decision WHERE job_id=? AND admit=1)"
    return [
        _check("""EXISTS(SELECT 1 FROM source_records s
            JOIN source_contexts c ON c.source_id=s.source_id
            JOIN processing_usage_ledger ledger ON ledger.reservation_id=?
            JOIN account_entitlement_authority authority ON authority.owner_id=c.billing_owner_id
            WHERE s.source_id=? AND c.context_id=? AND s.processing_mode='model'
            AND s.lifecycle='active' AND s.latest_version IS NOT NULL
            AND c.accessible=1 AND c.analysis_enabled=1 AND c.context_stale=0
            AND c.authorization_valid_until>=? AND ledger.state='reserved'
            AND ledger.billing_owner_id=c.billing_owner_id AND ledger.period=authority.period
            AND authority.dirty=0 AND authority.period_start<=? AND authority.valid_until>?)""",
            (reservation_id, source_id, context_id, now, now, now)),
        _check("""NOT EXISTS(SELECT 1 FROM background_jobs
            WHERE id=? OR logical_key=?)""", (job_id, logical_key)),
        ("""INSERT INTO d1_enqueue_decision(job_id,admit)
            SELECT ?,CASE WHEN
                (SELECT COUNT(*) FROM background_jobs WHERE job_type='analyze_source'
                    AND state IN ('queued','running','retry_wait'))<?
                AND (SELECT COUNT(*) FROM background_jobs WHERE job_type='analyze_source'
                    AND billing_owner_id=c.billing_owner_id
                    AND state IN ('queued','running','retry_wait'))<?
                THEN 1 ELSE 0 END
            FROM source_contexts c WHERE c.source_id=? AND c.context_id=?""",
            (job_id, global_active_limit, owner_active_limit, source_id, context_id)), _changed(),
        ("""INSERT INTO background_jobs
            (id,job_type,logical_key,generation,trusted_trigger,requester_id,
             billing_owner_id,source_id,context_id,reservation_id,source_revision,
             source_version_id,context_version,configuration_revision,authorization_revision,
             state,attempt,next_attempt_at,created_at,updated_at)
            SELECT ?,'analyze_source',?,1,?,NULL,c.billing_owner_id,s.source_id,c.context_id,?,
                s.source_revision,s.latest_version,c.context_version,c.configuration_revision,
                c.authorization_revision,'queued',0,NULL,?,?
            FROM source_records s JOIN source_contexts c ON c.source_id=s.source_id
            WHERE s.source_id=? AND c.context_id=? AND
                EXISTS(SELECT 1 FROM d1_enqueue_decision WHERE job_id=? AND admit=1)""",
            (job_id, logical_key, trigger, reservation_id, now, now, source_id, context_id, job_id)),
        _check("changes()=1 OR NOT " + admitted, (job_id,)),
        ("""UPDATE processing_usage_buckets SET reserved=reserved-1,updated_at=?
            WHERE (billing_owner_id,period)=(SELECT billing_owner_id,period
                FROM processing_usage_ledger WHERE reservation_id=?)
            AND metric='intelligent_processing' AND reserved>=1
            AND EXISTS(SELECT 1 FROM d1_enqueue_decision WHERE job_id=? AND admit=0)""",
            (now, reservation_id, job_id)), _check("changes()=1 OR " + admitted, (job_id,)),
        ("""UPDATE processing_usage_ledger SET state='released',finished_at=?
            WHERE reservation_id=? AND state='reserved'
            AND EXISTS(SELECT 1 FROM d1_enqueue_decision WHERE job_id=? AND admit=0)""",
            (now, reservation_id, job_id)), _check("changes()=1 OR " + admitted, (job_id,)),
        ("""UPDATE source_contexts SET processing_status='throttled',updated_at=?
            WHERE source_id=? AND context_id=?
            AND EXISTS(SELECT 1 FROM d1_enqueue_decision WHERE job_id=? AND admit=0)""",
            (now, source_id, context_id, job_id)), _check("changes()=1 OR " + admitted, (job_id,)),
        ("DELETE FROM d1_enqueue_decision WHERE job_id=?", (job_id,)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def _job_binding(job_id, now):
    return _check("""EXISTS(SELECT 1 FROM background_jobs j
        JOIN source_records s ON s.source_id=j.source_id
        JOIN source_contexts c ON c.source_id=j.source_id AND c.context_id=j.context_id
        JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
        WHERE j.id=? AND j.job_type='analyze_source' AND s.processing_mode='model' AND s.lifecycle='active'
        AND s.latest_version=j.source_version_id AND s.source_revision=j.source_revision
        AND c.accessible=1 AND c.analysis_enabled=1 AND c.context_stale=0
        AND c.authorization_valid_until>=? AND c.authorization_revision=j.authorization_revision
        AND c.configuration_revision=j.configuration_revision AND c.context_version=j.context_version
        AND c.billing_owner_id=j.billing_owner_id AND l.billing_owner_id=j.billing_owner_id
        AND l.state='reserved')""", (job_id, now))


def terminate_invalid_due_job(*, job_id, now):
    """Discard a due job whose source/context/reservation fence no longer holds."""
    return [
        _check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=?
            AND job_type='analyze_source' AND (
            (state IN ('queued','retry_wait') AND COALESCE(next_attempt_at,0)<=?)
            OR (state='running' AND COALESCE(claimed_until,0)<=?)))""",
            (job_id, now, now)),
        _check("""NOT EXISTS(SELECT 1 FROM background_jobs j
            JOIN source_records s ON s.source_id=j.source_id
            JOIN source_contexts c ON c.source_id=j.source_id AND c.context_id=j.context_id
            JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
            WHERE j.id=? AND s.processing_mode='model' AND s.lifecycle='active'
            AND s.latest_version=j.source_version_id AND s.source_revision=j.source_revision
            AND c.accessible=1 AND c.analysis_enabled=1 AND c.context_stale=0
            AND c.authorization_valid_until>=? AND c.authorization_revision=j.authorization_revision
            AND c.configuration_revision=j.configuration_revision AND c.context_version=j.context_version
            AND c.billing_owner_id=j.billing_owner_id AND l.billing_owner_id=j.billing_owner_id
            AND l.state='reserved')""", (job_id, now)),
        ("""UPDATE processing_usage_buckets SET reserved=reserved-1,updated_at=?
            WHERE (billing_owner_id,period)=(SELECT l.billing_owner_id,l.period
                FROM background_jobs j JOIN processing_usage_ledger l
                ON l.reservation_id=j.reservation_id WHERE j.id=? AND l.state='reserved')
            AND metric='intelligent_processing' AND reserved>=1""", (now, job_id)),
        _check("""changes()=1 OR NOT EXISTS(SELECT 1 FROM background_jobs j
            JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
            WHERE j.id=? AND l.state='reserved')""", (job_id,)),
        ("""UPDATE processing_usage_ledger SET state='released',finished_at=?
            WHERE reservation_id=(SELECT reservation_id FROM background_jobs WHERE id=?)
            AND state='reserved'""", (now, job_id)),
        _check("""changes()=1 OR NOT EXISTS(SELECT 1 FROM background_jobs j
            JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
            WHERE j.id=? AND l.state='reserved')""", (job_id,)),
        ("""UPDATE background_jobs SET state=CASE
            WHEN NOT EXISTS(SELECT 1 FROM source_records s WHERE s.source_id=background_jobs.source_id
                AND s.processing_mode='model' AND s.lifecycle='active'
                AND s.latest_version=background_jobs.source_version_id
                AND s.source_revision=background_jobs.source_revision)
              OR EXISTS(SELECT 1 FROM source_contexts c
                WHERE c.source_id=background_jobs.source_id AND c.context_id=background_jobs.context_id
                AND (c.context_version<>background_jobs.context_version
                    OR c.configuration_revision<>background_jobs.configuration_revision))
                THEN 'superseded'
            WHEN EXISTS(SELECT 1 FROM source_contexts c
                WHERE c.source_id=background_jobs.source_id AND c.context_id=background_jobs.context_id
                AND c.analysis_enabled=0) THEN 'cancelled'
            ELSE 'blocked' END,
            claim_token=NULL,claimed_until=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?""",
            (now, job_id)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]


def claim(*, job_id, token, now, account_snapshot, account_revision, owner_monthly_limit,
          global_monthly_limit, owner_rolling_limit, global_rolling_limit):
    period = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    return [
        _account(job_id, account_snapshot, account_revision, now), _job_binding(job_id, now),
        _check("""EXISTS(SELECT 1 FROM account_entitlement_authority authority,
            background_jobs j WHERE j.id=? AND authority.owner_id=j.billing_owner_id
            AND ?<=authority.monthly_processing_limit*3)""", (job_id, owner_monthly_limit)),
        ("""UPDATE background_jobs SET state='running',attempt=attempt+1,claim_token=?,
            claimed_until=?,next_attempt_at=NULL,updated_at=? WHERE id=? AND attempt<3 AND (
              (state IN ('queued','retry_wait') AND COALESCE(next_attempt_at,0)<=?)
              OR (state='running' AND COALESCE(claimed_until,0)<=?))""",
         (token, now + 120, now, job_id, now, now)), _changed(),
        ("""INSERT INTO d1_claim_authority(job_id,account_revision) VALUES (?,?)
            ON CONFLICT(job_id) DO UPDATE SET account_revision=excluded.account_revision""",
            (job_id, account_revision)),
        ("""INSERT INTO provider_attempts(attempt_id,billing_owner_id,input_key,period_utc,occurred_at,created_at)
            SELECT ?,j.billing_owner_id,j.logical_key,?,?,? FROM background_jobs j WHERE j.id=?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE period_utc=?)<?
              AND (SELECT COUNT(*) FROM provider_attempts attempts
                   JOIN account_entitlement_authority authority ON authority.owner_id=j.billing_owner_id
                   WHERE attempts.billing_owner_id=j.billing_owner_id
                   AND attempts.occurred_at>=authority.period_start
                   AND attempts.occurred_at<authority.valid_until)<?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE occurred_at BETWEEN ? AND ?)<?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE occurred_at BETWEEN ? AND ? AND billing_owner_id=j.billing_owner_id)<?""",
         (token, period, now, now, job_id, period, global_monthly_limit, owner_monthly_limit,
          max(0, now - 59), now, global_rolling_limit, max(0, now - 59), now, owner_rolling_limit)), _changed(),
        ("""UPDATE source_contexts SET processing_status='processing',updated_at=? WHERE (source_id,context_id)=
            (SELECT source_id,context_id FROM background_jobs WHERE id=?)""", (now, job_id)), _changed(),
        ("""INSERT INTO analysis_claim_owners(billing_owner_id,last_claim_order)
            SELECT billing_owner_id,(SELECT COALESCE(MAX(last_claim_order),0)+1 FROM analysis_claim_owners)
            FROM background_jobs WHERE id=? ON CONFLICT(billing_owner_id) DO UPDATE SET last_claim_order=excluded.last_claim_order""", (job_id,)),
        ("DELETE FROM d1_command_guard", ()),
    ]


def record_claim_failure(*, job_id, token, now, retryable, next_attempt_at):
    """Fence one failed execution and release its reservation only if terminal."""
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be boolean")
    if retryable and (type(next_attempt_at) is not int or next_attempt_at <= now):
        raise ValueError("retry requires a future deadline")
    retry_or_changed = """changes()=1 OR EXISTS(SELECT 1 FROM background_jobs
        WHERE id=? AND state='retry_wait')"""
    return [
        _check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=?
            AND job_type='analyze_source' AND state='running' AND claim_token=?
            AND claimed_until>?)""", (job_id, token, now)),
        ("""UPDATE background_jobs SET
            state=CASE WHEN ? AND attempt<3 THEN 'retry_wait' ELSE 'failed' END,
            claim_token=NULL,claimed_until=NULL,
            next_attempt_at=CASE WHEN ? AND attempt<3 THEN ? ELSE NULL END,
            updated_at=? WHERE id=? AND state='running' AND claim_token=? AND claimed_until>?""",
            (int(retryable), int(retryable), next_attempt_at, now, job_id, token, now)), _changed(),
        ("""UPDATE processing_usage_buckets SET reserved=reserved-1,updated_at=?
            WHERE (billing_owner_id,period)=(SELECT ledger.billing_owner_id,ledger.period
                FROM background_jobs job JOIN processing_usage_ledger ledger
                ON ledger.reservation_id=job.reservation_id WHERE job.id=?)
            AND metric='intelligent_processing' AND reserved>=1
            AND EXISTS(SELECT 1 FROM background_jobs WHERE id=? AND state='failed')""",
            (now, job_id, job_id)), _check(retry_or_changed, (job_id,)),
        ("""UPDATE processing_usage_ledger SET state='released',finished_at=?
            WHERE reservation_id=(SELECT reservation_id FROM background_jobs WHERE id=? AND state='failed')
            AND state='reserved'""", (now, job_id)), _check(retry_or_changed, (job_id,)),
        ("""UPDATE source_contexts SET processing_status='failed',updated_at=?
            WHERE (source_id,context_id)=(SELECT source_id,context_id FROM background_jobs
                WHERE id=? AND state='failed')""", (now, job_id)), _check(retry_or_changed, (job_id,)),
        ("DELETE FROM d1_command_guard", ()),
    ]


def publication(*, job_id, token, now, account_snapshot, account_revision, sources, fences,
                assessment, source_publication, item, expected_item_revision, version):
    """Publish a prevalidated immutable result; CAS every read used to build it.

    Rows use actual SQLite column names. Assessment validation/semantic projection
    remains in Server; this experiment validates the persistence boundary only.
    """
    commands = [_account(job_id, account_snapshot, account_revision, now), _job_binding(job_id, now),
        _check("""EXISTS(SELECT 1 FROM d1_claim_authority WHERE job_id=?
            AND account_revision=?)""", (job_id, account_revision)),
        _check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=? AND state='running'
            AND claim_token=? AND claimed_until>?)""", (job_id, token, now))]
    if len({s["sourceId"] for s in sources}) != len(sources) or {s["sourceId"] for s in sources} != {f["sourceId"] for f in fences}:
        raise ValueError("incomplete dependency fences")
    for source in sources:
        commands.append(_check("""EXISTS(SELECT 1 FROM source_records WHERE source_id=?
            AND latest_version=? AND source_revision=?)""",
            (source["sourceId"], source["sourceVersion"], source["sourceRevision"])))
    for fence in fences:
        commands.append(_check("""EXISTS(SELECT 1 FROM source_contexts c,background_jobs j WHERE j.id=?
            AND c.source_id=? AND c.context_id=? AND c.context_version=? AND c.configuration_revision=?
            AND c.authorization_revision=? AND c.authorization_valid_until>=? AND c.accessible=1
            AND c.analysis_enabled=1 AND c.context_stale=0 AND c.billing_owner_id=j.billing_owner_id)""",
            (job_id, fence["sourceId"], fence["contextId"], fence["contextVersion"],
             fence["configurationRevision"], fence["authorizationRevision"], now)))
    commands.append(_check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=? AND source_id=? AND
        source_version_id=? AND context_id=? AND billing_owner_id=?)""",
        (job_id, source_publication["source_id"], assessment["source_version_id"],
         source_publication["context_id"], assessment["billing_owner_id"])))
    # Finite column allowlists prevent this module becoming an arbitrary SQL API.
    for table, row, columns in (
        ("assessments", assessment, "id billing_owner_id source_version_id context_hash evaluated_context_version question_version extractor_version model input_hash dependencies_json answers_json usage_json status created_at"),
        ("source_assessment_publications", source_publication, "source_id context_id source_version_id context_version authorization_revision billing_owner_id assessment_json evidence_json sources_json fences_json coverage_json"),
        ("item_versions", version, "item_id item_version snapshot_hash sources_json context_fences_json snapshot_json observed_at"),
    ):
        names = columns.split()
        commands.append((f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})",
                         tuple(row[name] for name in names)))
    commands += [
        ("""UPDATE items SET current_item_version=?,current_snapshot_hash=?,revision=revision+1,updated_at=?
            WHERE id=? AND revision=? AND current_item_version=?""",
         (version["item_version"], version["snapshot_hash"], now, item["id"], expected_item_revision, version["item_version"] - 1)), _changed(),
        ("""UPDATE processing_usage_buckets SET reserved=reserved-1,used=used+1,updated_at=?
            WHERE (billing_owner_id,period)=(SELECT l.billing_owner_id,l.period FROM processing_usage_ledger l
              JOIN background_jobs j ON j.reservation_id=l.reservation_id WHERE j.id=?)
              AND metric='intelligent_processing' AND reserved>=1""", (now, job_id)), _changed(),
        ("""UPDATE processing_usage_ledger SET state='consumed',finished_at=? WHERE reservation_id=
            (SELECT reservation_id FROM background_jobs WHERE id=?) AND state='reserved'""", (now, job_id)), _changed(),
        ("UPDATE background_jobs SET state='succeeded',claim_token=NULL,claimed_until=NULL,updated_at=? WHERE id=? AND claim_token=?",
         (now, job_id, token)), _changed(),
        ("UPDATE source_contexts SET processing_status='assessed',item_id=?,updated_at=? WHERE source_id=? AND context_id=?",
         (item["id"], now, source_publication["source_id"], source_publication["context_id"])), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]
    return commands
