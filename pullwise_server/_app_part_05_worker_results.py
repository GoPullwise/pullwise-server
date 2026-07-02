from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_04_scan_audit_bundle as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

REQUIRED_COMPLETED_REVIEW_ARTIFACT_KINDS = {
    "report.human",
    "report.agent",
    "coverage",
    "qa",
    "token_budget",
}
WORKER_REVIEW_ARTIFACT_KINDS = {
    "report.human",
    "report.agent",
    "coverage",
    "qa",
    "token_budget",
    "repo_inventory",
    "repo_map",
    "risk_routing",
    "bundle_plan",
    "cluster_result",
    "validation_result",
    "raw_reviewer_output",
    "verified_reviewer_output",
    "codex_event_log",
    "worker_log",
    "progress_log",
    "error_report",
    "intent_map",
    "intent_test_plan",
    "intent_test_source",
    "intent_test_result",
    "intent_test_output",
    "disposable_test_patch",
}
REQUIRED_TERMINAL_REVIEW_ARTIFACT_KINDS = {"qa", "worker_log"}
REQUIRED_TERMINAL_REVIEW_ARTIFACT_ALTERNATIVES = {"error_report", "report.agent"}


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
            "worker_scope": scan.get("workerScope") or scan.get("worker_scope"),
            "worker_owner_user_id": scan.get("workerOwnerUserId") or scan.get("worker_owner_user_id") or scan.get("userId"),
        }
    )
    scan["jobId"] = job.get("job_id")
    with STATE_LOCK:
        remember_scan_snapshot_locked(scan)
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
        "claimedAt",
        "claimedByWorkerId",
        "completedAt",
        "durationMs",
        "effectiveAgentConfig",
        "error",
        "errorCode",
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
        "resultChecksum",
        "startedAt",
        "updatedAt",
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
        retried_job = db.retry_scan_job(
            scan_id,
            timestamp=queued_at,
            max_attempts=system_config.scan_job_max_attempts(),
        )
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


def review_job_repository_payload(job: dict) -> dict:
    repo = clean_repository_full_name(job.get("repo"))
    owner, separator, name = repo.partition("/")
    payload = {
        "provider": "github",
        "owner": owner if separator else "",
        "name": name if separator else repo,
        "full_name": repo,
        "clone_url": trusted_github_web_url(job.get("clone_url")),
        "commit_sha": clean_github_access_text(job.get("commit")) or "pending",
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "repo_id": clean_github_access_text(job.get("repo_id"), allow_int=True),
        "github_repo_id": clean_github_access_text(job.get("github_repo_id"), allow_int=True),
        "installation_id": clean_github_access_text(job.get("installation_id"), allow_int=True),
    }
    return {key: value for key, value in payload.items() if value not in {"", None}}


def review_job_model_profile(agent_config: dict) -> dict:
    codex = agent_config.get("codex") if isinstance(agent_config.get("codex"), dict) else {}
    effort = public_issue_text(codex.get("reasoningEffort")) or "medium"
    return {
        "default_model": public_issue_text(codex.get("model")),
        "core_effort": effort,
        "reviewer_effort": effort,
        "validator_effort": effort,
        "reporter_effort": effort,
        "intent_test_effort": effort,
        "non_core_effort": "medium",
    }


def review_job_intent_policy(agent_config: dict) -> dict:
    review_worker = agent_config.get("reviewWorker") if isinstance(agent_config.get("reviewWorker"), dict) else {}
    configured = review_worker.get("intentTestValidation") if isinstance(review_worker.get("intentTestValidation"), dict) else {}
    return {
        "enabled": configured.get("enabled", True) is not False,
        "max_tests_per_run": public_scan_count(configured.get("maxTestsPerRun")) or 20,
        "max_tests_per_bundle": public_scan_count(configured.get("maxTestsPerBundle")) or 2,
        "max_test_run_seconds_per_test": public_scan_count(configured.get("maxTestRunSecondsPerTest")) or 60,
        "max_total_test_run_seconds": public_scan_count(configured.get("maxTotalTestRunSeconds")) or 900,
        "only_tiers": configured.get("onlyTiers") or ["P0", "P1"],
    }


def review_job_review_request_payload(agent_config: dict, repository_limits: dict, language: dict) -> dict:
    review_worker = agent_config.get("reviewWorker") if isinstance(agent_config.get("reviewWorker"), dict) else {}
    max_wall_time_seconds = public_scan_count(review_worker.get("scanDeadlineSeconds")) or 0
    return {
        "mode": "full_repo",
        "profile": "standard",
        "focus": ["security", "correctness", "test_gap"],
        "output_language": language["code"],
        "budget": {
            "max_estimated_input_tokens": public_scan_count(repository_limits.get("maxEstimatedInputTokens")) or 800000,
            "max_wall_time_seconds": max_wall_time_seconds,
        },
        "policy": {
            "allow_source_modification": False,
            "allow_dependency_install": False,
            "allow_network": False,
            "helper_scripts_standard_library_only": True,
            "turn_timeout_seconds": public_scan_count(review_worker.get("turnTimeoutSeconds")) or 0,
            "intent_test_validation": review_job_intent_policy(agent_config),
        },
    }


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

WORKER_QUOTA_CONSUMING_PHASES = frozenset(
    {
        "repo_map",
        "risk_routing",
        "reviewer_fanout",
        "clustering_and_voting",
        "validator_disproof",
        "final_report_json",
    }
)


def worker_progress_phase_should_finalize_quota(phase: object) -> bool:
    return public_scan_phase(phase) in WORKER_QUOTA_CONSUMING_PHASES


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
        scan_worker_scope = scan.get("workerScope") if scan else None
        private_worker_scan = (
            db.normalize_worker_scope(job.get("worker_scope") or scan_worker_scope) == db.WORKER_SCOPE_PRIVATE
            or public_issue_text(scan.get("quotaState") if scan else "") == "private_worker"
        )
    if private_worker_scan:
        return {"skipped": True, "reason": "private_worker"}
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
        "job_type": "repo_review.full_scan",
        "priority": public_issue_text(job.get("priority")) or "normal",
        "run_id": public_issue_text(job.get("run_id")) or f"run_{public_issue_text(job.get('job_id'))}",
        "lease_id": public_issue_text(job.get("lease_id")) or f"lease_{public_issue_text(job.get('job_id'))}",
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
        "max_attempts": max(1, public_scan_count(db.scan_job_retry_state(job).get("maxAttempts"))),
        "retry": scan_retry_summary_for_job(job),
        "repo_id": clean_github_access_text(job.get("repo_id"), allow_int=True),
        "github_repo_id": clean_github_access_text(job.get("github_repo_id"), allow_int=True),
        "installation_id": clean_github_access_text(job.get("installation_id"), allow_int=True),
        "clone_url": trusted_github_web_url(job.get("clone_url")),
    }
    worker_scope = db.normalize_worker_scope(job.get("worker_scope"))
    if worker_scope == db.WORKER_SCOPE_PRIVATE:
        payload["workerScope"] = worker_scope
        payload["privateWorker"] = True
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
    payload["repository"] = review_job_repository_payload(job)
    payload["model_profile"] = review_job_model_profile(agent_config)
    payload["review_request"] = review_job_review_request_payload(agent_config, repository_limits, language)
    if include_clone_token:
        payload["clone_token"] = installation_clone_token_payload(job)
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


def public_review_worker_protocol(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    if public_issue_text(value.get("protocol_version")) != "review-worker-protocol/v1":
        return {}
    if public_issue_text(value.get("message_type")) not in {"", "review_run_result"}:
        return {}
    payload = db.to_jsonable(value)
    return payload if isinstance(payload, dict) else {}


def review_worker_protocol_envelope(body: dict) -> dict:
    if not isinstance(body, dict):
        return {}
    return public_review_worker_protocol(body.get("reviewWorkerProtocol") or body.get("review_worker_protocol"))


def validate_review_worker_protocol_envelope(job: dict, body: dict, *, status: str = "") -> dict:
    envelope = review_worker_protocol_envelope(body)
    if not envelope:
        return {}
    errors = []
    envelope_job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    envelope_worker = envelope.get("worker") if isinstance(envelope.get("worker"), dict) else {}
    execution = envelope.get("execution") if isinstance(envelope.get("execution"), dict) else {}
    progress_final = envelope.get("progress_final") if isinstance(envelope.get("progress_final"), dict) else {}
    summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
    quality_gate = envelope.get("quality_gate") if isinstance(envelope.get("quality_gate"), dict) else {}
    manifest = envelope.get("artifact_manifest") if isinstance(envelope.get("artifact_manifest"), list) else None
    expected_job_id = public_issue_text(job.get("job_id"))
    expected_run_id = public_issue_text(job.get("run_id")) or f"run_{expected_job_id}"
    expected_lease_id = public_issue_text(job.get("lease_id")) or f"lease_{expected_job_id}"
    expected_worker_id = public_issue_text(job.get("claimed_by_worker_id"))
    if public_issue_text(envelope_job.get("job_id")) != expected_job_id:
        errors.append("job.job_id")
    if public_issue_text(envelope_job.get("run_id")) != expected_run_id:
        errors.append("job.run_id")
    if public_issue_text(envelope_job.get("lease_id")) != expected_lease_id:
        errors.append("job.lease_id")
    if expected_worker_id and public_issue_text(envelope_worker.get("worker_id")) != expected_worker_id:
        errors.append("worker.worker_id")
    execution_status = public_issue_text(execution.get("status"))
    if execution_status not in {"completed", "failed", "cancelled", "partial_completed"}:
        errors.append("execution.status")
    wrapper_status = public_issue_text(status).lower()
    if wrapper_status == "done" and execution_status != "completed":
        errors.append("execution.status")
    if wrapper_status == "failed" and execution_status == "completed":
        errors.append("execution.status")
    if wrapper_status == "cancelled" and execution_status != "cancelled":
        errors.append("execution.status")
    if wrapper_status == "partial_completed" and execution_status != "partial_completed":
        errors.append("execution.status")
    if manifest is None:
        errors.append("artifact_manifest")
    else:
        manifest_kinds = set()
        for index, item in enumerate(manifest):
            if not isinstance(item, dict):
                errors.append(f"artifact_manifest[{index}]")
                continue
            kind = public_issue_text(item.get("kind"))
            if kind:
                manifest_kinds.add(kind)
            artifact_id = public_issue_text(item.get("artifact_id"))
            for field in ("artifact_id", "kind", "name", "media_type", "schema_id", "schema_version", "encoding", "compression", "sha256"):
                if not public_issue_text(item.get(field)):
                    errors.append(f"artifact_manifest[{index}].{field}")
            if kind and kind not in WORKER_REVIEW_ARTIFACT_KINDS:
                errors.append(f"artifact_manifest[{index}].kind")
            if public_issue_text(item.get("schema_version")) != "v1":
                errors.append(f"artifact_manifest[{index}].schema_version")
            if public_issue_text(item.get("encoding")) != "utf-8":
                errors.append(f"artifact_manifest[{index}].encoding")
            if public_issue_text(item.get("compression")) != "none":
                errors.append(f"artifact_manifest[{index}].compression")
            sha256 = public_issue_text(item.get("sha256")).lower()
            if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
                errors.append(f"artifact_manifest[{index}].sha256")
            if item.get("required") not in {True, False}:
                errors.append(f"artifact_manifest[{index}].required")
            if not isinstance(item.get("size_bytes"), int) or item.get("size_bytes") < 0:
                errors.append(f"artifact_manifest[{index}].size_bytes")
            storage = item.get("storage") if isinstance(item.get("storage"), dict) else {}
            storage_url = public_issue_text(storage.get("url"))
            if (
                public_issue_text(storage.get("type")) != "server_artifact"
                or not storage_url.startswith("/v1/review-runs/")
                or (artifact_id and not storage_url.endswith(f"/artifacts/{artifact_id}"))
            ):
                errors.append(f"artifact_manifest[{index}].storage")
        if execution_status == "completed":
            missing_required_kinds = sorted(REQUIRED_COMPLETED_REVIEW_ARTIFACT_KINDS - manifest_kinds)
            if missing_required_kinds:
                errors.append("artifact_manifest.required_completed_kinds:" + ",".join(missing_required_kinds))
        elif execution_status in {"failed", "cancelled", "partial_completed"}:
            missing_terminal_kinds = sorted(REQUIRED_TERMINAL_REVIEW_ARTIFACT_KINDS - manifest_kinds)
            if missing_terminal_kinds:
                errors.append("artifact_manifest.required_terminal_kinds:" + ",".join(missing_terminal_kinds))
            if not (REQUIRED_TERMINAL_REVIEW_ARTIFACT_ALTERNATIVES & manifest_kinds):
                errors.append("artifact_manifest.required_terminal_report:error_report_or_report.agent")
    if not public_issue_text(quality_gate.get("status")):
        errors.append("quality_gate.status")
    if not summary:
        errors.append("summary")
    else:
        if not public_issue_text(summary.get("overall_risk")):
            errors.append("summary.overall_risk")
        if not public_issue_text(summary.get("result_status")):
            errors.append("summary.result_status")
        if not isinstance(summary.get("finding_counts"), dict):
            errors.append("summary.finding_counts")
        if not isinstance(summary.get("coverage"), dict):
            errors.append("summary.coverage")
        if not isinstance(summary.get("top_findings"), list):
            errors.append("summary.top_findings")
    if not progress_final:
        errors.append("progress_final")
    else:
        try:
            overall_percent = float(progress_final.get("overall_percent"))
        except (TypeError, ValueError):
            errors.append("progress_final.overall_percent")
        else:
            if overall_percent < 0 or overall_percent > 100:
                errors.append("progress_final.overall_percent")
        if not public_issue_text(progress_final.get("status")):
            errors.append("progress_final.status")
    if errors:
        raise ValueError("Invalid review-worker-protocol/v1 envelope: " + ", ".join(errors[:12]))
    return envelope


def validate_review_worker_protocol_artifacts(job: dict, body: dict, *, status: str = "") -> None:
    envelope = validate_review_worker_protocol_envelope(job, body, status=status)
    if not envelope:
        return
    envelope_job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    run_id = public_issue_text(envelope_job.get("run_id") or job.get("run_id")) or f"run_{public_issue_text(job.get('job_id'))}"
    uploaded = {
        public_issue_text(item.get("artifact_id")): item
        for item in db.list_review_run_artifact_records(run_id)
        if isinstance(item, dict) and public_issue_text(item.get("artifact_id"))
    }
    manifest = envelope.get("artifact_manifest") if isinstance(envelope.get("artifact_manifest"), list) else []
    execution = envelope.get("execution") if isinstance(envelope.get("execution"), dict) else {}
    execution_status = public_issue_text(execution.get("status"))
    extensions = envelope.get("extensions") if isinstance(envelope.get("extensions"), dict) else {}
    worker_internal = extensions.get("worker_internal") if isinstance(extensions.get("worker_internal"), dict) else {}
    upload_error_recorded = bool(public_issue_text(worker_internal.get("artifact_upload_error")))
    allow_missing_uploads = execution_status in {"failed", "cancelled", "partial_completed"} and upload_error_recorded
    missing = []
    mismatched = []
    for item in manifest:
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        artifact_id = public_issue_text(item.get("artifact_id"))
        if not artifact_id:
            missing.append("<missing artifact_id>")
            continue
        stored = uploaded.get(artifact_id)
        if not stored:
            missing.append(artifact_id)
            continue
        if public_issue_text(stored.get("sha256")).lower() != public_issue_text(item.get("sha256")).lower():
            mismatched.append(artifact_id)
            continue
        if public_scan_count(stored.get("size_bytes")) != public_scan_count(item.get("size_bytes")):
            mismatched.append(artifact_id)
    if missing and not allow_missing_uploads:
        raise ValueError("Required review artifacts were not uploaded before result submit: " + ", ".join(missing[:10]))
    if mismatched:
        raise ValueError("Uploaded review artifacts do not match result manifest: " + ", ".join(mismatched[:10]))


def worker_result_error_code(body: dict) -> str:
    if not isinstance(body, dict):
        return ""
    return public_scan_error_code(body.get("error_code") or body.get("errorCode"))


def worker_result_checksum(body: dict) -> str:
    digest_payload = {
        "status": body.get("status"),
        "resolved_commit": worker_result_resolved_commit(body=body),
        "summary": body.get("summary") if isinstance(body.get("summary"), dict) else {},
        "duration_ms": body.get("duration_ms"),
        "error": body.get("error"),
        "error_code": worker_result_error_code(body),
        "preflight": public_scan_preflight(body.get("preflight")),
        "reviewDecisionEvents": (
            body.get("review_decision_events")
            if isinstance(body.get("review_decision_events"), list)
            else body.get("reviewDecisionEvents")
            if isinstance(body.get("reviewDecisionEvents"), list)
            else []
        ),
        "reviewWorkerProtocol": review_worker_protocol_envelope(body),
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


def worker_id_from_attempt_id(attempt_id: object) -> str:
    text = clean_github_access_text(attempt_id)
    if not text or "-" not in text:
        return ""
    worker_id, attempt = text.rsplit("-", 1)
    if not worker_id or not attempt.isdigit():
        return ""
    return worker_id


def prepare_worker_job_result_state(job: dict, body: dict, *, status: str, checksum: str) -> dict:
    preflight = public_scan_preflight(body.get("preflight"))
    resolved_commit = worker_result_resolved_commit(job=job, body=body, preflight=preflight)
    if resolved_commit:
        preflight["commit"] = resolved_commit
    job_for_findings = dict(job)
    if resolved_commit:
        job_for_findings["commit"] = resolved_commit
    validate_review_worker_protocol_artifacts(job, body, status=status)
    review_worker_protocol = review_worker_protocol_envelope(body)
    if not review_worker_protocol:
        raise ValueError("Worker result must include reviewWorkerProtocol.")
    normalized_findings = worker_protocol_findings(
        job_for_findings,
        review_worker_protocol,
        reserved_ids=worker_issue_reserved_ids(job_for_findings),
    )
    deterministic_findings = body.get("deterministicFindings")
    if isinstance(deterministic_findings, list):
        reserved_ids = {finding.get("id") for finding in normalized_findings if isinstance(finding, dict)}
        reserved_ids.update(worker_issue_reserved_ids(job_for_findings))
        for finding in deterministic_findings:
            if not isinstance(finding, dict):
                continue
            issue = worker_finding_payload(job_for_findings, finding, len(normalized_findings))
            issue["id"] = unique_issue_id(issue.get("id"), reserved_ids)
            normalized_findings.append(issue)
    summary = public_scan_issue_counts(summarize_findings(normalized_findings))
    effective_agent_config = public_scan_agent_config(body.get("effectiveAgentConfig"))
    human_report = public_result_human_report(body.get("humanReport"))
    agent_report = public_result_agent_report(body.get("agentReport"))
    reading_guide = public_result_reading_guide(body.get("readingGuide"))
    error_code = worker_result_error_code(body)
    completed_at = pull_request_timestamp(job.get("completed_at")) or now()
    return {
        "status": status,
        "checksum": checksum,
        "preflight": preflight,
        "resolved_commit": resolved_commit,
        "normalized_findings": normalized_findings,
        "summary": summary,
        "effective_agent_config": effective_agent_config,
        "human_report": human_report,
        "agent_report": agent_report,
        "reading_guide": reading_guide,
        "error_code": error_code,
        "completed_at": completed_at,
        "duration_ms": public_scan_count(body.get("duration_ms")),
        "error": clean_scan_error(body.get("error")) if status in {"failed", "cancelled", "partial_completed"} else "",
        "review_worker_protocol": review_worker_protocol,
    }


def apply_prepared_worker_job_result_to_state_locked(job: dict, prepared: dict) -> bool:
    status = public_issue_text(prepared.get("status")).lower()
    checksum = public_issue_text(prepared.get("checksum"))
    preflight = prepared.get("preflight") if isinstance(prepared.get("preflight"), dict) else {}
    resolved_commit = clean_github_access_text(prepared.get("resolved_commit"))
    normalized_findings = prepared.get("normalized_findings") if isinstance(prepared.get("normalized_findings"), list) else []
    summary = public_scan_issue_counts(prepared.get("summary"))
    effective_agent_config = public_scan_agent_config(prepared.get("effective_agent_config"))
    human_report = public_result_human_report(prepared.get("human_report"))
    agent_report = public_result_agent_report(prepared.get("agent_report"))
    reading_guide = public_result_reading_guide(prepared.get("reading_guide"))
    error_code = worker_result_error_code({"error_code": prepared.get("error_code")})
    review_worker_protocol = public_review_worker_protocol(prepared.get("review_worker_protocol"))
    completed_at = pull_request_timestamp(prepared.get("completed_at")) or now()
    scan = memory_scan_by_id(job.get("scan_id"))
    changed = False
    if scan:
        before = json.dumps(db.to_jsonable(scan), sort_keys=True)
        scan.update(
            {
                "status": status,
                "phase": "report",
                "progress": public_scan_display_progress(status, scan.get("progress")),
                "completedAt": completed_at,
                "durationMs": public_scan_count(prepared.get("duration_ms")),
                "issues": summary,
                "error": clean_scan_error(prepared.get("error")) if status in {"failed", "cancelled", "partial_completed"} else "",
                "resultChecksum": checksum,
            }
        )
        if status == "failed" and error_code:
            scan["errorCode"] = error_code
        else:
            scan.pop("errorCode", None)
        if resolved_commit:
            scan["commit"] = resolved_commit
        if preflight:
            scan["preflight"] = preflight
        if effective_agent_config:
            scan["effectiveAgentConfig"] = effective_agent_config
        if human_report:
            scan["humanReport"] = human_report
        else:
            scan.pop("humanReport", None)
        if agent_report:
            scan["agentReport"] = agent_report
        else:
            scan.pop("agentReport", None)
        if reading_guide:
            scan["readingGuide"] = reading_guide
        else:
            scan.pop("readingGuide", None)
        if review_worker_protocol:
            scan["reviewWorkerProtocol"] = review_worker_protocol
        else:
            scan.pop("reviewWorkerProtocol", None)
        changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
        if status in {"done", "partial_completed"}:
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


def apply_worker_job_retry_to_state_locked(job: dict, body: dict, *, checksum: str) -> bool:
    scan_id = public_issue_text(job.get("scan_id"))
    if not scan_id:
        return False
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if scan is None:
        scan = scan_from_recovered_job(job)
        if scan:
            remember_scan_snapshot_locked(scan)
    if scan is None:
        return False
    before = json.dumps(db.to_jsonable(scan), sort_keys=True)
    queued_at = now()
    retry = scan_retry_summary_for_job(job, reason="worker_result_failed")
    scan.update(
        {
            "status": "queued",
            "queuedAt": queued_at,
            "progress": 0,
            "phase": None,
            "jobId": public_issue_text(job.get("job_id")) or public_issue_text(scan.get("jobId")),
            "retry": retry,
            "updatedAt": queued_at,
            "recoveryReason": "worker_result_failed",
            "lastWorkerResultChecksum": checksum,
        }
    )
    commit = worker_result_resolved_commit(job=job, body=body)
    if commit:
        scan["commit"] = commit
    for key in (
        "claimedAt",
        "claimedByWorkerId",
        "completedAt",
        "durationMs",
        "error",
        "errorCode",
        "resultChecksum",
        "startedAt",
    ):
        scan.pop(key, None)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        db.upsert_scan(scan)
        mark_state_dirty()
    return changed


def apply_worker_job_result(job: dict, body: dict) -> dict:
    status = public_issue_text(body.get("status")).lower()
    if status not in {"done", "failed", "cancelled", "partial_completed"}:
        raise ValueError("status must be done, failed, cancelled, or partial_completed")
    expected_attempt_id = expected_worker_attempt_id(job)
    attempt_id = clean_github_access_text(body.get("attempt_id") or body.get("attemptId")) or expected_attempt_id
    last_attempt_id = clean_github_access_text(job.get("last_attempt_id"))
    if attempt_id != expected_attempt_id and attempt_id != last_attempt_id:
        return {"accepted": False, "conflict": True}
    review_worker_protocol = review_worker_protocol_envelope(body)
    if not review_worker_protocol:
        raise ValueError("Worker result must include reviewWorkerProtocol.")
    checksum = worker_result_checksum(body)
    record_result = db.record_scan_job_result(
        str(job["job_id"]),
        attempt_id=attempt_id,
        status=status,
        result_checksum=checksum,
        payload=body,
        retryable=worker_result_allows_auto_retry(body, status=status),
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
    retry_queued = bool(record_result.get("retry_queued"))
    if retry_queued:
        retry_job = record_result.get("job") if isinstance(record_result.get("job"), dict) else job
        if resolved_commit:
            retry_job = {**retry_job, "commit": resolved_commit}
        with STATE_LOCK:
            apply_worker_job_retry_to_state_locked(retry_job, body, checksum=checksum)
        result = {
            "accepted": True,
            "duplicate": duplicate,
            "conflict": False,
            "retryQueued": True,
            "issueCount": worker_result_issue_count(body),
            "reviewDecisionEvents": event_result,
            "retry": scan_retry_summary_for_job(retry_job, reason="worker_result_failed"),
        }
        if quota_finalized.get("consumed"):
            result["quotaConsumed"] = True
        return result
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
    if not isinstance(body, dict):
        return 0
    envelope = review_worker_protocol_envelope(body)
    summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
    top_findings = summary.get("top_findings") if isinstance(summary.get("top_findings"), list) else []
    return len(top_findings)


def worker_result_should_finalize_quota(job: dict, body: dict, *, status: str) -> bool:
    if status in {"done", "partial_completed"}:
        return True
    if worker_progress_phase_should_finalize_quota(job.get("progress_phase")):
        return True
    return False


WORKER_TERMINAL_REFUNDABLE_ERROR_CODES = frozenset(
    {
        "REPOSITORY_TOO_LARGE",
        "CODEX_AUTH_REQUIRED",
        "CODEX_AUTH_EXPIRED",
        "CODEX_AUTHORIZATION_FAILED",
        "CODEX_SUBSCRIPTION_INACTIVE",
        "CODEX_QUOTA_EXHAUSTED",
        "CODEX_VERSION_UNSUPPORTED",
    }
)


def worker_result_allows_auto_retry(body: dict, *, status: str) -> bool:
    if status != "failed":
        return False
    if worker_result_error_code(body) in WORKER_TERMINAL_REFUNDABLE_ERROR_CODES:
        return False
    return True


def rollback_scan_quota_for_refundable_worker_failure(job: dict, body: dict, *, status: str) -> dict:
    error_code = worker_result_error_code(body)
    if status != "failed" or error_code not in WORKER_TERMINAL_REFUNDABLE_ERROR_CODES:
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
    if error_code == "REPOSITORY_TOO_LARGE" and not has_repository_limit_evidence:
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
                scan["quotaReleaseReason"] = error_code
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
                "reason": error_code,
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


def worker_protocol_findings(job: dict, envelope: dict, *, reserved_ids: set[str] | None = None) -> list[dict]:
    summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
    top_findings = summary.get("top_findings") if isinstance(summary.get("top_findings"), list) else []
    used_issue_ids = set(reserved_ids or set())
    findings: list[dict] = []
    for item in top_findings:
        if not isinstance(item, dict):
            continue
        issue = worker_finding_payload(job, worker_protocol_finding_source(item), len(findings))
        issue["id"] = unique_issue_id(issue.get("id"), used_issue_ids)
        findings.append(issue)
    return findings


def worker_protocol_finding_source(finding: dict) -> dict:
    location = finding.get("location") if isinstance(finding.get("location"), dict) else {}
    locations = finding.get("locations") if isinstance(finding.get("locations"), list) else []
    primary = location or next((item for item in locations if isinstance(item, dict)), {})
    file_path = public_issue_file(primary.get("file") or primary.get("path") or finding.get("file"))
    line = public_scan_count(primary.get("line") or primary.get("startLine") or primary.get("start_line") or finding.get("line"))
    evidence_items = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    recommendation = review._safe_text_lenient(
        finding.get("recommendation") or finding.get("fix") or finding.get("remediation")
    )
    scenario = review._safe_text_lenient(
        finding.get("failure_scenario") or finding.get("scenario") or finding.get("impact") or finding.get("description")
    )
    return {
        "id": public_issue_text(finding.get("id") or finding.get("issue_id")),
        "title": public_issue_text(finding.get("title") or finding.get("summary")),
        "severity": review._safe_severity(finding.get("severity")),
        "status": public_issue_status(finding.get("status") or "open"),
        "file": file_path,
        "line": line,
        "description": review._safe_text_lenient(finding.get("description") or scenario),
        "recommendation": recommendation,
        "failureScenario": scenario,
        "evidence": evidence_items,
        "reproduction": finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {},
        "affectedLocations": locations or ([primary] if primary else []),
        "whyNotFalsePositive": review._safe_text_list(finding.get("whyNotFalsePositive") or finding.get("false_positive_checks")),
        "limitations": review._safe_text_list(finding.get("limitations")),
        "tags": ["review-worker"],
    }

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
    reported_verification_status = public_issue_text(issue.get("verificationStatus")).lower()
    if reported_verification_status in ISSUE_VERIFICATION_STATUSES:
        issue["reportedVerificationStatus"] = reported_verification_status
    issue["verificationStatus"] = public_issue_verification_status(
        issue,
        affected_locations=issue["affectedLocations"],
        evidence=issue["evidence"],
    )
    issue["evidenceChecklist"] = public_issue_evidence_checklist(
        issue,
        affected_locations=issue["affectedLocations"],
        evidence=issue["evidence"],
    )
    issue["confidenceLevel"] = public_issue_confidence_level(
        issue["verificationStatus"],
        issue["evidenceChecklist"],
    )
    if not public_issue_text(issue.get("title")):
        issue["title"] = f"Finding {index + 1}"
    return issue
