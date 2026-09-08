from datetime import datetime, timezone
import pytest

from pullwise_server import app, db
from tests.test_scan_queue_admission import account  # noqa: F401 - shared isolated account fixture
from tests.test_scan_quota_routes import RouteHarness


def claim(cookie):
    db.create_worker({"worker_id": "wk_execution", "name": "Execution fixture", "token_hash": "synthetic"})
    request = RouteHarness({"repo": "acme/api", "requestId": "execution-lease"}, cookie=cookie)
    app.PullwiseHandler.route(request, "POST")
    assert request.status == 201
    timestamp = app.now()
    job = db.claim_next_scan_job("wk_execution", lease_seconds=60, timestamp=timestamp,
        recover_before_claim=False, create_review_run=True)
    return job, timestamp


def proof(job, timestamp):
    return {"executor_id": "test-generation", "run_id": f"run_{job['job_id']}",
            "lease_id": f"lease_{job['job_id']}",
            "updated_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}


def heartbeat(job, timestamp, execution=None):
    return db.record_active_worker_heartbeat({"worker_id": "wk_execution", "running_jobs": 1,
        "status": "online", "execution": execution}, [job["job_id"]], lease_seconds=3600, timestamp=timestamp)


def test_watcher_without_execution_proof_does_not_renew_and_quota_is_released(account):
    job, timestamp = claim(account)
    assert heartbeat(job, timestamp + 30)["renewed_count"] == 0
    recovered = db.recover_expired_scan_jobs(timestamp=timestamp + 61)
    assert len(recovered) == 1
    with app.STATE_LOCK:
        app.apply_recovered_scan_jobs_locked(recovered)
    scan = db.get_user_scan_snapshot("usr_queue", job["scan_id"])
    assert scan["status"] == "failed"
    assert scan["quotaState"] == "released"
    assert app.quota.quota_payload_for_user(app.USERS["usr_queue"])["reserved"] == 0


def test_independent_execution_pulses_renew_long_work_but_replay_has_a_fixed_expiry(account):
    job, timestamp = claim(account)
    for offset in (10, 50, 90, 130):
        result = heartbeat(job, timestamp + offset, proof(job, timestamp + offset))
        assert result["renewed_count"] == 1
        assert db.get_scan_job(job["job_id"])["timeout_at"] == timestamp + offset + 60
        assert db.recover_expired_scan_jobs(timestamp=timestamp + offset + 1) == []
    # Watcher still runs, but a stopped executor can only replay its last pulse.
    heartbeat(job, timestamp + 180, proof(job, timestamp + 130))
    assert db.get_scan_job(job["job_id"])["timeout_at"] == timestamp + 190
    assert heartbeat(job, timestamp + 191, proof(job, timestamp + 130))["renewed_count"] == 0
    assert len(db.recover_expired_scan_jobs(timestamp=timestamp + 192)) == 1


def test_a_fresh_pulse_cannot_revive_expired_execution_authority(account):
    job, timestamp = claim(account)
    assert heartbeat(job, timestamp + 61, proof(job, timestamp + 61))["renewed_count"] == 0
    assert db.get_scan_job(job["job_id"])["timeout_at"] == timestamp + 60


def progress_event(job, timestamp, sequence=1):
    event = {"run_id": f"run_{job['job_id']}", "job_id": job["job_id"], "worker_id": "wk_execution",
        "sequence": sequence, "event_type": "phase_started", "phase": "review", "status": "running",
        "progress": 25, "created_at": timestamp, "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}
    return db.store_review_run_event_and_progress(event, event, {
        "job_id": job["job_id"], "phase": "review", "progress": 25, "status": "running",
        "started_at": timestamp, "timeout_at": timestamp + 14400,
    })


def test_business_progress_does_not_extend_execution_liveness(account):
    job, timestamp = claim(account)
    heartbeat(job, timestamp + 10, proof(job, timestamp + 10))
    progress_event(job, timestamp + 20)
    assert db.get_scan_job(job["job_id"])["timeout_at"] == timestamp + 70


def test_expired_business_event_is_rejected_without_persisting_progress(account):
    job, timestamp = claim(account)
    with pytest.raises(ValueError, match="no longer accepting"):
        progress_event(job, timestamp + 61)
    assert db.get_scan_job(job["job_id"])["timeout_at"] == timestamp + 60
    assert db.list_review_run_events(f"run_{job['job_id']}") == []
