from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import alerts
from . import _app_part_06_worker_admin as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

SCAN_SYSTEM_STATUS_CACHE_SECONDS = 5
SCAN_SYSTEM_STATUS_CACHE: dict[str, dict] = {}


def scan_system_status_payload(*, admin: bool = False) -> dict:
    cache_key = "admin" if admin else "public"
    current_time = now()
    current_db_path = db.database_path()
    cached = SCAN_SYSTEM_STATUS_CACHE.get(cache_key)
    if cached and cached.get("databasePath") == current_db_path and pull_request_timestamp(cached.get("expiresAt")) > current_time:
        return cached["payload"]
    worker_records = annotate_worker_runtime_payloads(
        db.list_workers(activated_only=True, worker_scope=db.WORKER_SCOPE_SHARED),
        include_latest_commands=admin,
    )
    workers = [worker_public_payload(worker, admin=False) for worker in worker_records]
    job_counts = db.scan_job_status_counts(worker_scope=db.WORKER_SCOPE_SHARED)
    queued_jobs = public_scan_count(job_counts.get("queued"))
    running_jobs = public_scan_count(job_counts.get("running"))
    if queued_jobs == 0 and running_jobs == 0 and SCANS:
        queued_jobs = len([scan for scan in SCANS if scan.get("status") == "queued"])
        running_jobs = len([scan for scan in SCANS if scan.get("status") == "running"])
    online = [worker for worker in workers if worker["status"] in {"idle", "busy"}]
    busy_workers = [worker for worker in workers if worker["status"] == "busy"]
    idle_workers = [worker for worker in workers if worker["status"] == "idle"]
    degraded = [worker for worker in workers if worker["status"] == "degraded"]
    offline = [worker for worker in workers if worker["status"] == "offline"]
    if not workers or (not online and not degraded):
        system_status = "down"
    elif degraded or offline:
        system_status = "degraded"
    else:
        system_status = "ok"
    payload = {
        "scanSystemStatus": system_status,
        "onlineWorkerCount": len(online),
        "totalWorkerCount": len(workers),
        "busyWorkerCount": len(busy_workers),
        "idleWorkerCount": len(idle_workers),
        "runningJobs": running_jobs,
        "queuedJobs": queued_jobs,
        "degradedWorkerCount": len(degraded),
        "offlineWorkerCount": len(offline),
    }
    alert_workers = workers
    if admin:
        admin_workers = [worker_public_payload(worker, admin=True) for worker in worker_records]
        payload["workers"] = admin_workers
        alert_workers = admin_workers
    alerts.sync_scan_system_alerts(payload, alert_workers)
    SCAN_SYSTEM_STATUS_CACHE[cache_key] = {
        "databasePath": current_db_path,
        "expiresAt": current_time + SCAN_SYSTEM_STATUS_CACHE_SECONDS,
        "payload": payload,
    }
    return payload


def clean_scan_error(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.replace("\x00", "").splitlines()
    first_line = (lines[0] if lines else "").strip()
    return redact_sensitive_text(first_line, max_length=500)


def public_issue_file(value: object, *, issue: dict | None = None, job: dict | None = None) -> str:
    path = review._safe_text(value)
    if not path:
        return ""

    repo_path = issue_repo_path(issue) if issue else None
    normalized = review._safe_finding_file(path, repo_path)
    if normalized:
        return normalized

    job_id = public_issue_text(job.get("job_id")) if isinstance(job, dict) else ""
    if not job_id and isinstance(issue, dict):
        job_id = public_issue_text(issue.get("jobId"))
    worker_relative = worker_checkout_relative_file(path, job_id)
    return fix_workflow.safe_issue_file(worker_relative) or ""


def issue_repo_path(issue: dict | None) -> str | None:
    if not isinstance(issue, dict):
        return None
    scan_id = public_issue_text(issue.get("scanId"))
    if not scan_id:
        return None
    scan = db.get_user_scan_snapshot(public_issue_text(issue.get("userId")), scan_id)
    if scan is None:
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    repo_path = scan.get("repoPath") if isinstance(scan, dict) else None
    return repo_path if isinstance(repo_path, str) and repo_path else None


def worker_checkout_relative_file(path: str, job_id: str) -> str | None:
    if not path or not job_id:
        return None
    normalized = path.replace("\\", "/")
    if not (normalized.startswith("/") or WINDOWS_DRIVE_PATH_RE.match(path)):
        return None
    marker = f"/{job_id}/"
    index = normalized.casefold().find(marker.casefold())
    if index < 0:
        return None
    return normalized[index + len(marker) :]


def issue_scan(issue: dict | None) -> dict | None:
    if not isinstance(issue, dict):
        return None
    scan_id = public_issue_text(issue.get("scanId"))
    if not scan_id:
        return None
    scan = db.get_user_scan_snapshot(public_issue_text(issue.get("userId")), scan_id)
    if scan is not None:
        return scan
    return next((item for item in SCANS if item.get("id") == scan_id), None)


def issue_commit(issue: dict | None, *, job: dict | None = None) -> str:
    if isinstance(issue, dict):
        commit = clean_github_access_text(issue.get("commit"))
        if commit and commit.lower() != "pending":
            return commit
        scan = issue_scan(issue)
        if scan:
            commit = clean_github_access_text(scan.get("commit"))
            if commit:
                return commit
    if isinstance(job, dict):
        return clean_github_access_text(job.get("commit")) or "pending"
    return "pending"


def issue_branch(issue: dict | None, *, job: dict | None = None) -> str:
    if isinstance(issue, dict):
        branch = clean_github_access_text(issue.get("branch"))
        if branch:
            return branch
        scan = issue_scan(issue)
        if scan:
            branch = clean_github_access_text(scan.get("branch"))
            if branch:
                return branch
    if isinstance(job, dict):
        return clean_github_access_text(job.get("branch")) or "main"
    return "main"


def github_blob_line_url(
    *,
    repo: object,
    commit: object,
    file: object,
    start_line: object = 0,
    end_line: object = 0,
) -> str | None:
    repo_name = clean_repository_full_name(repo)
    commit_sha = clean_github_access_text(commit)
    file_path = fix_workflow.safe_issue_file(public_issue_text(file)) or ""
    if not repo_name or not file_path or not GIT_COMMIT_SHA_RE.fullmatch(commit_sha or ""):
        return None
    encoded_file = "/".join(quote(part, safe="") for part in file_path.split("/"))
    url = f"{github_auth.github_web_url().rstrip('/')}/{repo_name}/blob/{quote(commit_sha, safe='')}/{encoded_file}"
    start = review._safe_non_negative_int(start_line)
    end = review._safe_non_negative_int(end_line)
    if start:
        url += f"#L{start}"
        if end and end != start:
            url += f"-L{end}"
    return trusted_github_web_url(url)


def public_line_range(source: dict) -> tuple[int, int]:
    start = review._safe_non_negative_int(
        source.get("startLine", source.get("start_line", source.get("line")))
    )
    end = review._safe_non_negative_int(source.get("endLine", source.get("end_line", start)))
    if start and end and end < start:
        end = start
    if start and not end:
        end = start
    return start, end


def public_issue_affected_locations(issue: dict, *, job: dict | None = None) -> list[dict]:
    locations = []
    seen = set()
    raw_locations = issue.get("affectedLocations") if isinstance(issue.get("affectedLocations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = public_issue_file(item.get("file") or item.get("path"), issue=issue, job=job)
        if not file_path:
            continue
        start_line, end_line = public_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        location = {"file": file_path, "startLine": start_line, "endLine": end_line}
        github_url = github_blob_line_url(
            repo=issue.get("repo") or (job or {}).get("repo"),
            commit=issue_commit(issue, job=job),
            file=file_path,
            start_line=start_line,
            end_line=end_line,
        )
        if github_url:
            location["url"] = github_url
        locations.append(location)

    file_path = public_issue_file(issue.get("file"), issue=issue, job=job)
    line = review._safe_non_negative_int(issue.get("line"))
    if file_path and (file_path, line, line) not in seen:
        location = {"file": file_path, "startLine": line, "endLine": line}
        github_url = github_blob_line_url(
            repo=issue.get("repo") or (job or {}).get("repo"),
            commit=issue_commit(issue, job=job),
            file=file_path,
            start_line=line,
            end_line=line,
        )
        if github_url:
            location["url"] = github_url
        locations.append(location)
    return locations[:10]


def public_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        candidate = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return candidate


def public_issue_reproduction(issue: dict) -> dict:
    raw = issue.get("reproduction") if isinstance(issue.get("reproduction"), dict) else {}
    commands = review._safe_text_list(raw.get("commands"))
    command = public_issue_text(raw.get("command"))
    if command and command not in commands:
        commands.append(command)
    payload: dict[str, object] = {}
    if commands:
        payload["commands"] = commands[:5]
    for source_key, target_key in (
        ("input", "input"),
        ("expected", "expected"),
        ("actual", "actual"),
        ("testFile", "testFile"),
        ("test_file", "testFile"),
        ("logPath", "logPath"),
        ("log_path", "logPath"),
    ):
        value = review._safe_text_lenient(raw.get(source_key))
        if value and target_key not in payload:
            payload[target_key] = value[:2000]
    return payload


def public_issue_reproduction_payload(issue: dict) -> dict:
    reproduction = public_issue_reproduction(issue)
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    return {
        "commands": commands,
        "input": public_issue_text(reproduction.get("input")),
        "expected": public_issue_text(reproduction.get("expected")),
        "actual": public_issue_text(reproduction.get("actual")),
        "testFile": public_issue_text(reproduction.get("testFile")),
        "logPath": public_issue_text(reproduction.get("logPath")),
    }


def public_issue_runtime_evidence_state(issue: dict, evidence: list[dict]) -> dict:
    reproduction = public_issue_reproduction(issue)
    has_reproduction_command = bool(reproduction.get("commands"))
    has_reproduction_output = any(
        bool(reproduction.get(key)) for key in ("actual", "expected", "input", "testFile", "logPath")
    )
    raw_evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
    has_raw_output = False
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        evidence_type = public_issue_text(item.get("type")).lower()
        if evidence_type not in {"runtime_log", "test", "fix_verification"}:
            continue
        if review._safe_text_lenient(item.get("output")):
            has_raw_output = True
            break
        if review._safe_text_lenient(item.get("actual")):
            has_raw_output = True
            break
    has_raw_runtime = bool(reproduction.get("testFile") or reproduction.get("logPath")) or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and bool(item.get("logPath") or item.get("file"))
        for item in evidence
    )
    has_runtime_output = has_reproduction_command and (has_raw_output or has_reproduction_output or has_raw_runtime)
    return {
        "has_reproduction_command": has_reproduction_command,
        "has_runtime_output": has_runtime_output,
        "has_raw_runtime": has_raw_runtime,
        "reproduction": reproduction,
    }


def public_issue_evidence(
    issue: dict,
    *,
    job: dict | None = None,
    affected_locations: list[dict] | None = None,
) -> list[dict]:
    affected_locations = affected_locations or public_issue_affected_locations(issue, job=job)
    evidence = []
    raw_evidence = issue.get("evidence") if isinstance(issue.get("evidence"), list) else []
    for item in raw_evidence:
        if isinstance(item, str):
            summary = review._safe_text_lenient(item)
            if summary:
                evidence.append({"type": "code", "label": "Evidence", "summary": summary[:2000]})
            continue
        if not isinstance(item, dict):
            continue
        evidence_type = public_issue_text(item.get("type")).lower()
        if evidence_type not in ISSUE_EVIDENCE_TYPES:
            evidence_type = "code"
        label = public_issue_text(item.get("label")) or evidence_type.replace("_", " ").title()
        summary = review._safe_text_lenient(item.get("summary"))
        file_path = public_issue_file(item.get("file") or item.get("path"), issue=issue, job=job)
        start_line, end_line = public_line_range(item)
        command = public_issue_text(item.get("command"))
        exit_code = public_optional_int(item.get("exitCode") if "exitCode" in item else item.get("exit_code"))
        log_path = public_issue_text(item.get("logPath") or item.get("log_path"))
        output_redacted = bool(review._safe_text_lenient(item.get("output")) or item.get("outputRedacted") is True)
        source_url = trusted_github_web_url(item.get("url"))
        github_url = github_blob_line_url(
            repo=issue.get("repo") or (job or {}).get("repo"),
            commit=issue_commit(issue, job=job),
            file=file_path,
            start_line=start_line,
            end_line=end_line,
        )
        if not any([summary, file_path, command, log_path, source_url, github_url]):
            continue
        record = {"type": evidence_type, "label": label, "summary": summary}
        if file_path:
            record["file"] = file_path
        if start_line:
            record["startLine"] = start_line
            record["endLine"] = end_line
        if command:
            record["command"] = command
        if exit_code is not None:
            record["exitCode"] = exit_code
        if log_path:
            record["logPath"] = log_path
        if output_redacted:
            record["outputRedacted"] = True
        if source_url or github_url:
            record["url"] = source_url or github_url
        evidence.append(record)

    reproduction = public_issue_reproduction(issue)
    has_runtime_evidence = any(item.get("type") in {"runtime_log", "test", "fix_verification"} for item in evidence)
    if reproduction and not has_runtime_evidence:
        commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
        test_file = public_issue_file(reproduction.get("testFile"), issue=issue, job=job)
        log_path = public_issue_text(reproduction.get("logPath"))
        summary = (
            review._safe_text_lenient(reproduction.get("actual"))
            or review._safe_text_lenient(reproduction.get("expected"))
            or review._safe_text_lenient(reproduction.get("input"))
            or "Reproduction details were provided by the worker."
        )
        record = {"type": "test" if test_file else "runtime_log", "label": "Reproduction", "summary": summary}
        if commands:
            record["command"] = str(commands[0])
        if test_file:
            record["file"] = test_file
        if log_path:
            record["logPath"] = log_path
        if reproduction.get("actual"):
            record["outputRedacted"] = True
        if any([record.get("command"), record.get("file"), record.get("logPath"), summary]):
            evidence.append(record)

    has_code_location = any(item.get("type") == "code" and item.get("file") for item in evidence)
    if affected_locations and not has_code_location:
        location = affected_locations[0]
        record = {
            "type": "code",
            "label": "Code location",
            "summary": "Primary repository location tied to this finding.",
            "file": location["file"],
            "startLine": location.get("startLine", 0),
            "endLine": location.get("endLine", 0),
        }
        if location.get("url"):
            record["url"] = location["url"]
        evidence.insert(0, record)
    return evidence[:20]


def public_issue_verification_status(
    issue: dict,
    *,
    affected_locations: list[dict] | None = None,
    evidence: list[dict] | None = None,
) -> str:
    status = public_issue_text(issue.get("verificationStatus")).lower()
    if status not in ISSUE_VERIFICATION_STATUSES:
        status = ""
    has_fixed_commit = bool(GIT_COMMIT_SHA_RE.fullmatch(issue_commit(issue) or ""))
    affected_locations = affected_locations or public_issue_affected_locations(issue)
    has_precise_location = any(location.get("file") and location.get("startLine") for location in affected_locations)
    evidence = evidence or public_issue_evidence(issue, affected_locations=affected_locations)
    has_evidence = bool(evidence)
    runtime_state = public_issue_runtime_evidence_state(issue, evidence)
    has_static_evidence = bool(affected_locations) or any(
        item.get("type") in {"code", "path", "trigger", "documentation", "tool"}
        and any([item.get("file"), item.get("summary"), item.get("command")])
        for item in evidence
    )
    verified_ready = (
        has_fixed_commit
        and has_precise_location
        and has_evidence
        and runtime_state["has_reproduction_command"]
        and runtime_state["has_runtime_output"]
        and runtime_state["has_raw_runtime"]
    )
    if status == "verified" and not verified_ready:
        return "static_proof" if has_static_evidence else "potential_risk"
    if status == "static_proof" and not has_static_evidence:
        return "potential_risk"
    if status:
        return status
    if verified_ready:
        return "verified"
    if has_static_evidence:
        return "static_proof"
    return "potential_risk"

def public_issue_evidence_checklist(
    issue: dict,
    *,
    affected_locations: list[dict],
    evidence: list[dict],
) -> list[dict]:
    commit = issue_commit(issue)
    runtime_state = public_issue_runtime_evidence_state(issue, evidence)
    return [
        {"label": "Fixed commit", "met": bool(GIT_COMMIT_SHA_RE.fullmatch(commit or ""))},
        {
            "label": "Precise file and line",
            "met": any(location.get("file") and location.get("startLine") for location in affected_locations),
        },
        {"label": "Evidence chain", "met": bool(evidence)},
        {"label": "Reproduction command", "met": runtime_state["has_reproduction_command"]},
        {"label": "Runtime output", "met": runtime_state["has_runtime_output"]},
        {"label": "Raw log or test", "met": runtime_state["has_raw_runtime"]},
    ]

def public_issue_confidence_level(verification_status: str, checklist: list[dict]) -> str:
    met = {item.get("label"): bool(item.get("met")) for item in checklist}
    if verification_status == "verified" and met.get("Fixed commit") and met.get("Evidence chain"):
        return "high"
    if verification_status == "static_proof" and met.get("Precise file and line") and met.get("Evidence chain"):
        return "high"
    if met.get("Evidence chain") or met.get("Precise file and line"):
        return "medium"
    return "low"


def append_public_reasoning_item(items: list[str], value: object, *, limit: int = 240) -> None:
    text = " ".join(review._safe_text_lenient(value).split())
    if not text:
        return
    text = text[:limit]
    if text not in items:
        items.append(text)


def public_issue_trace_stage(key: str, label: str, items: list[str], missing_summary: str) -> dict:
    cleaned: list[str] = []
    for item in items:
        append_public_reasoning_item(cleaned, item)
    status = "present" if cleaned else "missing"
    return {
        "key": key,
        "label": label,
        "status": status,
        "summary": cleaned[0] if cleaned else missing_summary,
        "items": cleaned[:6],
    }


def public_issue_evidence_trace(
    issue: dict,
    *,
    affected_locations: list[dict],
    evidence: list[dict],
) -> list[dict]:
    code_items: list[str] = []
    path_items: list[str] = []
    trigger_items: list[str] = []
    runtime_items: list[str] = []
    impact_items: list[str] = []
    fix_items: list[str] = []

    for location in affected_locations[:4]:
        if not isinstance(location, dict):
            continue
        file_path = public_issue_file(location.get("file"), issue=issue)
        if not file_path:
            continue
        start_line = public_scan_count(location.get("startLine"))
        end_line = public_scan_count(location.get("endLine"))
        label = file_path
        if start_line and end_line and end_line != start_line:
            label = f"{label}:L{start_line}-L{end_line}"
        elif start_line:
            label = f"{label}:L{start_line}"
        append_public_reasoning_item(code_items, f"Affected code location: {label}")

    for item in evidence[:12]:
        if not isinstance(item, dict):
            continue
        evidence_type = public_issue_text(item.get("type")).lower()
        label = public_issue_text(item.get("label")) or evidence_type.replace("_", " ").title()
        summary = review._safe_text_lenient(item.get("summary"))
        command = public_issue_text(item.get("command"))
        log_path = public_issue_text(item.get("logPath"))
        if evidence_type == "code" and summary:
            append_public_reasoning_item(code_items, f"{label}: {summary}")
        elif evidence_type == "path" and summary:
            append_public_reasoning_item(path_items, f"{label}: {summary}")
        elif evidence_type == "trigger" and summary:
            append_public_reasoning_item(trigger_items, f"{label}: {summary}")
        elif evidence_type in {"runtime_log", "test"}:
            if summary:
                append_public_reasoning_item(runtime_items, f"{label}: {summary}")
            if command:
                append_public_reasoning_item(runtime_items, f"Command: {command}")
            if log_path:
                append_public_reasoning_item(runtime_items, f"Worker log: {log_path}")
        elif evidence_type == "fix_verification":
            if summary:
                append_public_reasoning_item(fix_items, f"{label}: {summary}")
            if command:
                append_public_reasoning_item(fix_items, f"Fix verification command: {command}")

    reproduction = public_issue_reproduction(issue)
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if commands:
        append_public_reasoning_item(trigger_items, f"Reproduction command: {commands[0]}")
    if reproduction.get("input"):
        append_public_reasoning_item(trigger_items, f"Reproduction input: {reproduction['input']}")
    if reproduction.get("actual"):
        append_public_reasoning_item(runtime_items, f"Observed output: {reproduction['actual']}")
    if reproduction.get("logPath"):
        append_public_reasoning_item(runtime_items, f"Worker log: {reproduction['logPath']}")
    if reproduction.get("testFile"):
        append_public_reasoning_item(runtime_items, f"Test file: {reproduction['testFile']}")

    for item in review._safe_text_list(issue.get("whyNotFalsePositive"))[:4]:
        append_public_reasoning_item(path_items, f"Reachability check: {item}")
    disproof_attempt = review._safe_text_lenient(issue.get("disproofAttempt") or issue.get("disproof_attempt"))
    if disproof_attempt:
        append_public_reasoning_item(path_items, f"Disproof attempt: {disproof_attempt}")

    impact = review._safe_text_lenient(issue.get("impact"))
    if impact:
        append_public_reasoning_item(impact_items, f"Impact: {impact}")
    else:
        summary = review._safe_text_lenient(issue.get("summary"))
        if summary:
            append_public_reasoning_item(impact_items, f"Reported behavior: {summary}")

    recommendation = review._safe_text_lenient(issue.get("recommendation") or issue.get("remediation") or issue.get("fix"))
    if recommendation:
        append_public_reasoning_item(fix_items, f"Recommendation: {recommendation}")
    next_agent_task = review._safe_text_lenient(issue.get("nextAgentTask") or issue.get("next_agent_task"))
    if next_agent_task:
        append_public_reasoning_item(fix_items, f"Next agent task: {next_agent_task}")
    for step in review._safe_text_list(issue.get("steps"))[:4]:
        append_public_reasoning_item(fix_items, f"Remediation step: {step}")
    if review._safe_code_lines(issue.get("badCode")) or review._safe_code_lines(issue.get("goodCode")):
        append_public_reasoning_item(fix_items, "Suggested patch evidence is available for review.")
    if issue.get("fixBenefits"):
        append_public_reasoning_item(fix_items, f"Fix benefit: {review._safe_text_lenient(issue.get('fixBenefits'))}")
    if issue.get("fixRisks"):
        append_public_reasoning_item(fix_items, f"Fix risk: {review._safe_text_lenient(issue.get('fixRisks'))}")

    return [
        public_issue_trace_stage("code", "Code", code_items, "No code location evidence was captured."),
        public_issue_trace_stage("path", "Path", path_items, "No reachability or data-flow evidence was captured."),
        public_issue_trace_stage("trigger", "Trigger", trigger_items, "No trigger input or reproduction command was captured."),
        public_issue_trace_stage("runtime", "Runtime", runtime_items, "No runtime output or test evidence was captured."),
        public_issue_trace_stage("impact", "Impact", impact_items, "No impact statement was captured."),
        public_issue_trace_stage("fix", "Fix", fix_items, "No fix or validation evidence was captured."),
    ]

def public_issue_reasoning_breakdown(
    issue: dict,
    *,
    affected_locations: list[dict],
    evidence: list[dict],
) -> dict:
    facts: list[str] = []
    inferences: list[str] = []
    recommendations: list[str] = []

    commit = issue_commit(issue)
    if GIT_COMMIT_SHA_RE.fullmatch(commit or ""):
        append_public_reasoning_item(facts, f"Finding is pinned to commit {commit}.")
    for location in affected_locations[:3]:
        if not isinstance(location, dict):
            continue
        label = public_issue_file(location.get("file"), issue=issue)
        start_line = public_scan_count(location.get("startLine"))
        end_line = public_scan_count(location.get("endLine"))
        if not label:
            continue
        if start_line and end_line and end_line != start_line:
            label = f"{label}:L{start_line}-L{end_line}"
        elif start_line:
            label = f"{label}:L{start_line}"
        append_public_reasoning_item(facts, f"Affected location recorded: {label}.")

    for item in evidence[:8]:
        if not isinstance(item, dict):
            continue
        label = public_issue_text(item.get("label")) or public_issue_text(item.get("type")) or "Evidence"
        summary = review._safe_text_lenient(item.get("summary"))
        if summary:
            append_public_reasoning_item(facts, f"{label}: {summary}")
        command = public_issue_text(item.get("command"))
        if command and item.get("type") in {"runtime_log", "test", "tool", "fix_verification"}:
            append_public_reasoning_item(facts, f"Command captured: {command}")
        log_path = public_issue_text(item.get("logPath"))
        if log_path and item.get("type") in {"runtime_log", "test", "fix_verification"}:
            append_public_reasoning_item(facts, f"Worker log reference recorded at {log_path}.")

    summary = review._safe_text_lenient(issue.get("summary")) or public_issue_text(issue.get("description"))
    append_public_reasoning_item(inferences, summary)
    append_public_reasoning_item(inferences, review._safe_text_lenient(issue.get("detectionReasoning")))
    impact = review._safe_text_lenient(issue.get("impact"))
    if impact:
        append_public_reasoning_item(inferences, f"Impact: {impact}")
    verification_summary = review._safe_text_lenient(issue.get("verificationSummary"))
    if verification_summary:
        append_public_reasoning_item(inferences, f"Verification: {verification_summary}")
    for item in review._safe_text_list(issue.get("whyNotFalsePositive"))[:3]:
        append_public_reasoning_item(inferences, f"Negative check: {item}")

    recommendation = review._safe_text_lenient(issue.get("recommendation") or issue.get("remediation") or issue.get("fix"))
    append_public_reasoning_item(recommendations, recommendation)
    next_agent_task = review._safe_text_lenient(issue.get("nextAgentTask") or issue.get("next_agent_task"))
    append_public_reasoning_item(recommendations, next_agent_task)
    for step in review._safe_text_list(issue.get("steps"))[:6]:
        append_public_reasoning_item(recommendations, step)
    if review._safe_code_lines(issue.get("badCode")) or review._safe_code_lines(issue.get("goodCode")):
        append_public_reasoning_item(
            recommendations,
            "Inspect the suggested patch evidence and validate it before applying changes.",
        )
    fix_benefits = review._safe_text_lenient(issue.get("fixBenefits"))
    if fix_benefits:
        append_public_reasoning_item(recommendations, f"Expected fix benefit: {fix_benefits}")
    fix_risks = review._safe_text_lenient(issue.get("fixRisks"))
    if fix_risks:
        append_public_reasoning_item(recommendations, f"Fix review risk: {fix_risks}")

    return {
        "facts": facts[:10],
        "inferences": inferences[:8],
        "recommendations": recommendations[:8],
    }

def public_issue_json_value(value: object, *, depth: int = 0) -> object:
    if depth > 3:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return review._safe_text_lenient(value)[:2000]
    if isinstance(value, list):
        cleaned = [public_issue_json_value(item, depth=depth + 1) for item in value[:20]]
        return [item for item in cleaned if item not in (None, "", [], {})]
    if isinstance(value, dict):
        payload: dict[str, object] = {}
        for key, item in list(value.items())[:40]:
            public_key = public_issue_text(key)
            if not public_key:
                continue
            public_value = public_issue_json_value(item, depth=depth + 1)
            if public_value not in (None, "", [], {}):
                payload[public_key] = public_value
        return payload
    return None


def public_issue_audit_metadata(issue: dict, *, job: dict | None = None) -> dict:
    scan = issue_scan(issue)
    metadata = {
        "repo": clean_repository_full_name(issue.get("repo") or (job or {}).get("repo")),
        "branch": issue_branch(issue, job=job),
        "commit": issue_commit(issue, job=job),
        "scanId": public_issue_text(issue.get("scanId") or (job or {}).get("scan_id")),
        "jobId": public_issue_text(issue.get("jobId") or (job or {}).get("job_id")),
    }
    if isinstance(scan, dict):
        result_checksum = public_issue_text(scan.get("resultChecksum"))
        if result_checksum:
            metadata["resultChecksum"] = result_checksum
    return {key: value for key, value in metadata.items() if value}


def issue_payload(issue: dict) -> dict:
    issue_id = public_issue_text(issue.get("id")) or clean_pull_request_issue_id(issue.get("id"))
    fixability = issue_fixability_state(issue)
    auto_fix = fixability["autoFixable"]
    auto_fixable = auto_fix
    affected_locations = public_issue_affected_locations(issue)
    evidence = public_issue_evidence(issue, affected_locations=affected_locations)
    verification_status = public_issue_verification_status(
        issue,
        affected_locations=affected_locations,
        evidence=evidence,
    )
    evidence_checklist = public_issue_evidence_checklist(
        issue,
        affected_locations=affected_locations,
        evidence=evidence,
    )
    confidence_level = public_issue_confidence_level(verification_status, evidence_checklist)
    audit_metadata = public_issue_audit_metadata(issue)
    payload = {
        "id": issue_id,
        "userId": public_issue_text(issue.get("userId")),
        "scanId": public_issue_text(issue.get("scanId")),
        "jobId": public_issue_text(issue.get("jobId")),
        "repo": clean_repository_full_name(issue.get("repo")),
        "branch": audit_metadata.get("branch", "main"),
        "commit": audit_metadata.get("commit", "pending"),
        "status": public_issue_status(issue.get("status")),
        "severity": review._safe_severity(issue.get("severity")),
        "category": review._safe_category(issue.get("category")),
        "title": review._safe_text(issue.get("title"), "Untitled finding"),
        "summary": review._safe_text_lenient(issue.get("summary")) or public_issue_text(issue.get("description")),
        "impact": review._safe_text_lenient(issue.get("impact")),
        "detectionReasoning": review._safe_text_lenient(issue.get("detectionReasoning")),
        "failureScenario": review._safe_text_lenient(issue.get("failureScenario") or issue.get("failure_scenario")),
        "recommendation": review._safe_text_lenient(issue.get("recommendation") or issue.get("remediation") or issue.get("fix")),
        "nextAgentTask": review._safe_text_lenient(issue.get("nextAgentTask") or issue.get("next_agent_task")),
        "disproofAttempt": review._safe_text_lenient(issue.get("disproofAttempt") or issue.get("disproof_attempt")),
        "validationSources": public_issue_json_value(issue.get("validationSources") or issue.get("validation_sources")),
        "reproductionPath": public_issue_text(issue.get("reproductionPath") or issue.get("reproduction_path")),
        "verificationStatus": verification_status,
        "verificationSummary": review._safe_text_lenient(issue.get("verificationSummary")),
        "affectedLocations": affected_locations,
        "evidence": evidence,
        "whyNotFalsePositive": review._safe_text_list(issue.get("whyNotFalsePositive")),
        "limitations": review._safe_text_list(issue.get("limitations")),
        "evidenceChecklist": evidence_checklist,
        "confidenceLevel": confidence_level,
        "evidenceTrace": public_issue_evidence_trace(
            issue,
            affected_locations=affected_locations,
            evidence=evidence,
        ),
        "reasoningBreakdown": public_issue_reasoning_breakdown(
            issue,
            affected_locations=affected_locations,
            evidence=evidence,
        ),
        "audit": audit_metadata,
        "file": public_issue_file(issue.get("file"), issue=issue),
        "line": review._safe_non_negative_int(issue.get("line")),
        "confidence": review._safe_confidence(issue.get("confidence")),
        "confidenceRationale": review._safe_text_lenient(issue.get("confidenceRationale")),
        "autoFix": auto_fix,
        "autoFixable": auto_fixable,
        "fixabilityState": fixability["state"],
        "fixabilityReason": fixability["reason"],
        "effort": review._safe_text(issue.get("effort"), "-"),
        "fixBenefits": review._safe_text_lenient(issue.get("fixBenefits")),
        "fixRisks": review._safe_text_lenient(issue.get("fixRisks")),
        "tags": review._safe_text_list(issue.get("tags")),
        "steps": review._safe_text_list(issue.get("steps")),
        "badCode": review._safe_code_lines(issue.get("badCode")),
        "goodCode": review._safe_code_lines(issue.get("goodCode")),
        "references": review._safe_references(issue.get("references")),
        "createdAt": pull_request_timestamp(issue.get("createdAt")) or 0,
    }
    raw_markdown = review._safe_text_lenient(
        issue.get("rawMarkdown")
        or issue.get("raw_markdown")
        or issue.get("markdown")
        or issue.get("bodyMarkdown")
        or issue.get("body_markdown")
        or issue.get("descriptionMarkdown")
        or issue.get("description_markdown")
    )[:50000]
    if raw_markdown:
        payload["rawMarkdown"] = raw_markdown
    payload["reproduction"] = public_issue_reproduction_payload(issue)
    updated_at = pull_request_timestamp(issue.get("updatedAt"))
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    age = public_issue_text(issue.get("age"))
    if age:
        payload["age"] = age
    reported_verification_status = public_issue_text(issue.get("reportedVerificationStatus")).lower()
    if reported_verification_status in ISSUE_VERIFICATION_STATUSES and reported_verification_status != verification_status:
        payload["reportedVerificationStatus"] = reported_verification_status
    pull_request = issue.get("pullRequest")
    if isinstance(pull_request, dict):
        payload["pullRequest"] = safe_existing_pull_request(
            pull_request,
            issue_id=issue_id,
            fallback_title=pull_request_title(issue, issue_id),
        )
    pending = issue.get("pullRequestPending")
    if isinstance(pending, dict):
        payload["pullRequestPending"] = safe_pending_pull_request(pending, issue_id=issue_id)
    return payload


def public_issue_list_verification_status(issue: dict) -> str:
    status = public_issue_text(issue.get("verificationStatus")).lower()
    if status in ISSUE_VERIFICATION_STATUSES:
        return status
    reported_status = public_issue_text(issue.get("reportedVerificationStatus")).lower()
    if reported_status in ISSUE_VERIFICATION_STATUSES:
        return reported_status
    if public_issue_file(issue.get("file"), issue=issue) and review._safe_non_negative_int(issue.get("line")):
        return "static_proof"
    return "potential_risk"


def public_issue_list_confidence_level(issue: dict, verification_status: str) -> str:
    level = public_issue_text(issue.get("confidenceLevel") or issue.get("confidence_level")).lower()
    if level in {"high", "medium", "low"}:
        return level
    if verification_status == "verified":
        return "high"
    if verification_status == "static_proof":
        return "medium"
    return "low"


def issue_list_payload(issue: dict) -> dict:
    issue_id = public_issue_text(issue.get("id")) or clean_pull_request_issue_id(issue.get("id"))
    audit_metadata = public_issue_audit_metadata(issue)
    verification_status = public_issue_list_verification_status(issue)
    payload = {
        "id": issue_id,
        "userId": public_issue_text(issue.get("userId")),
        "scanId": public_issue_text(issue.get("scanId")),
        "jobId": public_issue_text(issue.get("jobId")),
        "repo": clean_repository_full_name(issue.get("repo")),
        "branch": audit_metadata.get("branch", "main"),
        "commit": audit_metadata.get("commit", "pending"),
        "status": public_issue_status(issue.get("status")),
        "severity": review._safe_severity(issue.get("severity")),
        "category": review._safe_category(issue.get("category")),
        "title": review._safe_text(issue.get("title"), "Untitled finding"),
        "verificationStatus": verification_status,
        "confidenceLevel": public_issue_list_confidence_level(issue, verification_status),
        "file": public_issue_file(issue.get("file"), issue=issue),
        "line": review._safe_non_negative_int(issue.get("line")),
        "confidence": review._safe_confidence(issue.get("confidence")),
        "confidenceRationale": review._safe_text_lenient(issue.get("confidenceRationale")),
        "effort": review._safe_text(issue.get("effort"), "-"),
        "createdAt": pull_request_timestamp(issue.get("createdAt")) or 0,
    }
    updated_at = pull_request_timestamp(issue.get("updatedAt"))
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    age = public_issue_text(issue.get("age"))
    if age:
        payload["age"] = age
    return payload


def safe_quota_usage_payload(value: object, *, default_scope: str) -> dict:
    usage = value if isinstance(value, dict) else {}
    used = non_negative_int(usage.get("used"))
    reserved = non_negative_int(usage.get("reserved"))
    limit = non_negative_int(usage.get("limit"))
    return {
        "scope": clean_github_access_text(usage.get("scope")) or default_scope,
        "period": clean_github_access_text(usage.get("period")) or current_review_usage_period(),
        "plan": clean_github_access_text(usage.get("plan")) or "free",
        "used": used,
        "reserved": reserved,
        "limit": limit,
        "remaining": max(
            0,
            non_negative_int(usage.get("remaining")) if "remaining" in usage else limit - used - reserved,
        ),
        "resetAt": non_negative_int(usage.get("resetAt")),
        "bucketId": clean_github_access_text(usage.get("bucketId"), allow_int=True),
    }


def issue_auto_fix_contract_ok(issue: dict) -> bool:
    return issue_fixability_state(issue)["autoFixable"]


def issue_fixability_state(issue: dict) -> dict:
    if issue.get("autoFix") is not True and issue.get("autoFixable") is not True:
        return issue_fixability_payload(
            False,
            "missing_patch",
            "No safe deterministic patch was generated for this issue.",
        )
    if not fix_workflow.safe_issue_file(issue.get("file")):
        return issue_fixability_payload(
            False,
            "unsafe_file",
            "Issue file is missing or is not a safe repository-relative path.",
        )
    if not fix_workflow.code_lines(issue.get("badCode")) or not fix_workflow.code_lines(issue.get("goodCode")):
        return issue_fixability_payload(
            False,
            "missing_patch",
            "Auto-fix requires non-empty current and suggested code blocks.",
        )

    scan_id = public_issue_text(issue.get("scanId"))
    if not scan_id:
        return issue_fixability_payload(
            True,
            "ready",
            "A safe deterministic patch is ready to preview.",
        )
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        return issue_fixability_payload(
            True,
            "ready",
            "A safe deterministic patch is ready to preview.",
        )
    repo_path = scan.get("repoPath")
    user_id = public_issue_text(issue.get("userId"))
    if not isinstance(repo_path, str) or not repo_path or not user_id:
        return issue_fixability_payload(
            True,
            "ready",
            "A safe deterministic patch is ready to preview.",
        )
    if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id) or not os.path.exists(repo_path):
        return issue_fixability_payload(
            True,
            "ready",
            "A safe deterministic patch is ready to preview.",
        )

    try:
        preview = fix_workflow.preview_issue_fix(repo_path, issue)
    except (OSError, UnicodeError, ValueError):
        return issue_fixability_payload(
            False,
            "preview_unavailable",
            "The stored patch could not be validated against the scanned checkout.",
        )
    if preview.get("valid") is True:
        return issue_fixability_payload(
            True,
            "ready",
            "A safe deterministic patch is ready to preview.",
        )
    reason = review._safe_text_lenient(preview.get("message"))[:500]
    return issue_fixability_payload(
        False,
        "patch_not_applicable",
        reason or "The stored patch no longer applies to the scanned checkout.",
    )


def issue_fixability_payload(auto_fixable: bool, state: str, reason: str) -> dict:
    return {
        "autoFixable": bool(auto_fixable),
        "state": state,
        "reason": reason,
    }


def public_issue_text(value: object) -> str:
    return review._safe_text(value)


def public_issue_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in ISSUE_STATUSES else "open"


def scan_queue_payload(scan: dict) -> dict | None:
    status = scan.get("status")
    if status not in {"queued", "running"}:
        return None

    limits = {
        "queuedGlobal": max_queued_scans_global(),
    }
    scan_id = public_issue_text(scan.get("id"))
    if scan_id and db.get_scan_job_for_scan(scan_id):
        stats = db.scan_queue_stats(scan_id)
        running_counts = {
            "global": public_scan_count(stats.get("running_global")),
        }
        if status == "running":
            return {
                "position": 0,
                "ahead": 0,
                "reason": "running",
                "message": "Your scan is running now.",
                "limits": limits,
                "running": running_counts,
            }
        position = public_scan_count(stats.get("position"))
        ahead = public_scan_count(stats.get("ahead"))
        if ahead > 0:
            reason = "waiting_for_turn"
            message = f"Queued with {plural(ahead, 'scan')} ahead."
        else:
            reason = "ready"
            message = "Queued and waiting for the next available worker."
        return {
            "position": position,
            "ahead": ahead,
            "reason": reason,
            "message": message,
            "limits": limits,
            "running": running_counts,
        }

    running = [item for item in SCANS if item.get("status") == "running"]
    running_counts = {
        "global": len(running),
    }

    if status == "running":
        return {
            "position": 0,
            "ahead": 0,
            "reason": "running",
            "message": "Your scan is running now.",
            "limits": limits,
            "running": running_counts,
        }

    queued = sorted(
        [item for item in SCANS if item.get("status") == "queued"],
        key=scan_queue_sort_key,
    )
    queue_index = next((index for index, item in enumerate(queued) if item.get("id") == scan.get("id")), -1)
    position = queue_index + 1 if queue_index >= 0 else 0
    ahead = max(0, position - 1)

    if ahead > 0:
        reason = "waiting_for_turn"
        message = f"Queued with {plural(ahead, 'scan')} ahead."
    else:
        reason = "ready"
        message = "Queued and waiting for the next available worker."

    return {
        "position": position,
        "ahead": ahead,
        "reason": reason,
        "message": message,
        "limits": limits,
        "running": running_counts,
    }


def scan_queue_sort_key(scan: dict) -> tuple[int, str]:
    return (
        int(scan.get("queuedAt") or scan.get("createdAt") or 0),
        str(scan.get("id") or ""),
    )


def max_queued_scans_global() -> int:
    return system_config.max_queued_scans_global()


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def user_issues(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [issue for issue in ISSUES if issue.get("userId") == session["userId"]]


def user_memory_issue_for_read(session: dict | None, issue_id: str) -> dict | None:
    if not session:
        return None
    user_id = public_issue_text(session.get("userId"))
    target_issue_id = public_issue_text(issue_id)
    if not user_id or not target_issue_id:
        return None
    return next(
        (
            item
            for item in ISSUES
            if public_issue_text(item.get("userId")) == user_id
            and public_issue_text(item.get("id")) == target_issue_id
        ),
        None,
    )


def issue_with_memory_pr_state(issue: dict | None, memory_issue: dict | None) -> dict | None:
    if not issue or not memory_issue:
        return issue
    merged = issue
    for key in ("pullRequest", "pullRequestPending"):
        if key not in merged and isinstance(memory_issue.get(key), dict):
            if merged is issue:
                merged = dict(issue)
            merged[key] = memory_issue[key]
    return merged


def sync_user_issues_from_memory_to_db(user_id: str) -> None:
    target_user_id = public_issue_text(user_id)
    if not target_user_id:
        return
    base_timestamp = now()
    index = 0
    for issue in ISSUES:
        if public_issue_text(issue.get("userId")) == target_user_id:
            db.upsert_issue(issue, timestamp=base_timestamp - index)
            index += 1


def user_issue_filters(params: dict) -> dict:
    raw_status = public_issue_text(params.get("status")).lower()
    raw_severity = public_issue_text(params.get("severity")).lower()
    raw_sort = public_issue_text(params.get("sort")).lower()
    sort = raw_sort if raw_sort in {"severity", "newest", "file"} else ""
    return {
        "status": public_issue_status(raw_status) if raw_status and raw_status != "all" else "",
        "severity": review._safe_severity(raw_severity) if raw_severity and raw_severity != "all" else "",
        "scan_id": public_issue_text(params.get("scanId")),
        "query": public_issue_text(params.get("q")).lower(),
        "sort": sort,
    }


def user_issues_page_for_read(session: dict | None, params: dict) -> dict:
    if not session:
        return {"items": [], "total": 0, "limit": 50, "offset": 0}
    user_id = public_issue_text(session.get("userId"))
    if db.count_user_issues(user_id) == 0:
        sync_user_issues_from_memory_to_db(user_id)
    limit, offset = pagination_params(params)
    filters = user_issue_filters(params)
    return db.list_user_issues_page(
        user_id,
        status=filters["status"],
        severity=filters["severity"],
        scan_id=filters["scan_id"],
        query=filters["query"],
        sort=filters["sort"],
        limit=limit,
        offset=offset,
    )


def user_issue_for_read(session: dict | None, issue_id: str) -> dict | None:
    if not session:
        return None
    user_id = public_issue_text(session.get("userId"))
    target_issue_id = public_issue_text(issue_id)
    memory_issue = user_memory_issue_for_read(session, target_issue_id)
    issue = db.get_user_issue(user_id, target_issue_id)
    if issue:
        return issue_with_memory_pr_state(issue, memory_issue)
    if db.count_user_issues(user_id) == 0:
        sync_user_issues_from_memory_to_db(user_id)
        issue = db.get_user_issue(user_id, target_issue_id)
        if issue:
            return issue_with_memory_pr_state(issue, memory_issue)
    return memory_issue


ISSUE_STATUS_IDENTITY_FIELDS = ("scanId", "jobId", "repo", "file", "line", "title", "createdAt")


def issue_status_identity_value(field: str, value: object) -> str:
    if field == "line":
        return str(review._safe_non_negative_int(value))
    if field == "createdAt":
        timestamp = pull_request_timestamp(value)
        return str(timestamp) if timestamp is not None else public_issue_text(value)
    return public_issue_text(value)


def issue_status_identity_matches(issue: dict, body: dict) -> bool:
    matched = False
    for field in ISSUE_STATUS_IDENTITY_FIELDS:
        if field not in body:
            continue
        matched = True
        if field == "repo":
            expected = clean_repository_full_name(body.get(field))
            actual = clean_repository_full_name(issue.get(field))
        else:
            expected = issue_status_identity_value(field, body.get(field))
            actual = issue_status_identity_value(field, issue.get(field))
        if actual != expected:
            return False
    return matched


def find_issue_for_status_update(issues: list[dict], issue_id: str, body: dict) -> dict:
    matches = [issue for issue in issues if issue.get("id") == issue_id]
    if not matches:
        raise ResourceNotFound("Issue")
    if len(matches) > 1 and isinstance(body, dict):
        identity_matches = [issue for issue in matches if issue_status_identity_matches(issue, body)]
        if len(identity_matches) == 1:
            return identity_matches[0]
    return matches[0]


def pagination_params(params: dict, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    try:
        limit = int(params.get("limit") or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(max_limit, limit)), max(0, offset)


def paginated_response(items: list[dict], *, keys: tuple[str, ...], params: dict) -> dict:
    limit, offset = pagination_params(params)
    total = len(items)
    page = items[offset : offset + limit]
    return paginated_page_response(page, total=total, limit=limit, offset=offset, keys=keys)


def paginated_page_response(
    page: list[dict],
    *,
    total: int,
    limit: int,
    offset: int,
    keys: tuple[str, ...],
) -> dict:
    next_offset = offset + len(page)
    payload = {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": next_offset < total,
        "nextOffset": next_offset if next_offset < total else None,
    }
    for key in keys:
        payload[key] = page
    return payload


def filter_user_scan_records(scans: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    status = public_scan_status(raw_status) if raw_status and raw_status != "all" else ""
    repo = clean_repository_full_name(params.get("repo"))
    if status:
        scans = [scan for scan in scans if public_scan_status(scan.get("status")) == status]
    if repo:
        scans = [scan for scan in scans if clean_repository_full_name(scan.get("repo")) == repo]
    return sorted(scans, key=lambda scan: (pull_request_timestamp(scan.get("createdAt")) or 0, public_issue_text(scan.get("id"))), reverse=True)


def filter_user_scan_payloads(scans: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    status = public_scan_status(raw_status) if raw_status and raw_status != "all" else ""
    repo = clean_repository_full_name(params.get("repo"))
    if status:
        scans = [scan for scan in scans if scan.get("status") == status]
    if repo:
        scans = [scan for scan in scans if scan.get("repo") == repo]
    return sorted(scans, key=lambda scan: (pull_request_timestamp(scan.get("createdAt")) or 0, public_issue_text(scan.get("id"))), reverse=True)


def filter_user_issue_records(issues: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    raw_severity = public_issue_text(params.get("severity")).lower()
    status = public_issue_status(raw_status) if raw_status and raw_status != "all" else ""
    severity = review._safe_severity(raw_severity) if raw_severity and raw_severity != "all" else ""
    scan_id = public_issue_text(params.get("scanId"))
    query = public_issue_text(params.get("q")).lower()
    if status:
        issues = [issue for issue in issues if public_issue_status(issue.get("status")) == status]
    if severity:
        issues = [issue for issue in issues if review._safe_severity(issue.get("severity")) == severity]
    if scan_id:
        issues = [issue for issue in issues if public_issue_text(issue.get("scanId")) == scan_id]
    if query:
        issues = [
            issue
            for issue in issues
            if any(
                query in public_issue_text(value).lower()
                for value in (issue.get("title"), issue.get("file"), issue.get("repo"), issue.get("category"), issue.get("id"))
            )
        ]
    return issues


def filter_user_issue_payloads(issues: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    raw_severity = public_issue_text(params.get("severity")).lower()
    status = public_issue_status(raw_status) if raw_status and raw_status != "all" else ""
    severity = review._safe_severity(raw_severity) if raw_severity and raw_severity != "all" else ""
    scan_id = public_issue_text(params.get("scanId"))
    query = public_issue_text(params.get("q")).lower()
    if status:
        issues = [issue for issue in issues if issue.get("status") == status]
    if severity:
        issues = [issue for issue in issues if issue.get("severity") == severity]
    if scan_id:
        issues = [issue for issue in issues if issue.get("scanId") == scan_id]
    if query:
        issues = [
            issue
            for issue in issues
            if any(
                query in public_issue_text(value).lower()
                for value in (issue.get("title"), issue.get("file"), issue.get("repo"), issue.get("category"), issue.get("id"))
            )
        ]
    return issues
