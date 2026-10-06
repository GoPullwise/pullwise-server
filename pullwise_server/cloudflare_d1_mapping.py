"""Atomic account/payment D1 commands over independently bounded state records."""
import json

from .cloudflare_state_records import decode_record, encode_record, record_name

_OWNER_PREDICATE = """name GLOB 'record:users:*'
    AND name='record:users:' || json_extract(payload,'$.id') AND (
        json_extract(payload,'$.id')=? OR
        (json_extract(payload,'$.billing.customerId')<>'' AND json_extract(payload,'$.billing.customerId')=?) OR
        (json_extract(payload,'$.billing.subscriptionId')<>'' AND json_extract(payload,'$.billing.subscriptionId')=?) OR
        (json_extract(payload,'$.billingCheckout.requestId')<>'' AND json_extract(payload,'$.billingCheckout.requestId')=?))"""


def billing_owner_candidates(update):
    from .billing_account_rules import billing_update_text

    params = tuple(billing_update_text(update.get(key))
        for key in ("userId", "customerId", "subscriptionId", "requestId"))
    return "SELECT json_extract(payload,'$.id') AS ownerId FROM app_state WHERE " + _OWNER_PREDICATE + " LIMIT 2", params


def _check(predicate, params=()):
    return ("INSERT INTO d1_command_guard VALUES(CASE WHEN " + predicate + " THEN 1 ELSE 0 END)", tuple(params))


def _changed():
    return _check("changes()=1")


def _account_blob(owner_id, payload):
    account = decode_record("users", owner_id, payload)
    return encode_record("users", owner_id, account)


def _account_guard(owner_id, account_snapshot, expected_revision):
    _account_blob(owner_id, account_snapshot)
    return _check("""EXISTS(SELECT 1 FROM app_state u,
        account_entitlement_authority authority WHERE u.name=? AND u.payload=?
        AND authority.owner_id=? AND authority.revision=?)""",
        (record_name("users", owner_id), account_snapshot, owner_id, expected_revision))


def _account_update(owner_id, account_snapshot, next_account_json, now):
    return ("""UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?""",
        (_account_blob(owner_id, next_account_json), now,
         record_name("users", owner_id), account_snapshot))


def _dirty(owner_id, expected_revision, now):
    return ("""UPDATE account_entitlement_authority SET revision=revision+1,dirty=1,
        valid_until=? WHERE owner_id=? AND revision=?""", (now, owner_id, expected_revision))


def _projection(owner_id, account_snapshot, now):
    from .account_cycle_rules import effective_user_plan, period_start_for_key, quota_cycle_for_user

    user = json.loads(_account_blob(owner_id, account_snapshot))
    plan = effective_user_plan(user, timestamp=now)
    period, valid_until = quota_cycle_for_user(user, plan, timestamp=now)
    if valid_until <= now:
        raise ValueError("expired account cycle")
    return plan, period, period_start_for_key(period, valid_until), valid_until


def initialize_account(*, owner_id, account_snapshot, now):
    """Initialize a persisted owner's projection without rewriting account state."""
    plan, period, period_start, valid_until = _projection(owner_id, account_snapshot, now)
    return [_check("EXISTS(SELECT 1 FROM app_state u WHERE u.name=? AND u.payload=?)",
        (record_name("users", owner_id), account_snapshot)),
        ("""INSERT INTO account_entitlement_authority
        (owner_id,revision,plan,period,period_start,valid_until,dirty)
        VALUES (?,1,?,?,?,?,0)""", (owner_id, plan, period, period_start, valid_until)),
        ("DELETE FROM d1_command_guard", ())]


def record_billing_webhook_receipt(*, event_id, raw_sha256, update_json, now):
    """Keep verified raw hash and normalized update immutable, including replay."""
    update = json.loads(update_json)
    if (not isinstance(event_id, str) or not event_id
            or not isinstance(raw_sha256, str) or len(raw_sha256) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha256)
            or not isinstance(update, dict) or update.get("eventId") != event_id):
        raise ValueError("invalid mapped webhook receipt")
    encode_record("billingPendingUpdates", event_id, update)
    return [("""INSERT OR IGNORE INTO billing_webhook_receipts
        (event_id,raw_sha256,update_json,received_at,state)
        VALUES (?,?,?,?,'pending')""", (event_id, raw_sha256, update_json, now)),
        _check("""EXISTS(SELECT 1 FROM billing_webhook_receipts WHERE event_id=?
            AND raw_sha256=? AND update_json=?)""", (event_id, raw_sha256, update_json)),
        ("DELETE FROM d1_command_guard", ())]


def stage_account_write(*, owner_id, expected_revision, account_snapshot,
                        next_account_json, now):
    return [_account_guard(owner_id, account_snapshot, expected_revision),
        _account_update(owner_id, account_snapshot, next_account_json, now), _changed(),
        _dirty(owner_id, expected_revision, now), _changed(),
        ("DELETE FROM d1_command_guard", ())]


def park_webhook_receipt(*, receipt_event_id, expected_update_json, now):
    """Save one unknown-owner update; unrelated pending receipts need no map CAS."""
    from .billing_account_rules import MAX_BILLING_PENDING_UPDATES

    update = json.loads(expected_update_json)
    if not isinstance(update, dict) or update.get("eventId") != receipt_event_id:
        raise ValueError("invalid pending receipt transition")
    pending_name = record_name("billingPendingUpdates", receipt_event_id)
    pending_json = encode_record("billingPendingUpdates", receipt_event_id, update)
    return [_check("""EXISTS(SELECT 1 FROM billing_webhook_receipts
        WHERE event_id=? AND update_json=? AND state='pending')""",
        (receipt_event_id, expected_update_json)),
        _check("NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)",
            (record_name("billingEvents", receipt_event_id),)),
        _check("(SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:billingPendingUpdates:*')<?",
            (MAX_BILLING_PENDING_UPDATES,)),
        ("INSERT INTO app_state(name,payload,updated_at) VALUES (?,?,?)",
            (pending_name, pending_json, now)), _changed(),
        ("DELETE FROM d1_command_guard", ())]


def stage_account_event(*, owner_id, expected_revision, account_snapshot,
                        next_account_json, event_id, event_record_json, now):
    """Publish the account change and its unique audit fact in one transaction."""
    event_record = json.loads(event_record_json)
    if not isinstance(event_record, dict):
        raise ValueError("billing event record is invalid")
    event_json = encode_record("billingEvents", event_id, {**event_record, "ownerId": owner_id})
    event_name = record_name("billingEvents", event_id)
    return [_account_guard(owner_id, account_snapshot, expected_revision),
        _check("NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)", (event_name,)),
        _account_update(owner_id, account_snapshot, next_account_json, now), _changed(),
        ("INSERT INTO app_state(name,payload,updated_at) VALUES (?,?,?)",
            (event_name, event_json, now)), _changed(),
        _dirty(owner_id, expected_revision, now), _changed(),
        ("DELETE FROM d1_command_guard", ())]


def apply_webhook_receipt(*, receipt_event_id, expected_update_json, owner_id,
                          expected_revision, account_snapshot, next_account_json,
                          event_record_json, expected_pending_json, now):
    """Atomically consume a receipt and optional parked record with account CAS."""
    update = json.loads(expected_update_json)
    if not isinstance(update, dict) or update.get("eventId") != receipt_event_id:
        raise ValueError("invalid webhook receipt")
    pending_name = record_name("billingPendingUpdates", receipt_event_id)
    if expected_pending_json is None:
        pending_guard = _check("NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)", (pending_name,))
        pending_commands = []
    else:
        if json.loads(expected_pending_json) != update:
            raise ValueError("pending receipt conflicts with saved update")
        pending_guard = _check("EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)",
            (pending_name, expected_pending_json))
        pending_commands = [("DELETE FROM app_state WHERE name=? AND payload=?",
            (pending_name, expected_pending_json)), _changed()]
    account_commands = stage_account_event(owner_id=owner_id,
        expected_revision=expected_revision, account_snapshot=account_snapshot,
        next_account_json=next_account_json, event_id=receipt_event_id,
        event_record_json=event_record_json, now=now)
    owner_sql, owner_params = billing_owner_candidates(update)
    return [_check("""EXISTS(SELECT 1 FROM billing_webhook_receipts
        WHERE event_id=? AND update_json=? AND state='pending')""",
        (receipt_event_id, expected_update_json)),
        _check("(SELECT COUNT(*) FROM (" + owner_sql + "))=1", owner_params), pending_guard,
        *account_commands[:-1], *pending_commands,
        ("UPDATE billing_webhook_receipts SET state='applied' WHERE event_id=? AND state='pending'",
            (receipt_event_id,)), _changed(), ("DELETE FROM d1_command_guard", ())]


def refresh_account_entitlement(*, owner_id, expected_revision, account_snapshot, now):
    plan, period, period_start, valid_until = _projection(owner_id, account_snapshot, now)
    return [_account_guard(owner_id, account_snapshot, expected_revision),
        ("""UPDATE account_entitlement_authority SET revision=revision+1,plan=?,period=?,
        period_start=?,valid_until=?,dirty=0 WHERE owner_id=? AND revision=?""",
        (plan, period, period_start, valid_until, owner_id, expected_revision)), _changed(),
        ("DELETE FROM d1_command_guard", ())]
