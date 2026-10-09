"""OTP rate admission uses only bounded, persistent DO SQLite counters."""
import hashlib
import sqlite3
from contextlib import closing

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_preview_rate import EmailRateLimiter, EmailRateLimit, PreviewRateLimiter
from pullwise_server.cloudflare_validation_budget import BudgetJournal


def subject(value):
    return hashlib.sha256(value.encode()).hexdigest()


EMAIL = subject("person@example.invalid")
IP = subject("192.0.2.1")


def admit(limiter, kind="send", *, email=EMAIL, ip=IP, now=120):
    return limiter.email(kind, email_subject64hex=email, ip_subject64hex=ip, now=now)


def snapshot(connection):
    return [tuple(row) for row in connection.execute("SELECT * FROM email_rate_clock ORDER BY id")], [
        tuple(row) for row in connection.execute("SELECT * FROM email_request_rates ORDER BY subject")]


@pytest.fixture
def storage():
    with closing(sqlite3.connect(":memory:")) as connection:
        sql = LocalSql(connection)
        yield connection, sql, EmailRateLimiter(sql)


def rejected_unchanged(connection, callback, *, retry_after=None):
    before = snapshot(connection)
    changes = connection.total_changes
    with pytest.raises(EmailRateLimit) as caught:
        callback()
    assert snapshot(connection) == before
    assert connection.total_changes == changes
    assert 1 <= caught.value.retry_after <= 3600
    if retry_after is not None:
        assert caught.value.retry_after == retry_after
    assert caught.value.response() == {"error": {"code": "EMAIL_RATE_LIMIT",
        "message": "Too many requests. Please try again shortly."}}


def test_send_email_cooldown_does_not_consume_other_ip_or_global_buckets(storage):
    connection, _, limiter = storage
    assert admit(limiter, now=120.9) is None
    rejected_unchanged(connection, lambda: admit(limiter, ip=subject("other-ip"), now=121), retry_after=59)
    rejected_unchanged(connection, lambda: admit(limiter, now=179.99), retry_after=1)
    admit(limiter, now=180)
    assert len(snapshot(connection)[1]) == 3
    assert sorted(row[2] for row in snapshot(connection)[1]) == [2, 2, 2]


def test_send_email_hour_limit_survives_ip_rotation_and_restarts(storage):
    connection, sql, limiter = storage
    for index in range(6):
        admit(limiter, ip=subject(str(index)), now=120 + index * 60)
    restarted = EmailRateLimiter(sql)
    rejected_unchanged(connection, lambda: admit(restarted, ip=subject("new-ip"), now=480), retry_after=3120)
    admit(restarted, now=3600)


def test_send_ip_hour_limit_survives_email_rotation(storage):
    connection, _, limiter = storage
    for index in range(30):
        admit(limiter, email=subject(str(index)))
    rejected_unchanged(connection, lambda: admit(limiter, email=subject("fresh-email")), retry_after=3480)
    admit(limiter, email=subject("fresh-email"), ip=subject("fresh-ip"))
    admit(limiter, email=subject("fresh-email-next-hour"), now=3600)


def test_global_send_hour_limit_applies_across_email_and_ip_rotation(storage):
    connection, _, limiter = storage
    for index in range(500):
        admit(limiter, email=subject(str(index)), ip=subject(str(index // 30)))
    rejected_unchanged(connection, lambda: admit(limiter, email=subject("new-email"),
        ip=subject("new-ip")), retry_after=3480)
    admit(limiter, email=subject("new-email"), ip=subject("new-ip"), now=3600)


def test_send_cooldown_spans_hour_boundary_and_expiry_cleanup(storage):
    connection, sql, limiter = storage
    admit(limiter, now=3599)
    restarted = EmailRateLimiter(sql)
    # Another subject can be admitted and trigger cleanup of expired hour rows;
    # the original email's 60-second cooldown row must remain.
    admit(restarted, email=subject("another-email"), ip=subject("another-ip"), now=3600)
    rejected_unchanged(connection, lambda: admit(restarted, now=3600), retry_after=59)
    admit(restarted, now=3659)
    assert max(row[2] for row in snapshot(connection)[1]) == 2


def test_verify_ip_minute_limit_does_not_consume_fresh_email_bucket(storage):
    connection, _, limiter = storage
    for index in range(10):
        admit(limiter, "verify", email=subject(str(index)))
    rejected_unchanged(connection, lambda: admit(limiter, "verify", email=subject("new-email")), retry_after=60)
    admit(limiter, "verify", email=subject("new-email"), ip=subject("new-ip"))
    admit(limiter, "verify", email=subject("new-email-next-minute"), now=180)


def test_verify_email_minute_limit_survives_ip_rotation_and_restart(storage):
    connection, sql, limiter = storage
    for index in range(30):
        admit(limiter, "verify", ip=subject(str(index)))
    rejected_unchanged(connection, lambda: admit(EmailRateLimiter(sql), "verify",
        ip=subject("fresh-ip")), retry_after=60)
    admit(limiter, "verify", now=180)


def test_send_and_verify_buckets_remain_independent(storage):
    connection, _, limiter = storage
    admit(limiter, now=60)
    for _ in range(10):
        admit(limiter, "verify")
    rejected_unchanged(connection, lambda: admit(limiter, "verify"), retry_after=60)
    admit(limiter)
    assert len(snapshot(connection)[1]) == 5


def test_late_clock_cannot_reopen_window_or_shorten_cooldown_after_restart(storage):
    connection, sql, limiter = storage
    admit(limiter, now=3600)
    restarted = EmailRateLimiter(sql)
    rejected_unchanged(connection, lambda: admit(restarted, now=120), retry_after=60)
    for _ in range(10):
        admit(restarted, "verify", now=120)
    rejected_unchanged(connection, lambda: admit(EmailRateLimiter(sql), "verify", now=3599), retry_after=60)
    assert snapshot(connection)[0] == [(1, 3600, 5)]
    admit(limiter, now=3660)


def test_capacity_rejection_performs_no_cleanup_or_other_counter_mutation(storage):
    connection, _, limiter = storage
    limiter.MAX_SUBJECTS = 3
    admit(limiter)
    rejected_unchanged(connection, lambda: admit(limiter, "verify"), retry_after=60)
    # At expiry a finite cleanup makes room for the two verification subjects.
    admit(limiter, "verify", now=3600)
    assert snapshot(connection)[0] == [(1, 3600, 2)]


def test_cleanup_is_finite_indexed_and_uses_exact_key_deletes(storage):
    connection, sql, limiter = storage
    for index in range(100):
        admit(limiter, "verify", email=subject(str(index)), ip=subject(str(index)))
    assert len(snapshot(connection)[1]) == 200
    queries = []
    original_exec = sql.exec
    def record(query, *params):
        queries.append((query, params))
        return original_exec(query, *params)
    sql.exec = record
    admit(limiter, "verify", email=subject("new-email"), ip=subject("new-ip"), now=180)
    assert len(snapshot(connection)[1]) == 200 - limiter.CLEANUP_LIMIT + 2
    cleanup_read = next((query, params) for query, params in queries if "INDEXED BY" in query)
    assert "ORDER BY expires_at,subject LIMIT ?" in cleanup_read[0]
    assert cleanup_read[1][-1] == limiter.CLEANUP_LIMIT
    plan = connection.execute("EXPLAIN QUERY PLAN " + cleanup_read[0], cleanup_read[1]).fetchall()
    assert any("COVERING INDEX email_request_rates_expiry" in row[3] for row in plan)
    assert not any("USE TEMP B-TREE" in row[3] for row in plan)
    cleanup_delete = next((query, params) for query, params in queries if query.startswith("DELETE"))
    assert "WHERE subject IN (" in cleanup_delete[0] and len(cleanup_delete[1]) == limiter.CLEANUP_LIMIT
    assert snapshot(connection)[0][0][2] == len(snapshot(connection)[1])


def test_unknown_send_outcome_remains_reserved_and_has_no_refund_api(storage):
    connection, sql, limiter = storage
    # Provider work would begin only after this admission; losing its response
    # does not undo the counters or allow an immediate duplicate dispatch.
    admit(limiter)
    rejected_unchanged(connection, lambda: admit(EmailRateLimiter(sql)), retry_after=60)
    for index in range(1, 6):
        admit(limiter, now=120 + index * 60)
    rejected_unchanged(connection, lambda: admit(limiter, now=480), retry_after=3120)
    assert not hasattr(limiter, "refund")


def test_rate_rejection_does_not_clean_unrelated_expired_subjects(storage):
    connection, _, limiter = storage
    for index in range(100):
        admit(limiter, "verify", email=subject(str(index)), ip=subject(str(index)))
    admit(limiter, now=180)
    assert len(snapshot(connection)[1]) == 200 - limiter.CLEANUP_LIMIT + 3
    rejected_unchanged(connection, lambda: admit(limiter, ip=subject("new-ip"), now=181), retry_after=59)


def test_email_admission_never_changes_preview_rates_or_d1_accounting_journal(storage):
    connection, sql, limiter = storage
    journal = BudgetJournal(sql, preview_product=True, product_operations=True)
    preview = PreviewRateLimiter(sql)
    preview.actor("ordinary-user", channel="write", now=120)
    journal_before = journal.snapshot()
    preview_before = list(connection.execute("SELECT * FROM preview_request_rates ORDER BY subject"))
    admit(limiter)
    rejected_unchanged(connection, lambda: admit(limiter), retry_after=60)
    for _ in range(10):
        admit(limiter, "verify")
    rejected_unchanged(connection, lambda: admit(limiter, "verify"), retry_after=60)
    assert journal.snapshot() == journal_before
    assert list(connection.execute("SELECT * FROM preview_request_rates ORDER BY subject")) == preview_before
    assert all(len(row[0]) == 64 and set(row[0]) <= set("0123456789abcdef")
               for row in snapshot(connection)[1])
    assert "person@example.invalid" not in repr(snapshot(connection))
    assert "192.0.2.1" not in repr(snapshot(connection))


@pytest.mark.parametrize("change", [
    {"kind": "wrong"}, {"kind": []}, {"now": True}, {"now": -1}, {"now": float("nan")},
    {"now": float("inf")}, {"now": float("-inf")}, {"now": EmailRateLimiter.MAX_NOW + 1},
    {"now": "120"}, {"email_subject64hex": "plain@example.invalid"},
    {"email_subject64hex": "a" * 63}, {"email_subject64hex": "A" * 64},
    {"email_subject64hex": "g" * 64}, {"ip_subject64hex": "192.0.2.1"},
    {"ip_subject64hex": None}, {"ip_subject64hex": "\x00" * 64},
])
def test_invalid_input_is_rejected_before_any_storage_change(storage, change):
    connection, _, limiter = storage
    arguments = {"kind": "send", "email_subject64hex": EMAIL, "ip_subject64hex": IP, "now": 120}
    arguments.update(change)
    before = snapshot(connection)
    with pytest.raises(ValueError, match="invalid email rate admission"):
        limiter.email(**arguments)
    assert snapshot(connection) == before


def test_email_rate_limit_retry_after_is_bounded():
    assert EmailRateLimit(0).retry_after == 1
    assert EmailRateLimit(5000).retry_after == 3600


def test_safe_integer_clock_margin_at_native_binding_boundary(storage):
    connection, _, limiter = storage
    admit(limiter, now=limiter.MAX_NOW)
    assert all(row[4] <= 9007199254740991 for row in snapshot(connection)[1])
    rejected_unchanged(connection, lambda: admit(limiter, now=limiter.MAX_NOW), retry_after=60)
