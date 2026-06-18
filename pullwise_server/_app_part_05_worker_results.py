from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

def create_scan_job_for_scan(scan: dict) -> dict:
    user_id = str(scan.get("userId") or "").strip()
    user = USERS.get(user_id) if user_id else None
    plan = quota.effective_user_plan(user)
    job = db.create_scan_job(
        {
            "job_id": make_id("job"),
            "scan_id": scan.get("id"),
            "repo": scan.get("repo"),
            "branch": scan.get("branch"),
            "commit": scan.get("commit"),
            "status": "queued",
            "created_at": scan.get("queuedAt") or scan.get("createdAt") or now(),
            "user_id": scan.get("userId"),
            "repo_id": scan.get("repoId"),
            "github_repo_id": scan.get("githubRepoId"),
            "installation_id": scan.get("installationId"),
            "clone_url": scan.get("cloneUrl"),
            "review_output_language": clean_review_output_language(scan.get("reviewOutputLanguage")),
            "provider_chain": [billing.review_agent_provider(plan)],
            "max_attempts": system_config.scan_job_max_attempts(),
        }
    )
    scan["jobId"] = job.get("job_id")
    index_memory_scan(scan)
    db.upsert_scan(scan)
    return job


def reset_scan_for_retry_locked(scan: dict, *, job: dict, queued_at: int | None = None) -> None:
    scan_id = public_issue_text(scan.get("id"))
    if scan_id:
        ISSUES[:] = [issue for issue in ISSUES if public_issue_text(issue.get("scanId")) != scan_id]
        db.delete_issues_for_scan(scan_id, user_id=public_issue_text(scan.get("userId")))
    queued_timestamp = pull_request_timestamp(queued_at) or now()
    scan.update(
        {
            "status": "queued",
            "queuedAt": queued_timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "jobId": public_issue_text(job.get("job_id")) or public_issue_text(scan.get("jobId")),
        }
    )
    commit = clean_github_access_text(job.get("commit"))
    if commit:
        scan["commit"] = commit
    for key in (
        "aiUsage",
        "auditSwarm",
        "claimedAt",
        "claimedByWorkerId",
        "completedAt",
        "completionAudit",
        "convergenceState",
        "durationMs",
        "effectiveAgentConfig",
        "error",
        "errorCode",
        "graphVerifiedReport",
        "impactGraph",
        "jobTrace",
        "preflight",
        "quotaConsumedAt",
        "quotaConsumeTrigger",
        "quotaRefunded",
        "quotaReleasedAt",
        "quotaReleaseReason",
        "quotaReservedAt",
        "quotaState",
        "recoveredAt",
        "recoveryReason",
        "repositoryGraph",
        "resultChecksum",
        "semanticGraph",
        "startedAt",
        "updatedAt",
        "verificationAudit",
    ):
        scan.pop(key, None)
    db.upsert_scan(scan)


def retry_scan_job_for_scan_locked(scan: dict, *, queued_at: int | None = None) -> dict:
    scan_id = public_issue_text(scan.get("id"))
    if not scan_id:
        raise ValueError("Scan id is required.")
    job = db.get_scan_job_for_scan(scan_id)
    if job:
        job_status = public_issue_text(job.get("status")).lower()
        if job_status not in {"failed", "lost", "cancelled"}:
            raise RuntimeError("Only failed, lost, or cancelled scan jobs can be retried.")
        retried_job = db.retry_scan_job(scan_id, timestamp=queued_at)
        if not retried_job:
            raise RuntimeError("Scan job could not be retried.")
        job = retried_job
    else:
        if public_scan_status(scan.get("status")) not in {"failed", "cancelled"}:
            raise RuntimeError("Only failed, lost, or cancelled scan jobs can be retried.")
        job = create_scan_job_for_scan(scan)
        if not job:
            raise RuntimeError("Scan job could not be created.")
    reset_scan_for_retry_locked(scan, job=job, queued_at=queued_at)
    return job


def worker_plan_for_job(job: dict, scan: dict | None = None) -> str:
    user_id = str(job.get("user_id") or (scan or {}).get("userId") or "").strip()
    user = USERS.get(user_id) if user_id else None
    return quota.effective_user_plan(user)


def worker_agent_config_for_job(job: dict, scan: dict | None = None) -> dict:
    return billing.review_agent_config(worker_plan_for_job(job, scan))


def worker_graph_verified_job_enabled(job: dict, scan: dict | None = None) -> bool:
    agent_config = worker_agent_config_for_job(job, scan)
    graph_config = agent_config.get("graphVerified") if isinstance(agent_config.get("graphVerified"), dict) else {}
    return graph_config.get("enabled") is True


def quota_request_id_for_scan(scan: dict | None) -> str | None:
    request_id = public_issue_text((scan or {}).get("requestId"))
    return request_id or None


def scan_quota_has_been_consumed(scan: dict | None) -> bool:
    if not isinstance(scan, dict):
        return False
    if public_issue_text(scan.get("quotaState")) in {"consumed", "refunded"}:
        return True
    if public_issue_text(scan.get("quotaState")) in {"reserved", "released"}:
        return False
    if pull_request_timestamp(scan.get("quotaConsumedAt")):
        return True
    bucket_ids = scan.get("quotaBucketIds") if isinstance(scan.get("quotaBucketIds"), dict) else {}
    if bucket_ids.get("user"):
        return True
    return isinstance(scan.get("billingUsage"), dict) or isinstance(scan.get("repoUsage"), dict)


def refresh_scan_quota_usage_locked(scan: dict, user: dict | None, repository: dict | None) -> None:
    if user:
        scan["billingUsage"] = quota.quota_payload_for_user(user)
    if repository:
        scan["repoUsage"] = quota.quota_payload_for_repository(repository, user)


def finalize_scan_quota_for_job(job: dict, *, trigger: str = "codex_started") -> dict:
    scan_id = public_issue_text(job.get("scan_id"))
    user_id = public_issue_text(job.get("user_id"))
    repo_id = public_issue_text(job.get("repo_id"))
    if not scan_id or not user_id or not repo_id:
        return {}
    with STATE_LOCK:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        request_id = quota_request_id_for_scan(scan)
        already_consumed = scan_quota_has_been_consumed(scan)
    if already_consumed:
        return {"deduplicated": True, "consumed": True}
    user = USERS.get(user_id)
    repository = db.get_repository(repo_id)
    if not user or not repository:
        return {}
    quota_result = quota.consume_reserved_scan_quota(
        user=user,
        repository=repository,
        requested_by_user_id=user_id,
        scan_id=scan_id,
        request_id=request_id,
    )
    if not quota_result.get("consumed"):
        return quota_result
    consumed_at = now()
    with STATE_LOCK:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        if scan:
            scan["quotaState"] = "consumed"
            scan["quotaConsumedAt"] = consumed_at
            scan["quotaConsumeTrigger"] = public_issue_text(trigger) or "codex_started"
            scan["quotaBucketIds"] = quota_result.get("bucketIds") or scan.get("quotaBucketIds") or {}
            refresh_scan_quota_usage_locked(scan, user, repository)
            db.upsert_scan(scan)
            mark_state_dirty()
    return quota_result


def release_scan_quota_reservation_for_scan(scan: dict, *, reason: str = "scan_cancelled") -> dict:
    scan_id = public_issue_text((scan or {}).get("id"))
    user_id = public_issue_text((scan or {}).get("userId"))
    if not scan_id or not user_id or scan_quota_has_been_consumed(scan):
        return {}
    request_id = quota_request_id_for_scan(scan)
    release_result = quota.release_scan_quota_reservation(
        scan_id=scan_id,
        requested_by_user_id=user_id,
        request_id=request_id,
        record_ledger=True,
    )
    if not release_result.get("ledgerRows") and not release_result.get("bucketRows"):
        return release_result
    user = USERS.get(user_id)
    repo_id = public_issue_text(scan.get("repoId"))
    repository = db.get_repository(repo_id) if repo_id else None
    scan["quotaState"] = "released"
    scan["quotaReleasedAt"] = now()
    scan["quotaReleaseReason"] = public_scan_error_code(reason) or public_issue_text(reason) or "scan_cancelled"
    refresh_scan_quota_usage_locked(scan, user, repository)
    db.upsert_scan(scan)
    mark_state_dirty()
    return release_result


def scan_queue_limit_error(_user_id: str = "") -> tuple[int, str, str] | None:
    counts = db.scan_queue_limit_counts()
    if counts["queued_global"] == 0:
        queued = [scan for scan in SCANS if scan.get("status") == "queued"]
        counts = {
            "queued_global": len(queued),
        }
    if counts["queued_global"] >= max_queued_scans_global():
        return HTTPStatus.TOO_MANY_REQUESTS, "The global scan queue is full. Try again after queued scans start.", "QUEUE_FULL_GLOBAL"
    return None


def scan_job_payload(job: dict, *, include_clone_token: bool = False) -> dict:
    scan = db.get_user_scan_snapshot(
        public_issue_text(job.get("user_id")),
        public_issue_text(job.get("scan_id")),
    )
    if scan is None:
        scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
    payload = {
        "job_id": public_issue_text(job.get("job_id")),
        "scan_id": public_issue_text(job.get("scan_id")),
        "repo": clean_repository_full_name(job.get("repo")),
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "commit": clean_github_access_text(job.get("commit")) or "pending",
        "status": public_issue_text(job.get("status")) if job.get("status") in SCAN_JOB_STATUSES else "queued",
        "attempt": public_scan_count(job.get("attempt")),
        "claimed_by_worker_id": public_issue_text(job.get("claimed_by_worker_id")),
        "claimed_at": pull_request_timestamp(job.get("claimed_at")),
        "started_at": pull_request_timestamp(job.get("started_at")),
        "completed_at": pull_request_timestamp(job.get("completed_at")),
        "timeout_at": pull_request_timestamp(job.get("timeout_at")),
        "error": clean_scan_error(job.get("error")),
        "result_checksum": public_issue_text(job.get("result_checksum")),
        "repo_id": clean_github_access_text(job.get("repo_id"), allow_int=True),
        "github_repo_id": clean_github_access_text(job.get("github_repo_id"), allow_int=True),
        "installation_id": clean_github_access_text(job.get("installation_id"), allow_int=True),
        "clone_url": trusted_github_web_url(job.get("clone_url")),
    }
    plan = worker_plan_for_job(job, scan)
    agent_config = billing.review_agent_config(plan)
    job_provider_chain = db.normalize_provider_list(job.get("provider_chain"))
    if job_provider_chain:
        agent_config = dict(agent_config)
        agent_config["provider"] = job_provider_chain[0]
    payload["agentConfig"] = agent_config
    repository_limits = repository_scan_limits_payload(plan)
    payload["repositoryLimits"] = repository_limits
    language = review_output_language_payload(job.get("review_output_language"))
    payload["review_output_language"] = language["code"]
    payload["review_output_language_label"] = language["label"]
    if include_clone_token:
        payload["clone_token"] = installation_clone_token_payload(job)
    if isinstance(scan, dict):
        changed_files = public_changed_files(scan.get("changedFiles") or scan.get("changed_files"))
        if changed_files:
            payload["changed_files"] = changed_files
            payload["changedFiles"] = changed_files
        base_commit = clean_github_access_text(scan.get("baseCommit") or scan.get("base_commit"))
        if base_commit:
            payload["base_commit"] = base_commit
            payload["baseCommit"] = base_commit
    return payload


def worker_task_activity_payload(job: dict) -> dict:
    claimed_at = pull_request_timestamp(job.get("claimed_at"))
    started_at = pull_request_timestamp(job.get("started_at"))
    completed_at = pull_request_timestamp(job.get("completed_at"))
    updated_at = pull_request_timestamp(job.get("updated_at"))
    created_at = pull_request_timestamp(job.get("created_at"))
    last_activity_at = max(
        [value for value in (completed_at, updated_at, started_at, claimed_at, created_at) if value],
        default=None,
    )
    return {
        "worker_id": public_issue_text(job.get("claimed_by_worker_id")),
        "job_id": public_issue_text(job.get("job_id")),
        "scan_id": public_issue_text(job.get("scan_id")),
        "repo": clean_repository_full_name(job.get("repo")),
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "commit": clean_github_access_text(job.get("commit")) or "pending",
        "status": public_issue_text(job.get("status")) if job.get("status") in SCAN_JOB_STATUSES else "queued",
        "attempt": public_scan_count(job.get("attempt")),
        "progress_phase": public_scan_phase(job.get("progress_phase")),
        "progress": public_scan_progress(job.get("progress")),
        "claimed_at": claimed_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "last_activity_at": last_activity_at,
    }


def installation_clone_token_payload(job: dict) -> dict | None:
    installation_id = clean_github_access_text(job.get("installation_id"), allow_int=True)
    if not installation_id or not github_auth.app_api_configured():
        return None
    token_payload = github_auth.create_installation_access_token(installation_id)
    token = token_payload.get("token")
    if not token:
        raise github_auth.GitHubError("GitHub did not return an installation access token.")
    return {
        "token": token,
        "expires_at": public_issue_text(token_payload.get("expires_at")),
        "repo": clean_repository_full_name(job.get("repo")),
    }


def worker_result_error_code(body: dict) -> str:
    if not isinstance(body, dict):
        return ""
    return public_scan_error_code(body.get("error_code") or body.get("errorCode"))


def worker_result_checksum(body: dict) -> str:
    provided = clean_github_access_text(body.get("result_checksum"))
    if provided:
        return provided
    digest_payload = {
        "status": body.get("status"),
        "resolved_commit": worker_result_resolved_commit(body=body),
        "summary": body.get("summary") if isinstance(body.get("summary"), dict) else {},
        "duration_ms": body.get("duration_ms"),
        "error": body.get("error"),
        "error_code": worker_result_error_code(body),
        "aiUsage": public_scan_ai_usage(body.get("aiUsage") or body.get("ai_usage")),
        "preflight": public_scan_preflight(body.get("preflight")),
        "reviewDecisionEvents": (
            body.get("review_decision_events")
            if isinstance(body.get("review_decision_events"), list)
            else body.get("reviewDecisionEvents")
            if isinstance(body.get("reviewDecisionEvents"), list)
            else []
        ),
        "graphVerifiedReport": public_graph_verified_report(
            body.get("graphVerifiedReport"),
            include_markdown=True,
            include_debug=True,
        ),
    }
    data = json.dumps(db.to_jsonable(digest_payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def worker_result_resolved_commit(
    job: dict | None = None,
    body: dict | None = None,
    preflight: dict | None = None,
) -> str:
    candidates: list[object] = []
    if isinstance(body, dict):
        candidates.extend([body.get("resolved_commit"), body.get("resolvedCommit"), body.get("commit")])
    if isinstance(preflight, dict):
        candidates.append(preflight.get("commit"))
    if isinstance(job, dict):
        candidates.append(job.get("commit"))
    for value in candidates:
        commit = clean_github_access_text(value)
        if commit and commit.lower() != "pending" and GIT_COMMIT_SHA_RE.fullmatch(commit):
            return commit.lower()
    return ""


def expected_worker_attempt_id(job: dict) -> str:
    worker_id = public_issue_text(job.get("claimed_by_worker_id"))
    attempt = public_scan_count(job.get("attempt"))
    if worker_id and attempt:
        return f"{worker_id}-{attempt}"
    return f"attempt_{attempt}"


def prepare_worker_job_result_state(job: dict, body: dict, *, status: str, checksum: str) -> dict:
    preflight = public_scan_preflight(body.get("preflight"))
    resolved_commit = worker_result_resolved_commit(job=job, body=body, preflight=preflight)
    if resolved_commit:
        preflight["commit"] = resolved_commit
    job_for_findings = dict(job)
    if resolved_commit:
        job_for_findings["commit"] = resolved_commit
    graph_verified_report = public_graph_verified_report(
        body.get("graphVerifiedReport"),
        include_markdown=True,
        include_debug=True,
    )
    if not graph_verified_report:
        raise ValueError("GraphVerified worker result must include graphVerifiedReport.")
    normalized_findings = worker_graph_verified_findings(
        job_for_findings,
        graph_verified_report,
        reserved_ids=worker_issue_reserved_ids(job_for_findings),
    )
    summary = public_scan_issue_counts(summarize_findings(normalized_findings))
    ai_usage = public_scan_ai_usage(body.get("aiUsage") or body.get("ai_usage"))
    effective_agent_config = public_scan_agent_config(body.get("effectiveAgentConfig"))
    error_code = worker_result_error_code(body)
    completed_at = pull_request_timestamp(job.get("completed_at")) or now()
    return {
        "status": status,
        "checksum": checksum,
        "preflight": preflight,
        "resolved_commit": resolved_commit,
        "graph_verified_report": graph_verified_report,
        "normalized_findings": normalized_findings,
        "summary": summary,
        "ai_usage": ai_usage,
        "effective_agent_config": effective_agent_config,
        "error_code": error_code,
        "completed_at": completed_at,
        "duration_ms": public_scan_count(body.get("duration_ms")),
        "error": clean_scan_error(body.get("error")) if status == "failed" else "",
    }


def apply_prepared_worker_job_result_to_state_locked(job: dict, prepared: dict) -> bool:
    status = public_issue_text(prepared.get("status")).lower()
    checksum = public_issue_text(prepared.get("checksum"))
    preflight = prepared.get("preflight") if isinstance(prepared.get("preflight"), dict) else {}
    resolved_commit = clean_github_access_text(prepared.get("resolved_commit"))
    normalized_findings = prepared.get("normalized_findings") if isinstance(prepared.get("normalized_findings"), list) else []
    summary = public_scan_issue_counts(prepared.get("summary"))
    ai_usage = public_scan_ai_usage(prepared.get("ai_usage"))
    effective_agent_config = public_scan_agent_config(prepared.get("effective_agent_config"))
    error_code = worker_result_error_code({"error_code": prepared.get("error_code")})
    graph_verified_report = public_graph_verified_report(
        prepared.get("graph_verified_report"),
        include_markdown=True,
        include_debug=True,
    )
    completed_at = pull_request_timestamp(prepared.get("completed_at")) or now()
    scan = memory_scan_by_id(job.get("scan_id"))
    changed = False
    if scan:
        before = json.dumps(db.to_jsonable(scan), sort_keys=True)
        scan.update(
            {
                "status": status,
                "phase": "report",
                "progress": 100 if status == "done" else public_scan_progress(scan.get("progress")),
                "completedAt": completed_at,
                "durationMs": public_scan_count(prepared.get("duration_ms")),
                "issues": summary,
                "error": clean_scan_error(prepared.get("error")) if status == "failed" else "",
                "resultChecksum": checksum,
            }
        )
        if status == "failed" and error_code:
            scan["errorCode"] = error_code
        else:
            scan.pop("errorCode", None)
        if resolved_commit:
            scan["commit"] = resolved_commit
        for key in (
            "auditSwarm",
            "completionAudit",
            "convergenceState",
            "impactGraph",
            "jobTrace",
            "repositoryGraph",
            "semanticGraph",
            "verificationAudit",
        ):
            scan.pop(key, None)
        if preflight:
            scan["preflight"] = preflight
        if ai_usage:
            scan["aiUsage"] = ai_usage
        if effective_agent_config:
            scan["effectiveAgentConfig"] = effective_agent_config
        scan["graphVerifiedReport"] = graph_verified_report
        changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
        if status == "done":
            before_issues = json.dumps(
                db.to_jsonable([issue for issue in ISSUES if issue.get("scanId") == scan.get("id") and issue.get("jobId") == job.get("job_id")]),
                sort_keys=True,
            )
            ISSUES[:] = [
                issue
                for issue in ISSUES
                if not (issue.get("scanId") == scan.get("id") and issue.get("jobId") == job.get("job_id"))
            ]
            ISSUES.extend(normalized_findings)
            db.replace_scan_issues(
                public_issue_text(scan.get("id")),
                user_id=public_issue_text(scan.get("userId")),
                job_id=public_issue_text(job.get("job_id")),
                issues=normalized_findings,
            )
            after_issues = json.dumps(db.to_jsonable(normalized_findings), sort_keys=True)
            changed = changed or before_issues != after_issues
    if changed:
        db.upsert_scan(scan)
        mark_state_dirty()
    return changed


def apply_worker_job_result_to_state_locked(job: dict, body: dict, *, status: str, checksum: str) -> bool:
    prepared = prepare_worker_job_result_state(job, body, status=status, checksum=checksum)
    return apply_prepared_worker_job_result_to_state_locked(job, prepared)


def apply_worker_job_result(job: dict, body: dict) -> dict:
    status = public_issue_text(body.get("status")).lower()
    if status not in {"done", "failed"}:
        raise ValueError("status must be done or failed")
    expected_attempt_id = expected_worker_attempt_id(job)
    attempt_id = clean_github_access_text(body.get("attempt_id")) or expected_attempt_id
    if attempt_id != expected_attempt_id:
        return {"accepted": False, "conflict": True}
    graph_verified_report = public_graph_verified_report(
        body.get("graphVerifiedReport"),
        include_markdown=True,
        include_debug=True,
    )
    if not graph_verified_report:
        raise ValueError("GraphVerified worker result must include graphVerifiedReport.")
    checksum = worker_result_checksum(body)
    record_result = db.record_scan_job_result(
        str(job["job_id"]),
        attempt_id=attempt_id,
        status=status,
        result_checksum=checksum,
        payload=body,
    )
    if record_result.get("conflict"):
        return {"accepted": False, "conflict": True}
    duplicate = bool(record_result.get("duplicate"))
    if duplicate:
        quota_rollback = rollback_scan_quota_for_refundable_worker_failure(job, body, status=status)
        result = {"accepted": True, "duplicate": True, "conflict": False, "issueCount": worker_result_issue_count(body)}
        if quota_rollback.get("reservationReleased"):
            result["quotaRelease"] = quota_rollback
        elif quota_rollback.get("ledgerRows"):
            result["quotaRollback"] = quota_rollback
        return result
    resolved_commit = worker_result_resolved_commit(job=job, body=body)
    if resolved_commit:
        updated_job = db.update_scan_job_commit(str(job["job_id"]), resolved_commit)
        if updated_job:
            job = updated_job
        else:
            job = {**job, "commit": resolved_commit}
    event_result = record_worker_review_decision_events(job, body, attempt_id=attempt_id, status=status)
    quota_finalized = {}
    if worker_result_should_finalize_quota(job, body, status=status):
        quota_finalized = finalize_scan_quota_for_job(job, trigger="worker_result")
    prepared_result = prepare_worker_job_result_state(job, body, status=status, checksum=checksum)
    with STATE_LOCK:
        apply_prepared_worker_job_result_to_state_locked(job, prepared_result)
    quota_rollback = rollback_scan_quota_for_refundable_worker_failure(job, body, status=status)
    result = {
        "accepted": True,
        "duplicate": duplicate,
        "conflict": False,
        "issueCount": worker_result_issue_count(body),
        "reviewDecisionEvents": event_result,
    }
    if quota_finalized.get("consumed"):
        result["quotaConsumed"] = True
    if quota_rollback.get("reservationReleased"):
        result["quotaRelease"] = quota_rollback
    elif quota_rollback.get("ledgerRows"):
        result["quotaRollback"] = quota_rollback
    return result


def worker_result_issue_count(body: dict) -> int:
    report = public_graph_verified_report(body.get("graphVerifiedReport")) if isinstance(body, dict) else {}
    return public_scan_count(report.get("confirmedCount"))


def worker_result_should_finalize_quota(job: dict, body: dict, *, status: str) -> bool:
    if status == "done":
        return True
    if public_scan_phase(job.get("progress_phase")) in {"ai", "report"}:
        return True
    if public_scan_ai_usage(body.get("aiUsage") or body.get("ai_usage")):
        return True
    return False


def rollback_scan_quota_for_refundable_worker_failure(job: dict, body: dict, *, status: str) -> dict:
    if status != "failed" or worker_result_error_code(body) != "REPOSITORY_TOO_LARGE":
        return {}
    scan_id = public_issue_text(job.get("scan_id"))
    user_id = public_issue_text(job.get("user_id"))
    if not scan_id or not user_id:
        return {}
    with STATE_LOCK:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        request_id = public_issue_text((scan or {}).get("requestId")) or None
        repo_id = public_issue_text((scan or {}).get("repoId") or job.get("repo_id"))
        has_repository_limit_evidence = worker_result_has_repository_limit_evidence(body, scan)
        quota_consumed = scan_quota_has_been_consumed(scan)
    if not has_repository_limit_evidence:
        return {}
    if not quota_consumed:
        release_result = quota.release_scan_quota_reservation(
            scan_id=scan_id,
            requested_by_user_id=user_id,
            request_id=request_id,
            record_ledger=True,
        )
        if not release_result.get("ledgerRows"):
            return release_result
        user = USERS.get(user_id)
        repository = db.get_repository(repo_id) if repo_id else None
        with STATE_LOCK:
            scan = next((item for item in SCANS if item.get("id") == scan_id), None)
            if scan:
                scan["quotaState"] = "released"
                scan["quotaReleasedAt"] = now()
                scan["quotaReleaseReason"] = "REPOSITORY_TOO_LARGE"
                refresh_scan_quota_usage_locked(scan, user, repository)
                db.upsert_scan(scan)
                mark_state_dirty()
        release_result["reservationReleased"] = True
        return release_result
    rollback_result = quota.rollback_scan_quota(
        scan_id=scan_id,
        requested_by_user_id=user_id,
        request_id=request_id,
    )
    if not rollback_result.get("ledgerRows"):
        return rollback_result

    user = USERS.get(user_id)
    repository = db.get_repository(repo_id) if repo_id else None
    user_usage = quota.quota_payload_for_user(user) if user else None
    repo_usage = quota.quota_payload_for_repository(repository, user) if repository else None
    with STATE_LOCK:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        if scan:
            if user_usage:
                scan["billingUsage"] = user_usage
            if repo_usage:
                scan["repoUsage"] = repo_usage
            scan["quotaRefunded"] = {
                "reason": "REPOSITORY_TOO_LARGE",
                "ledgerRows": public_scan_count(rollback_result.get("ledgerRows")),
                "bucketRows": public_scan_count(rollback_result.get("bucketRows")),
            }
            scan["quotaState"] = "refunded"
            db.upsert_scan(scan)
            mark_state_dirty()
    return rollback_result


def worker_result_has_repository_limit_evidence(body: dict, scan: dict | None) -> bool:
    preflight = public_scan_preflight(body.get("preflight") if isinstance(body, dict) else None)
    if not preflight and isinstance(scan, dict):
        preflight = public_scan_preflight(scan.get("preflight"))
    if preflight.get("repositoryLimitExceeded") is not True:
        return False
    limits = preflight.get("repositoryLimits") if isinstance(preflight.get("repositoryLimits"), dict) else {}
    reasons = preflight.get("repositoryLimitReasons") if isinstance(preflight.get("repositoryLimitReasons"), list) else []
    return bool(reasons or public_scan_count(limits.get("maxFiles")) or public_scan_count(limits.get("maxBytes")))


def worker_issue_reserved_ids(job: dict) -> set[str]:
    user_id = public_issue_text(job.get("user_id"))
    scan_id = public_issue_text(job.get("scan_id"))
    job_id = public_issue_text(job.get("job_id"))
    reserved = set(db.list_user_issue_ids(user_id, exclude_scan_id=scan_id, exclude_job_id=job_id))
    if reserved or db.count_user_issues(user_id) > 0:
        return reserved
    reserved = set()
    for issue in ISSUES:
        if user_id and public_issue_text(issue.get("userId")) != user_id:
            continue
        if public_issue_text(issue.get("scanId")) == scan_id and public_issue_text(issue.get("jobId")) == job_id:
            continue
        issue_id = public_issue_text(issue.get("id"))
        if issue_id:
            reserved.add(issue_id)
    return reserved


def unique_issue_id(base_id: object, used_ids: set[str]) -> str:
    issue_id = public_issue_text(base_id) or make_id("iss")
    if issue_id not in used_ids:
        used_ids.add(issue_id)
        return issue_id
    suffix = 2
    while True:
        candidate = f"{issue_id}-{suffix}"
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate
        suffix += 1


def worker_graph_verified_findings(job: dict, report: dict, *, reserved_ids: set[str] | None = None) -> list[dict]:
    final_json = report.get("finalJson") if isinstance(report.get("finalJson"), dict) else {}
    confirmed = final_json.get("confirmed") if isinstance(final_json.get("confirmed"), list) else []
    used_issue_ids = set(reserved_ids or set())
    findings = []
    for index, item in enumerate(confirmed):
        if not isinstance(item, dict):
            continue
        issue = worker_graph_verified_item_to_finding(job, report, item, index)
        if not issue:
            continue
        issue["id"] = unique_issue_id(issue.get("id"), used_issue_ids)
        findings.append(issue)
    return findings


def worker_graph_verified_item_to_finding(job: dict, report: dict, item: dict, index: int) -> dict:
    if not graph_verified_report_item_is_public(item):
        return {}
    public_item = public_graph_verified_confirmed_item(item)
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    judge = item.get("judge") if isinstance(item.get("judge"), dict) else {}
    repro = item.get("repro") if isinstance(item.get("repro"), dict) else {}
    verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
    graph_evidence = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    reproduction = worker_graph_verified_reproduction(candidate, judge, repro)
    code_evidence = worker_graph_verified_code_evidence(candidate.get("evidence"), job=job)
    locations = worker_graph_verified_locations(code_evidence, job=job)
    primary = locations[0] if locations else {}
    candidate_id = public_issue_text(candidate.get("candidate_id") or candidate.get("issue_id")) or f"candidate_{index + 1}"
    title = public_issue_text(candidate.get("title")) or review._safe_text_lenient(candidate.get("claim")).split(". ", 1)[0]
    if not title:
        title = f"Graph-verified finding {index + 1}"
    limitations = []
    limitations.extend(review._safe_text_list(judge.get("limitations")))
    limitations.extend(review._safe_text_list(repro.get("limitations")))
    finding = {
        "id": public_issue_text(candidate.get("issue_id")) or candidate_id,
        "userId": public_issue_text(job.get("user_id")),
        "scanId": public_issue_text(job.get("scan_id")),
        "jobId": public_issue_text(job.get("job_id")),
        "repo": clean_repository_full_name(job.get("repo")),
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "commit": clean_github_access_text(job.get("commit")) or "pending",
        "status": "open",
        "createdAt": now(),
        "graphVerified": True,
        "candidateId": candidate_id,
        "dedupeKey": public_issue_text(candidate.get("dedupe_key")),
        "severity": worker_graph_verified_severity(candidate.get("severity")),
        "category": review._safe_category(candidate.get("category")) or "Quality",
        "title": title[:240],
        "summary": review._safe_text_lenient(candidate.get("claim") or judge.get("reason") or repro.get("summary")),
        "graphEvidence": graph_evidence,
        "codeEvidence": code_evidence,
        "triggerCondition": review._safe_text_lenient(candidate.get("trigger_condition")),
        "expectedBehavior": review._safe_text_lenient(candidate.get("expected_behavior")),
        "observedBehavior": (
            review._safe_text_lenient(worker_graph_verified_observed_behavior(candidate, judge, repro))
        ),
        "reproduction": reproduction,
        "judgeEvidence": worker_graph_verified_judge_evidence(judge),
        "reproProof": worker_graph_verified_repro_proof(repro),
        "verificationLevel": public_issue_text(judge.get("level") or repro.get("level") or verification.get("level")),
        "safeToShowUser": True,
        "whyThisMatters": worker_graph_verified_why_this_matters(candidate, code_evidence),
        "suggestedFixDirection": review._safe_text_lenient(candidate.get("fix_direction") or candidate.get("suggested_fix")),
        "limitations": list(dict.fromkeys(item for item in limitations if item))[:8],
        "affectedLocations": locations,
        "file": public_issue_text(primary.get("file")),
        "line": public_scan_count(primary.get("startLine")),
        "graphVerifiedReport": {
            "runId": public_issue_text(report.get("runId")),
            "mode": public_issue_text(report.get("mode")),
            "base": public_issue_text(report.get("base")),
            "head": public_issue_text(report.get("head")),
        },
    }
    if public_item:
        finding["graphVerifiedItem"] = public_item
    return {key: value for key, value in finding.items() if value not in ("", [], {})}


def worker_graph_verified_code_evidence(value: object, *, job: dict | None = None) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    evidence = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        file_path = public_issue_file(raw_item.get("file") or raw_item.get("path"), job=job)
        lines = graph_verified_evidence_line_text(raw_item)
        why = review._safe_text_lenient(raw_item.get("why_it_matters") or raw_item.get("summary"))
        item = {}
        if file_path:
            item["file"] = file_path
        if lines:
            item["lines"] = lines
        if why:
            item["why_it_matters"] = why
        if item:
            evidence.append(item)
        if len(evidence) >= 20:
            break
    return evidence


def worker_graph_verified_locations(evidence: list[dict], *, job: dict | None = None) -> list[dict]:
    locations = []
    seen = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        file_path = public_issue_file(item.get("file"), job=job)
        if not file_path:
            continue
        start_line, end_line = worker_graph_verified_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    return locations[:10]


def worker_graph_verified_reproduction(candidate: dict, judge: dict, repro: dict) -> dict:
    commands = []
    exit_code = None
    raw_commands = repro.get("commands_run") if isinstance(repro.get("commands_run"), list) else []
    for item in raw_commands:
        if isinstance(item, dict):
            command = public_issue_text(item.get("cmd") or item.get("command"))
            if command:
                commands.append(command)
            if exit_code is None and graph_verified_command_has_exit_code(item):
                try:
                    exit_code = int(item.get("exit_code") if "exit_code" in item else item.get("exitCode"))
                except (TypeError, ValueError):
                    exit_code = None
        else:
            command = public_issue_text(item)
            if command:
                commands.append(command)
    proof = repro.get("proof") if isinstance(repro.get("proof"), dict) else {}
    evidence_summary = judge.get("evidence_summary") if isinstance(judge.get("evidence_summary"), dict) else {}
    if not commands and public_issue_text(evidence_summary.get("command")):
        commands.append(public_issue_text(evidence_summary.get("command")))
    log_path = ""
    for item in raw_commands:
        if isinstance(item, dict):
            log_path = public_issue_text(item.get("log_path") or item.get("logPath"))
            if log_path:
                break
    log_path = log_path or public_issue_text(evidence_summary.get("log_path"))
    reproduction = {
        "commands": list(dict.fromkeys(commands))[:5],
        "input": review._safe_text_lenient(candidate.get("trigger_condition")),
        "expected": review._safe_text_lenient(proof.get("expected") or candidate.get("expected_behavior")),
        "actual": review._safe_text_lenient(
            proof.get("actual")
            or evidence_summary.get("observable")
            or repro.get("summary")
            or candidate.get("actual_behavior_hypothesis")
        ),
        "logPath": log_path,
    }
    if exit_code is not None:
        reproduction["exitCode"] = exit_code
    return reproduction


def worker_graph_verified_judge_evidence(judge: dict) -> dict:
    evidence_summary = judge.get("evidence_summary") if isinstance(judge.get("evidence_summary"), dict) else {}
    payload = {
        "status": public_issue_text(judge.get("status")),
        "level": public_issue_text(judge.get("level")),
        "safeToShowUser": judge.get("safe_to_show_user") is True,
        "reason": review._safe_text_lenient(judge.get("reason")),
        "command": public_issue_text(evidence_summary.get("command")),
        "logPath": public_issue_text(evidence_summary.get("log_path")),
        "observable": review._safe_text_lenient(evidence_summary.get("observable")),
    }
    if "safe_to_show_user" not in judge:
        payload.pop("safeToShowUser", None)
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def worker_graph_verified_repro_proof(repro: dict) -> dict:
    proof = repro.get("proof") if isinstance(repro.get("proof"), dict) else {}
    payload = {
        "type": public_issue_text(proof.get("type")),
        "expected": review._safe_text_lenient(proof.get("expected")),
        "actual": review._safe_text_lenient(proof.get("actual")),
        "logExcerpt": review._safe_text_lenient(proof.get("log_excerpt")),
        "graphPathExercised": repro.get("graph_path_exercised") is True,
    }
    if "graph_path_exercised" not in repro:
        payload.pop("graphPathExercised", None)
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def worker_graph_verified_observed_behavior(candidate: dict, judge: dict, repro: dict) -> str:
    proof = repro.get("proof") if isinstance(repro.get("proof"), dict) else {}
    evidence_summary = judge.get("evidence_summary") if isinstance(judge.get("evidence_summary"), dict) else {}
    return (
        proof.get("actual")
        or evidence_summary.get("observable")
        or repro.get("summary")
        or candidate.get("actual_behavior_hypothesis")
        or ""
    )


def worker_graph_verified_why_this_matters(candidate: dict, code_evidence: list[dict]) -> str:
    for item in code_evidence:
        text = review._safe_text_lenient(item.get("why_it_matters"))
        if text:
            return text
    return review._safe_text_lenient(candidate.get("impact") or candidate.get("why_this_matters"))


def worker_graph_verified_severity(value: object) -> str:
    severity = public_issue_text(value).lower()
    return severity if severity in {"critical", "high", "medium", "low", "info"} else "info"


def worker_graph_verified_line_range(source: dict) -> tuple[int, int]:
    if not isinstance(source, dict):
        return (0, 0)
    lines = public_issue_text(source.get("lines"))
    if lines:
        numbers = [int(item) for item in re.findall(r"\d+", lines)[:2]]
        if numbers:
            start = numbers[0]
            end = numbers[1] if len(numbers) > 1 and numbers[1] >= start else start
            return start, end
    start = review._safe_non_negative_int(
        source.get("startLine")
        or source.get("start_line")
        or source.get("lineStart")
        or source.get("line_start")
        or source.get("line")
    )
    end = review._safe_non_negative_int(
        source.get("endLine")
        or source.get("end_line")
        or source.get("lineEnd")
        or source.get("line_end")
    )
    if not end or end < start:
        end = start
    return start, end


def worker_finding_payload(job: dict, finding: object, index: int) -> dict:
    source = finding if isinstance(finding, dict) else {}
    scan_id = public_issue_text(job.get("scan_id"))
    repo = clean_repository_full_name(job.get("repo"))
    issue = dict(source)
    issue.setdefault("id", make_id("iss"))
    issue.update(
        {
            "userId": public_issue_text(job.get("user_id")),
            "scanId": scan_id,
            "jobId": public_issue_text(job.get("job_id")),
            "repo": repo,
            "branch": clean_github_access_text(job.get("branch")) or "main",
            "commit": clean_github_access_text(job.get("commit")) or "pending",
            "status": public_issue_status(issue.get("status")),
            "createdAt": now(),
        }
    )
    issue["file"] = public_issue_file(issue.get("file"), job=job)
    issue["affectedLocations"] = public_issue_affected_locations(issue, job=job)
    issue["evidence"] = public_issue_evidence(issue, job=job, affected_locations=issue["affectedLocations"])
    issue["reproduction"] = public_issue_reproduction(issue, job=job)
    reported_verification_status = public_issue_text(issue.get("verificationStatus")).lower()
    if reported_verification_status in ISSUE_VERIFICATION_STATUSES:
        issue["reportedVerificationStatus"] = reported_verification_status
    issue["verificationStatus"] = public_issue_verification_status(
        issue,
        affected_locations=issue["affectedLocations"],
        evidence=issue["evidence"],
        reproduction=issue["reproduction"],
    )
    issue["evidenceChecklist"] = public_issue_evidence_checklist(
        issue,
        affected_locations=issue["affectedLocations"],
        evidence=issue["evidence"],
        reproduction=issue["reproduction"],
    )
    issue["confidenceLevel"] = public_issue_confidence_level(
        issue["verificationStatus"],
        issue["evidenceChecklist"],
    )
    if not public_issue_text(issue.get("title")):
        issue["title"] = f"Finding {index + 1}"
    return issue
