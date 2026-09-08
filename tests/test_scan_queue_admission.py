import os
import threading
from contextlib import closing
from unittest.mock import patch

import pytest

from pullwise_server import app, db, system_config
from tests.db_template import install_initialized_db_template
from tests.test_scan_quota_routes import RouteHarness, quota_config, reset_state, seed_user


@pytest.fixture
def account(tmp_path):
    with patch.dict(os.environ, {"PULLWISE_DB_PATH": str(tmp_path / "queue.sqlite3")}), \
         patch.object(app, "persist_state"), \
         patch.object(system_config, "config", return_value=quota_config(free_limit=20)), \
         patch.object(app, "max_queued_scans_global", return_value=1):
        reset_state()
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
        yield seed_user("usr_queue", "ses_queue")
        reset_state()


def race(cookie, request_ids):
    gate = threading.Barrier(2)
    check = app.scan_queue_limit_error
    def simultaneous_check(*args):
        result = check(*args)
        gate.wait(timeout=10)
        return result
    handlers = [RouteHarness({"repo": "acme/api", "requestId": request_id}, cookie=cookie) for request_id in request_ids]
    errors = []
    def invoke(handler):
        try:
            app.PullwiseHandler.route(handler, "POST")
        except BaseException as error:
            errors.append(error)
    with patch.object(app, "scan_queue_limit_error", side_effect=simultaneous_check):
        threads = [threading.Thread(target=invoke, args=(handler,)) for handler in handlers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    return handlers


def test_concurrent_distinct_requests_reserve_one_queue_slot_and_release_rejected_quota(account):
    handlers = race(account, ["first", "second"])
    assert sorted(handler.status for handler in handlers) == [201, 429]
    assert db.scan_queue_limit_counts()["queued_global"] == 1
    rejected = next(handler for handler in handlers if handler.status == 429)
    assert rejected.payload["code"] == "QUEUE_FULL_GLOBAL"
    with closing(db.connect()) as connection:
        assert connection.execute("SELECT SUM(reserved) FROM quota_buckets").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM quota_ledger WHERE reason = 'scan_reserved'").fetchone()[0] == 2
    accepted = next(handler for handler in handlers if handler.status == 201)
    cancel = RouteHarness({}, cookie=account, path=f"/scans/{accepted.payload['id']}/cancel")
    app.PullwiseHandler.route(cancel, "POST")
    retry = RouteHarness(rejected._body, cookie=account)
    app.PullwiseHandler.route(retry, "POST")
    assert retry.status == 201


def test_same_request_stays_idempotent_under_capacity_contention(account):
    handlers = race(account, ["same", "same"])
    assert sorted(handler.status for handler in handlers) == [200, 201]
    assert handlers[0].payload["id"] == handlers[1].payload["id"]
    assert db.scan_queue_limit_counts()["queued_global"] == 1


def test_failed_snapshot_write_releases_the_queue_slot_and_quota(account):
    with closing(db.connect()) as connection, connection:
        connection.execute("CREATE TRIGGER reject_scan_snapshot BEFORE INSERT ON scans BEGIN SELECT RAISE(ABORT, 'snapshot write failed'); END")
    handler = RouteHarness({"repo": "acme/api", "requestId": "snapshot-failure"}, cookie=account)
    app.PullwiseHandler.route(handler, "POST")
    assert handler.status == 500
    assert db.scan_queue_limit_counts()["queued_global"] == 0
    with closing(db.connect()) as connection, connection:
        assert connection.execute("SELECT COALESCE(SUM(reserved), 0) FROM quota_buckets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_scan_snapshot")
    retry = RouteHarness(handler._body, cookie=account)
    app.PullwiseHandler.route(retry, "POST")
    assert retry.status == 201
