"""Finite account and payment D1 transactions for the Worker runtime.

Each returned list executes as one D1 batch.
"""
import json



def _check(predicate, params=()):
    return ("INSERT INTO d1_command_guard VALUES(CASE WHEN " + predicate + " THEN 1 ELSE 0 END)", tuple(params))


def _changed():
    return _check("changes()=1")


def _projection(owner_id, account_snapshot, now):
    """Keep account CAS metadata current without reviving processing quotas."""
    from .account_cycle_rules import effective_user_plan, period_start_for_key, quota_cycle_for_user

    user = json.loads(account_snapshot)
    if not isinstance(user, dict) or user.get("id") != owner_id:
        raise ValueError("account snapshot does not match owner")
    plan = effective_user_plan(user, timestamp=now)
    period, valid_until = quota_cycle_for_user(user, plan, timestamp=now)
    if valid_until <= now:
        raise ValueError("expired account cycle")
    return plan, period, period_start_for_key(period, valid_until), valid_until


def initialize_account(*, owner_id, account_snapshot, now):
    """Local synthetic seed after the account has been persisted; no API entrypoint."""
    plan, period, period_start, valid_until = _projection(owner_id, account_snapshot, now)
    return [_check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?)""", (owner_id, account_snapshot)),
        ("""INSERT INTO account_entitlement_authority
        (owner_id,revision,plan,period,period_start,valid_until,dirty)
        VALUES (?,1,?,?,?,?,0)""",
        (owner_id, plan, period, period_start, valid_until)),
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


def park_webhook_receipt(*, receipt_event_id, expected_update_json,
                         expected_pending_json, next_pending_json, now):
    """Append one verified unmatched receipt to pending state atomically."""
    from .billing_account_rules import MAX_BILLING_PENDING_UPDATES

    update = json.loads(expected_update_json)
    before = json.loads(expected_pending_json)
    after = json.loads(next_pending_json)
    if (not isinstance(update, dict) or update.get("eventId") != receipt_event_id
            or not isinstance(before, list) or not isinstance(after, list)
            or any(isinstance(item, dict) and item.get("eventId") == receipt_event_id
                   for item in before)
            or after != [*before, update] or len(after) > MAX_BILLING_PENDING_UPDATES):
        raise ValueError("invalid pending receipt transition")
    pending_commands = stage_pending_billing_updates(
        expected_pending_json=expected_pending_json,
        next_pending_json=next_pending_json, now=now)
    return [
        _check("""EXISTS(SELECT 1 FROM billing_webhook_receipts
            WHERE event_id=? AND update_json=? AND state='pending')""",
            (receipt_event_id, expected_update_json)),
        _check("""EXISTS(SELECT 1 FROM app_state WHERE name='billingEvents'
            AND json_type(payload,?) IS NULL)""", (_json_path(receipt_event_id),)),
        *pending_commands,
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


def apply_webhook_receipt(*, receipt_event_id, expected_update_json, owner_id, expected_revision,
                          account_snapshot, next_account_json, expected_events_json,
                          next_events_json, expected_pending_json, next_pending_json, now):
    """Settle a pending verified receipt with its trusted billing state change."""
    before_events = json.loads(expected_events_json)
    after_events = json.loads(next_events_json)
    receipt_update = json.loads(expected_update_json)
    if (not isinstance(receipt_event_id, str) or not receipt_event_id
            or not isinstance(before_events, dict) or not isinstance(after_events, dict)
            or not isinstance(receipt_update, dict)
            or receipt_update.get("eventId") != receipt_event_id
            or receipt_event_id in before_events or receipt_event_id not in after_events):
        raise ValueError("receipt must add its billing event record")
    state_commands = stage_billing_reconciliation(
        owner_id=owner_id, expected_revision=expected_revision,
        account_snapshot=account_snapshot, next_account_json=next_account_json,
        expected_events_json=expected_events_json, next_events_json=next_events_json,
        expected_pending_json=expected_pending_json, next_pending_json=next_pending_json, now=now)
    return [
        _check("""EXISTS(SELECT 1 FROM billing_webhook_receipts
            WHERE event_id=? AND update_json=? AND state='pending')""",
            (receipt_event_id, expected_update_json)),
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
    plan, period, period_start, valid_until = _projection(owner_id, account_snapshot, now)
    return [
        _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,
            account_entitlement_authority authority WHERE a.name='users' AND u.key=?
            AND u.value=? AND authority.owner_id=? AND authority.revision=?)""",
            (owner_id, account_snapshot, owner_id, expected_revision)),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,plan=?,period=?,
            period_start=?,valid_until=?,dirty=0
            WHERE owner_id=? AND revision=?""",
            (plan, period, period_start, valid_until, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]
