import copy

import pytest

from pullwise_server.github_ci_reader import GitHubCIReader
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


TARGET = {"module": "ci", "control_key": "repo:r:ci", "repository_id": "r", "github_repository_id": "42"}
REPO = {"id": 42, "private": False, "full_name": "acme/app"}
RUN = {"id": 7, "run_attempt": 1, "repository": {"id": 42}, "workflow_id": 3,
       "head_sha": "a" * 40, "status": "completed", "conclusion": "failure",
       "updated_at": "2026-09-22T01:00:00Z", "pull_requests": []}
JOB = {"id": 9, "run_id": 7, "name": "test", "status": "completed", "conclusion": "failure",
       "completed_at": "2026-09-22T01:00:00Z", "html_url": "https://github.com/acme/app/actions/runs/7/job/9",
       "steps": [{"number": 1, "name": "timeout test assertion", "status": "completed", "conclusion": "failure"}]}
BASE = "/repos/acme/app/actions"


class Transport:
    def __init__(self, *, run=None, jobs=None):
        self.run = copy.deepcopy(RUN if run is None else run)
        self.jobs = copy.deepcopy([JOB] if jobs is None else jobs)
        self.calls = []
        self.overrides = {}

    def __call__(self, path, *, token):
        self.calls.append(path)
        assert token == "server-token"
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path in ("/repositories/42", "/repos/acme/app"):
            payload = REPO
        elif path == f"{BASE}/runs?per_page=1&page=1":
            payload = {"workflow_runs": [self.run], "total_count": 1}
        elif "/attempts/" in path and "/jobs?" not in path:
            payload = dict(self.run, run_attempt=int(path.rsplit("/", 1)[1]))
        elif "/jobs?" in path:
            payload = {"jobs": self.jobs, "total_count": len(self.jobs)}
        elif path == f"{BASE}/runs/7":
            payload = self.run
        elif path == f"{BASE}/jobs/9":
            payload = self.jobs[0]
        else:
            raise AssertionError(path)
        return GitHubResponse(200, copy.deepcopy(payload), {})


def read(transport, **kwargs):
    return GitHubCIReader(get_json=transport, token_for_target=lambda target: "server-token").read_page(
        target=kwargs.pop("target", TARGET), cursor=kwargs.pop("cursor", None),
        high_watermark=kwargs.pop("high_watermark", None), event=kwargs.pop("event", None))


def test_optional_log_reader_retains_redacted_bounded_evidence_without_symptoms():
    from pullwise_server.github_ci_logs import CILogResult
    calls = []
    def logs(name, job_id, *, token):
        calls.append((name, job_id))
        return CILogResult(text="test output\nAuthorization: private-token\nAssertion failed\n", coverage="complete")
    reader = GitHubCIReader(get_json=Transport(), token_for_target=lambda target: "server-token",
                            page_size=1, log_reader=logs)
    page = reader.read_page(target=TARGET, cursor=None, high_watermark=None, event=None)
    source = page.sources[0]
    assert calls == [("acme/app", "9")]
    assert source["completeness"] == "partial"
    assert source["sourceFacts"]["logCoverage"]["state"] == "partial"
    window = source["content"]["windows"][0]
    assert "private-token" not in window["text"]
    assert window["symptoms"] == []
    assert window["stage"] == "unknown"
    assert (window["startLine"], window["endLine"]) == (1, 3)


def test_optional_log_failure_keeps_failure_fact_and_rate_limit_stops_page():
    def failed(*args, **kwargs):
        raise RuntimeError("PRIVATE-URL")
    reader = GitHubCIReader(get_json=Transport(), token_for_target=lambda target: "server-token",
                            page_size=1, log_reader=failed)
    page = reader.read_page(target=TARGET, cursor=None, high_watermark=None, event=None)
    assert page.sources[0]["sourceFacts"]["conclusion"] == "failure"
    assert page.sources[0]["completeness"] == "unavailable"
    assert "PRIVATE-URL" not in repr(page)
    def limited(*args, **kwargs):
        raise GitHubUnavailable("PRIVATE-URL", retry_at=1234)
    reader.log_reader = limited
    with pytest.raises(GitHubUnavailable) as error:
        reader.read_page(target=TARGET, cursor=None, high_watermark=None, event=None)
    assert error.value.retry_at == 1234
    assert "PRIVATE-URL" not in str(error.value)


def test_log_reads_are_bounded_to_one_job_page_and_skip_successes():
    with pytest.raises(ValueError, match="LOG_PAGE"):
        GitHubCIReader(get_json=Transport(), token_for_target=lambda target: "server-token",
                       log_reader=lambda *args, **kwargs: None)
    def must_not_read(*args, **kwargs):
        raise AssertionError("success has no failure evidence")
    reader = GitHubCIReader(get_json=Transport(run=dict(RUN, conclusion="success"), jobs=[dict(JOB, conclusion="success")]),
                            token_for_target=lambda target: "server-token", page_size=1, log_reader=must_not_read)
    page = reader.read_page(target=TARGET, cursor=None, high_watermark=None, event=None)
    assert page.sources == ()


def test_failure_preserves_native_steps_but_never_invents_logs_or_symptoms():
    transport = Transport()
    page = read(transport)
    assert len(page.sources) == 1
    source = page.sources[0]
    assert source["sourceFacts"]["steps"] == JOB["steps"]
    assert source["content"]["windows"] == []
    assert source["completeness"] == "unavailable"
    assert source["ruleActions"] == ["investigate_failure"]
    assert source["sourceFacts"]["recoveryStatus"] == "unknown"
    assert page.run_states[0]["coverage"]["jobsComplete"] is True
    assert page.next_cursor is None
    assert len(transport.calls) <= 6


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "neutral", None])
def test_nonfailure_only_retains_native_run_state(conclusion):
    job = dict(JOB, conclusion=conclusion, status="in_progress" if conclusion is None else "completed")
    page = read(Transport(run=dict(RUN, conclusion=conclusion, status=job["status"]), jobs=[job]))
    assert page.sources == ()
    assert page.run_states[0]["conclusion"] == conclusion
    assert page.run_states[0]["jobs"][0]["jobId"] == "9"


def test_rerun_walks_historical_attempt_without_inferring_recovery():
    transport = Transport(run=dict(RUN, run_attempt=2, conclusion="success"), jobs=[dict(JOB, conclusion="success")])
    first = read(transport)
    assert first.sources == ()
    assert first.next_cursor
    transport.jobs = [JOB]
    second = read(transport, cursor=first.next_cursor, high_watermark=first.high_watermark)
    assert second.sources[0]["sourceFacts"]["runAttempt"] == 1
    assert second.sources[0]["sourceFacts"]["recovery"] is None
    assert second.next_cursor is None


def test_job_pagination_stays_in_attempt_and_marks_partial():
    transport = Transport(jobs=[dict(JOB, id=n) for n in range(1, 101)])
    path = f"{BASE}/runs/7/attempts/1/jobs"
    transport.overrides[path + "?per_page=100&page=1"] = GitHubResponse(200,
        {"jobs": transport.jobs, "total_count": 101}, {"link": f'<https://api.github.com/repositories/42/actions/runs/7/attempts/1/jobs?per_page=100&page=2>; rel="next"'})
    page = read(transport)
    assert len(page.sources) == 100
    assert page.run_states[0]["coverage"]["jobsComplete"] is False
    transport.jobs = [dict(JOB, id=101)]
    following = read(transport, cursor=page.next_cursor, high_watermark=page.high_watermark)
    assert len(following.sources) == 1
    assert following.run_states[0]["coverage"]["jobsComplete"] is False


@pytest.mark.parametrize("link", [
    'https://evil.test/repositories/42/actions/runs/7/attempts/1/jobs?per_page=100&page=2',
    'https://api.github.com/repositories/43/actions/runs/7/attempts/1/jobs?per_page=100&page=2',
    'https://api.github.com/repositories/42/actions/runs/8/attempts/1/jobs?per_page=100&page=2',
    'https://api.github.com/repositories/42/actions/runs/7/attempts/1/jobs?per_page=100&page=1',
])
def test_rejects_unbound_pagination(link):
    transport = Transport()
    transport.overrides[f"{BASE}/runs/7/attempts/1/jobs?per_page=100&page=1"] = GitHubResponse(
        200, {"jobs": [JOB], "total_count": 101}, {"link": f'<{link}>; rel="next"'})
    with pytest.raises(ValueError):
        read(transport)


def test_event_refetches_job_run_and_attempt_without_trusting_event_conclusion():
    transport = Transport()
    page = read(transport, event={"event": "workflow_job", "resource_id": "9", "repository_id": "42", "conclusion": "success"})
    assert len(page.sources) == 1
    assert f"{BASE}/jobs/9" in transport.calls
    assert f"{BASE}/runs/7" in transport.calls


@pytest.mark.parametrize("mutation", ["repository", "job_run", "attempt", "duplicate", "status", "steps", "rename"])
def test_rejects_mismatched_or_malformed_authority(mutation):
    transport = Transport()
    if mutation == "repository":
        transport.run["repository"]["id"] = 43
    elif mutation == "job_run":
        transport.jobs[0]["run_id"] = 8
    elif mutation == "attempt":
        transport.jobs[0]["run_attempt"] = 2
    elif mutation == "duplicate":
        transport.jobs.append(copy.deepcopy(JOB))
    elif mutation == "status":
        transport.jobs[0]["status"] = "in_progress"
    elif mutation == "steps":
        transport.jobs[0]["steps"] = "invalid"
    elif mutation == "rename":
        transport.overrides["/repos/acme/app"] = GitHubResponse(200, dict(REPO, id=43), {})
    with pytest.raises(ValueError):
        read(transport)


def test_cursor_binds_target_and_watermark_before_network():
    transport = Transport(run=dict(RUN, run_attempt=2))
    page = read(transport)
    transport.calls.clear()
    with pytest.raises(ValueError):
        read(transport, cursor=page.next_cursor, high_watermark="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        read(transport, target=dict(TARGET, control_key="other"), cursor=page.next_cursor, high_watermark=page.high_watermark)
    assert transport.calls == []


def test_backoff_survives_adapter():
    transport = Transport()
    transport.overrides[f"{BASE}/runs/7/attempts/1/jobs?per_page=100&page=1"] = GitHubUnavailable("limited", retry_at=2000000000)
    with pytest.raises(GitHubUnavailable) as caught:
        read(transport)
    assert caught.value.retry_at == 2000000000


def test_empty_runs_retains_watermark_and_verifies_name_route():
    transport = Transport()
    transport.overrides[f"{BASE}/runs?per_page=1&page=1"] = GitHubResponse(200, {"workflow_runs": [], "total_count": 0}, {})
    page = read(transport, high_watermark="2026-09-20T00:00:00Z")
    assert page.sources == () and page.run_states == ()
    assert page.high_watermark == "2026-09-20T00:00:00Z"
    assert transport.calls[-1] == "/repos/acme/app"


@pytest.mark.parametrize("field,value", [("status", []), ("conclusion", {}), ("run_attempt", True)])
def test_malformed_job_fields_fail_with_validation_error(field, value):
    transport = Transport()
    transport.jobs[0][field] = value
    with pytest.raises(ValueError):
        read(transport)


def test_truncated_jobs_without_next_link_cannot_claim_complete():
    transport = Transport()
    transport.overrides[f"{BASE}/runs/7/attempts/1/jobs?per_page=100&page=1"] = GitHubResponse(
        200, {"jobs": [JOB], "total_count": 101}, {})
    with pytest.raises(ValueError):
        read(transport)


def test_runs_pagination_advances_only_after_attempt_finished():
    transport = Transport()
    transport.overrides[f"{BASE}/runs?per_page=1&page=1"] = GitHubResponse(200,
        {"workflow_runs": [RUN], "total_count": 2},
        {"link": f'<https://api.github.com/repositories/42/actions/runs?per_page=1&page=2>; rel="next"'})
    page = read(transport)
    assert page.next_cursor
    transport.overrides[f"{BASE}/runs?per_page=1&page=2"] = GitHubResponse(
        200, {"workflow_runs": [], "total_count": 0}, {})
    following = read(transport, cursor=page.next_cursor, high_watermark=page.high_watermark)
    assert following.next_cursor is None
    assert following.sources == ()


def test_workflow_job_with_unknown_attempt_membership_is_unavailable():
    transport = Transport(jobs=[dict(JOB, id=10)])
    transport.overrides[f"{BASE}/jobs/9"] = GitHubResponse(200, JOB, {})
    with pytest.raises(GitHubUnavailable, match="MEMBERSHIP"):
        read(transport, event={"event": "workflow_job", "repository_id": "42", "resource_id": "9"})


@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_job_event_with_authoritative_attempt_works_outside_first_jobs_page(conclusion):
    transport = Transport(run=dict(RUN, run_attempt=2), jobs=[dict(JOB, id=10)])
    transport.overrides[f"{BASE}/jobs/9"] = GitHubResponse(
        200, dict(JOB, run_attempt=1, conclusion=conclusion), {})
    page = read(transport, event={"event": "workflow_job", "repository_id": "42", "resource_id": "9"})
    assert len(page.sources) == (1 if conclusion == "failure" else 0)
    assert page.run_states[0]["runAttempt"] == 1
    assert page.run_states[0]["jobs"][0]["jobId"] == "9"
    assert page.run_states[0]["jobs"][0]["conclusion"] == conclusion
    assert page.run_states[0]["coverage"] == {"jobsComplete": False, "jobsPage": 1, "nextJobsPage": None}
    assert not any("/jobs?" in path for path in transport.calls)
    assert len(transport.calls) <= 5


def test_direct_job_event_still_requires_matching_authoritative_attempt():
    transport = Transport()
    transport.overrides[f"{BASE}/jobs/9"] = GitHubResponse(200, dict(JOB, run_attempt=1), {})
    transport.overrides[f"{BASE}/runs/7/attempts/1"] = GitHubResponse(200, dict(RUN, run_attempt=2), {})
    with pytest.raises(ValueError):
        read(transport, event={"event": "workflow_job", "repository_id": "42", "resource_id": "9"})


def test_workflow_run_event_ignores_untrusted_payload_and_retains_partial_coverage():
    transport = Transport()
    path = f"{BASE}/runs/7/attempts/1/jobs"
    transport.overrides[path + "?per_page=100&page=1"] = GitHubResponse(
        200, {"jobs": [JOB], "total_count": 101},
        {"link": f'<https://api.github.com/repos/acme/app/actions/runs/7/attempts/1/jobs?per_page=100&page=2>; rel="next"'})
    page = read(transport, event={"event": "workflow_run", "repository_id": "42", "resource_id": "7", "run_attempt": 99})
    assert page.run_states[0]["runAttempt"] == 1
    assert page.run_states[0]["coverage"] == {"jobsComplete": False, "jobsPage": 1, "nextJobsPage": 2}
    # Events refetch one bounded page; the persisted scheduled scan owns continuation.
    assert page.next_cursor is None


def test_pull_request_context_excludes_untrusted_urls_and_payload_extras():
    transport = Transport(run=dict(RUN, pull_requests=[{
        "number": 12, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
        "url": "https://evil.test/token-secret"}]))
    page = read(transport)
    assert page.run_states[0]["pullRequests"] == [{"number": 12, "headSha": "a" * 40, "baseSha": "b" * 40}]
    assert "token-secret" not in repr(page)
