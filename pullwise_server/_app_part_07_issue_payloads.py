from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

def scan_system_status_payload(*, admin: bool = False) -> dict:
    worker_records = db.list_workers()
    workers = [worker_public_payload(worker, admin=False) for worker in worker_records]
    queued_jobs = len([scan for scan in SCANS if scan.get("status") == "queued"])
    running_jobs = len([scan for scan in SCANS if scan.get("status") == "running"])
    online = [worker for worker in workers if worker["status"] in {"idle", "busy"}]
    degraded = [worker for worker in workers if worker["status"] == "degraded"]
    offline = [worker for worker in workers if worker["status"] == "offline"]
    total_capacity = sum(public_scan_count(worker.get("max_concurrent_jobs")) for worker in online + degraded)
    available_capacity = sum(public_scan_count(worker.get("free_slots")) for worker in online + degraded)
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
        "totalCapacity": total_capacity,
        "availableCapacity": available_capacity,
        "runningJobs": running_jobs,
        "queuedJobs": queued_jobs,
        "degradedWorkerCount": len(degraded),
        "offlineWorkerCount": len(offline),
    }
    if admin:
        payload["workers"] = [worker_public_payload(worker, admin=True) for worker in worker_records]
    return payload


def clean_scan_error(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.replace("\x00", "").splitlines()
    return (lines[0] if lines else "").strip()[:500]


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
        file_path = public_issue_file(item.get("file"), issue=issue, job=job)
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


def public_issue_reproduction(issue: dict, *, job: dict | None = None) -> dict:
    source = issue.get("reproduction") if isinstance(issue.get("reproduction"), dict) else {}
    test_file = public_issue_file(source.get("testFile") or source.get("test_file"), issue=issue, job=job)
    return {
        "commands": review._safe_text_list(source.get("commands")),
        "input": review._safe_text_lenient(source.get("input")),
        "expected": review._safe_text_lenient(source.get("expected")),
        "actual": review._safe_text_lenient(source.get("actual")),
        "testFile": test_file,
        "logPath": public_issue_text(source.get("logPath") or source.get("log_path")),
    }


def public_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        candidate = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return candidate


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
        if not isinstance(item, dict):
            continue
        evidence_type = public_issue_text(item.get("type")).lower()
        if evidence_type not in ISSUE_EVIDENCE_TYPES:
            evidence_type = "code"
        label = public_issue_text(item.get("label")) or evidence_type.replace("_", " ").title()
        summary = review._safe_text_lenient(item.get("summary"))
        file_path = public_issue_file(item.get("file"), issue=issue, job=job)
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
    reproduction: dict | None = None,
) -> str:
    status = public_issue_text(issue.get("verificationStatus")).lower()
    if status not in ISSUE_VERIFICATION_STATUSES:
        status = ""
    has_fixed_commit = bool(GIT_COMMIT_SHA_RE.fullmatch(issue_commit(issue) or ""))
    affected_locations = affected_locations or public_issue_affected_locations(issue)
    has_precise_location = any(location.get("file") and location.get("startLine") for location in affected_locations)
    evidence = evidence or public_issue_evidence(issue, affected_locations=affected_locations)
    reproduction = reproduction or public_issue_reproduction(issue)
    has_reproduction_command = bool(reproduction.get("commands"))
    has_reproduction_output = has_reproduction_command and any(
        [reproduction.get("actual"), reproduction.get("logPath"), reproduction.get("testFile")]
    )
    has_runtime_evidence = has_reproduction_output or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and any([item.get("command"), item.get("logPath"), item.get("file"), item.get("exitCode") is not None])
        for item in evidence
    )
    has_raw_runtime_output = has_reproduction_output or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and bool(item.get("logPath"))
        for item in evidence
    )
    has_static_evidence = bool(affected_locations) or any(
        item.get("type") in {"code", "path", "trigger", "documentation", "tool"}
        and any([item.get("file"), item.get("summary"), item.get("command")])
        for item in evidence
    )
    verified_ready = (
        has_fixed_commit
        and has_precise_location
        and has_reproduction_command
        and has_runtime_evidence
        and has_raw_runtime_output
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
    reproduction: dict,
) -> list[dict]:
    commit = issue_commit(issue)
    has_runtime = bool(reproduction.get("commands") and reproduction.get("actual")) or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and bool(item.get("logPath"))
        for item in evidence
    )
    return [
        {"label": "Fixed commit", "met": bool(GIT_COMMIT_SHA_RE.fullmatch(commit or ""))},
        {
            "label": "Precise file and line",
            "met": any(location.get("file") and location.get("startLine") for location in affected_locations),
        },
        {"label": "Evidence chain", "met": bool(evidence)},
        {"label": "Reproduction command", "met": bool(reproduction.get("commands"))},
        {"label": "Runtime output", "met": has_runtime},
        {
            "label": "Raw log or test",
            "met": bool(reproduction.get("logPath") or reproduction.get("testFile"))
            or any(item.get("logPath") for item in evidence),
        },
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
    reproduction: dict,
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

    for item in review._safe_text_list(issue.get("whyNotFalsePositive"))[:4]:
        append_public_reasoning_item(path_items, f"Reachability check: {item}")

    reproduction_path = review._safe_text_lenient(issue.get("reproductionPath"))
    append_public_reasoning_item(trigger_items, reproduction_path)
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if commands:
        append_public_reasoning_item(trigger_items, f"Command: {public_issue_text(commands[0])}")
    if reproduction.get("input"):
        append_public_reasoning_item(trigger_items, f"Input: {review._safe_text_lenient(reproduction.get('input'))}")

    if reproduction.get("actual"):
        append_public_reasoning_item(runtime_items, f"Observed result: {review._safe_text_lenient(reproduction.get('actual'))}")
    if reproduction.get("testFile"):
        test_file = public_issue_file(reproduction.get("testFile"), issue=issue)
        if test_file:
            append_public_reasoning_item(runtime_items, f"Test file: {test_file}")
    if reproduction.get("logPath"):
        append_public_reasoning_item(runtime_items, f"Log path: {public_issue_text(reproduction.get('logPath'))}")

    impact = review._safe_text_lenient(issue.get("impact"))
    if impact:
        append_public_reasoning_item(impact_items, f"Impact: {impact}")
    else:
        summary = review._safe_text_lenient(issue.get("summary"))
        if summary:
            append_public_reasoning_item(impact_items, f"Reported behavior: {summary}")

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
    reproduction: dict,
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

    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if commands:
        append_public_reasoning_item(facts, f"Reproduction command captured: {public_issue_text(commands[0])}.")
    for key, label in (("input", "Reproduction input"), ("expected", "Expected result"), ("actual", "Observed result")):
        value = review._safe_text_lenient(reproduction.get(key))
        if value:
            append_public_reasoning_item(facts, f"{label}: {value}")
    test_file = public_issue_file(reproduction.get("testFile"), issue=issue)
    if test_file:
        append_public_reasoning_item(facts, f"Reproduction test file: {test_file}.")
    if reproduction.get("logPath"):
        append_public_reasoning_item(facts, f"Reproduction log path: {public_issue_text(reproduction.get('logPath'))}.")

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

    for step in review._safe_text_list(issue.get("steps"))[:6]:
        append_public_reasoning_item(recommendations, step)
    if review._safe_code_lines(issue.get("badCode")) or review._safe_code_lines(issue.get("goodCode")):
        append_public_reasoning_item(
            recommendations,
            "Inspect the suggested patch evidence and validate it before applying changes.",
        )
    if commands:
        append_public_reasoning_item(
            recommendations,
            "After a fix, rerun the captured reproduction command and compare the expected and observed results.",
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


def public_issue_audit_swarm(issue: dict) -> dict:
    source = issue.get("auditSwarm") if isinstance(issue.get("auditSwarm"), dict) else {}
    payload = {
        "protocol": public_issue_text(source.get("protocol")),
        "shardId": public_issue_text(source.get("shardId") or source.get("shard_id")),
        "agentRole": public_issue_text(source.get("agentRole") or source.get("agent_role")),
        "verdict": public_issue_text(source.get("verdict")).lower(),
    }
    if payload["verdict"] not in {"confirmed", "rejected", "inconclusive", "candidate"}:
        payload["verdict"] = ""
    return {key: value for key, value in payload.items() if value}


def public_issue_review_calibration(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    decision = public_issue_text(source.get("decision")).lower()
    if decision not in {"reported", "audit_only", "rejected"}:
        return {}
    score_band = public_issue_text(source.get("scoreBand") or source.get("score_band")).lower()
    if score_band not in {"report_band", "audit_band", "reject_band"}:
        score_band = ""
    score_kind = public_issue_text(source.get("scoreKind") or source.get("score_kind")).lower()
    if score_kind not in {"ranking_score", "truth_probability"}:
        score_kind = ""
    verification_status = public_issue_text(
        source.get("verificationStatus") or source.get("verification_status")
    ).lower()
    if verification_status not in ISSUE_VERIFICATION_STATUSES:
        verification_status = ""
    payload = {
        "protocol": "pullwise-review-calibration-public/0.1",
        "decision": decision,
        "reason": public_issue_text(source.get("reason"))[:120],
        "scoreBand": score_band,
        "scoreKind": score_kind,
        "verificationStatus": verification_status,
        "auditOnly": source.get("auditOnly") is True or source.get("audit_only") is True,
        "guardrailApplied": source.get("guardrailApplied") is True or source.get("guardrail_applied") is True,
    }
    return {key: item for key, item in payload.items() if item not in ("", [], {})}


def public_issue_feedback_reason(value: object) -> str:
    reason = public_issue_text(value).lower().replace("-", "_").replace(" ", "_")
    if reason == "valid":
        return "useful"
    if reason == "speculative":
        return "too_speculative"
    return reason if reason in REVIEW_USER_FEEDBACK_REASONS else ""


def public_issue_feedback_reason_from_label(label: dict) -> str:
    if public_issue_text(label.get("label_source")).lower() != "user_explicit":
        return ""
    reason = " ".join(review._safe_text_lenient(label.get("label_reason")).split())
    prefix = "feedback:"
    if not reason.lower().startswith(prefix):
        return ""
    value = reason[len(prefix) :].split(" ", 1)[0]
    return public_issue_feedback_reason(value)


def public_issue_feedback(issue: dict) -> dict:
    fallback_reason = public_issue_feedback_reason(issue.get("feedbackReason") or issue.get("feedback_reason"))
    event = review_decision_event_for_issue(issue)
    observation_key = public_issue_text(event.get("candidate_observation_key"))
    if observation_key:
        user_id = public_issue_text(issue.get("userId"))
        for label in db.list_review_outcome_labels(observation_key):
            if user_id and public_issue_text(label.get("created_by")) not in {"", user_id}:
                continue
            feedback_reason = public_issue_feedback_reason_from_label(label)
            if feedback_reason:
                return {
                    "reason": feedback_reason,
                    "label": review_outcome_label_payload(label),
                }
    return {"reason": fallback_reason} if fallback_reason else {}


def issue_payload(issue: dict) -> dict:
    issue_id = public_issue_text(issue.get("id")) or clean_pull_request_issue_id(issue.get("id"))
    auto_fix = issue_auto_fix_contract_ok(issue)
    auto_fixable = auto_fix
    affected_locations = public_issue_affected_locations(issue)
    evidence = public_issue_evidence(issue, affected_locations=affected_locations)
    reproduction = public_issue_reproduction(issue)
    verification_status = public_issue_verification_status(
        issue,
        affected_locations=affected_locations,
        evidence=evidence,
        reproduction=reproduction,
    )
    evidence_checklist = public_issue_evidence_checklist(
        issue,
        affected_locations=affected_locations,
        evidence=evidence,
        reproduction=reproduction,
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
        "reproductionPath": review._safe_text_lenient(issue.get("reproductionPath")),
        "verificationStatus": verification_status,
        "verificationSummary": review._safe_text_lenient(issue.get("verificationSummary")),
        "affectedLocations": affected_locations,
        "evidence": evidence,
        "reproduction": reproduction,
        "whyNotFalsePositive": review._safe_text_list(issue.get("whyNotFalsePositive")),
        "limitations": review._safe_text_list(issue.get("limitations")),
        "evidenceChecklist": evidence_checklist,
        "confidenceLevel": confidence_level,
        "evidenceTrace": public_issue_evidence_trace(
            issue,
            affected_locations=affected_locations,
            evidence=evidence,
            reproduction=reproduction,
        ),
        "reasoningBreakdown": public_issue_reasoning_breakdown(
            issue,
            affected_locations=affected_locations,
            evidence=evidence,
            reproduction=reproduction,
        ),
        "audit": audit_metadata,
        "file": public_issue_file(issue.get("file"), issue=issue),
        "line": review._safe_non_negative_int(issue.get("line")),
        "confidence": review._safe_confidence(issue.get("confidence")),
        "confidenceRationale": review._safe_text_lenient(issue.get("confidenceRationale")),
        "autoFix": auto_fix,
        "autoFixable": auto_fixable,
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
    updated_at = pull_request_timestamp(issue.get("updatedAt"))
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    age = public_issue_text(issue.get("age"))
    if age:
        payload["age"] = age
    audit_swarm = public_issue_audit_swarm(issue)
    if audit_swarm:
        payload["auditSwarm"] = audit_swarm
    review_calibration = public_issue_review_calibration(
        issue.get("reviewCalibration") or issue.get("review_calibration")
    )
    if review_calibration:
        payload["reviewCalibration"] = review_calibration
    feedback = public_issue_feedback(issue)
    if feedback:
        payload["feedbackReason"] = feedback["reason"]
        if feedback.get("label"):
            payload["feedbackLabel"] = feedback["label"]
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


def safe_quota_usage_payload(value: object, *, default_scope: str) -> dict:
    usage = value if isinstance(value, dict) else {}
    used = non_negative_int(usage.get("used"))
    limit = non_negative_int(usage.get("limit"))
    return {
        "scope": clean_github_access_text(usage.get("scope")) or default_scope,
        "period": clean_github_access_text(usage.get("period")) or current_review_usage_period(),
        "plan": clean_github_access_text(usage.get("plan")) or "free",
        "used": used,
        "limit": limit,
        "remaining": max(0, non_negative_int(usage.get("remaining")) if "remaining" in usage else limit - used),
        "resetAt": non_negative_int(usage.get("resetAt")),
        "bucketId": clean_github_access_text(usage.get("bucketId"), allow_int=True),
    }


def issue_auto_fix_contract_ok(issue: dict) -> bool:
    if issue.get("autoFix") is not True and issue.get("autoFixable") is not True:
        return False
    if not fix_workflow.safe_issue_file(issue.get("file")):
        return False
    if not fix_workflow.code_lines(issue.get("badCode")) or not fix_workflow.code_lines(issue.get("goodCode")):
        return False

    scan_id = public_issue_text(issue.get("scanId"))
    if not scan_id:
        return True
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        return True
    repo_path = scan.get("repoPath")
    user_id = public_issue_text(issue.get("userId"))
    if not isinstance(repo_path, str) or not repo_path or not user_id:
        return True
    if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id) or not os.path.exists(repo_path):
        return True

    try:
        return fix_workflow.preview_issue_fix(repo_path, issue).get("valid") is True
    except (OSError, UnicodeError, ValueError):
        return False


def public_issue_text(value: object) -> str:
    return review._safe_text(value)


def public_issue_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in ISSUE_STATUSES else "open"


def scan_queue_payload(scan: dict) -> dict | None:
    status = scan.get("status")
    if status not in {"queued", "running"}:
        return None

    user_id = str(scan.get("userId") or "")
    limits = {
        "perUser": max_scan_concurrency_per_user(),
        "queuedGlobal": max_queued_scans_global(),
        "queuedPerUser": max_queued_scans_per_user(),
    }
    running = [item for item in SCANS if item.get("status") == "running"]
    running_for_user = [item for item in running if str(item.get("userId") or "") == user_id]
    running_counts = {
        "global": len(running),
        "user": len(running_for_user),
    }

    if status == "running":
        return {
            "position": 0,
            "ahead": 0,
            "userPosition": 0,
            "userAhead": 0,
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

    user_queued = [item for item in queued if str(item.get("userId") or "") == user_id]
    user_index = next((index for index, item in enumerate(user_queued) if item.get("id") == scan.get("id")), -1)
    user_position = user_index + 1 if user_index >= 0 else 0
    user_ahead = max(0, user_position - 1)

    if running_counts["user"] >= limits["perUser"]:
        reason = "user_limit"
        message = (
            f"You already have {plural(running_counts['user'], 'scan')} running; "
            "this scan is queued and will start when one finishes."
        )
    elif ahead > 0:
        reason = "waiting_for_turn"
        message = f"Queued with {plural(ahead, 'scan')} ahead."
    else:
        reason = "ready"
        message = "Queued and waiting for the next available worker."

    return {
        "position": position,
        "ahead": ahead,
        "userPosition": user_position,
        "userAhead": user_ahead,
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


def max_scan_concurrency_per_user() -> int:
    return system_config.max_running_scans_per_user()


def max_queued_scans_global() -> int:
    return system_config.max_queued_scans_global()


def max_queued_scans_per_user() -> int:
    return system_config.max_queued_scans_per_user()


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def user_issues(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [issue for issue in ISSUES if issue.get("userId") == session["userId"]]


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


def filter_user_scan_payloads(scans: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    status = public_scan_status(raw_status) if raw_status and raw_status != "all" else ""
    repo = clean_repository_full_name(params.get("repo"))
    if status:
        scans = [scan for scan in scans if scan.get("status") == status]
    if repo:
        scans = [scan for scan in scans if scan.get("repo") == repo]
    return sorted(scans, key=lambda scan: (pull_request_timestamp(scan.get("createdAt")) or 0, public_issue_text(scan.get("id"))), reverse=True)


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
