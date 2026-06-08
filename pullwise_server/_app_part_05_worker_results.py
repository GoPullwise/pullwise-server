from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

def create_scan_job_for_scan(scan: dict) -> dict:
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
            "max_attempts": env_int("PULLWISE_SCAN_JOB_MAX_ATTEMPTS", 3),
        }
    )
    scan["jobId"] = job.get("job_id")
    return job


def scan_queue_limit_error(user_id: str) -> tuple[int, str, str] | None:
    queued = [scan for scan in SCANS if scan.get("status") == "queued"]
    queued_for_user = [scan for scan in queued if str(scan.get("userId") or "") == user_id]
    running_for_user = [
        scan
        for scan in SCANS
        if scan.get("status") == "running" and str(scan.get("userId") or "") == user_id
    ]
    if len(queued) >= max_queued_scans_global():
        return HTTPStatus.TOO_MANY_REQUESTS, "The global scan queue is full. Try again after queued scans start.", "QUEUE_FULL_GLOBAL"
    if len(queued_for_user) >= max_queued_scans_per_user():
        return HTTPStatus.TOO_MANY_REQUESTS, "You have too many queued scans. Wait for one to start before adding another.", "QUEUE_FULL_USER"
    if len(running_for_user) >= max_scan_concurrency_per_user() and len(queued_for_user) >= max_queued_scans_per_user():
        return HTTPStatus.TOO_MANY_REQUESTS, "You have too many active scans. Wait for one to finish before adding another.", "ACTIVE_LIMIT_USER"
    return None


def scan_job_payload(job: dict, *, include_clone_token: bool = False) -> dict:
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
    language = review_output_language_payload(job.get("review_output_language"))
    payload["review_output_language"] = language["code"]
    payload["review_output_language_label"] = language["label"]
    if include_clone_token:
        payload["clone_token"] = installation_clone_token_payload(job)
    convergence_context = worker_convergence_context_for_job(job)
    if convergence_context:
        payload["convergence_context"] = convergence_context
    review_calibration_context = worker_review_calibration_context_for_job(job)
    if review_calibration_context:
        payload["review_calibration_context"] = review_calibration_context
    return payload


def worker_task_activity_payload(job: dict) -> dict:
    claimed_at = pull_request_timestamp(job.get("claimed_at"))
    started_at = pull_request_timestamp(job.get("started_at"))
    completed_at = pull_request_timestamp(job.get("completed_at"))
    updated_at = pull_request_timestamp(job.get("updated_at"))
    created_at = pull_request_timestamp(job.get("created_at"))
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
        "last_activity_at": completed_at or started_at or claimed_at or updated_at or created_at,
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
        "audit_protocol": body.get("audit_protocol") or body.get("auditProtocol"),
        "issue_cards": body.get("issue_cards") if isinstance(body.get("issue_cards"), list) else [],
        "verification_results": (
            body.get("verification_results") if isinstance(body.get("verification_results"), list) else []
        ),
        "summary": body.get("summary") if isinstance(body.get("summary"), dict) else {},
        "duration_ms": body.get("duration_ms"),
        "error": body.get("error"),
        "error_code": worker_result_error_code(body),
        "ai_usage": public_scan_ai_usage(body.get("ai_usage") or body.get("aiUsage")),
        "preflight": public_scan_preflight(body.get("preflight")),
        "verification_audit": public_scan_verification_audit_input(
            body.get("verification_audit") or body.get("verificationAudit")
        ),
        "review_decision_events": (
            body.get("review_decision_events")
            if isinstance(body.get("review_decision_events"), list)
            else body.get("reviewDecisionEvents")
            if isinstance(body.get("reviewDecisionEvents"), list)
            else []
        ),
        "convergence_state": public_scan_convergence_state(
            body.get("convergence_state") or body.get("convergenceState")
        ),
        "audit_swarm": public_scan_audit_swarm_from_worker_body(
            body,
            status=public_issue_text(body.get("status")).lower(),
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


def apply_worker_job_result_to_state_locked(job: dict, body: dict, *, status: str, checksum: str) -> bool:
    preflight = public_scan_preflight(body.get("preflight"))
    resolved_commit = worker_result_resolved_commit(job=job, body=body, preflight=preflight)
    if resolved_commit:
        preflight["commit"] = resolved_commit
    job_for_findings = dict(job)
    if resolved_commit:
        job_for_findings["commit"] = resolved_commit
    normalized_findings = worker_audit_swarm_findings(
        job_for_findings,
        body,
        reserved_ids=worker_issue_reserved_ids(job_for_findings),
    )
    summary = public_scan_issue_counts(body.get("summary") if isinstance(body.get("summary"), dict) else summarize_findings(normalized_findings))
    verification_audit = public_scan_verification_audit_input(
        body.get("verification_audit") or body.get("verificationAudit")
    )
    convergence_state = convergence_state_from_worker_result(job, body)
    audit_swarm = public_scan_audit_swarm_from_worker_body(body, status=status)
    ai_usage = public_scan_ai_usage(body.get("ai_usage") or body.get("aiUsage"))
    error_code = worker_result_error_code(body)
    completed_at = pull_request_timestamp(job.get("completed_at")) or now()
    scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
    changed = False
    if scan:
        before = json.dumps(db.to_jsonable(scan), sort_keys=True)
        scan.update(
            {
                "status": status,
                "phase": "report",
                "progress": 100 if status == "done" else public_scan_progress(scan.get("progress")),
                "completedAt": completed_at,
                "durationMs": public_scan_count(body.get("duration_ms")),
                "issues": summary,
                "error": clean_scan_error(body.get("error")) if status == "failed" else "",
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
        if any(
            verification_audit.get(key)
            for key in (
                "candidateCount",
                "reportedCount",
                "auditOnlyCount",
                "rejectedCount",
                "downgradedCount",
                "verifiedSuppressionCount",
                "verifiedCount",
                "staticProofCount",
                "potentialRiskCount",
                "unverifiedCount",
                "summary",
                "rejectedReasons",
                "rejectedSamples",
                "auditOnlySamples",
            )
        ):
            scan["verificationAudit"] = verification_audit
        if audit_swarm:
            scan["auditSwarm"] = audit_swarm
        if ai_usage:
            scan["aiUsage"] = ai_usage
        if status == "done" and convergence_state:
            scan["convergenceState"] = convergence_state
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
            after_issues = json.dumps(db.to_jsonable(normalized_findings), sort_keys=True)
            changed = changed or before_issues != after_issues
    if changed:
        mark_state_dirty()
    return changed


def apply_worker_job_result(job: dict, body: dict) -> dict:
    status = public_issue_text(body.get("status")).lower()
    if status not in {"done", "failed"}:
        raise ValueError("status must be done or failed")
    expected_attempt_id = expected_worker_attempt_id(job)
    attempt_id = clean_github_access_text(body.get("attempt_id")) or expected_attempt_id
    if attempt_id != expected_attempt_id:
        return {"accepted": False, "conflict": True}
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
        issue_cards = body.get("issue_cards") if isinstance(body.get("issue_cards"), list) else []
        quota_rollback = rollback_scan_quota_for_refundable_worker_failure(job, body, status=status)
        result = {"accepted": True, "duplicate": True, "conflict": False, "issueCount": len(issue_cards)}
        if quota_rollback.get("ledgerRows"):
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
    with STATE_LOCK:
        apply_worker_job_result_to_state_locked(job, body, status=status, checksum=checksum)
    quota_rollback = rollback_scan_quota_for_refundable_worker_failure(job, body, status=status)
    issue_cards = body.get("issue_cards") if isinstance(body.get("issue_cards"), list) else []
    result = {
        "accepted": True,
        "duplicate": duplicate,
        "conflict": False,
        "issueCount": len(issue_cards),
        "reviewDecisionEvents": event_result,
    }
    if quota_rollback.get("ledgerRows"):
        result["quotaRollback"] = quota_rollback
    return result


def rollback_scan_quota_for_refundable_worker_failure(job: dict, body: dict, *, status: str) -> dict:
    if status != "failed" or worker_result_error_code(body) != "REPOSITORY_TOO_LARGE":
        return {}
    scan_id = public_issue_text(job.get("scan_id"))
    user_id = public_issue_text(job.get("user_id"))
    if not scan_id or not user_id:
        return {}
    rollback_result = quota.rollback_scan_quota(
        scan_id=scan_id,
        requested_by_user_id=user_id,
        match_request_id=False,
    )
    if not rollback_result.get("ledgerRows"):
        return rollback_result

    with STATE_LOCK:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        repo_id = public_issue_text((scan or {}).get("repoId") or job.get("repo_id"))
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
            mark_state_dirty()
    return rollback_result


def worker_issue_reserved_ids(job: dict) -> set[str]:
    user_id = public_issue_text(job.get("user_id"))
    scan_id = public_issue_text(job.get("scan_id"))
    job_id = public_issue_text(job.get("job_id"))
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


def worker_audit_swarm_findings(job: dict, body: dict, *, reserved_ids: set[str] | None = None) -> list[dict]:
    cards = body.get("issue_cards") if isinstance(body.get("issue_cards"), list) else []
    results = body.get("verification_results") if isinstance(body.get("verification_results"), list) else []
    results_by_issue = worker_audit_swarm_results_by_issue(results)
    projected = []
    used_issue_ids = set(reserved_ids or set())
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        finding = worker_audit_swarm_card_to_finding(
            card,
            results_by_issue.get(worker_audit_swarm_issue_id(card), []),
            index,
            job=job,
        )
        issue = worker_finding_payload(job, finding, index)
        issue["id"] = unique_issue_id(issue.get("id"), used_issue_ids)
        projected.append(issue)
    return projected


def worker_audit_swarm_results_by_issue(results: list) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        issue_id = public_issue_text(result.get("issue_id") or result.get("issueId"))
        if issue_id:
            grouped.setdefault(issue_id, []).append(result)
    return grouped


def worker_audit_swarm_issue_id(card: dict) -> str:
    return public_issue_text(card.get("issue_id") or card.get("issueId") or card.get("id"))


def worker_audit_swarm_card_to_finding(card: dict, results: list[dict], index: int, *, job: dict | None = None) -> dict:
    locations = worker_audit_swarm_locations(card, job=job)
    primary = locations[0] if locations else {}
    issue_id = worker_audit_swarm_issue_id(card) or make_id("iss")
    verdict = worker_audit_swarm_verdict(results)
    evidence = worker_audit_swarm_evidence(card, results, primary, job=job)
    reproduction = worker_audit_swarm_reproduction(card, results)
    false_positive_checks = review._safe_text_list(card.get("false_positive_checks") or card.get("falsePositiveChecks"))
    invariants = review._safe_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))
    audit_swarm = {
        "protocol": "audit-swarm/0.1",
        "shardId": public_issue_text(card.get("shard_id") or card.get("shardId")),
        "agentRole": public_issue_text(card.get("agent_role") or card.get("agentRole")),
        "verdict": verdict,
    }
    finding = {
        "id": issue_id,
        "severity": worker_audit_swarm_severity(card.get("severity")),
        "category": worker_audit_swarm_category(card),
        "title": public_issue_text(card.get("title")) or f"Audit candidate {index + 1}",
        "summary": review._safe_text_lenient(card.get("claim") or card.get("summary") or card.get("description")),
        "impact": review._safe_text_lenient(card.get("impact")) or worker_audit_swarm_invariant_impact(invariants),
        "detectionReasoning": worker_audit_swarm_detection_reasoning(card, results),
        "reproductionPath": worker_audit_swarm_reproduction_path(card, results),
        "verificationStatus": worker_audit_swarm_verification_status(verdict, results),
        "verificationSummary": worker_audit_swarm_verification_summary(results, verdict),
        "affectedLocations": locations,
        "evidence": evidence,
        "reproduction": reproduction,
        "whyNotFalsePositive": false_positive_checks[:8],
        "limitations": [
            *(f"Violated invariant: {item}" for item in invariants),
            *review._safe_text_list(card.get("limitations")),
        ][:8],
        "file": public_issue_text(primary.get("file")),
        "line": public_scan_count(primary.get("startLine")),
        "confidence": worker_audit_swarm_confidence(card.get("confidence"), verdict),
        "confidenceRationale": worker_audit_swarm_confidence_rationale(card, results, verdict),
        "autoFix": False,
        "effort": public_issue_text(card.get("effort")) or "review required",
        "fixBenefits": review._safe_text_lenient(card.get("fixBenefits") or card.get("fix_benefits")),
        "fixRisks": review._safe_text_lenient(card.get("fixRisks") or card.get("fix_risks")),
        "tags": worker_audit_swarm_tags(card, results),
        "steps": worker_audit_swarm_steps(card, results),
        "badCode": [],
        "goodCode": [],
        "references": worker_audit_swarm_references(card),
        "auditSwarm": {key: value for key, value in audit_swarm.items() if value},
    }
    review_calibration = public_issue_review_calibration(
        card.get("review_calibration") or card.get("reviewCalibration")
    )
    if review_calibration:
        finding["reviewCalibration"] = review_calibration
    return finding


def worker_audit_swarm_locations(card: dict, *, job: dict | None = None) -> list[dict]:
    locations = []
    seen = set()
    raw_locations = card.get("locations") if isinstance(card.get("locations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = public_issue_file(item.get("file") or item.get("path"), job=job)
        if not file_path:
            continue
        start_line, end_line = worker_audit_swarm_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    return locations[:10]


def worker_audit_swarm_line_range(source: dict) -> tuple[int, int]:
    start = public_scan_count(source.get("startLine") or source.get("start_line") or source.get("line"))
    end = public_scan_count(source.get("endLine") or source.get("end_line"))
    lines = public_issue_text(source.get("lines") or source.get("lineRange") or source.get("line_range"))
    if lines and not start:
        match = re.search(r"(\d+)(?:\s*[-:]\s*(\d+))?", lines)
        if match:
            start = public_scan_count(match.group(1))
            end = public_scan_count(match.group(2) or match.group(1))
    if start and (not end or end < start):
        end = start
    return start, end


def worker_audit_swarm_verdict(results: list[dict]) -> str:
    verdicts = [public_issue_text(result.get("verdict")).lower() for result in results]
    if any(
        public_issue_text(result.get("verdict")).lower() == "confirmed"
        and worker_audit_swarm_confirmed_verification_has_support(result)
        for result in results
    ):
        return "confirmed"
    if verdicts and all(verdict == "rejected" for verdict in verdicts):
        return "rejected"
    if "inconclusive" in verdicts:
        return "inconclusive"
    return "candidate"


def worker_audit_swarm_confirmed_verification_has_support(result: dict) -> bool:
    if review._safe_text_list(result.get("commands_run") or result.get("commandsRun")):
        return True
    if review._safe_text_list(result.get("evidence")):
        return True
    if review._safe_text_lenient(result.get("result_summary") or result.get("resultSummary") or result.get("summary")):
        return True
    if review._safe_text_lenient(result.get("output")):
        return True
    if public_issue_text(result.get("logPath") or result.get("log_path")):
        return True
    return False


def worker_audit_swarm_verification_status(verdict: str, results: list[dict]) -> str:
    if verdict == "confirmed":
        proof_types = {public_issue_text(result.get("proof_type") or result.get("proofType")).lower() for result in results}
        has_command = any(review._safe_text_list(result.get("commands_run") or result.get("commandsRun")) for result in results)
        if proof_types & {"failing_test", "runtime_log", "test", "command"} or has_command:
            return "verified"
        return "static_proof"
    if verdict == "rejected":
        return "unverified"
    return "potential_risk"


def worker_audit_swarm_severity(value: object) -> str:
    severity = public_issue_text(value).lower()
    return {
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
        "p4": "info",
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "info": "info",
    }.get(severity, "medium")


def worker_audit_swarm_category(card: dict) -> str:
    raw = " ".join(
        public_issue_text(value).lower()
        for value in (card.get("category"), card.get("agent_role"), card.get("agentRole"))
        if public_issue_text(value)
    )
    if "security" in raw or "auth" in raw or "permission" in raw:
        return "Security"
    if "performance" in raw:
        return "Performance"
    if "dependencies" in raw or "dependency" in raw or "cve" in raw:
        return "Dependencies"
    if "test" in raw or "coverage" in raw:
        return "Tests"
    if "doc" in raw:
        return "Docs"
    if "architecture" in raw or "contract" in raw or "api" in raw:
        return "Architecture"
    return "Quality"


def worker_audit_swarm_confidence(value: object, verdict: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))
    if verdict == "confirmed":
        return max(confidence, 0.85)
    if verdict == "rejected":
        return min(confidence, 0.2)
    if verdict == "inconclusive":
        return min(confidence, 0.79)
    return confidence


def worker_audit_swarm_evidence(
    card: dict,
    results: list[dict],
    primary: dict,
    *,
    job: dict | None = None,
) -> list[dict]:
    evidence = []
    raw_evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
    for index, item in enumerate(raw_evidence):
        if isinstance(item, dict):
            summary = review._safe_text_lenient(item.get("summary") or item.get("claim") or item.get("text"))
            file_path = public_issue_file(item.get("file") or item.get("path"), job=job) or public_issue_text(primary.get("file"))
            start_line, end_line = worker_audit_swarm_line_range(item)
            output_redacted = bool(review._safe_text_lenient(item.get("output")) or item.get("outputRedacted") is True)
            record = {
                "type": worker_audit_swarm_evidence_type(item.get("type"), "code" if file_path else "path"),
                "label": public_issue_text(item.get("label")) or "Discovery evidence",
                "summary": summary,
                "file": file_path,
                "startLine": start_line or public_scan_count(primary.get("startLine")),
                "endLine": end_line or public_scan_count(primary.get("endLine") or primary.get("startLine")),
                "command": public_issue_text(item.get("command")),
                "exitCode": public_scan_count(item.get("exitCode") or item.get("exit_code")),
                "logPath": public_issue_text(item.get("logPath") or item.get("log_path")),
                "outputRedacted": output_redacted,
                "url": public_issue_text(item.get("url")),
            }
        else:
            record = {
                "type": "code" if primary.get("file") else "path",
                "label": "Discovery evidence" if index == 0 else "Evidence",
                "summary": review._safe_text_lenient(item),
                "file": public_issue_text(primary.get("file")),
                "startLine": public_scan_count(primary.get("startLine")),
                "endLine": public_scan_count(primary.get("endLine") or primary.get("startLine")),
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "outputRedacted": False,
                "url": "",
            }
        if any(record.get(key) for key in ("summary", "file", "command", "logPath", "url")):
            evidence.append(record)
    for result in results:
        if not isinstance(result, dict):
            continue
        role = public_issue_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        proof_type = public_issue_text(result.get("proof_type") or result.get("proofType"))
        commands = review._safe_text_list(result.get("commands_run") or result.get("commandsRun"))
        for summary in review._safe_text_list(result.get("evidence"))[:4]:
            evidence.append(
                {
                    "type": worker_audit_swarm_evidence_type(proof_type, "test" if commands else "tool"),
                    "label": f"{role} verification",
                    "summary": summary,
                    "file": public_issue_text(primary.get("file")),
                    "startLine": public_scan_count(primary.get("startLine")),
                    "endLine": public_scan_count(primary.get("endLine") or primary.get("startLine")),
                    "command": commands[0] if commands else "",
                    "exitCode": 0,
                    "logPath": public_issue_text(result.get("logPath") or result.get("log_path")),
                    "outputRedacted": bool(
                        review._safe_text_lenient(result.get("output")) or result.get("outputRedacted") is True
                    ),
                    "url": "",
                }
            )
    return evidence[:20]


def worker_audit_swarm_evidence_type(value: object, default: str) -> str:
    raw = public_issue_text(value).lower()
    if raw in {"failing_test", "test"}:
        return "test"
    if raw in {"runtime", "runtime_log", "command"}:
        return "runtime_log"
    if raw in {"static", "static_proof", "code"}:
        return "code"
    if raw in {"path", "reachability", "data_flow", "data-flow"}:
        return "path"
    if raw in {"trigger", "input"}:
        return "trigger"
    if raw in {"documentation", "docs"}:
        return "documentation"
    if raw in {"fix", "fix_verification"}:
        return "fix_verification"
    if raw in {"tool", "environment"}:
        return raw
    return default


def worker_audit_swarm_reproduction(card: dict, results: list[dict]) -> dict:
    commands = []
    for result in results:
        if isinstance(result, dict):
            commands.extend(review._safe_text_list(result.get("commands_run") or result.get("commandsRun")))
    reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
    commands.extend(review._safe_text_list(reproduction.get("commands")))
    return {
        "commands": list(dict.fromkeys(command for command in commands if command))[:5],
        "input": review._safe_text_lenient(reproduction.get("input") or card.get("input") or card.get("trigger")),
        "expected": review._safe_text_lenient(reproduction.get("expected") or card.get("expected")),
        "actual": worker_audit_swarm_actual(results) or review._safe_text_lenient(reproduction.get("actual") or card.get("actual")),
        "testFile": public_issue_text(reproduction.get("testFile") or reproduction.get("test_file")),
        "logPath": public_issue_text(reproduction.get("logPath") or reproduction.get("log_path")),
    }


def worker_audit_swarm_actual(results: list[dict]) -> str:
    for result in results:
        summary = review._safe_text_lenient(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            return summary
    return ""


def worker_audit_swarm_detection_reasoning(card: dict, results: list[dict]) -> str:
    parts = []
    role = public_issue_text(card.get("agent_role") or card.get("agentRole"))
    shard = public_issue_text(card.get("shard_id") or card.get("shardId"))
    if role or shard:
        parts.append(f"{role or 'reviewer'} reported this candidate" + (f" in shard `{shard}`." if shard else "."))
    claim = review._safe_text_lenient(card.get("claim"))
    if claim:
        parts.append(f"Claim: {claim}")
    for invariant in review._safe_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))[:3]:
        parts.append(f"Violated invariant: {invariant}")
    for result in results[:3]:
        role = public_issue_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        verdict = public_issue_text(result.get("verdict"))
        summary = review._safe_text_lenient(result.get("result_summary") or result.get("resultSummary"))
        if verdict or summary:
            parts.append(f"{role} verdict: {verdict or 'reviewed'}" + (f" - {summary}" if summary else "."))
    return " ".join(parts)[:1200]


def worker_audit_swarm_reproduction_path(card: dict, results: list[dict]) -> str:
    parts = []
    reproduction_idea = review._safe_text_lenient(card.get("reproduction_idea") or card.get("reproductionIdea"))
    suggested_test = review._safe_text_lenient(card.get("suggested_test") or card.get("suggestedTest"))
    if reproduction_idea:
        parts.append(reproduction_idea)
    if suggested_test:
        parts.append(f"Suggested test: {suggested_test}")
    for result in results:
        commands = review._safe_text_list(result.get("commands_run") or result.get("commandsRun"))
        if commands:
            parts.append(f"Verifier command: {commands[0]}")
            break
    return " ".join(parts)[:1000]


def worker_audit_swarm_verification_summary(results: list[dict], verdict: str) -> str:
    for result in results:
        summary = review._safe_text_lenient(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            role = public_issue_text(result.get("verifier_role") or result.get("verifierRole"))
            return f"{role}: {summary}" if role else summary
    if verdict == "confirmed":
        return "Audit verifier confirmed this candidate."
    if verdict == "rejected":
        return "Audit verifier rejected this candidate."
    if verdict == "inconclusive":
        return "Audit verifier could not conclusively prove or disprove this candidate."
    return "Discovery candidate has not been independently verified."


def worker_audit_swarm_verification_evidence(results: list[dict]) -> list[str]:
    evidence = []
    for result in results:
        role = public_issue_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        for item in review._safe_text_list(result.get("evidence"))[:3]:
            evidence.append(f"{role}: {item}")
    return evidence[:6]


def worker_audit_swarm_invariant_impact(invariants: list[str]) -> str:
    if invariants:
        return f"The finding may violate this required behavior: {invariants[0]}"
    return ""


def worker_audit_swarm_confidence_rationale(card: dict, results: list[dict], verdict: str) -> str:
    explicit = review._safe_text_lenient(card.get("confidenceRationale") or card.get("confidence_rationale"))
    if explicit:
        return explicit
    if results:
        return f"Audit Swarm verifier verdict is {verdict}."
    return "Audit Swarm discovery supplied this confidence without an independent verifier result."


def worker_audit_swarm_tags(card: dict, results: list[dict]) -> list[str]:
    tags = ["audit-swarm"]
    tags.extend(review._safe_text_list(card.get("risk_tags") or card.get("riskTags")))
    for value in (card.get("agent_role"), card.get("agentRole"), card.get("shard_id"), card.get("shardId")):
        text = public_issue_text(value)
        if text:
            tags.append(text)
    for result in results:
        role = public_issue_text(result.get("verifier_role") or result.get("verifierRole"))
        if role:
            tags.append(role)
    return list(dict.fromkeys(re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")[:40] for tag in tags if tag))[:12]


def worker_audit_swarm_steps(card: dict, results: list[dict]) -> list[str]:
    steps = review._safe_text_list(card.get("steps"))
    suggested_test = review._safe_text_lenient(card.get("suggested_test") or card.get("suggestedTest"))
    if suggested_test:
        steps.append(f"Add or run the suggested test: {suggested_test}")
    for result in results:
        steps.extend(review._safe_text_list(result.get("notes_for_fix") or result.get("notesForFix")))
    return list(dict.fromkeys(step for step in steps if step))[:8]


def worker_audit_swarm_references(card: dict) -> list[dict]:
    references = []
    raw = card.get("references") if isinstance(card.get("references"), list) else []
    for item in raw:
        if isinstance(item, dict):
            label = public_issue_text(item.get("label")) or public_issue_text(item.get("url"))
            url = public_issue_text(item.get("url"))
        else:
            label = public_issue_text(item)
            url = public_issue_text(item)
        if label and url.startswith(("http://", "https://")):
            references.append({"label": label, "url": url})
    return references[:10]


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


