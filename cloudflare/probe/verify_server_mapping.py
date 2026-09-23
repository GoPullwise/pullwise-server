"""Local D1 test driver. No SQL/fixture upload or external-provider requests."""
import argparse
import hashlib
import hmac
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor


WEBHOOK_SECRET = b"synthetic-webhook-secret"
WEBHOOK_RAW = (b'{"id":"evt-local","eventType":"subscription.canceled",'
               b'"object":{"id":"synthetic-sub","metadata":{"userId":"owner"}}}')
LATER_WEBHOOK_RAW = (b'{"id":"later","eventType":"subscription.canceled",'
                     b'"object":{"id":"later-sub","customer":{"id":"customer"}}}')
COMPOSE_WEBHOOK_RAW = (b'{"id":"evt-compose","eventType":"subscription.paid",'
                       b'"object":{"id":"sub_fixture","status":"active",'
                       b'"product":{"id":"synthetic-max"},'
                       b'"metadata":{"userId":"owner","plan":"max"}}}')


def call(path, method="POST", *, raw_body=None, headers=None):
    request = urllib.request.Request("http://127.0.0.1:8796/server-map/" + path, method=method,
                                     data=(raw_body if raw_body is not None else b"{}") if method == "POST" else None,
                                     headers=headers or {})
    try:
        # A fresh local Python Worker/D1 import can cold-start slowly.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def webhook(path="webhook-receipt", *, raw=WEBHOOK_RAW, valid=True):
    signature = hmac.new(WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest() if valid else "bad"
    return call(path, raw_body=raw, headers={"creem-signature": signature})


def scheduled():
    url = "http://127.0.0.1:8796/cdn-cgi/local/scheduled?format=json"
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=90) as response:
        return response.status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        status, state = call("state", "GET")
        assert status == 200 and state["used"] == 1 and state["attempts"] == 1 and state["results"] == 1, state
        assert state["accountRevision"] == 4 and state["accountDirty"] == 0, state
        assert state["eventA"] == state["eventB"] == state["originalPaymentFactPreserved"] == 1, state
        assert state["webhookReceipts"] == 1, state
        assert webhook()[0] == 200
        assert call("publish")[0] == 409
        print("Server mapping restart/replay passed on local D1")
        return
    assert call("reset")[0] == 200
    assert call("schedule-enable")[0] == 200
    assert scheduled() == 200
    state = call("state", "GET")[1]
    assert state["jobState"] == "running" and state["jobAttempt"] == 1, state
    assert state["attempts"] == 1 and state["used"] == 0, state
    assert scheduled() == 200
    assert call("state", "GET")[1]["attempts"] == 1
    assert call("reset")[0] == 200
    assert call("stale-analysis-source")[0] == 200
    assert call("schedule-enable")[0] == 200
    assert scheduled() == 200
    state = call("state", "GET")[1]
    assert state["jobState"] == "superseded" and state["chargeState"] == "released", state
    assert state["reserved"] == state["attempts"] == 0, state
    assert call("reset")[0] == 200
    assert call("old-analysis-cycle")[0] == 200
    assert call("schedule-enable")[0] == 200
    assert scheduled() == 200
    state = call("state", "GET")[1]
    assert state["jobState"] == "blocked" and state["chargeState"] == "released", state
    assert state["reserved"] == state["attempts"] == 0, state
    assert call("reset")[0] == 200
    assert call("expired-third-attempt")[0] == 200
    assert call("schedule-enable")[0] == 200
    assert scheduled() == 200
    state = call("state", "GET")[1]
    assert state["jobState"] == "failed" and state["chargeState"] == "released", state
    assert state["reserved"] == state["attempts"] == 0, state
    assert call("reset")[0] == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: call("claim")[0], range(2)))
    assert sorted(statuses) == [200, 409], statuses
    assert call("publish")[0] == 200
    assert call("publish")[0] == 409
    assert call("reset")[0] == 200
    assert call("reserve-first")[0] == 200
    assert call("reserve-first")[0] == 409
    state = call("state", "GET")[1]
    assert state["reserved"] == 2 and state["limitValue"] == 5000, state
    assert state["secondReservation"] == state["encryptedToken"] == 1, state
    assert call("enqueue-first")[1] == {"rejected": False}
    state = call("state", "GET")[1]
    assert state["jobCount"] == 2 and state["reserved"] == 2, state
    assert call("reset")[0] == 200
    assert call("reserve-first")[0] == 200
    assert call("enqueue-denied")[1] == {"rejected": True}
    state = call("state", "GET")[1]
    assert state["jobCount"] == 1 and state["reserved"] == 1, state
    assert state["secondLedgerState"] == "released" and state["thirdSourceStatus"] == "throttled", state
    assert call("reset")[0] == 200
    assert call("upgrade-max")[0] == 200
    assert call("refresh-upgrade")[0] == 200
    state = call("state", "GET")[1]
    assert state["limitValue"] == 25000 and state["reserved"] == 1, state
    assert call("reset")[0] == 200
    assert call("claim")[0] == 200
    status, reuse = call("reserve-charge")
    assert status == 200 and reuse["reservation"]["reused"] is True, reuse
    assert call("retry-claim")[0] == 200
    state = call("state", "GET")[1]
    assert state["jobState"] == "retry_wait" and state["jobAttempt"] == 1, state
    assert state["reserved"] == 1 and state["attempts"] == 1, state
    assert call("claim")[0] == 409
    assert call("claim-after-retry")[0] == 200
    assert call("terminal-claim")[0] == 200
    assert call("terminal-claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["jobState"] == "failed" and state["jobAttempt"] == 2, state
    assert state["reserved"] == state["used"] == 0 and state["attempts"] == 2, state
    status, reopened = call("reserve-charge")
    assert status == 200 and reopened["reservation"]["reused"] is False, reopened
    status, reuse = call("reserve-charge")
    assert status == 200 and reuse["reservation"]["reused"] is True, reuse
    state = call("state", "GET")[1]
    assert state["chargeReservationId"] == "reopened-reservation" and state["reserved"] == 1, state
    for fault in ("edit-parent", "change-account", "break-reservation"):
        assert call("reset")[0] == 200
        assert call("claim")[0] == 200
        assert call(fault)[0] == 200
        assert call("publish")[0] == 409
        state = call("state", "GET")[1]
        assert state["results"] == 0 and state["versions"] == 0 and state["used"] == 0, state
        assert state["jobState"] == "running" and state["paymentFactsPreserved"] == 1, state
    assert call("reset")[0] == 200
    assert call("change-account")[0] == 200
    assert call("claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["attempts"] == 0 and state["jobState"] == "queued", state
    assert call("reset")[0] == 200
    assert call("account-write-a")[0] == 200
    assert call("claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["accountRevision"] == 2 and state["accountDirty"] == 1, state
    assert call("account-write-b")[0] == 200
    assert call("refresh-account")[0] == 200
    assert call("claim-frozen")[0] == 409
    assert call("claim-current")[0] == 200
    assert call("publish-current")[0] == 200
    assert call("reset")[0] == 200
    assert webhook(raw=LATER_WEBHOOK_RAW)[0] == 200
    assert call("pending-event")[0] == 200
    state = call("state", "GET")[1]
    assert state["pendingCount"] == 1 and state["laterEvent"] == 0, state
    assert call("reconcile-pending")[0] == 200
    assert call("claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["pendingCount"] == 0 and state["laterEvent"] == 1, state
    assert state["accountRevision"] == 3 and state["accountDirty"] == 1, state
    assert call("refresh-reconciled")[0] == 200
    assert call("claim-reconciled")[0] == 200
    assert call("publish-reconciled")[0] == 200
    assert call("reset")[0] == 200
    assert webhook()[0] == 200
    assert call("webhook-apply")[0] == 200
    assert call("webhook-apply")[0] == 409
    state = call("state", "GET")[1]
    assert state["webhookReceipts"] == state["appliedReceipts"] == 1, state
    assert state["accountRevision"] == 2 and state["accountDirty"] == 1, state
    assert call("reset")[0] == 200
    status, accepted = webhook("creem-compose", raw=COMPOSE_WEBHOOK_RAW)
    assert status == 200 and accepted == {"received": True, "state": "applied", "eventId": "evt-compose"}, accepted
    state = call("state", "GET")[1]
    assert state["accountRevision"] == 3 and state["accountDirty"] == 0, state
    assert state["limitValue"] == 25000 and state["appliedReceipts"] == 1, state
    status, replay = webhook("creem-compose", raw=COMPOSE_WEBHOOK_RAW)
    assert status == 200 and replay["state"] == "duplicate", replay
    assert call("state", "GET")[1]["accountRevision"] == 3
    assert call("reset")[0] == 200
    assert call("event-a")[0] == 200
    assert call("event-a")[0] == 409
    assert call("claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["accountRevision"] == 2 and state["accountDirty"] == 1
    assert state["eventA"] == 1 and state["originalPaymentFactPreserved"] == 1
    assert call("event-b")[0] == 200
    assert call("refresh-account")[0] == 200
    assert call("claim-frozen")[0] == 409  # Old revision remains fenced after A→B→A.
    assert call("claim-current")[0] == 200
    assert call("publish-current")[0] == 200
    state = call("state", "GET")[1]
    assert state["accountRevision"] == 4 and state["accountDirty"] == 0
    assert state["eventA"] == state["eventB"] == state["originalPaymentFactPreserved"] == 1
    assert webhook(valid=False)[0] == 409
    assert webhook()[0] == 200
    assert webhook()[0] == 200
    assert webhook("webhook-conflict", raw=(b'{"id":"evt-local","eventType":"subscription.canceled",'
        b'"object":{"id":"different-sub","metadata":{"userId":"owner"}}}'))[0] == 409
    assert call("state", "GET")[1]["webhookReceipts"] == 1
    print("Actual Server schema: combined claim/budget, payment-fact revision fencing and atomic publication passed on local D1")


if __name__ == "__main__":
    main()
