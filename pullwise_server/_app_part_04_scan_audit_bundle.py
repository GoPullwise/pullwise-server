from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_03_billing_pages as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

def public_scan_agent_text(value: object, *, max_length: int = 128) -> str:
    text = clean_github_access_text(value) or ""
    if len(text) > max_length:
        return ""
    return text


def public_scan_agent_reasoning_effort(value: object) -> str:
    effort = public_scan_agent_text(value).lower()
    return effort if effort in {"low", "medium", "high", "xhigh"} else ""


def public_scan_agent_provider(value: object) -> str:
    provider = public_scan_agent_text(value).lower()
    return provider if provider == "codex" else ""


def public_scan_agent_config(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    raw_agent = source.get("agent") if isinstance(source.get("agent"), dict) else {}
    provider = public_scan_agent_provider(source.get("provider") or raw_agent.get("cli"))
    if not provider:
        return {}
    cli = public_scan_agent_text(source.get("cli") or raw_agent.get("command") or raw_agent.get("cli"))
    model = public_scan_agent_text(source.get("model") or raw_agent.get("model"))
    reasoning_effort = public_scan_agent_reasoning_effort(
        source.get("reasoningEffort")
        or raw_agent.get("reasoningEffort")
    )
    payload = {
        "provider": provider,
        "agent": {
            "cli": provider,
            "command": cli,
            "model": model,
            "reasoningEffort": reasoning_effort,
        },
        "cli": cli,
        "model": model,
        "reasoningEffort": reasoning_effort,
    }
    raw_provider = source.get("codex") if isinstance(source.get("codex"), dict) else {}
    provider_payload = {}
    command = public_scan_agent_text(raw_provider.get("command") or raw_provider.get("cli"))
    provider_model = public_scan_agent_text(raw_provider.get("model"))
    provider_effort = public_scan_agent_reasoning_effort(raw_provider.get("reasoningEffort"))
    if command:
        provider_payload["cli"] = command
        provider_payload["command"] = command
    if provider_model:
        provider_payload["model"] = provider_model
    if provider_effort:
        provider_payload["reasoningEffort"] = provider_effort
    if provider_payload:
        payload["codex"] = provider_payload
    return payload


def public_scan_retry(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    max_attempts = max(1, public_scan_count(value.get("maxAttempts") or value.get("max_attempts") or 1))
    attempt = public_scan_count(value.get("attempt"))
    retry_attempts = public_scan_count(value.get("retryAttempts") or value.get("retry_attempts"))
    if "retryAttempts" not in value and "retry_attempts" not in value:
        retry_attempts = max(0, max_attempts - 1)
    remaining = public_scan_count(value.get("remainingAttempts") or value.get("remaining_attempts"))
    payload = {
        "attempt": attempt,
        "maxAttempts": max_attempts,
        "retryAttempts": max(0, retry_attempts),
        "remainingAttempts": max(0, min(remaining, max_attempts)),
        "attemptedWorkers": public_scan_count(value.get("attemptedWorkers") or value.get("attempted_workers")),
    }
    reason = public_issue_text(value.get("reason"))
    if reason:
        payload["reason"] = reason
    return payload


def public_scan_progress_log(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = {}
    timestamp = pull_request_timestamp(value.get("time") or value.get("logTime") or value.get("log_time"))
    if timestamp is not None:
        payload["time"] = timestamp
    phase = public_scan_phase(value.get("phase"))
    if phase:
        payload["phase"] = phase
    if "progress" in value:
        payload["progress"] = public_scan_progress(value.get("progress"))
    message = public_issue_text(value.get("message") or value.get("progressMessage") or value.get("progress_message"))
    if message:
        payload["message"] = message
    logs_summary = public_issue_text(value.get("logsSummary") or value.get("logs_summary"))
    if logs_summary:
        payload["logsSummary"] = logs_summary
    return payload if payload.get("time") is not None or phase or message or logs_summary else {}


def public_scan_progress_logs(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    logs = []
    for item in value:
        entry = public_scan_progress_log(item)
        if entry:
            logs.append(entry)
    return logs[-20:]

def scan_payload(scan: dict) -> dict:
    payload = {
        "id": public_issue_text(scan.get("id")),
        "userId": public_issue_text(scan.get("userId")),
        "repo": clean_repository_full_name(scan.get("repo")),
        "branch": clean_github_access_text(scan.get("branch")) or "main",
        "commit": clean_github_access_text(scan.get("commit")) or "pending",
        "status": public_scan_status(scan.get("status")),
        "phase": public_scan_phase(scan.get("phase")),
        "progress": public_scan_progress(scan.get("progress")),
        "issues": public_scan_issue_counts(scan.get("issues")),
        "verification": public_scan_verification_counts(scan),
        "createdAt": pull_request_timestamp(scan.get("createdAt")) or 0,
    }
    progress_message = public_issue_text(scan.get("progressMessage") or scan.get("progress_message"))
    if progress_message:
        payload["progressMessage"] = progress_message
    logs_summary = public_issue_text(scan.get("logsSummary") or scan.get("logs_summary"))
    if logs_summary:
        payload["logsSummary"] = logs_summary
    progress_logs = public_scan_progress_logs(scan.get("progressLogs") or scan.get("progress_logs"))
    if progress_logs:
        payload["progressLogs"] = progress_logs
    effective_agent_config = public_scan_agent_config(scan.get("effectiveAgentConfig"))
    if effective_agent_config:
        payload["effectiveAgentConfig"] = effective_agent_config
    preflight = public_scan_preflight(scan.get("preflight"))
    if preflight:
        payload["preflight"] = preflight
    graph_verified_report = public_graph_verified_report(
        scan.get("graphVerifiedReport")
    )
    if graph_verified_report:
        payload["graphVerifiedReport"] = graph_verified_report
    for key in ("queuedAt", "startedAt", "completedAt", "updatedAt", "recoveredAt"):
        if key in scan:
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    if "error" in scan:
        payload["error"] = clean_scan_error(scan.get("error"))
    error_code = public_scan_error_code(scan.get("errorCode") or scan.get("error_code"))
    if error_code:
        payload["errorCode"] = error_code
    if "time" in scan:
        payload["time"] = public_issue_text(scan.get("time"))
    if "by" in scan:
        payload["by"] = public_issue_text(scan.get("by"))
    if "reviewOutputLanguage" in scan:
        language = review_output_language_payload(scan.get("reviewOutputLanguage"))
        payload["reviewOutputLanguage"] = language["code"]
    if "installationId" in scan:
        payload["installationId"] = clean_github_access_text(scan.get("installationId"), allow_int=True)
    for key in ("repoId", "githubRepoId"):
        if key in scan:
            payload[key] = clean_github_access_text(scan.get(key), allow_int=True)
    if isinstance(scan.get("quotaBucketIds"), dict):
        payload["quotaBucketIds"] = {
            key: clean_github_access_text(value, allow_int=True)
            for key, value in scan["quotaBucketIds"].items()
            if clean_github_access_text(value, allow_int=True)
        }
    if isinstance(scan.get("billingUsage"), dict):
        payload["billingUsage"] = safe_quota_usage_payload(scan.get("billingUsage"), default_scope="user")
    if isinstance(scan.get("repoUsage"), dict):
        payload["repoUsage"] = safe_quota_usage_payload(scan.get("repoUsage"), default_scope="repository")
    quota_state = public_issue_text(scan.get("quotaState"))
    if quota_state in {"reserved", "consumed", "released", "refunded"}:
        payload["quotaState"] = quota_state
    for key in ("quotaReservedAt", "quotaConsumedAt", "quotaReleasedAt"):
        if pull_request_timestamp(scan.get(key)):
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    quota_trigger = public_issue_text(scan.get("quotaConsumeTrigger"))
    if quota_trigger:
        payload["quotaConsumeTrigger"] = quota_trigger
    quota_release_reason = public_issue_text(scan.get("quotaReleaseReason"))
    if quota_release_reason:
        payload["quotaReleaseReason"] = quota_release_reason
    if isinstance(scan.get("quotaRefunded"), dict):
        refunded = scan["quotaRefunded"]
        reason = public_scan_error_code(refunded.get("reason"))
        if reason:
            payload["quotaRefunded"] = {
                "reason": reason,
                "ledgerRows": public_scan_count(refunded.get("ledgerRows")),
                "bucketRows": public_scan_count(refunded.get("bucketRows")),
            }
    if isinstance(scan.get("riskDecision"), dict):
        decision = public_issue_text(scan["riskDecision"].get("decision"))
        reason = public_issue_text(scan["riskDecision"].get("reason"))
        risk_payload = {}
        if decision:
            risk_payload["decision"] = decision
        if reason:
            risk_payload["reason"] = reason
        matched_repository_id = clean_github_access_text(scan["riskDecision"].get("matchedRepositoryId"), allow_int=True)
        if matched_repository_id:
            risk_payload["matchedRepositoryId"] = matched_repository_id
        if risk_payload:
            payload["riskDecision"] = risk_payload
    if isinstance(scan.get("repoFingerprint"), dict):
        fingerprint_payload = {}
        for source_key, target_key in (
            ("headSha", "headSha"),
            ("treeSha", "treeSha"),
            ("lockfileHash", "lockfileHash"),
            ("manifestHash", "manifestHash"),
            ("sourceFingerprint", "sourceFingerprint"),
        ):
            value = clean_github_access_text(scan["repoFingerprint"].get(source_key))
            if value:
                fingerprint_payload[target_key] = value
        if fingerprint_payload:
            payload["repoFingerprint"] = fingerprint_payload
    if "installationAccount" in scan:
        payload["installationAccount"] = clean_github_access_text(scan.get("installationAccount"))
    if "installationTargetType" in scan:
        payload["installationTargetType"] = clean_github_access_text(scan.get("installationTargetType"))
    if "repositorySelection" in scan:
        payload["repositorySelection"] = clean_github_access_text(scan.get("repositorySelection"))
    if "cloneUrl" in scan:
        payload["cloneUrl"] = trusted_github_web_url(scan.get("cloneUrl"))
    if "jobId" in scan:
        payload["jobId"] = public_issue_text(scan.get("jobId"))
    claimed_by_worker_id = public_issue_text(scan.get("claimedByWorkerId"))
    if claimed_by_worker_id:
        payload["worker"] = {"id": claimed_by_worker_id}
    if pull_request_timestamp(scan.get("claimedAt")):
        payload["claimedAt"] = pull_request_timestamp(scan.get("claimedAt")) or 0
    queue = scan_queue_payload(scan)
    if queue:
        payload["queue"] = queue
    retry = public_scan_retry(scan.get("retry"))
    if retry:
        payload["retry"] = retry
    return payload


def scan_list_payload(scan: dict, issue_summary: dict | None = None) -> dict:
    verification_counts = (
        dict(issue_summary.get("counts"))
        if isinstance(issue_summary, dict) and isinstance(issue_summary.get("counts"), dict)
        else public_scan_verification_counts(scan)
    )
    payload = {
        "id": public_issue_text(scan.get("id")),
        "userId": public_issue_text(scan.get("userId")),
        "repo": clean_repository_full_name(scan.get("repo")),
        "branch": clean_github_access_text(scan.get("branch")) or "main",
        "commit": clean_github_access_text(scan.get("commit")) or "pending",
        "status": public_scan_status(scan.get("status")),
        "phase": public_scan_phase(scan.get("phase")),
        "progress": public_scan_progress(scan.get("progress")),
        "issues": public_scan_issue_counts(scan.get("issues")),
        "verification": verification_counts,
        "createdAt": pull_request_timestamp(scan.get("createdAt")) or 0,
    }
    progress_message = public_issue_text(scan.get("progressMessage") or scan.get("progress_message"))
    if progress_message:
        payload["progressMessage"] = progress_message
    logs_summary = public_issue_text(scan.get("logsSummary") or scan.get("logs_summary"))
    if logs_summary:
        payload["logsSummary"] = logs_summary
    progress_logs = public_scan_progress_logs(scan.get("progressLogs") or scan.get("progress_logs"))
    if progress_logs:
        payload["progressLogs"] = progress_logs
    effective_agent_config = public_scan_agent_config(scan.get("effectiveAgentConfig"))
    if effective_agent_config:
        payload["effectiveAgentConfig"] = effective_agent_config
    graph_verified_report = public_graph_verified_report(scan.get("graphVerifiedReport"))
    if graph_verified_report:
        payload["graphVerifiedReport"] = graph_verified_report
    for key in ("queuedAt", "startedAt", "completedAt", "updatedAt", "recoveredAt"):
        if key in scan:
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    if "error" in scan:
        payload["error"] = clean_scan_error(scan.get("error"))
    error_code = public_scan_error_code(scan.get("errorCode") or scan.get("error_code"))
    if error_code:
        payload["errorCode"] = error_code
    if "time" in scan:
        payload["time"] = public_issue_text(scan.get("time"))
    if "by" in scan:
        payload["by"] = public_issue_text(scan.get("by"))
    if "reviewOutputLanguage" in scan:
        language = review_output_language_payload(scan.get("reviewOutputLanguage"))
        payload["reviewOutputLanguage"] = language["code"]
    if "installationId" in scan:
        payload["installationId"] = clean_github_access_text(scan.get("installationId"), allow_int=True)
    for key in ("repoId", "githubRepoId"):
        if key in scan:
            payload[key] = clean_github_access_text(scan.get(key), allow_int=True)
    if isinstance(scan.get("quotaBucketIds"), dict):
        payload["quotaBucketIds"] = {
            key: clean_github_access_text(value, allow_int=True)
            for key, value in scan["quotaBucketIds"].items()
            if clean_github_access_text(value, allow_int=True)
        }
    if isinstance(scan.get("billingUsage"), dict):
        payload["billingUsage"] = safe_quota_usage_payload(scan.get("billingUsage"), default_scope="user")
    if isinstance(scan.get("repoUsage"), dict):
        payload["repoUsage"] = safe_quota_usage_payload(scan.get("repoUsage"), default_scope="repository")
    quota_state = public_issue_text(scan.get("quotaState"))
    if quota_state in {"reserved", "consumed", "released", "refunded"}:
        payload["quotaState"] = quota_state
    for key in ("quotaReservedAt", "quotaConsumedAt", "quotaReleasedAt"):
        if pull_request_timestamp(scan.get(key)):
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    quota_trigger = public_issue_text(scan.get("quotaConsumeTrigger"))
    if quota_trigger:
        payload["quotaConsumeTrigger"] = quota_trigger
    quota_release_reason = public_issue_text(scan.get("quotaReleaseReason"))
    if quota_release_reason:
        payload["quotaReleaseReason"] = quota_release_reason
    if isinstance(scan.get("quotaRefunded"), dict):
        refunded = scan["quotaRefunded"]
        reason = public_scan_error_code(refunded.get("reason"))
        if reason:
            payload["quotaRefunded"] = {
                "reason": reason,
                "ledgerRows": public_scan_count(refunded.get("ledgerRows")),
                "bucketRows": public_scan_count(refunded.get("bucketRows")),
            }
    if isinstance(scan.get("riskDecision"), dict):
        decision = public_issue_text(scan["riskDecision"].get("decision"))
        reason = public_issue_text(scan["riskDecision"].get("reason"))
        risk_payload = {}
        if decision:
            risk_payload["decision"] = decision
        if reason:
            risk_payload["reason"] = reason
        matched_repository_id = clean_github_access_text(scan["riskDecision"].get("matchedRepositoryId"), allow_int=True)
        if matched_repository_id:
            risk_payload["matchedRepositoryId"] = matched_repository_id
        if risk_payload:
            payload["riskDecision"] = risk_payload
    if isinstance(scan.get("repoFingerprint"), dict):
        fingerprint_payload = {}
        for source_key, target_key in (
            ("headSha", "headSha"),
            ("treeSha", "treeSha"),
            ("lockfileHash", "lockfileHash"),
            ("manifestHash", "manifestHash"),
            ("sourceFingerprint", "sourceFingerprint"),
        ):
            value = clean_github_access_text(scan["repoFingerprint"].get(source_key))
            if value:
                fingerprint_payload[target_key] = value
        if fingerprint_payload:
            payload["repoFingerprint"] = fingerprint_payload
    if "installationAccount" in scan:
        payload["installationAccount"] = clean_github_access_text(scan.get("installationAccount"))
    if "installationTargetType" in scan:
        payload["installationTargetType"] = clean_github_access_text(scan.get("installationTargetType"))
    if "repositorySelection" in scan:
        payload["repositorySelection"] = clean_github_access_text(scan.get("repositorySelection"))
    if "cloneUrl" in scan:
        payload["cloneUrl"] = trusted_github_web_url(scan.get("cloneUrl"))
    if "jobId" in scan:
        payload["jobId"] = public_issue_text(scan.get("jobId"))
    claimed_by_worker_id = public_issue_text(scan.get("claimedByWorkerId"))
    if claimed_by_worker_id:
        payload["worker"] = {"id": claimed_by_worker_id}
    if pull_request_timestamp(scan.get("claimedAt")):
        payload["claimedAt"] = pull_request_timestamp(scan.get("claimedAt")) or 0
    queue = scan_queue_payload(scan)
    if queue:
        payload["queue"] = queue
    retry = public_scan_retry(scan.get("retry"))
    if retry:
        payload["retry"] = retry
    return payload


def scan_list_payloads(scans: list[dict]) -> list[dict]:
    issue_summary_index = scan_issue_summary_index(scans)
    return [
        scan_list_payload(scan, issue_summary=issue_summary_index.get(scan_issue_summary_key(scan)))
        for scan in scans
    ]


def public_scan_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in SCAN_STATUSES else "queued"


def public_scan_phase(value: object) -> str:
    phase = public_issue_text(value)
    return phase if phase in SCAN_PHASES else ""


def public_scan_progress(value: object) -> float:
    if isinstance(value, bool):
        return 0
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(progress):
        return 0
    return min(100, max(0, progress))


def public_scan_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


def public_scan_issue_counts(value: object) -> dict:
    counts = value if isinstance(value, dict) else {}
    return {
        "critical": public_scan_count(counts.get("critical")),
        "high": public_scan_count(counts.get("high")),
        "medium": public_scan_count(counts.get("medium")),
        "low": public_scan_count(counts.get("low")),
        "info": public_scan_count(counts.get("info")),
    }



def public_scan_compact_text(value: object, *, max_length: int = 240) -> str:
    text = " ".join(review._safe_text_lenient(value).split())
    return text[:max_length]


def public_scan_compact_status(value: object, *, max_length: int = 48) -> str:
    return public_scan_compact_text(value, max_length=max_length).lower()


def public_scan_compact_text_list(value: object, *, limit: int = 8, max_length: int = 240) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif value in (None, "", [], {}):
        raw_items = []
    else:
        raw_items = [value]
    items = []
    seen = set()
    for item in raw_items:
        text = public_scan_compact_text(item, max_length=max_length)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def public_scan_completion_audit_checks(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    checks = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            check = {
                "label": public_scan_compact_text(
                    item.get("label") or item.get("title") or item.get("name") or item.get("key"),
                    max_length=120,
                ),
                "status": public_scan_compact_status(item.get("status") or item.get("verdict"), max_length=40),
                "summary": public_scan_compact_text(
                    item.get("summary") or item.get("detail") or item.get("message"),
                    max_length=280,
                ),
            }
            check = {key: field for key, field in check.items() if field}
        else:
            label = public_scan_compact_text(item, max_length=120)
            check = {"label": label} if label else {}
        if not check:
            continue
        dedupe_key = json.dumps(check, ensure_ascii=False, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        checks.append(check)
        if len(checks) >= 12:
            break
    return checks


def public_scan_completion_audit(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    retry_recommended = source.get("retryRecommended")
    if retry_recommended is None:
        retry_recommended = source.get("retry_recommended")
    payload = {
        "protocol": public_scan_compact_text(source.get("protocol"), max_length=80),
        "status": public_scan_compact_status(source.get("status"), max_length=40),
        "blockers": public_scan_compact_text_list(source.get("blockers"), limit=8, max_length=240),
        "warnings": public_scan_compact_text_list(source.get("warnings"), limit=10, max_length=240),
        "checks": public_scan_completion_audit_checks(source.get("checks")),
        "retryRecommended": bool(retry_recommended),
        "retryReason": public_scan_compact_text(
            source.get("retryReason") or source.get("retry_reason"),
            max_length=280,
        ),
        "summary": public_scan_compact_text(source.get("summary"), max_length=800),
    }
    return {key: field for key, field in payload.items() if field not in ("", [], {}, False)}


def public_scan_job_trace_rejected_reasons(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    reasons = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            reason = public_scan_compact_text(
                item.get("reason") or item.get("code") or item.get("label"),
                max_length=120,
            )
            payload = {"reason": reason} if reason else {}
            count = public_scan_count(item.get("count"))
            if count:
                payload["count"] = count
        else:
            reason = public_scan_compact_text(item, max_length=120)
            payload = {"reason": reason} if reason else {}
        if not payload:
            continue
        dedupe_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        reasons.append(payload)
        if len(reasons) >= 10:
            break
    return reasons


def public_scan_job_trace_checkpoints(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    checkpoints = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            checkpoint = {
                "key": public_scan_compact_text(
                    item.get("key") or item.get("id") or item.get("label") or item.get("name") or item.get("stage"),
                    max_length=80,
                ),
                "status": public_scan_compact_status(item.get("status"), max_length=40),
                "summary": public_scan_compact_text(
                    item.get("summary") or item.get("message") or item.get("detail"),
                    max_length=280,
                ),
                "attempt": public_scan_count(item.get("attempt")),
                "durationMs": public_scan_count(item.get("durationMs") or item.get("duration_ms")),
            }
            checkpoint = {key: field for key, field in checkpoint.items() if field not in ("", 0)}
        else:
            key = public_scan_compact_text(item, max_length=80)
            checkpoint = {"key": key} if key else {}
        if not checkpoint:
            continue
        dedupe_key = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        checkpoints.append(checkpoint)
        if len(checkpoints) >= 20:
            break
    return checkpoints


def public_scan_job_trace(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    payload = {
        "protocol": public_scan_compact_text(source.get("protocol"), max_length=80),
        "checkpoints": public_scan_job_trace_checkpoints(source.get("checkpoints")),
        "summaries": public_scan_compact_text_list(source.get("summaries"), limit=12, max_length=280),
        "candidateFindingsBeforeFilter": public_scan_count(
            source.get("candidateFindingsBeforeFilter") or source.get("candidate_findings_before_filter")
        ),
        "rejectedReasons": public_scan_job_trace_rejected_reasons(
            source.get("rejectedReasons") or source.get("rejected_reasons")
        ),
        "nextRetryHint": public_scan_compact_text(
            source.get("nextRetryHint") or source.get("next_retry_hint"),
            max_length=280,
        ),
    }
    return {key: field for key, field in payload.items() if field not in ("", [], {}, 0)}


def public_confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        confidence = float(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence):
        return 0.0
    return min(1.0, max(0.0, confidence))


def review_scope_parts(scope_key: str) -> dict:
    parts = {}
    for item in str(scope_key or "").split("|"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key and value:
            parts[key] = value
    return parts


def review_shadow_evaluation(scope_key: str) -> dict:
    parts = review_scope_parts(scope_key)
    events = db.list_review_decision_events_for_scope(
        user_id=parts.get("user", ""),
        repo_key=parts.get("repo", ""),
        branch=parts.get("branch", ""),
    )
    latest_by_observation: dict[str, dict] = {}
    for event in events:
        observation_key = public_issue_text(event.get("candidate_observation_key"))
        if observation_key and observation_key not in latest_by_observation:
            latest_by_observation[observation_key] = event
    metrics = {
        "scopeKey": public_issue_text(scope_key),
        "candidateCount": len(latest_by_observation),
        "labeledOutcomeCount": 0,
        "currentReportedCount": 0,
        "currentReportedLabeledCount": 0,
        "currentReportedValidCount": 0,
        "currentReportedFalsePositiveCount": 0,
        "currentFalsePositiveProxy": None,
        "currentPrecisionProxy": None,
        "proposedReportedCount": 0,
        "proposedReportedLabeledCount": 0,
        "proposedReportedValidCount": 0,
        "proposedReportedFalsePositiveCount": 0,
        "proposedFalsePositiveProxy": None,
        "proposedPrecisionProxy": None,
        "proposedAuditOnlyCount": 0,
        "proposedRejectedCount": 0,
        "auditOnlyReviewedCount": 0,
        "auditOnlyValidCount": 0,
        "auditOnlyPromotionRate": None,
        "estimatedFalsePositiveReduction": 0,
        "verifiedSuppressionCount": 0,
        "byVerificationStatus": {},
        "scoreDistributionByVerificationStatus": {},
    }
    for observation_key, event in latest_by_observation.items():
        current_decision = public_issue_text(event.get("decision")).lower()
        if current_decision == "reported":
            metrics["currentReportedCount"] += 1
        factors = review_json_dict(event.get("score_factors_json"))
        proposed_decision = public_issue_text(factors.get("proposedDecision")).lower()
        if proposed_decision not in {"reported", "audit_only", "rejected"}:
            proposed_decision = current_decision if current_decision in {"reported", "audit_only", "rejected"} else "rejected"
        if proposed_decision == "reported":
            metrics["proposedReportedCount"] += 1
        elif proposed_decision == "audit_only":
            metrics["proposedAuditOnlyCount"] += 1
        else:
            metrics["proposedRejectedCount"] += 1
        status = public_issue_text(event.get("verification_status")).lower()
        if status not in ISSUE_VERIFICATION_STATUSES:
            status = "potential_risk"
        status_metrics = metrics["byVerificationStatus"].setdefault(
            status,
            {
                "candidateCount": 0,
                "labeledOutcomeCount": 0,
                "currentReportedCount": 0,
                "currentReportedFalsePositiveCount": 0,
                "proposedReportedCount": 0,
                "proposedReportedFalsePositiveCount": 0,
                "proposedAuditOnlyCount": 0,
                "proposedRejectedCount": 0,
            },
        )
        status_metrics["candidateCount"] += 1
        score_band = review_shadow_score_band(event, factors)
        score_distribution = metrics["scoreDistributionByVerificationStatus"].setdefault(
            status,
            {"unknown": 0, "lt_0_60": 0, "0_60_0_70": 0, "0_70_0_82": 0, "0_82_0_90": 0, "0_90_1_00": 0},
        )
        score_distribution[score_band] = score_distribution.get(score_band, 0) + 1
        if current_decision == "reported":
            status_metrics["currentReportedCount"] += 1
        if proposed_decision == "reported":
            status_metrics["proposedReportedCount"] += 1
        elif proposed_decision == "audit_only":
            status_metrics["proposedAuditOnlyCount"] += 1
        else:
            status_metrics["proposedRejectedCount"] += 1
        if status in {"verified", "static_proof"} and proposed_decision != "reported":
            metrics["verifiedSuppressionCount"] += 1
        label = effective_review_outcome_label(observation_key)
        outcome = public_issue_text(label.get("outcome_label")).lower()
        if outcome not in {"valid", "false_positive"}:
            continue
        metrics["labeledOutcomeCount"] += 1
        status_metrics["labeledOutcomeCount"] += 1
        is_valid = outcome == "valid"
        is_false_positive = outcome == "false_positive"
        if current_decision == "reported":
            metrics["currentReportedLabeledCount"] += 1
            metrics["currentReportedValidCount"] += 1 if is_valid else 0
            metrics["currentReportedFalsePositiveCount"] += 1 if is_false_positive else 0
            status_metrics["currentReportedFalsePositiveCount"] += 1 if is_false_positive else 0
        if proposed_decision == "reported":
            metrics["proposedReportedLabeledCount"] += 1
            metrics["proposedReportedValidCount"] += 1 if is_valid else 0
            metrics["proposedReportedFalsePositiveCount"] += 1 if is_false_positive else 0
            status_metrics["proposedReportedFalsePositiveCount"] += 1 if is_false_positive else 0
        elif proposed_decision == "audit_only":
            metrics["auditOnlyReviewedCount"] += 1
            metrics["auditOnlyValidCount"] += 1 if is_valid else 0
    if metrics["currentReportedLabeledCount"]:
        metrics["currentFalsePositiveProxy"] = metrics["currentReportedFalsePositiveCount"] / metrics["currentReportedLabeledCount"]
        metrics["currentPrecisionProxy"] = metrics["currentReportedValidCount"] / metrics["currentReportedLabeledCount"]
    if metrics["proposedReportedLabeledCount"]:
        metrics["proposedFalsePositiveProxy"] = metrics["proposedReportedFalsePositiveCount"] / metrics["proposedReportedLabeledCount"]
        metrics["proposedPrecisionProxy"] = metrics["proposedReportedValidCount"] / metrics["proposedReportedLabeledCount"]
    if metrics["auditOnlyReviewedCount"]:
        metrics["auditOnlyPromotionRate"] = metrics["auditOnlyValidCount"] / metrics["auditOnlyReviewedCount"]
    metrics["estimatedFalsePositiveReduction"] = (
        metrics["currentReportedFalsePositiveCount"] - metrics["proposedReportedFalsePositiveCount"]
    )
    return metrics


def review_shadow_score_band(event: dict, factors: dict) -> str:
    score = None
    for value in (
        event.get("truth_probability"),
        factors.get("truthProbability"),
        event.get("decision_score"),
        factors.get("decisionScore"),
    ):
        score = public_review_probability(value)
        if score is not None:
            break
    if score is None:
        return "unknown"
    if score < 0.60:
        return "lt_0_60"
    if score < 0.70:
        return "0_60_0_70"
    if score < 0.82:
        return "0_70_0_82"
    if score < 0.90:
        return "0_82_0_90"
    return "0_90_1_00"


def graph_verified_report_item_is_public(value: object) -> bool:
    source = value if isinstance(value, dict) else {}
    candidate = source.get("candidate") if isinstance(source.get("candidate"), dict) else {}
    judge = source.get("judge") if isinstance(source.get("judge"), dict) else {}
    repro = source.get("repro") if isinstance(source.get("repro"), dict) else {}
    verification = source.get("verification") if isinstance(source.get("verification"), dict) else {}
    if public_issue_text(judge.get("status")).lower() != "confirmed":
        return False
    verification_status = public_issue_text(verification.get("status") or verification.get("verdict")).lower()
    if verification_status and verification_status != "confirmed":
        return False
    if judge.get("safe_to_show_user") is not True:
        return False
    if verification and verification.get("safe_to_show_user") is not True:
        return False
    level = public_issue_text(judge.get("level") or verification.get("level") or repro.get("level")).upper()
    if level not in {"L2", "L3"}:
        return False
    if public_issue_text(repro.get("status")).lower() != "reproduced":
        return False
    if repro.get("graph_path_exercised") is not True:
        return False
    if not graph_verified_item_has_repro_log_and_exit_code(judge, repro):
        return False
    if not graph_verified_item_has_code_evidence_location(candidate):
        return False
    return graph_verified_item_has_graph_evidence(candidate)


def graph_verified_item_has_graph_evidence(candidate: dict) -> bool:
    graph_evidence = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    if not graph_evidence:
        return False
    if public_scan_compact_text(graph_evidence.get("slice_id"), max_length=240):
        return True
    for key in ("codegraph_files", "path_summary"):
        values = public_scan_compact_text_list(graph_evidence.get(key), limit=20, max_length=1000)
        if values:
            return True
    return False


def graph_verified_item_has_repro_log_and_exit_code(judge: dict, repro: dict) -> bool:
    evidence_summary = judge.get("evidence_summary") if isinstance(judge.get("evidence_summary"), dict) else {}
    summary_log_path = public_scan_compact_text(evidence_summary.get("log_path"), max_length=500)
    commands = repro.get("commands_run") if isinstance(repro.get("commands_run"), list) else []
    for command in commands:
        if not isinstance(command, dict):
            continue
        command_text = review._safe_text_lenient(command.get("cmd") or command.get("command"))[:4000]
        log_path = public_scan_compact_text(command.get("log_path") or command.get("logPath"), max_length=500)
        if command_text and (log_path or summary_log_path) and graph_verified_command_has_exit_code(command):
            return True
    return False


def graph_verified_command_has_exit_code(command: dict) -> bool:
    if "exit_code" in command:
        value = command.get("exit_code")
    elif "exitCode" in command:
        value = command.get("exitCode")
    else:
        return False
    if isinstance(value, bool):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def graph_verified_item_has_code_evidence_location(candidate: dict) -> bool:
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if public_issue_file(item.get("file") or item.get("path")) and graph_verified_evidence_line_text(item):
            return True
    return False


def graph_verified_evidence_has_line_number(item: dict) -> bool:
    return bool(graph_verified_evidence_line_text(item))


def graph_verified_evidence_line_text(item: dict) -> str:
    lines = public_scan_compact_text(
        item.get("lines") or item.get("lineRange") or item.get("line_range"),
        max_length=80,
    )
    if lines and re.search(r"\d", lines):
        return lines
    start = 0
    for key in ("line", "start_line", "startLine", "line_start", "lineStart"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        try:
            start = int(value)
            if start > 0:
                break
        except (TypeError, ValueError):
            pass
    if start <= 0:
        return ""
    end = 0
    for key in ("end_line", "endLine", "line_end", "lineEnd"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        try:
            end = int(value)
            if end >= start:
                break
        except (TypeError, ValueError):
            pass
    if end and end != start:
        return f"{start}-{end}"
    return str(start)


def public_graph_verified_report(
    value: object,
    *,
    include_markdown: bool = False,
    include_debug: bool = False,
) -> dict:
    source = value if isinstance(value, dict) else {}
    rejected = public_scan_count(source.get("rejectedCount"))
    blocked = public_scan_count(source.get("blockedCount"))
    final_json = source.get("finalJson")
    if not isinstance(final_json, dict):
        final_json = {}
    confirmed_items = public_graph_verified_confirmed_items(final_json.get("confirmed"))
    confirmed = len(confirmed_items)
    payload = {
        "version": public_scan_compact_text(source.get("version"), max_length=64) or "graph-verified-code-review/1",
        "runId": public_scan_compact_text(source.get("runId"), max_length=128),
        "mode": public_scan_compact_status(source.get("mode"), max_length=32),
        "scanMode": public_scan_compact_status(source.get("scanMode"), max_length=32),
        "head": public_scan_compact_text(source.get("head"), max_length=128),
        "confirmedCount": confirmed,
        "rejectedCount": rejected,
        "blockedCount": blocked,
        "finalJson": {
            "confirmed": confirmed_items,
        },
    }
    if include_markdown:
        final_markdown = review._safe_text_lenient(source.get("finalMarkdown"))[:120000]
        if final_markdown:
            payload["finalMarkdown"] = final_markdown
    if include_debug:
        debug_markdown = review._safe_text_lenient(source.get("debugMarkdown"))[:120000]
        if debug_markdown:
            payload["debugMarkdown"] = debug_markdown
    if not any(
        [
            payload["runId"],
            payload["mode"],
            payload["scanMode"],
            payload["head"],
            payload["confirmedCount"],
            payload["rejectedCount"],
            payload["blockedCount"],
            payload.get("finalMarkdown"),
            payload.get("debugMarkdown"),
            payload["finalJson"]["confirmed"],
        ]
    ):
        return {}
    return payload


def public_graph_verified_confirmed_items(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for item in raw_items:
        public_item = public_graph_verified_confirmed_item(item)
        if public_item:
            items.append(public_item)
        if len(items) >= 50:
            break
    return items


def public_graph_verified_confirmed_item(value: object) -> dict:
    if not graph_verified_report_item_is_public(value):
        return {}
    source = value if isinstance(value, dict) else {}
    candidate = public_graph_verified_candidate(source.get("candidate"))
    judge = public_graph_verified_judge(source.get("judge"))
    repro = public_graph_verified_repro(source.get("repro"))
    verification = public_graph_verified_verification(source.get("verification"))
    item = {}
    if candidate:
        item["candidate"] = candidate
    if judge:
        item["judge"] = judge
    if repro:
        item["repro"] = repro
    if verification:
        item["verification"] = verification
    return item


def public_graph_verified_candidate(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    candidate = {}
    for key in (
        "issue_id",
        "candidate_id",
        "dedupe_key",
        "category",
        "severity",
        "confidence",
        "repro_likelihood",
    ):
        text = public_scan_compact_text(source.get(key), max_length=240)
        if text:
            candidate[key] = text
    for key in (
        "claim",
        "trigger_condition",
        "expected_behavior",
        "actual_behavior_hypothesis",
        "minimal_repro_idea",
        "suggested_fix",
        "fix_direction",
    ):
        text = review._safe_text_lenient(source.get(key))[:4000]
        if text:
            candidate[key] = text
    evidence = public_graph_verified_evidence_list(source.get("evidence"))
    if evidence:
        candidate["evidence"] = evidence
    graph_evidence = source.get("graph_evidence") if isinstance(source.get("graph_evidence"), dict) else {}
    public_graph_evidence = {}
    slice_id = public_scan_compact_text(graph_evidence.get("slice_id"), max_length=240)
    if slice_id:
        public_graph_evidence["slice_id"] = slice_id
    files = public_scan_compact_text_list(graph_evidence.get("codegraph_files"), limit=20, max_length=500)
    if files:
        public_graph_evidence["codegraph_files"] = files
    path_summary = public_scan_compact_text_list(graph_evidence.get("path_summary"), limit=20, max_length=1000)
    if path_summary:
        public_graph_evidence["path_summary"] = path_summary
    if public_graph_evidence:
        candidate["graph_evidence"] = public_graph_evidence
    affected_tests = public_scan_compact_text_list(source.get("affected_tests"), limit=20, max_length=500)
    if affected_tests:
        candidate["affected_tests"] = affected_tests
    return candidate


def public_graph_verified_evidence_list(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = {}
        file_path = public_issue_file(raw_item.get("file") or raw_item.get("path"))
        if file_path:
            item["file"] = file_path
        lines = graph_verified_evidence_line_text(raw_item)
        if lines:
            item["lines"] = lines
        why = review._safe_text_lenient(raw_item.get("why_it_matters") or raw_item.get("summary"))[:2000]
        if why:
            item["why_it_matters"] = why
        if item:
            items.append(item)
        if len(items) >= 20:
            break
    return items


def public_graph_verified_judge(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    judge = {}
    for key in ("candidate_id", "status", "level", "reason"):
        text = public_scan_compact_text(source.get(key), max_length=1000)
        if text:
            judge[key] = text
    safe_to_show = source.get("safe_to_show_user")
    if isinstance(safe_to_show, bool):
        judge["safe_to_show_user"] = safe_to_show
    evidence_summary = source.get("evidence_summary") if isinstance(source.get("evidence_summary"), dict) else {}
    public_summary = {}
    for key in ("command", "log_path", "observable"):
        text = review._safe_text_lenient(evidence_summary.get(key))[:4000]
        if text:
            public_summary[key] = text
    if public_summary:
        judge["evidence_summary"] = public_summary
    limitations = public_scan_compact_text_list(source.get("limitations"), limit=20, max_length=1000)
    if limitations:
        judge["limitations"] = limitations
    return judge


def public_graph_verified_repro(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    repro = {}
    for key in ("candidate_id", "status", "level", "summary", "why_valid", "why_not_reproduced", "safety_notes"):
        text = review._safe_text_lenient(source.get(key))[:4000]
        if text:
            repro[key] = text
    commands = public_graph_verified_repro_commands(source.get("commands_run"))
    if commands:
        repro["commands_run"] = commands
    files_written = public_scan_compact_text_list(source.get("files_written"), limit=50, max_length=500)
    if files_written:
        repro["files_written"] = files_written
    proof = public_graph_verified_proof(source.get("proof"))
    if proof:
        repro["proof"] = proof
    graph_path_exercised = source.get("graph_path_exercised")
    if isinstance(graph_path_exercised, bool):
        repro["graph_path_exercised"] = graph_path_exercised
    touched_symbols = public_scan_compact_text_list(source.get("touched_symbols"), limit=50, max_length=500)
    if touched_symbols:
        repro["touched_symbols"] = touched_symbols
    return repro


def public_graph_verified_repro_commands(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    commands = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        command = {
            "cmd": review._safe_text_lenient(raw_item.get("cmd"))[:4000],
            "cwd": public_scan_compact_text(raw_item.get("cwd"), max_length=500),
            "log_path": public_scan_compact_text(raw_item.get("log_path"), max_length=500),
        }
        exit_code = raw_item.get("exit_code")
        if not isinstance(exit_code, bool):
            try:
                command["exit_code"] = int(exit_code)
            except (TypeError, ValueError):
                pass
        duration_ms = public_scan_count(raw_item.get("duration_ms"))
        if duration_ms:
            command["duration_ms"] = duration_ms
        command = {key: val for key, val in command.items() if val not in ("", None)}
        if command:
            commands.append(command)
        if len(commands) >= 20:
            break
    return commands


def public_graph_verified_proof(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    proof = {}
    for key in ("type", "expected", "actual", "log_excerpt"):
        text = review._safe_text_lenient(source.get(key))[:4000]
        if text:
            proof[key] = text
    return proof


def public_graph_verified_verification(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    verification = {}
    for key in ("verdict", "status", "level", "summary", "reason"):
        text = review._safe_text_lenient(source.get(key))[:4000]
        if text:
            verification[key] = text
    safe_to_show = source.get("safe_to_show_user")
    if isinstance(safe_to_show, bool):
        verification["safe_to_show_user"] = safe_to_show
    return verification


def public_scan_error_code(value: object) -> str:
    error_code = public_issue_text(value).replace("-", "_").upper()
    return error_code if error_code in {
        "GRAPH_VERIFIED_COMPLETION_FAILED",
        "REPOSITORY_TOO_LARGE",
        "CODEX_AUTH_REQUIRED",
        "CODEX_AUTH_EXPIRED",
        "CODEX_AUTHORIZATION_FAILED",
        "CODEX_SUBSCRIPTION_INACTIVE",
        "CODEX_QUOTA_EXHAUSTED",
        "CODEX_VERSION_UNSUPPORTED",
    } else ""


def public_review_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def public_review_probability(value: object) -> float | None:
    number = public_review_float(value)
    if number is None:
        return None
    return min(1.0, max(0.0, number))


def public_review_line(value: object) -> int | None:
    line = public_scan_count(value)
    return line or None


def review_json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def public_review_score_factors(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    factors = {}
    allowed = {
        "scoreKind",
        "mode",
        "model",
        "proposedDecision",
        "proposedReason",
        "originalDecision",
        "originalReason",
        "guardrailApplied",
        "reliabilitySource",
        "cohortKey",
        "rawConfidence",
        "calibratedConfidence",
        "sourceFactor",
        "sourceAdjustment",
        "evidenceStrength",
        "deltaRelevance",
        "categoryAdjustment",
        "truthProbability",
        "decisionScore",
        "driftState",
        "provider",
        "workerVersion",
        "auditProtocol",
        "promptVersion",
        "verifierVersion",
        "staticCheckerVersion",
        "baseSha",
        "headSha",
    }
    for raw_key, raw_value in source.items():
        key = public_issue_text(raw_key)[:80]
        if not key or key not in allowed:
            continue
        if isinstance(raw_value, bool):
            factors[key] = raw_value
        elif isinstance(raw_value, (int, float)) and math.isfinite(float(raw_value)):
            factors[key] = float(raw_value)
        elif raw_value is None:
            continue
        else:
            text = " ".join(review._safe_text_lenient(raw_value).split())[:160]
            if text:
                factors[key] = text
        if len(factors) >= 40:
            break
    return factors


def review_event_hash(*parts: object) -> str:
    payload = "|".join(public_issue_text(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_observation_key_for_event(event: dict) -> str:
    provided = clean_github_access_text(event.get("candidate_observation_key") or event.get("candidateObservationKey"))
    if provided:
        return provided
    return review_event_hash(
        event.get("user_id"),
        event.get("repo_id") or event.get("github_repo_id") or event.get("repo_full_name"),
        event.get("branch"),
        event.get("commit_sha"),
        event.get("source"),
        event.get("fingerprint"),
        event.get("candidate_id"),
        event.get("verification_status"),
    )


def review_decision_event_id(event: dict) -> str:
    provided = clean_github_access_text(event.get("event_id") or event.get("eventId"))
    if provided:
        return provided
    return review_event_hash(
        event.get("job_id"),
        event.get("attempt_id"),
        event.get("candidate_id"),
        event.get("fingerprint"),
        event.get("decision"),
        event.get("scoring_protocol"),
    )


def review_decision_events_from_worker_result(job: dict, body: dict, *, attempt_id: str, status: str) -> list[dict]:
    if status != "done":
        return []
    raw_events = body.get("review_decision_events") or body.get("reviewDecisionEvents")
    if not isinstance(raw_events, list):
        return []
    resolved_commit = worker_result_resolved_commit(job=job, body=body)
    commit_sha = resolved_commit or clean_github_access_text(job.get("commit")) or ""
    branch = clean_github_access_text(job.get("branch")) or "main"
    scope = {
        "scan_id": public_issue_text(job.get("scan_id")),
        "job_id": public_issue_text(job.get("job_id")),
        "attempt_id": public_issue_text(attempt_id),
        "user_id": public_issue_text(job.get("user_id")),
        "repo_id": clean_github_access_text(job.get("repo_id"), allow_int=True) or "",
        "github_repo_id": clean_github_access_text(job.get("github_repo_id"), allow_int=True) or "",
        "repo_full_name": clean_repository_full_name(job.get("repo")),
        "branch": branch,
        "commit_sha": commit_sha,
    }
    events = []
    for raw_event in raw_events[:200]:
        if not isinstance(raw_event, dict):
            continue
        protocol = public_issue_text(raw_event.get("protocol") or raw_event.get("schema_version") or raw_event.get("schemaVersion"))
        if protocol != REVIEW_DECISION_EVENT_PROTOCOL_VERSION:
            continue
        decision = public_issue_text(raw_event.get("decision")).lower()
        if decision not in {"reported", "audit_only", "rejected"}:
            continue
        verification_status = public_issue_text(
            raw_event.get("verification_status") or raw_event.get("verificationStatus")
        ).lower()
        if verification_status not in ISSUE_VERIFICATION_STATUSES:
            verification_status = "potential_risk"
        severity = review._safe_severity(raw_event.get("severity"))
        category = review._safe_category(raw_event.get("category"))
        event = {
            **scope,
            "protocol": protocol,
            "base_sha": clean_github_access_text(raw_event.get("base_sha") or raw_event.get("baseSha")) or "",
            "head_sha": clean_github_access_text(raw_event.get("head_sha") or raw_event.get("headSha")) or commit_sha,
            "candidate_id": public_issue_text(raw_event.get("candidate_id") or raw_event.get("candidateId"))[:160],
            "fingerprint": clean_github_access_text(raw_event.get("fingerprint")) or "",
            "source": public_issue_text(raw_event.get("source"))[:80],
            "provider": public_issue_text(raw_event.get("provider"))[:80],
            "model": public_issue_text(raw_event.get("model"))[:120],
            "category": category,
            "severity": severity,
            "verification_status": verification_status,
            "file_path": public_issue_file(raw_event.get("file_path") or raw_event.get("filePath") or raw_event.get("file")),
            "line_start": public_review_line(raw_event.get("line_start") or raw_event.get("lineStart") or raw_event.get("line")),
            "line_end": public_review_line(raw_event.get("line_end") or raw_event.get("lineEnd")),
            "normalized_title": " ".join(
                review._safe_text_lenient(raw_event.get("normalized_title") or raw_event.get("normalizedTitle") or raw_event.get("title")).split()
            )[:180],
            "raw_confidence": public_review_probability(raw_event.get("raw_confidence") or raw_event.get("rawConfidence")),
            "calibrated_confidence": public_review_probability(
                raw_event.get("calibrated_confidence") or raw_event.get("calibratedConfidence")
            ),
            "source_reliability_mean": public_review_probability(
                raw_event.get("source_reliability_mean") or raw_event.get("sourceReliabilityMean")
            ),
            "source_reliability_lb": public_review_probability(
                raw_event.get("source_reliability_lb") or raw_event.get("sourceReliabilityLb")
            ),
            "source_adjustment": public_review_float(raw_event.get("source_adjustment") or raw_event.get("sourceAdjustment")),
            "evidence_strength": public_review_float(raw_event.get("evidence_strength") or raw_event.get("evidenceStrength")),
            "delta_relevance": public_review_float(raw_event.get("delta_relevance") or raw_event.get("deltaRelevance")),
            "category_adjustment": public_review_float(raw_event.get("category_adjustment") or raw_event.get("categoryAdjustment")),
            "truth_probability": public_review_probability(raw_event.get("truth_probability") or raw_event.get("truthProbability")),
            "decision_score": public_review_float(raw_event.get("decision_score") or raw_event.get("decisionScore")),
            "decision": decision,
            "decision_reason": public_issue_text(raw_event.get("decision_reason") or raw_event.get("decisionReason"))[:120],
            "scoring_protocol": public_issue_text(raw_event.get("scoring_protocol") or raw_event.get("scoringProtocol"))[:120],
            "score_factors": public_review_score_factors(raw_event.get("score_factors") or raw_event.get("scoreFactors")),
            "created_at": now(),
        }
        event["candidate_observation_key"] = (
            clean_github_access_text(raw_event.get("candidate_observation_key") or raw_event.get("candidateObservationKey"))
            or review_observation_key_for_event(event)
        )
        event["event_id"] = (
            clean_github_access_text(raw_event.get("event_id") or raw_event.get("eventId"))
            or review_decision_event_id(event)
        )
        if event["event_id"] and event["candidate_observation_key"]:
            events.append(event)
    return events


def record_worker_review_decision_events(job: dict, body: dict, *, attempt_id: str, status: str) -> dict[str, int]:
    events = review_decision_events_from_worker_result(job, body, attempt_id=attempt_id, status=status)
    return db.record_review_decision_events(events)


def review_outcome_label_priority(label_source: object) -> int:
    source = public_issue_text(label_source).lower()
    return {
        "manual_review": 60,
        "user_explicit": 50,
        "verifier_explicit": 40,
        "deterministic_static": 35,
        "autofix": 30,
        "system_weak": 10,
        "weak_lifecycle": 10,
    }.get(source, 0)


def effective_review_outcome_label(candidate_observation_key: str) -> dict:
    labels = db.list_review_outcome_labels(candidate_observation_key)
    if not labels:
        return {}
    return sorted(
        labels,
        key=lambda item: (
            review_outcome_label_priority(item.get("label_source")),
            public_review_float(item.get("outcome_weight")) or 0.0,
            public_scan_count(item.get("created_at")),
        ),
    )[-1]


def record_review_outcome_label(
    *,
    event_id: str = "",
    candidate_observation_key: str,
    outcome_label: str,
    label_source: str,
    outcome_weight: float,
    label_reason: str = "",
    created_by: str = "",
) -> dict:
    outcome = public_issue_text(outcome_label).lower()
    if outcome not in {"valid", "false_positive", "ambiguous"}:
        raise ValueError("outcome_label must be valid, false_positive, or ambiguous")
    source = public_issue_text(label_source).lower()
    if source not in {"verifier_explicit", "user_explicit", "manual_review", "autofix", "deterministic_static", "system_weak", "weak_lifecycle"}:
        raise ValueError("label_source is invalid")
    observation_key = clean_github_access_text(candidate_observation_key) or ""
    if not observation_key:
        raise ValueError("candidate_observation_key is required")
    weight = max(0.0, min(1.0, float(outcome_weight or 0.0)))
    label_outcome_key = "" if source == "user_explicit" and created_by else outcome
    label_id = review_event_hash(observation_key, source, label_outcome_key, event_id or "", created_by or "")
    label = db.upsert_review_outcome_label(
        {
            "label_id": f"rol_{label_id[:32]}",
            "event_id": clean_github_access_text(event_id) or "",
            "candidate_observation_key": observation_key,
            "outcome_label": outcome,
            "label_source": source,
            "outcome_weight": weight,
            "label_reason": " ".join(review._safe_text_lenient(label_reason).split())[:240],
            "created_at": now(),
            "created_by": public_issue_text(created_by)[:120],
        }
    )
    return label


def record_verifier_outcome(*, event_id: str = "", candidate_observation_key: str, valid: bool, reason: str = "") -> dict:
    return record_review_outcome_label(
        event_id=event_id,
        candidate_observation_key=candidate_observation_key,
        outcome_label="valid" if valid else "false_positive",
        label_source="verifier_explicit",
        outcome_weight=1.0,
        label_reason=reason,
    )


def record_user_feedback_outcome(
    *, event_id: str = "", candidate_observation_key: str, false_positive: bool, user_id: str = "", reason: str = ""
) -> dict:
    return record_review_outcome_label(
        event_id=event_id,
        candidate_observation_key=candidate_observation_key,
        outcome_label="false_positive" if false_positive else "valid",
        label_source="user_explicit",
        outcome_weight=1.0,
        label_reason=reason,
        created_by=user_id,
    )


def record_manual_review_outcome(
    *, event_id: str = "", candidate_observation_key: str, outcome_label: str, reviewer_id: str = "", reason: str = ""
) -> dict:
    return record_review_outcome_label(
        event_id=event_id,
        candidate_observation_key=candidate_observation_key,
        outcome_label=outcome_label,
        label_source="manual_review",
        outcome_weight=1.0,
        label_reason=reason,
        created_by=reviewer_id,
    )


def review_outcome_label_payload(label: dict) -> dict:
    return {
        "labelId": public_issue_text(label.get("label_id")),
        "eventId": public_issue_text(label.get("event_id")),
        "candidateObservationKey": public_issue_text(label.get("candidate_observation_key")),
        "outcomeLabel": public_issue_text(label.get("outcome_label")).lower(),
        "labelSource": public_issue_text(label.get("label_source")).lower(),
        "outcomeWeight": public_review_float(label.get("outcome_weight")) or 0.0,
        "labelReason": " ".join(review._safe_text_lenient(label.get("label_reason")).split())[:240],
        "createdAt": pull_request_timestamp(label.get("created_at")) or 0,
        "createdBy": public_issue_text(label.get("created_by"))[:120],
    }


def record_admin_manual_review_outcome(body: dict, *, reviewer_id: str) -> dict:
    observation_key = clean_github_access_text(
        body.get("candidateObservationKey") or body.get("candidate_observation_key") or body.get("observationKey")
    )
    if not observation_key:
        raise ValueError("candidateObservationKey is required")
    outcome = public_issue_text(body.get("outcomeLabel") or body.get("outcome_label") or body.get("outcome")).lower()
    outcome = outcome.replace("-", "_").replace(" ", "_")
    if isinstance(body.get("falsePositive"), bool):
        outcome = "false_positive" if body.get("falsePositive") else "valid"
    if outcome in {"useful", "confirmed", "accepted"}:
        outcome = "valid"
    if outcome not in {"valid", "false_positive", "ambiguous"}:
        raise ValueError("outcomeLabel must be valid, false_positive, or ambiguous")
    reason = " ".join(
        review._safe_text_lenient(body.get("reason") or body.get("note") or body.get("message")).split()
    )[:240]
    label = record_manual_review_outcome(
        event_id=clean_github_access_text(body.get("eventId") or body.get("event_id")) or "",
        candidate_observation_key=observation_key,
        outcome_label=outcome,
        reviewer_id=reviewer_id,
        reason=reason,
    )
    return {
        "label": review_outcome_label_payload(label),
        "effectiveLabel": review_outcome_label_payload(effective_review_outcome_label(observation_key)),
    }


def record_autofix_outcome(*, event_id: str = "", candidate_observation_key: str, valid: bool, reason: str = "") -> dict:
    return record_review_outcome_label(
        event_id=event_id,
        candidate_observation_key=candidate_observation_key,
        outcome_label="valid" if valid else "ambiguous",
        label_source="autofix",
        outcome_weight=0.9,
        label_reason=reason,
    )


def record_weak_lifecycle_signal(
    *, event_id: str = "", candidate_observation_key: str, outcome_label: str = "ambiguous", reason: str = ""
) -> dict:
    return record_review_outcome_label(
        event_id=event_id,
        candidate_observation_key=candidate_observation_key,
        outcome_label=outcome_label,
        label_source="weak_lifecycle",
        outcome_weight=0.25,
        label_reason=reason,
    )


def review_normalized_title(value: object) -> str:
    return " ".join(review._safe_text_lenient(value).split()).lower()


def review_decision_event_match_score(issue: dict, event: dict) -> int:
    score = 0
    issue_id = public_issue_text(issue.get("id"))
    if issue_id and public_issue_text(event.get("candidate_id")) == issue_id:
        score += 8
    issue_file = public_issue_file(issue.get("file"))
    event_file = public_issue_file(event.get("file_path") or event.get("filePath"))
    if issue_file and event_file and issue_file == event_file:
        score += 3
    issue_line = public_scan_count(issue.get("line"))
    event_line = public_scan_count(event.get("line_start") or event.get("lineStart") or event.get("line"))
    if issue_line and event_line and issue_line == event_line:
        score += 2
    if review_normalized_title(issue.get("title")) and review_normalized_title(issue.get("title")) == review_normalized_title(
        event.get("normalized_title") or event.get("normalizedTitle")
    ):
        score += 2
    issue_status = public_issue_verification_status(issue)
    if issue_status and public_issue_text(event.get("verification_status")).lower() == issue_status:
        score += 1
    return score


def review_decision_event_for_issue(issue: dict) -> dict:
    job_id = public_issue_text(issue.get("jobId") or issue.get("job_id"))
    if not job_id:
        return {}
    events = db.list_review_decision_events(job_id=job_id, limit=500)
    if not events:
        return {}
    scored = [
        (review_decision_event_match_score(issue, event), event)
        for event in events
        if public_issue_text(event.get("candidate_observation_key"))
    ]
    scored = [(score, event) for score, event in scored if score > 0]
    if not scored:
        return {}
    scored.sort(key=lambda item: (item[0], public_scan_count(item[1].get("created_at"))), reverse=True)
    if len(scored) > 1 and scored[0][0] < 8 and scored[0][0] == scored[1][0]:
        return {}
    return scored[0][1]


REVIEW_USER_FEEDBACK_REASONS = {
    "useful": "User marked issue useful / valid.",
    "valid": "User marked issue useful / valid.",
    "false_positive": "False positive.",
    "not_relevant": "Not relevant to this PR.",
    "duplicate": "Duplicate issue.",
    "expected_behavior": "Expected behavior.",
    "too_speculative": "Too speculative.",
    "speculative": "Too speculative.",
    "low_impact": "Low impact.",
    "already_fixed": "Already fixed.",
}


def review_user_feedback_reason(body: dict) -> tuple[str, str]:
    for key in ("feedbackReason", "feedback_reason", "reasonCode", "reason_code"):
        value = public_issue_text(body.get(key)).lower().replace("-", "_").replace(" ", "_")
        if value in REVIEW_USER_FEEDBACK_REASONS:
            return value, REVIEW_USER_FEEDBACK_REASONS[value]
    return "", ""


def review_user_feedback_false_positive(body: dict) -> bool | None:
    for key in ("falsePositive", "false_positive", "isFalsePositive", "is_false_positive"):
        if isinstance(body.get(key), bool):
            return bool(body.get(key))
    outcome = public_issue_text(
        body.get("outcome")
        or body.get("outcomeLabel")
        or body.get("outcome_label")
        or body.get("feedback")
        or body.get("feedbackReason")
        or body.get("feedback_reason")
        or body.get("resolution")
    ).lower()
    if outcome in {"false_positive", "false-positive", "false positive", "dismissed_false_positive"}:
        return True
    if outcome in {"valid", "confirmed", "accepted", "fixed", "useful"}:
        return False
    return None


def record_issue_status_outcome_label(issue: dict, *, next_status: str, body: dict, user_id: str) -> dict:
    event = review_decision_event_for_issue(issue)
    observation_key = public_issue_text(event.get("candidate_observation_key"))
    if not observation_key:
        return {}
    feedback_code, feedback_default_reason = review_user_feedback_reason(body)
    supplied_reason = " ".join(review._safe_text_lenient(body.get("reason") or body.get("note") or body.get("message")).split())
    if feedback_code:
        reason = f"feedback:{feedback_code} - {supplied_reason or feedback_default_reason}"[:240]
    else:
        reason = supplied_reason[:240]
    explicit_false_positive = review_user_feedback_false_positive(body)
    if explicit_false_positive is not None:
        return record_user_feedback_outcome(
            event_id=public_issue_text(event.get("event_id")),
            candidate_observation_key=observation_key,
            false_positive=explicit_false_positive,
            user_id=user_id,
            reason=reason or ("marked false positive" if explicit_false_positive else "marked valid"),
        )
    if feedback_code:
        return record_review_outcome_label(
            event_id=public_issue_text(event.get("event_id")),
            candidate_observation_key=observation_key,
            outcome_label="ambiguous",
            label_source="user_explicit",
            outcome_weight=1.0,
            label_reason=reason or feedback_default_reason,
            created_by=user_id,
        )
    if next_status == "fixed":
        return record_user_feedback_outcome(
            event_id=public_issue_text(event.get("event_id")),
            candidate_observation_key=observation_key,
            false_positive=False,
            user_id=user_id,
            reason=reason or "issue marked fixed",
        )
    if next_status == "snoozed":
        return record_weak_lifecycle_signal(
            event_id=public_issue_text(event.get("event_id")),
            candidate_observation_key=observation_key,
            outcome_label="ambiguous",
            reason=reason or "issue snoozed",
        )
    return {}


def first_present(source: dict, *keys: str) -> object:
    for key in keys:
        if key in source:
            return source.get(key)
    return None


def empty_scan_verification_counts() -> dict:
    return {"verified": 0, "static_proof": 0, "potential_risk": 0, "unverified": 0}


def scan_issue_summary_key(scan: dict) -> tuple[str, str]:
    return (
        public_issue_text(scan.get("id")) if isinstance(scan, dict) else "",
        public_issue_text(scan.get("userId")) if isinstance(scan, dict) else "",
    )


def scan_issue_summary_index(scans: list[dict]) -> dict[tuple[str, str], dict]:
    summaries: dict[tuple[str, str], dict] = {}
    scan_users_by_id: dict[str, set[str]] = {}
    for scan in scans:
        scan_id, scan_user_id = scan_issue_summary_key(scan)
        if not scan_id:
            continue
        key = (scan_id, scan_user_id)
        summaries.setdefault(key, {"counts": empty_scan_verification_counts(), "downgradedCount": 0})
        scan_users_by_id.setdefault(scan_id, set()).add(scan_user_id)
    if not summaries:
        return summaries

    for issue in ISSUES:
        scan_id = public_issue_text(issue.get("scanId"))
        scan_user_ids = scan_users_by_id.get(scan_id)
        if not scan_user_ids:
            continue
        issue_user_id = public_issue_text(issue.get("userId"))
        matching_keys = [
            (scan_id, scan_user_id)
            for scan_user_id in scan_user_ids
            if not scan_user_id or not issue_user_id or issue_user_id == scan_user_id
        ]
        if not matching_keys:
            continue
        status = public_issue_verification_status(issue)
        if status not in ISSUE_VERIFICATION_STATUSES:
            status = "potential_risk"
        reported_status = public_issue_text(issue.get("reportedVerificationStatus")).lower()
        for key in matching_keys:
            summary = summaries[key]
            summary["counts"][status] += 1
            if reported_status in ISSUE_VERIFICATION_STATUSES and reported_status != status:
                summary["downgradedCount"] += 1
    return summaries


def public_scan_verification_counts(scan: dict) -> dict:
    scan_id = public_issue_text(scan.get("id")) if isinstance(scan, dict) else ""
    scan_user_id = public_issue_text(scan.get("userId")) if isinstance(scan, dict) else ""
    counts = empty_scan_verification_counts()
    if not scan_id:
        return counts
    for issue in ISSUES:
        if public_issue_text(issue.get("scanId")) != scan_id:
            continue
        issue_user_id = public_issue_text(issue.get("userId"))
        if scan_user_id and issue_user_id and issue_user_id != scan_user_id:
            continue
        status = public_issue_verification_status(issue)
        if status not in counts:
            status = "potential_risk"
        counts[status] += 1
    return counts


def scan_audit_bundle_payload(scan: dict) -> dict:
    public_scan = scan_payload(scan)
    scan_id = public_issue_text(scan.get("id"))
    scan_user_id = public_issue_text(scan.get("userId"))
    issue_payloads = []
    for issue in ISSUES:
        if public_issue_text(issue.get("scanId")) != scan_id:
            continue
        issue_user_id = public_issue_text(issue.get("userId"))
        if scan_user_id and issue_user_id and issue_user_id != scan_user_id:
            continue
        issue_payloads.append(issue_payload(issue))
    reproduction_commands = []
    evidence_items = 0
    for issue in issue_payloads:
        reproduction = issue.get("reproduction") if isinstance(issue.get("reproduction"), dict) else {}
        for command in reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []:
            text = public_issue_text(command)
            if text and text not in reproduction_commands:
                reproduction_commands.append(text)
        evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
        code_evidence = issue.get("codeEvidence") if isinstance(issue.get("codeEvidence"), list) else []
        evidence_items += len(evidence) + len(code_evidence)
    preflight = public_scan.get("preflight") or {}
    graph_verified_report = public_graph_verified_report(
        scan.get("graphVerifiedReport"),
        include_markdown=True,
        include_debug=True,
    )
    if not graph_verified_report:
        graph_verified_report = public_graph_verified_report(
            {"finalJson": {"confirmed": []}},
            include_markdown=True,
            include_debug=True,
        )
    public_scan = dict(public_scan)
    for key in (
        "auditSwarm",
        "completionAudit",
        "impactGraph",
        "jobTrace",
        "repositoryGraph",
        "semanticGraph",
        "verificationAudit",
    ):
        public_scan.pop(key, None)
    log_artifact_count = len(audit_bundle_log_artifacts_from_preflight(preflight))
    bundle = {
        "schemaVersion": 1,
        "generatedAt": now(),
        "kind": "pullwise.graph_verified_audit_bundle",
        "scan": public_scan,
        "preflight": preflight,
        "verification": public_scan.get("verification") or public_scan_verification_counts(scan),
        "evidenceSummary": {
            "issueCount": len(issue_payloads),
            "evidenceItemCount": evidence_items,
            "reproductionCommandCount": len(reproduction_commands),
            "logArtifactCount": log_artifact_count,
        },
        "reproductionCommands": reproduction_commands[:50],
        "issues": issue_payloads,
        "limitations": [
            "This bundle is generated from structured scan records stored by Pullwise.",
            "Verifier stdout/stderr is not embedded in this bundle; logPath values only identify worker-local logs.",
            "Reproduction commands are exported as untrusted text for manual review, not as executable scripts.",
            "All repository links are pinned to the recorded commit when a valid commit SHA is available.",
        ],
    }
    bundle["graphVerifiedReport"] = graph_verified_report
    artifacts = audit_bundle_artifacts(bundle)
    bundle["artifactManifest"] = [
        {key: artifact[key] for key in ("path", "mediaType", "size", "sha256")}
        for artifact in artifacts
    ]
    bundle["artifacts"] = artifacts
    return bundle


def scan_audit_bundle_zip_bytes(scan: dict) -> bytes:
    bundle = scan_audit_bundle_payload(scan)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for artifact in bundle.get("artifacts") if isinstance(bundle.get("artifacts"), list) else []:
            if not isinstance(artifact, dict):
                continue
            path = public_issue_text(artifact.get("path"))
            content = artifact.get("content")
            if not path or not isinstance(content, str):
                continue
            archive.writestr(path, content.encode("utf-8"))
    return buffer.getvalue()


def audit_bundle_cache_dir() -> str:
    configured = env("PULLWISE_AUDIT_BUNDLE_CACHE_DIR", "").strip()
    if configured:
        return configured
    database_parent = os.path.dirname(db.database_path())
    if database_parent:
        return os.path.join(database_parent, "audit-bundles")
    return os.path.join(project_root(), ".pullwise", "audit-bundles")


def audit_bundle_cache_source(scan: dict) -> dict:
    scan_id = public_issue_text(scan.get("id"))
    scan_user_id = public_issue_text(scan.get("userId"))
    issues = []
    for issue in ISSUES:
        if public_issue_text(issue.get("scanId")) != scan_id:
            continue
        issue_user_id = public_issue_text(issue.get("userId"))
        if scan_user_id and issue_user_id and issue_user_id != scan_user_id:
            continue
        issues.append(issue)
    return {"scan": scan, "issues": issues}


def audit_bundle_cache_key(scan: dict) -> str:
    payload = db.to_jsonable(audit_bundle_cache_source(scan))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_bundle_cache_path(scan: dict, cache_key: str | None = None) -> str:
    key = cache_key or audit_bundle_cache_key(scan)
    scan_id = audit_bundle_safe_artifact_name(public_issue_text(scan.get("id")) or "scan")
    return os.path.join(audit_bundle_cache_dir(), f"{scan_id}-{key}.zip")


@contextmanager
def audit_bundle_cache_lock(cache_key: str) -> Iterator[None]:
    with AUDIT_BUNDLE_CACHE_LOCKS_GUARD:
        entry = AUDIT_BUNDLE_CACHE_LOCKS.get(cache_key)
        if entry is None:
            entry = AuditBundleCacheLockEntry()
            AUDIT_BUNDLE_CACHE_LOCKS[cache_key] = entry
        entry.refs += 1

    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with AUDIT_BUNDLE_CACHE_LOCKS_GUARD:
            entry.refs -= 1
            if entry.refs == 0 and AUDIT_BUNDLE_CACHE_LOCKS.get(cache_key) is entry:
                AUDIT_BUNDLE_CACHE_LOCKS.pop(cache_key, None)


def read_audit_bundle_cache(path: str) -> bytes | None:
    try:
        with open(path, "rb") as cache_file:
            cached = cache_file.read()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to read audit bundle cache at %s.", path)
        return None
    return cached or None


def write_audit_bundle_cache(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{threading.get_ident()}.tmp"
    with open(temp_path, "wb") as cache_file:
        cache_file.write(payload)
    os.replace(temp_path, path)


def cleanup_audit_bundle_cache_for_scan(scan: dict, keep_path: str) -> None:
    cache_dir = audit_bundle_cache_dir()
    scan_id = audit_bundle_safe_artifact_name(public_issue_text(scan.get("id")) or "scan")
    prefix = f"{scan_id}-"
    try:
        names = os.listdir(cache_dir)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Failed to list audit bundle cache directory at %s.", cache_dir)
        return
    keep_path = os.path.abspath(keep_path)
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".zip"):
            continue
        candidate = os.path.abspath(os.path.join(cache_dir, name))
        if candidate == keep_path:
            continue
        try:
            os.remove(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("Failed to remove stale audit bundle cache at %s.", candidate)


def get_or_create_scan_audit_bundle_zip_bytes(scan: dict) -> bytes:
    cache_key = audit_bundle_cache_key(scan)
    cache_path = audit_bundle_cache_path(scan, cache_key)
    cached = read_audit_bundle_cache(cache_path)
    if cached is not None:
        return cached

    with audit_bundle_cache_lock(cache_key):
        cached = read_audit_bundle_cache(cache_path)
        if cached is not None:
            return cached
        payload = scan_audit_bundle_zip_bytes(scan)
        write_audit_bundle_cache(cache_path, payload)
        cleanup_audit_bundle_cache_for_scan(scan, cache_path)
        return payload


def audit_bundle_artifacts(bundle: dict) -> list[dict]:
    if isinstance(bundle.get("graphVerifiedReport"), dict):
        artifacts = [
            audit_bundle_artifact("README.md", "text/markdown", audit_bundle_readme_markdown(bundle)),
            audit_bundle_artifact("report.md", "text/markdown", audit_bundle_report_markdown(bundle)),
            audit_bundle_artifact(
                "scan/scan.json",
                "application/json",
                json.dumps(
                    bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            ),
            audit_bundle_artifact(
                "preflight/preflight.json",
                "application/json",
                json.dumps(
                    bundle.get("preflight") if isinstance(bundle.get("preflight"), dict) else {},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            ),
            audit_bundle_artifact("reproduction/commands.txt", "text/plain", audit_bundle_repro_commands_text(bundle)),
        ]
        artifacts.extend(audit_bundle_graph_verified_artifacts(bundle["graphVerifiedReport"]))
        artifacts = artifacts[:99]
        artifacts.append(
            audit_bundle_artifact(
                "artifact-manifest.json",
                "application/json",
                audit_bundle_artifact_manifest_json(artifacts),
            )
        )
        return artifacts
    artifacts = [
        audit_bundle_artifact("README.md", "text/markdown", audit_bundle_readme_markdown(bundle)),
        audit_bundle_artifact("report.md", "text/markdown", audit_bundle_report_markdown(bundle)),
        audit_bundle_artifact("reproduction/commands.txt", "text/plain", audit_bundle_repro_commands_text(bundle)),
        audit_bundle_artifact("environment.json", "application/json", audit_bundle_environment_json(bundle)),
        audit_bundle_artifact("tool-versions.json", "application/json", audit_bundle_tool_versions_json(bundle)),
    ]
    if isinstance(bundle.get("graphVerifiedReport"), dict):
        artifacts.extend(audit_bundle_graph_verified_artifacts(bundle["graphVerifiedReport"]))
    artifacts.append(audit_bundle_artifact("audit.json", "application/json", audit_bundle_json_text(bundle)))
    artifacts.extend(audit_bundle_log_artifacts(bundle))
    artifacts.extend(audit_bundle_patch_artifacts(bundle))
    for issue in bundle.get("issues") if isinstance(bundle.get("issues"), list) else []:
        if isinstance(issue, dict):
            issue_id = audit_bundle_safe_artifact_name(public_issue_text(issue.get("id")) or "issue")
            artifacts.append(
                audit_bundle_artifact(
                    f"issues/{issue_id}.md",
                    "text/markdown",
                    audit_bundle_issue_markdown(issue),
                )
            )
    artifacts = artifacts[:99]
    artifacts.append(
        audit_bundle_artifact(
            "artifact-manifest.json",
            "application/json",
            audit_bundle_artifact_manifest_json(artifacts),
        )
    )
    return artifacts


def audit_bundle_artifact(path: str, media_type: str, content: str) -> dict:
    content = content if isinstance(content, str) else ""
    encoded = content.encode("utf-8")
    return {
        "path": path,
        "mediaType": media_type,
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "content": content,
    }


def audit_bundle_graph_verified_artifacts(report: dict) -> list[dict]:
    final_json = report.get("finalJson") if isinstance(report.get("finalJson"), dict) else {}
    artifacts = [
        audit_bundle_artifact(
            "graph-verified/final.json",
            "application/json",
            json.dumps(final_json, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    ]
    final_markdown = review._safe_text_lenient(report.get("finalMarkdown"))
    if final_markdown:
        artifacts.append(audit_bundle_artifact("graph-verified/final.md", "text/markdown", final_markdown + "\n"))
    debug_markdown = review._safe_text_lenient(report.get("debugMarkdown"))
    if debug_markdown:
        artifacts.append(audit_bundle_artifact("graph-verified/debug.md", "text/markdown", debug_markdown + "\n"))
    return artifacts


def audit_bundle_artifact_manifest_json(artifacts: list[dict]) -> str:
    entries = [
        {key: artifact[key] for key in ("path", "mediaType", "size", "sha256")}
        for artifact in artifacts
        if isinstance(artifact, dict)
        and all(key in artifact for key in ("path", "mediaType", "size", "sha256"))
    ]
    payload = {
        "schemaVersion": 1,
        "selfExcluded": True,
        "note": "artifact-manifest.json is excluded from its own artifacts list to avoid a self-referential hash.",
        "artifacts": entries,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def audit_bundle_log_artifacts(bundle: dict) -> list[dict]:
    preflight = bundle.get("preflight") if isinstance(bundle.get("preflight"), dict) else {}
    return audit_bundle_log_artifacts_from_preflight(preflight)


def audit_bundle_log_artifacts_from_preflight(preflight: dict) -> list[dict]:
    return []


def audit_bundle_log_artifact_path(run: dict, index: int) -> str:
    log_path = public_issue_text(run.get("logPath"))
    safe_path = audit_bundle_safe_artifact_path(log_path)
    if safe_path:
        return safe_path if safe_path.startswith("logs/") else f"logs/{safe_path}"
    label = public_issue_text(run.get("script")) or public_issue_text(run.get("command")) or f"run-{index + 1}"
    return f"logs/verifier/{index + 1:02d}-{audit_bundle_safe_artifact_name(label)}.log"


def audit_bundle_safe_artifact_path(value: str) -> str:
    parts = []
    for part in str(value or "").replace("\\", "/").split("/"):
        safe = audit_bundle_safe_artifact_name(part)
        if safe:
            parts.append(safe)
    return "/".join(parts[:8])


def audit_bundle_verifier_log_text(run: dict, index: int, output: str) -> str:
    lines = [
        "Pullwise verifier output",
        "",
        f"Run: {index + 1}",
    ]
    for key, label in (
        ("script", "Script"),
        ("command", "Command"),
        ("status", "Status"),
        ("exitCode", "Exit code"),
        ("durationMs", "Duration ms"),
        ("logPath", "Source logPath"),
    ):
        value = public_issue_text(run.get(key))
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(["", "--- output ---", output, ""])
    return "\n".join(lines)


def audit_bundle_safe_artifact_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe[:80] or "issue"


def audit_bundle_issue_title(issue: dict) -> str:
    issue_id = public_issue_text(issue.get("id")) or "issue"
    title = review._safe_text(issue.get("title"), "Untitled finding")
    return f"{issue_id}: {title}"


def audit_bundle_readme_markdown(bundle: dict) -> str:
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    return "\n".join(
        [
            "# Pullwise GraphVerified Audit Bundle",
            "",
            f"Repository: {public_issue_text(scan.get('repo')) or 'unknown'}",
            f"Branch: {public_issue_text(scan.get('branch')) or 'main'}",
            f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
            f"Generated at: {pull_request_timestamp(bundle.get('generatedAt')) or 0}",
            "",
            "This bundle contains only GraphVerified scan evidence. Start with report.md, then inspect graph-verified/final.json and issues/*.md.",
            "",
        ]
    )


def audit_bundle_report_markdown(bundle: dict) -> str:
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    evidence_summary = bundle.get("evidenceSummary") if isinstance(bundle.get("evidenceSummary"), dict) else {}
    graph_verified_report = bundle.get("graphVerifiedReport") if isinstance(bundle.get("graphVerifiedReport"), dict) else {}
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    lines = [
        "# GraphVerified Audit Report",
        "",
        f"Repo: {public_issue_text(scan.get('repo')) or 'unknown'}",
        f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
        f"Scan: {public_issue_text(scan.get('id')) or 'unknown'}",
        "",
        "## Summary",
        "",
        f"- Confirmed issues: {public_scan_count(graph_verified_report.get('confirmedCount'))}",
        f"- Rejected candidates: {public_scan_count(graph_verified_report.get('rejectedCount'))}",
        f"- Blocked candidates: {public_scan_count(graph_verified_report.get('blockedCount'))}",
        f"- Reproduction commands: {public_scan_count(evidence_summary.get('reproductionCommandCount'))}",
        f"- Evidence items: {public_scan_count(evidence_summary.get('evidenceItemCount'))}",
    ]
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No confirmed GraphVerified issues were included in this bundle.")
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        lines.append(f"- [{audit_bundle_issue_title(issue)}](issues/{audit_bundle_safe_artifact_name(public_issue_text(issue.get('id')) or 'issue')}.md)")
    lines.append("")
    return "\n".join(lines)


def audit_bundle_dockerfile(bundle: dict) -> str:
    return "\n".join(
        [
            "# Pullwise audit reproduction container.",
            "# Build from the unzipped audit bundle with: docker build -t pullwise-audit .",
            "# This scaffold documents the audit environment and does not run captured commands.",
            "FROM ubuntu:22.04",
            "",
            "ENV DEBIAN_FRONTEND=noninteractive",
            "RUN apt-get update \\",
            "    && apt-get install -y --no-install-recommends bash ca-certificates git \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
            "WORKDIR /audit",
            "COPY . /audit",
            "",
            "# Add project-specific runtimes, databases, or service dependencies here if required.",
            "CMD [\"sh\", \"-c\", \"printf '%s\\n' 'Pullwise audit bundles do not include executable reproduction scripts.'\"]",
            "",
        ]
    )


def audit_bundle_repro_script(bundle: dict) -> str:
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    repo = clean_repository_full_name(scan.get("repo"))
    commit = clean_github_access_text(scan.get("commit")) or "pending"
    repo_url = f"{github_auth.github_web_url().rstrip('/')}/{repo}.git" if repo else ""
    commands = bundle.get("reproductionCommands") if isinstance(bundle.get("reproductionCommands"), list) else []
    lines = [
        "#!/usr/bin/env sh",
        "set -eu",
        "",
        "# Pullwise reproduction helper. Inspect this file before running commands.",
        "ISSUE_ID=${1:-}",
        "if [ -n \"$ISSUE_ID\" ]; then",
        "  SAFE_ISSUE=$(printf '%s' \"$ISSUE_ID\" | sed 's/[^A-Za-z0-9_.-]/_/g; s/^[._]*//; s/[._]*$//')",
        "  ISSUE_SCRIPT=\"reproduction/commands/${SAFE_ISSUE}.txt\"",
        "  if [ ! -f \"$ISSUE_SCRIPT\" ]; then",
        "    echo \"No reproduction script found for issue: $ISSUE_ID\" >&2",
        "    exit 2",
        "  fi",
        "  exec sh \"$ISSUE_SCRIPT\"",
        "fi",
        "",
        f"REPO_URL={shell_single_quote(repo_url)}",
        f"COMMIT={shell_single_quote(commit)}",
        "WORKDIR=${PULLWISE_REPO_DIR:-}",
        "",
        "if [ -z \"$WORKDIR\" ]; then",
        "  WORKDIR=\"${PWD}/pullwise-repro\"",
        "  if [ ! -d \"$WORKDIR/.git\" ]; then",
        "    git clone \"$REPO_URL\" \"$WORKDIR\"",
        "  fi",
        "fi",
        "",
        "cd \"$WORKDIR\"",
        "git checkout \"$COMMIT\"",
        "",
        "cat <<'PULLWISE_REPRO_COMMANDS'",
        "# Reproduction commands captured by Pullwise:",
    ]
    if commands:
        for command in commands[:50]:
            text = public_issue_text(command)
            if text:
                lines.append(text)
    else:
        lines.append("# No executable reproduction commands were captured.")
    lines.extend(
        [
            "PULLWISE_REPRO_COMMANDS",
            "",
            "echo \"Commands printed only. Review manually before copying into a shell.\"",
            "exit 0",
            "",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def audit_bundle_issue_repro_artifacts(bundle: dict) -> list[dict]:
    artifacts = []
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_id = public_issue_text(issue.get("id")) or "issue"
        commands = audit_bundle_issue_reproduction_commands(issue)
        if not commands:
            continue
        safe_issue_id = audit_bundle_safe_artifact_name(issue_id)
        artifacts.append(
            audit_bundle_artifact(
                f"reproduction/commands/{safe_issue_id}.txt",
                "text/plain",
                "\n".join(commands) + "\n",
            )
        )
    return artifacts[:20]


def audit_bundle_issue_reproduction_commands(issue: dict) -> list[str]:
    reproduction = issue.get("reproduction") if isinstance(issue.get("reproduction"), dict) else {}
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    return [public_issue_text(command) for command in commands[:20] if public_issue_text(command)]


def audit_bundle_patch_artifacts(bundle: dict) -> list[dict]:
    artifacts = []
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        patch_text = audit_bundle_issue_patch_diff(issue)
        if not patch_text:
            continue
        issue_id = audit_bundle_safe_artifact_name(public_issue_text(issue.get("id")) or "issue")
        artifacts.append(
            audit_bundle_artifact(
                f"patches/{issue_id}.diff",
                "text/x-diff",
                patch_text,
            )
        )
    return artifacts[:20]


def audit_bundle_issue_patch_diff(issue: dict) -> str:
    file_path = fix_workflow.safe_issue_file(issue.get("file"))
    if not file_path:
        return ""
    bad_lines = fix_workflow.code_lines(issue.get("badCode"))
    good_lines = fix_workflow.code_lines(issue.get("goodCode"))
    if not bad_lines or not good_lines:
        return ""
    line = review._safe_non_negative_int(issue.get("line")) or audit_bundle_first_location_line(issue)
    old_count = max(1, len(bad_lines))
    new_count = max(1, len(good_lines))
    lines = [
        "# Pullwise suggested patch. Inspect and validate before applying.",
        f"# Issue: {public_issue_text(issue.get('id')) or 'issue'}",
        f"# Title: {review._safe_text(issue.get('title'), 'Untitled finding')}",
        "--- a/" + file_path,
        "+++ b/" + file_path,
        f"@@ -{line},{old_count} +{line},{new_count} @@",
    ]
    lines.extend("-" + line for line in bad_lines)
    lines.extend("+" + line for line in good_lines)
    lines.append("")
    return "\n".join(lines)


def audit_bundle_first_location_line(issue: dict) -> int:
    locations = issue.get("affectedLocations") if isinstance(issue.get("affectedLocations"), list) else []
    for location in locations:
        if isinstance(location, dict):
            line = public_scan_count(location.get("startLine"))
            if line:
                return line
    return 1


def audit_bundle_issue_repro_script(scan: dict, issue: dict, commands: list[str]) -> str:
    repo = clean_repository_full_name(scan.get("repo"))
    commit = clean_github_access_text(scan.get("commit")) or "pending"
    repo_url = f"{github_auth.github_web_url().rstrip('/')}/{repo}.git" if repo else ""
    issue_id = public_issue_text(issue.get("id")) or "issue"
    title = review._safe_text(issue.get("title"), "Untitled finding")
    lines = [
        "#!/usr/bin/env sh",
        "set -eu",
        "",
        f"# Pullwise reproduction helper for {issue_id}: {title}",
        f"REPO_URL={shell_single_quote(repo_url)}",
        f"COMMIT={shell_single_quote(commit)}",
        "WORKDIR=${PULLWISE_REPO_DIR:-}",
        "",
        "if [ -z \"$WORKDIR\" ]; then",
        "  WORKDIR=\"${PWD}/pullwise-repro\"",
        "  if [ ! -d \"$WORKDIR/.git\" ]; then",
        "    git clone \"$REPO_URL\" \"$WORKDIR\"",
        "  fi",
        "fi",
        "",
        "cd \"$WORKDIR\"",
        "git checkout \"$COMMIT\"",
        "",
        "cat <<'PULLWISE_REPRO_COMMANDS'",
        f"# Reproduction commands captured by Pullwise for {issue_id}:",
    ]
    lines.extend(commands)
    lines.extend(
        [
            "PULLWISE_REPRO_COMMANDS",
            "",
            "echo \"Commands printed only. Review manually before copying into a shell.\"",
            "exit 0",
            "",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def audit_bundle_repro_commands_text(bundle: dict) -> str:
    commands = bundle.get("reproductionCommands") if isinstance(bundle.get("reproductionCommands"), list) else []
    if not commands:
        return "No reproduction commands were captured.\n"
    lines = [
        "# Untrusted reproduction commands captured by Pullwise.",
        "# Review manually before copying any command into a shell.",
        "",
    ]
    lines.extend(public_issue_text(command) for command in commands[:50] if public_issue_text(command))
    return "\n".join(lines) + "\n"


def audit_bundle_environment_json(bundle: dict) -> str:
    environment = {
        "scan": bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {},
        "preflight": bundle.get("preflight") if isinstance(bundle.get("preflight"), dict) else {},
        "verification": bundle.get("verification") if isinstance(bundle.get("verification"), dict) else {},
        "evidenceSummary": bundle.get("evidenceSummary") if isinstance(bundle.get("evidenceSummary"), dict) else {},
        "limitations": review._safe_text_list(bundle.get("limitations")),
    }
    return json.dumps(environment, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def audit_bundle_tool_versions_json(bundle: dict) -> str:
    preflight = bundle.get("preflight") if isinstance(bundle.get("preflight"), dict) else {}
    tools = preflight.get("toolVersions") if isinstance(preflight.get("toolVersions"), list) else []
    payload = {
        "schemaVersion": 1,
        "scan": bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {},
        "tools": [tool for tool in tools if isinstance(tool, dict)][:20],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def audit_bundle_json_text(bundle: dict) -> str:
    payload = {key: value for key, value in bundle.items() if key not in {"artifacts", "artifactManifest"}}
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def audit_bundle_issue_markdown(issue: dict) -> str:
    lines = [
        f"# {audit_bundle_issue_title(issue)}",
        "",
        f"Status: {public_issue_text(issue.get('verificationStatus')) or 'potential_risk'}",
        f"Severity: {review._safe_severity(issue.get('severity'))}",
        f"Confidence: {public_issue_text(issue.get('confidenceLevel')) or 'low'}",
        f"Repo: {clean_repository_full_name(issue.get('repo')) or 'unknown'}",
        f"Commit: {clean_github_access_text(issue.get('commit')) or 'pending'}",
        "",
    ]
    summary = review._safe_text_lenient(issue.get("summary"))
    if summary:
        lines.extend(["## Conclusion", "", summary, ""])
    checklist = issue.get("evidenceChecklist") if isinstance(issue.get("evidenceChecklist"), list) else []
    if checklist:
        lines.extend(["## Confidence Evidence", ""])
        for item in checklist:
            if not isinstance(item, dict):
                continue
            label = public_issue_text(item.get("label"))
            if not label:
                continue
            marker = "met" if item.get("met") else "missing"
            lines.append(f"- {label}: {marker}")
        lines.append("")
    evidence_trace = issue.get("evidenceTrace") if isinstance(issue.get("evidenceTrace"), list) else []
    if evidence_trace:
        lines.extend(["## Evidence Trace", ""])
        for stage in evidence_trace:
            if not isinstance(stage, dict):
                continue
            label = public_issue_text(stage.get("label")) or public_issue_text(stage.get("key")) or "Stage"
            status = public_issue_text(stage.get("status")) or "missing"
            summary = review._safe_text_lenient(stage.get("summary"))
            lines.append(f"- {label} [{status}]: {summary}")
            items = review._safe_text_list(stage.get("items"))
            for item in items[:4]:
                lines.append(f"  - {item}")
        lines.append("")
    reasoning = issue.get("reasoningBreakdown") if isinstance(issue.get("reasoningBreakdown"), dict) else {}
    reasoning_sections = (
        ("facts", "Facts"),
        ("inferences", "Inferences"),
        ("recommendations", "Recommendations"),
    )
    if any(review._safe_text_list(reasoning.get(key)) for key, _title in reasoning_sections):
        lines.extend(["## Facts, Inferences, and Recommendations", ""])
        for key, title in reasoning_sections:
            items = review._safe_text_list(reasoning.get(key))
            if not items:
                continue
            lines.extend([f"### {title}", ""])
            lines.extend(f"- {item}" for item in items)
            lines.append("")
    locations = issue.get("affectedLocations") if isinstance(issue.get("affectedLocations"), list) else []
    if locations:
        lines.extend(["## Affected Locations", ""])
        for location in locations:
            if not isinstance(location, dict):
                continue
            label = f"{public_issue_text(location.get('file'))}:L{public_scan_count(location.get('startLine'))}"
            if public_scan_count(location.get("endLine")) and location.get("endLine") != location.get("startLine"):
                label += f"-L{public_scan_count(location.get('endLine'))}"
            url = trusted_github_web_url(public_issue_text(location.get("url"))) or ""
            lines.append(f"- {label}" + (f" ({url})" if url else ""))
        lines.append("")
    evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
    if evidence:
        lines.extend(["## Evidence Chain", ""])
        for item in evidence:
            if not isinstance(item, dict):
                continue
            label = public_issue_text(item.get("label")) or public_issue_text(item.get("type")) or "Evidence"
            summary = review._safe_text_lenient(item.get("summary"))
            lines.append(f"- {label}: {summary}")
        lines.append("")
    if audit_bundle_issue_patch_diff(issue):
        issue_id = audit_bundle_safe_artifact_name(public_issue_text(issue.get("id")) or "issue")
        lines.extend(["## Suggested Patch", "", f"See `../patches/{issue_id}.diff`.", ""])
    reproduction = issue.get("reproduction") if isinstance(issue.get("reproduction"), dict) else {}
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if commands or reproduction:
        lines.extend(["## Reproduction", ""])
        if commands:
            lines.extend(["```sh", *[public_issue_text(command) for command in commands if public_issue_text(command)], "```", ""])
        for key, label in (("input", "Input"), ("expected", "Expected"), ("actual", "Actual"), ("testFile", "Test file"), ("logPath", "Log path")):
            value = review._safe_text_lenient(reproduction.get(key))
            if value:
                lines.append(f"- {label}: {value}")
        lines.append("")
    for key, title in (("whyNotFalsePositive", "Why this is not a false positive"), ("limitations", "When this may not apply")):
        items = review._safe_text_list(issue.get(key))
        if items:
            lines.extend([f"## {title}", ""])
            lines.extend(f"- {item}" for item in items)
            lines.append("")
    return "\n".join(lines)


def shell_single_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "'\"'\"'") + "'"


def public_scan_preflight(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = {}
    for key in ("mode", "execution", "summary", "repo", "branch", "commit", "workerVersion"):
        text = (
            " ".join(review._safe_text_lenient(value.get(key)).split())
            if key == "summary"
            else public_issue_text(value.get(key))
        )
        if text:
            payload[key] = text
    provider = public_scan_agent_provider(value.get("provider"))
    if provider:
        payload["provider"] = provider
    environment = public_scan_preflight_environment(value.get("environment"))
    if environment:
        payload["environment"] = environment
    repository_stats = public_scan_preflight_repository_stats(value.get("repositoryStats"))
    if repository_stats:
        payload["repositoryStats"] = repository_stats
    repository_limits = public_scan_preflight_repository_limits(value.get("repositoryLimits"))
    if repository_limits:
        payload["repositoryLimits"] = repository_limits
    if value.get("repositoryLimitExceeded") is True:
        payload["repositoryLimitExceeded"] = True
    repository_limit_reasons = review._safe_text_list(value.get("repositoryLimitReasons"))[:5]
    if repository_limit_reasons:
        payload["repositoryLimitReasons"] = repository_limit_reasons
    for key in ("languages", "packageManagers", "availableScripts", "limitations"):
        items = review._safe_text_list(value.get(key))[:20]
        if items:
            payload[key] = items
    manifests = []
    raw_manifests = value.get("manifests") if isinstance(value.get("manifests"), list) else []
    for item in raw_manifests:
        if not isinstance(item, dict):
            continue
        file_path = fix_workflow.safe_issue_file(public_issue_text(item.get("file"))) or ""
        manifest_type = public_issue_text(item.get("type"))
        if file_path and manifest_type:
            manifests.append({"file": file_path, "type": manifest_type})
    if manifests:
        payload["manifests"] = manifests[:20]
    tools = []
    raw_tools = value.get("toolVersions") if isinstance(value.get("toolVersions"), list) else []
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        name = public_issue_text(item.get("name"))
        if not name:
            continue
        record = {
            "name": name,
            "available": item.get("available") is True,
            "exitCode": public_optional_int(item.get("exitCode")) or 0,
        }
        command = public_issue_text(item.get("command"))
        output = " ".join(review._safe_text_lenient(item.get("output")).split())[:200]
        if command:
            record["command"] = command
        if output:
            record["output"] = output
        tools.append(record)
    if tools:
        payload["toolVersions"] = tools[:10]
    verifier = public_scan_preflight_verifier(value.get("verifier"))
    if verifier:
        payload["verifier"] = verifier
    return payload


def public_scan_preflight_repository_stats(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = {
        "fileCount": public_scan_count(value.get("fileCount")),
        "totalBytes": public_scan_count(value.get("totalBytes")),
    }
    if value.get("scanStoppedEarly") is True:
        payload["scanStoppedEarly"] = True
    return payload


def public_scan_preflight_repository_limits(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "maxFiles": public_scan_count(value.get("maxFiles")),
        "maxBytes": public_scan_count(value.get("maxBytes")),
    }


def public_scan_preflight_environment(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = {}
    for key in ("os", "osRelease", "platform", "machine", "pythonVersion"):
        text = public_issue_text(value.get(key))
        if text:
            payload[key] = text
    return payload


def public_scan_preflight_verifier(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = {"enabled": value.get("enabled") is True}
    summary = " ".join(review._safe_text_lenient(value.get("summary")).split())
    if summary:
        payload["summary"] = summary
    runs = []
    raw_runs = value.get("runs") if isinstance(value.get("runs"), list) else []
    for item in raw_runs:
        if not isinstance(item, dict):
            continue
        script = public_issue_text(item.get("script"))
        command = public_issue_text(item.get("command"))
        status = public_issue_text(item.get("status")).lower()
        if status not in {"passed", "failed", "skipped", "timeout", "flaky"}:
            status = "skipped"
        if not script and not command:
            continue
        record = {
            "script": script,
            "command": command,
            "status": status,
            "exitCode": public_optional_int(item.get("exitCode")) or 0,
            "durationMs": public_scan_count(item.get("durationMs")),
        }
        if isinstance(item.get("confirmedFailure"), bool):
            record["confirmedFailure"] = item.get("confirmedFailure")
        attempts = public_scan_preflight_verifier_attempts(item.get("attempts"))
        if attempts:
            record["attempts"] = attempts
        log_path = public_issue_text(item.get("logPath"))
        output = review._safe_text_lenient(item.get("output"))[:4000]
        if log_path:
            record["logPath"] = log_path
        if output or item.get("outputRedacted") is True:
            record["outputRedacted"] = True
        runs.append(record)
    if runs:
        payload["runs"] = runs[:10]
    return payload


def public_scan_preflight_verifier_attempts(value: object) -> list[dict]:
    raw_attempts = value if isinstance(value, list) else []
    attempts = []
    for item in raw_attempts:
        if not isinstance(item, dict):
            continue
        status = public_issue_text(item.get("status")).lower()
        if status not in {"passed", "failed", "skipped", "timeout"}:
            status = "skipped"
        record = {
            "attempt": public_scan_count(item.get("attempt")),
            "status": status,
            "exitCode": public_optional_int(item.get("exitCode")) or 0,
            "durationMs": public_scan_count(item.get("durationMs")),
        }
        if review._safe_text_lenient(item.get("output")) or item.get("outputRedacted") is True:
            record["outputRedacted"] = True
        attempts.append(record)
    return attempts[:3]
