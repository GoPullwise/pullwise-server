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


def public_scan_progress_step_id(value: object) -> str:
    text = public_issue_text(value)[:80]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:")
    return text if text and all(char in allowed for char in text) else ""


def public_scan_progress_step_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in {"pending", "running", "completed", "skipped", "failed", "cancelled"} else "pending"


def public_scan_progress_step(value: object, index: int) -> dict:
    if not isinstance(value, dict):
        return {}
    step_id = public_scan_progress_step_id(value.get("id") or value.get("phase") or value.get("key"))
    label = public_issue_text(value.get("label") or value.get("title") or step_id).strip()[:120]
    if not step_id and not label:
        return {}
    status = public_scan_progress_step_status(value.get("status"))
    payload = {
        "id": step_id or f"step_{index}",
        "index": public_scan_count(value.get("index")) or index,
        "label": label or step_id,
        "status": status,
        "percent": public_scan_progress(value.get("percent") if "percent" in value else value.get("progress")),
    }
    description = public_issue_text(value.get("description") or value.get("message")).strip()[:240]
    if description:
        payload["description"] = description
    error = public_issue_text(
        value.get("error")
        or value.get("errorMessage")
        or value.get("error_message")
        or value.get("errorReason")
        or value.get("error_reason")
        or value.get("failureReason")
        or value.get("failure_reason")
        or value.get("reason")
        or value.get("cause")
    ).strip()[:300]
    if not error and status in {"failed", "cancelled"}:
        error = public_issue_text(value.get("message") or value.get("description")).strip()[:300]
    if error:
        payload["error"] = error
    if "targetPercent" in value or "target_percent" in value:
        payload["targetPercent"] = public_scan_progress(value.get("targetPercent") if "targetPercent" in value else value.get("target_percent"))
    return payload


def public_scan_progress_steps(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    steps = []
    seen = set()
    for raw_index, item in enumerate(value[:80], start=1):
        step = public_scan_progress_step(item, raw_index)
        step_id = step.get("id")
        if not step or step_id in seen:
            continue
        seen.add(step_id)
        steps.append(step)
    return steps

def public_result_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in {"done", "failed"} else ""


def public_result_reading_guide(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    guide = {}
    for key in ("forUser", "forAgentQuick", "forAgentDeep", "forAgentFix", "forDebug"):
        text = public_scan_compact_text(source.get(key), max_length=240)
        if text:
            guide[key] = text
    return guide


def public_result_human_report(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    report = {}
    title = public_scan_compact_text(source.get("title"), max_length=240)
    if title:
        report["title"] = title
    summary_markdown = review._safe_text_lenient(source.get("summaryMarkdown"))[:12000]
    if summary_markdown:
        report["summaryMarkdown"] = summary_markdown
    sections = []
    raw_sections = source.get("sections") if isinstance(source.get("sections"), list) else []
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            continue
        section = {}
        heading = public_scan_compact_text(raw_section.get("heading"), max_length=160)
        markdown = review._safe_text_lenient(raw_section.get("markdown"))[:12000]
        if heading:
            section["heading"] = heading
        if markdown:
            section["markdown"] = markdown
        if section:
            sections.append(section)
        if len(sections) >= 20:
            break
    if sections:
        report["sections"] = sections
    return report


def public_result_agent_issue(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    issue = {}
    issue_id = public_issue_text(source.get("id"))
    if issue_id:
        issue["id"] = issue_id
    severity = public_scan_compact_text(source.get("severity"), max_length=32).lower()
    if severity in {"critical", "high", "medium", "low", "info"}:
        issue["severity"] = severity
    title = public_scan_compact_text(source.get("title"), max_length=240)
    if title:
        issue["title"] = title
    primary_file = public_issue_file(source.get("primaryFile"))
    if primary_file:
        issue["primaryFile"] = primary_file
    primary_line = public_scan_count(source.get("primaryLine"))
    if primary_line:
        issue["primaryLine"] = primary_line
    confidence = public_scan_compact_text(source.get("confidence"), max_length=80)
    if confidence:
        issue["confidence"] = confidence
    tags = public_scan_compact_text_list(source.get("tags"), limit=12, max_length=80)
    if tags:
        issue["tags"] = tags
    read_next = public_scan_compact_text_list(source.get("readNext"), limit=8, max_length=500)
    if read_next:
        issue["readNext"] = read_next
    for key in ("evidencePath", "sourcePath"):
        text = public_scan_compact_text(source.get(key), max_length=500)
        if text:
            issue[key] = text
    return issue


def public_result_agent_action(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    action_type = public_scan_compact_text(source.get("type"), max_length=80)
    if action_type not in {"inspect_file", "write_fix"}:
        return {}
    action = {"type": action_type}
    target_issue_id = public_issue_text(source.get("targetIssueId"))
    if target_issue_id:
        action["targetIssueId"] = target_issue_id
    path = public_issue_file(source.get("path"))
    if path:
        action["path"] = path
    reason = public_scan_compact_text(source.get("reason"), max_length=240)
    if reason:
        action["reason"] = reason
    return action


def public_result_agent_tokens_hint(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    hint = {}
    for key in ("recommendedEntry", "detailsPath", "debugPath"):
        text = public_scan_compact_text(source.get(key), max_length=500)
        if text:
            hint[key] = text
    return hint


def public_result_agent_report(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    report = {}
    schema_version = public_scan_compact_text(source.get("schemaVersion"), max_length=80)
    report["schemaVersion"] = schema_version or "pullwise-agent-result/1"
    one_line = public_scan_compact_text(source.get("oneLine"), max_length=800)
    if one_line:
        report["oneLine"] = one_line
    status = public_result_status(source.get("status"))
    if status:
        report["status"] = status
    raw_issues = source.get("issueIndex") if isinstance(source.get("issueIndex"), list) else []
    issue_index = []
    for raw_issue in raw_issues:
        issue = public_result_agent_issue(raw_issue)
        if issue:
            issue_index.append(issue)
        if len(issue_index) >= 50:
            break
    report["issueIndex"] = issue_index
    raw_actions = source.get("nextActions") if isinstance(source.get("nextActions"), list) else []
    actions = []
    for raw_action in raw_actions:
        action = public_result_agent_action(raw_action)
        if action:
            actions.append(action)
        if len(actions) >= 20:
            break
    if actions:
        report["nextActions"] = actions
    tokens_hint = public_result_agent_tokens_hint(source.get("tokensHint"))
    if tokens_hint:
        report["tokensHint"] = tokens_hint
    return report if one_line or issue_index or actions or tokens_hint else {}

def scan_agent_fix_audit_bundle_path(scan: dict) -> str:
    scan_id = public_issue_text(scan.get("id"))
    if not scan_id:
        return ""
    repo_id = clean_github_access_text(scan.get("repoId"), allow_int=True)
    if repo_id:
        return (
            f"/api/v1/repositories/{quote(repo_id, safe='')}/scans/"
            f"{quote(scan_id, safe='')}/audit-bundle.zip"
        )
    return f"/scans/{quote(scan_id, safe='')}/audit-bundle.zip"


def scan_agent_fix_audit_bundle_url(scan: dict) -> str:
    path = scan_agent_fix_audit_bundle_path(scan)
    if not path:
        return ""
    base_url = public_scan_compact_text(env("PULLWISE_API_BASE_URL", ""), max_length=400).rstrip("/")
    if base_url.startswith(("https://", "http://")):
        return f"{base_url}{path}"
    return path


def scan_agent_fix_issue_location(issue: dict) -> str:
    primary_file = public_scan_compact_text(issue.get("primaryFile"), max_length=300)
    primary_line = public_scan_count(issue.get("primaryLine"))
    if primary_file and primary_line:
        return f"{primary_file}:{primary_line}"
    return primary_file or (str(primary_line) if primary_line else "")


def scan_agent_fix_issue_line(issue: dict) -> str:
    title = public_scan_compact_text(issue.get("title") or issue.get("id"), max_length=180)
    severity = public_scan_compact_text(issue.get("severity"), max_length=40)
    issue_id = public_scan_compact_text(issue.get("id"), max_length=100)
    location = scan_agent_fix_issue_location(issue)
    parts = []
    if severity:
        parts.append(severity)
    if title:
        parts.append(title)
    line = ": ".join(parts) if parts else issue_id
    suffix = []
    if location:
        suffix.append(location)
    if issue_id and issue_id not in line:
        suffix.append(issue_id)
    return f"- {line} ({'; '.join(suffix)})" if suffix else f"- {line}"


def scan_agent_fix_issue_lines(agent_report: dict) -> list[str]:
    raw_issues = agent_report.get("issueIndex") if isinstance(agent_report.get("issueIndex"), list) else []
    issue_lines = []
    for raw_issue in raw_issues[:5]:
        if isinstance(raw_issue, dict):
            line = scan_agent_fix_issue_line(raw_issue)
            if line and line not in issue_lines:
                issue_lines.append(line)
    return issue_lines
def scan_agent_fix_confirmed_count(scan: dict, agent_report: dict) -> int:
    raw_issues = agent_report.get("issueIndex") if isinstance(agent_report.get("issueIndex"), list) else []
    if raw_issues:
        return len(raw_issues)
    counts = public_scan_issue_counts(scan.get("issues"))
    return sum(public_scan_count(value) for value in counts.values())
def scan_agent_fix_prompt(scan: dict) -> str:
    status = public_scan_status(scan.get("status"))
    if status not in {"done", "failed"}:
        return ""
    scan_id = public_issue_text(scan.get("id"))
    repo = clean_repository_full_name(scan.get("repo"))
    bundle_url = scan_agent_fix_audit_bundle_url(scan)
    if not scan_id or not repo or not bundle_url:
        return ""
    branch = clean_github_access_text(scan.get("branch")) or "main"
    commit = clean_github_access_text(scan.get("commit")) or "pending"
    agent_report = public_result_agent_report(scan.get("agentReport"))
    summary = public_scan_compact_text(agent_report.get("oneLine"), max_length=260)
    issue_lines = scan_agent_fix_issue_lines(agent_report)
    confirmed_count = scan_agent_fix_confirmed_count(scan, agent_report)
    lines = [
        "Task: fix the Pullwise scan findings in this repository.",
        f"Repository: {repo}",
        f"Branch: {branch}",
        f"Commit: {commit}",
        f"Scan ID: {scan_id}",
        f"Scan status: {status}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if confirmed_count:
        lines.append(f"Confirmed issues: {confirmed_count}")
    if issue_lines:
        lines.append("Top issues:")
        lines.extend(issue_lines)
    lines.extend(
        [
            f"Audit bundle ZIP: {bundle_url}",
            "Download and unzip the bundle, then inspect report.md, scan/scan.json, and issues/*.md.",
            "Apply the smallest correct code/test changes in the repository. Re-run the relevant tests or commands from the bundle before finishing.",
            "If the ZIP requires auth, use the same Pullwise API key/session that returned this prompt.",
        ]
    )
    return "\n".join(lines)


def public_review_run_json(value: object) -> dict:
    if isinstance(value, dict):
        return db.to_jsonable(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def public_review_run_artifact_payload(row: dict) -> dict:
    storage = public_review_run_json(row.get("storage_json"))
    storage_url = public_issue_text(row.get("storage_url"))
    if not storage and storage_url:
        storage = {"type": "server_artifact", "url": storage_url}
    inline_json = public_review_run_json(row.get("inline_json"))
    payload = {
        "artifactId": public_issue_text(row.get("artifact_id")),
        "runId": public_issue_text(row.get("run_id")),
        "kind": public_issue_text(row.get("kind")),
        "name": public_issue_text(row.get("name")),
        "mediaType": public_issue_text(row.get("media_type")),
        "schemaId": public_issue_text(row.get("schema_id")),
        "schemaVersion": public_issue_text(row.get("schema_version")),
        "required": bool(row.get("required")),
        "sha256": public_issue_text(row.get("sha256")),
        "sizeBytes": public_scan_count(row.get("size_bytes")),
        "storage": storage,
        "createdAt": pull_request_timestamp(row.get("created_at")) or 0,
        "updatedAt": pull_request_timestamp(row.get("updated_at")) or 0,
    }
    if inline_json:
        payload["inlineJson"] = inline_json
    return {key: value for key, value in payload.items() if value not in ("", None) and value != {}}



def review_run_debug_bundle_url(artifacts: list[dict]) -> str:
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        kind = public_issue_text(artifact.get("kind"))
        name = public_issue_text(artifact.get("name"))
        if kind != "debug_bundle" and name != "debug-bundle.zip":
            continue
        storage = artifact.get("storage") if isinstance(artifact.get("storage"), dict) else {}
        url = public_issue_text(storage.get("url") or artifact.get("storageUrl") or artifact.get("url"))
        if url:
            return url
    return ""
def public_review_run_payload(scan: dict) -> dict:
    run_id = public_issue_text(scan.get("runId") or scan.get("run_id"))
    job_id = public_issue_text(scan.get("jobId") or scan.get("job_id"))
    run = db.get_review_run(run_id) if run_id else None
    if run is None and job_id:
        run = db.get_latest_review_run_for_job(job_id)
    if not run:
        return {}
    resolved_run_id = public_issue_text(run.get("run_id"))
    artifacts = [public_review_run_artifact_payload(row) for row in db.list_review_run_artifact_records(resolved_run_id)]
    payload = {
        "runId": resolved_run_id,
        "jobId": public_issue_text(run.get("job_id")),
        "workerId": public_issue_text(run.get("worker_id")),
        "status": public_issue_text(run.get("status")),
        "resultStatus": public_issue_text(run.get("result_status")),
        "protocolVersion": public_issue_text(run.get("protocol_version")),
        "workerVersion": public_issue_text(run.get("worker_version")),
        "engine": {"type": public_issue_text(run.get("engine_type"))} if public_issue_text(run.get("engine_type")) else {},
        "codexThreadId": public_issue_text(run.get("codex_thread_id")),
        "startedAt": pull_request_timestamp(run.get("started_at")) or 0,
        "completedAt": pull_request_timestamp(run.get("completed_at")) or 0,
        "durationMs": public_scan_count(run.get("duration_ms")),
        "summary": public_review_run_json(run.get("summary_json")),
        "qualityGate": public_review_run_json(run.get("quality_gate_json")),
        "usage": public_review_run_json(run.get("usage_json")),
        "progress": public_review_run_json(run.get("progress_json")),
        "error": public_review_run_json(run.get("error_json")),
        "artifactCount": len(artifacts),
        "debugBundleUrl": review_run_debug_bundle_url(artifacts),
        "artifacts": artifacts,
    }
    return {key: value for key, value in payload.items() if value not in ("", None) and value != {} and value != []}


def scan_payload(scan: dict) -> dict:
    status = public_scan_status(scan.get("status"))
    payload = {
        "id": public_issue_text(scan.get("id")),
        "userId": public_issue_text(scan.get("userId")),
        "repo": clean_repository_full_name(scan.get("repo")),
        "branch": clean_github_access_text(scan.get("branch")) or "main",
        "commit": clean_github_access_text(scan.get("commit")) or "pending",
        "status": status,
        "phase": public_scan_phase(scan.get("phase")),
        "progress": public_scan_display_progress(status, scan.get("progress")),
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
    progress_steps = public_scan_progress_steps(scan.get("progressSteps") or scan.get("progress_steps"))
    if progress_steps:
        payload["progressSteps"] = progress_steps
    effective_agent_config = public_scan_agent_config(scan.get("effectiveAgentConfig"))
    if effective_agent_config:
        payload["effectiveAgentConfig"] = effective_agent_config
    preflight = public_scan_preflight(scan.get("preflight"))
    if preflight:
        payload["preflight"] = preflight
    human_report = public_result_human_report(scan.get("humanReport"))
    if human_report:
        payload["humanReport"] = human_report
    agent_report = public_result_agent_report(scan.get("agentReport"))
    if agent_report:
        payload["agentReport"] = agent_report
    review_run = public_review_run_payload(scan)
    if review_run:
        payload["reviewRun"] = review_run
        if "progressSteps" not in payload:
            review_progress = review_run.get("progress") if isinstance(review_run.get("progress"), dict) else {}
            progress_steps = public_scan_progress_steps(review_progress.get("steps"))
            if progress_steps:
                payload["progressSteps"] = progress_steps
    reading_guide = public_result_reading_guide(scan.get("readingGuide"))
    if reading_guide:
        payload["readingGuide"] = reading_guide
    agent_fix_prompt = scan_agent_fix_prompt(scan)
    if agent_fix_prompt:
        payload["agentFixPrompt"] = agent_fix_prompt
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
    status = public_scan_status(scan.get("status"))
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
        "status": status,
        "phase": public_scan_phase(scan.get("phase")),
        "progress": public_scan_display_progress(status, scan.get("progress")),
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
    progress_steps = public_scan_progress_steps(scan.get("progressSteps") or scan.get("progress_steps"))
    if progress_steps:
        payload["progressSteps"] = progress_steps
    effective_agent_config = public_scan_agent_config(scan.get("effectiveAgentConfig"))
    if effective_agent_config:
        payload["effectiveAgentConfig"] = effective_agent_config
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
    return public_scan_progress_step_id(value)


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


INCOMPLETE_TERMINAL_SCAN_PROGRESS_MAX = 94


def public_scan_display_progress(status_value: object, progress_value: object) -> float:
    status = public_scan_status(status_value)
    progress = public_scan_progress(progress_value)
    if status == "done":
        return 100
    if status in {"failed", "cancelled", "partial_completed", "lost"}:
        return min(progress, INCOMPLETE_TERMINAL_SCAN_PROGRESS_MAX)
    return progress


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


SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(x-access-token:)[^\s@]+|"
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b(pw[a-z]_[A-Za-z0-9._~+/=-]+)\b|"
    r"\b(sk-[A-Za-z0-9._~+/=-]+)\b|"
    r"\b(gh[a-z]_[A-Za-z0-9_]{12,})\b|"
    r"\b(github_pat_[A-Za-z0-9_]+)\b|"
    r"((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s'\"`]+"
)


def redact_sensitive_text(value: object, *, max_length: int | None = None) -> str:
    text = review._safe_text_lenient(value)
    if not text:
        return ""

    def replacement(match: re.Match) -> str:
        for group_index in (1, 2, 7):
            prefix = match.group(group_index)
            if prefix:
                return f"{prefix}[redacted]"
        return "[redacted]"

    redacted = SENSITIVE_TEXT_RE.sub(replacement, text)
    return redacted[:max_length] if max_length is not None else redacted

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


def public_scan_error_code(value: object) -> str:
    error_code = public_issue_text(value).replace("-", "_").upper()
    return error_code if error_code in {
        "CODEX_REVIEW_COMPLETION_FAILED",
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


def record_user_status_outcome(
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


def record_issue_status_outcome_label(issue: dict, *, next_status: str, body: dict, user_id: str) -> dict:
    event = review_decision_event_for_issue(issue)
    observation_key = public_issue_text(event.get("candidate_observation_key"))
    if not observation_key:
        return {}
    supplied_reason = " ".join(review._safe_text_lenient(body.get("reason") or body.get("note") or body.get("message")).split())
    reason = supplied_reason[:240]
    if next_status == "fixed":
        return record_user_status_outcome(
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


def scan_issue_records_for_read(scan: dict) -> list[dict]:
    scan_id = public_issue_text(scan.get("id")) if isinstance(scan, dict) else ""
    scan_user_id = public_issue_text(scan.get("userId")) if isinstance(scan, dict) else ""
    issue_records: list[dict] = []
    if scan_id and scan_user_id:
        offset = 0
        while True:
            page = db.list_user_issues_page(scan_user_id, scan_id=scan_id, limit=100, offset=offset)
            items = page.get("items") if isinstance(page, dict) else []
            if not isinstance(items, list) or not items:
                break
            issue_records.extend(issue for issue in items if isinstance(issue, dict))
            offset += len(items)
            try:
                total = int(page.get("total") or 0)
            except (TypeError, ValueError, OverflowError):
                total = 0
            if total <= 0 or len(issue_records) >= total:
                break
    if issue_records:
        return issue_records
    if not scan_id:
        return []
    fallback_records = []
    for issue in ISSUES:
        if not isinstance(issue, dict):
            continue
        if public_issue_text(issue.get("scanId")) != scan_id:
            continue
        issue_user_id = public_issue_text(issue.get("userId"))
        if scan_user_id and issue_user_id and issue_user_id != scan_user_id:
            continue
        fallback_records.append(issue)
    return fallback_records


def public_scan_verification_counts(scan: dict) -> dict:
    counts = empty_scan_verification_counts()
    for issue in scan_issue_records_for_read(scan):
        status = public_issue_verification_status(issue)
        if status not in counts:
            status = "potential_risk"
        counts[status] += 1
    return counts


def scan_audit_bundle_payload(scan: dict) -> dict:
    public_scan = scan_payload(scan)
    issue_payloads = []
    for issue in scan_issue_records_for_read(scan):
        issue_payloads.append(issue_payload(issue))
    evidence_items = 0
    for issue in issue_payloads:
        evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
        evidence_items += len(evidence)
    preflight = public_scan.get("preflight") or {}
    public_scan = dict(public_scan)

    log_artifact_count = len(audit_bundle_log_artifacts_from_preflight(preflight))
    bundle = {
        "schemaVersion": 1,
        "generatedAt": now(),
        "kind": "pullwise.review_audit_bundle",
        "scan": public_scan,
        "preflight": preflight,
        "verification": public_scan.get("verification") or public_scan_verification_counts(scan),
        "evidenceSummary": {
            "issueCount": len(issue_payloads),
            "evidenceItemCount": evidence_items,
            "logArtifactCount": log_artifact_count,
        },
        "issues": issue_payloads,
        "limitations": [
            "This bundle is generated from structured scan records stored by Pullwise.",
            "Verifier stdout/stderr is not embedded in this bundle; logPath values only identify worker-local logs.",
            "All repository links are pinned to the recorded commit when a valid commit SHA is available.",
        ],
    }
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
    return {"scan": scan, "issues": scan_issue_records_for_read(scan)}


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
        audit_bundle_artifact("environment.json", "application/json", audit_bundle_environment_json(bundle)),
        audit_bundle_artifact("tool-versions.json", "application/json", audit_bundle_tool_versions_json(bundle)),
    ]
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
            "# Pullwise Review Audit Bundle",
            "",
            f"Repository: {public_issue_text(scan.get('repo')) or 'unknown'}",
            f"Branch: {public_issue_text(scan.get('branch')) or 'main'}",
            f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
            f"Generated at: {pull_request_timestamp(bundle.get('generatedAt')) or 0}",
            "",
            "Start with report.md, then inspect scan/scan.json and issues/*.md.",
            "",
        ]
    )


def audit_bundle_report_markdown(bundle: dict) -> str:
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    evidence_summary = bundle.get("evidenceSummary") if isinstance(bundle.get("evidenceSummary"), dict) else {}
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    lines = [
        "# Pullwise Review Audit Report",
        "",
        f"Repo: {public_issue_text(scan.get('repo')) or 'unknown'}",
        f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
        f"Scan: {public_issue_text(scan.get('id')) or 'unknown'}",
        "",
        "## Summary",
        "",
        f"- Issues: {len(issues)}",
        f"- Evidence items: {public_scan_count(evidence_summary.get('evidenceItemCount'))}",
    ]
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No issues were included in this bundle.")
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        lines.append(f"- [{audit_bundle_issue_title(issue)}](issues/{audit_bundle_safe_artifact_name(public_issue_text(issue.get('id')) or 'issue')}.md)")
    lines.append("")
    return "\n".join(lines)


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
