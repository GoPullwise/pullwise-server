from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

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
    ai_usage = public_scan_ai_usage(scan.get("aiUsage") or scan.get("ai_usage"))
    if ai_usage:
        payload["aiUsage"] = ai_usage
    verification_audit = public_scan_verification_audit(scan)
    if public_scan_verification_audit_has_data(verification_audit):
        payload["verificationAudit"] = verification_audit
    preflight = public_scan_preflight(scan.get("preflight"))
    if preflight:
        payload["preflight"] = preflight
    audit_swarm = public_scan_audit_swarm(scan.get("auditSwarm") or scan.get("audit_swarm"))
    if audit_swarm:
        payload["auditSwarm"] = audit_swarm
    for key in ("queuedAt", "startedAt", "completedAt", "updatedAt", "recoveredAt"):
        if key in scan:
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    if "error" in scan:
        payload["error"] = clean_scan_error(scan.get("error"))
    if "time" in scan:
        payload["time"] = public_issue_text(scan.get("time"))
    if "by" in scan:
        payload["by"] = public_issue_text(scan.get("by"))
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
    return payload


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


def worker_max_concurrency_cap() -> int:
    return max(1, env_int("PULLWISE_WORKER_MAX_CONCURRENCY_CAP", 32))


def worker_admin_capacity(value: object) -> int:
    capacity = public_scan_count(value) or 1
    cap = worker_max_concurrency_cap()
    if capacity > cap:
        raise ValueError(f"max_concurrent_jobs cannot exceed {cap}.")
    return capacity


def worker_heartbeat_capacity(value: object) -> int:
    return min(public_scan_count(value) or 1, worker_max_concurrency_cap())


def public_scan_issue_counts(value: object) -> dict:
    counts = value if isinstance(value, dict) else {}
    return {
        "critical": public_scan_count(counts.get("critical")),
        "high": public_scan_count(counts.get("high")),
        "medium": public_scan_count(counts.get("medium")),
        "low": public_scan_count(counts.get("low")),
        "info": public_scan_count(counts.get("info")),
    }


def public_scan_ai_usage(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    model = clean_github_access_text(source.get("model") or source.get("modelName") or source.get("model_name"))
    return {"model": model} if model else {}


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


def public_convergence_finding_record(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    fingerprint = clean_github_access_text(source.get("fingerprint"))
    if not fingerprint:
        return {}
    status = public_issue_text(source.get("status")).lower()
    if status not in {"open", "resolved"}:
        status = "open"
    record = {
        "fingerprint": fingerprint,
        "status": status,
    }
    issue_id = public_issue_text(source.get("issue_id") or source.get("issueId"))
    if issue_id:
        record["issue_id"] = issue_id
    title = review._safe_text_lenient(source.get("title"))[:180]
    if title:
        record["title"] = " ".join(title.split())
    file_path = public_issue_file(source.get("file"))
    if file_path:
        record["file"] = file_path
    line = public_scan_count(source.get("line"))
    if line:
        record["line"] = line
    confidence = public_confidence(source.get("confidence"))
    if confidence:
        record["confidence"] = confidence
    source_name = public_issue_text(source.get("source"))[:80]
    if source_name:
        record["source"] = source_name
    return record


def public_convergence_source_stats(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    stats = {}
    for raw_source, raw_counts in value.items():
        source = public_issue_text(raw_source)[:80]
        if not source or not isinstance(raw_counts, dict):
            continue
        stats[source] = {
            "reported": public_scan_count(raw_counts.get("reported")),
            "confirmed": public_scan_count(raw_counts.get("confirmed")),
            "resolved": public_scan_count(raw_counts.get("resolved")),
            "rejected": public_scan_count(raw_counts.get("rejected")),
        }
        if len(stats) >= 50:
            break
    return stats


def public_scan_convergence_state(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    head_sha = (clean_github_access_text(source.get("head_sha") or source.get("headSha")) or "").lower()
    if head_sha and not GIT_COMMIT_SHA_RE.fullmatch(head_sha):
        head_sha = ""
    open_findings = []
    raw_open_findings = source.get("open_findings") or source.get("openFindings")
    if isinstance(raw_open_findings, list):
        for item in raw_open_findings:
            record = public_convergence_finding_record(item)
            if record and record.get("status") == "open":
                open_findings.append(record)
            if len(open_findings) >= 100:
                break
    resolved_fingerprints = []
    raw_resolved = source.get("resolved_fingerprints") or source.get("resolvedFingerprints")
    if isinstance(raw_resolved, list):
        for item in raw_resolved:
            fingerprint = clean_github_access_text(item)
            if fingerprint:
                resolved_fingerprints.append(fingerprint)
            if len(resolved_fingerprints) >= 200:
                break
    state = {
        "protocol": CONVERGENCE_PROTOCOL_VERSION,
        "scopeKey": public_issue_text(source.get("scope_key") or source.get("scopeKey"))[:240],
        "headSha": head_sha,
        "openFindings": open_findings,
        "resolvedFingerprints": resolved_fingerprints,
        "sourceStats": public_convergence_source_stats(source.get("source_stats") or source.get("sourceStats")),
    }
    return state if state["headSha"] or state["openFindings"] or state["resolvedFingerprints"] else {}


def convergence_scope_key(repo: object, branch: object) -> str:
    repo_name = clean_repository_full_name(repo)
    branch_name = clean_github_access_text(branch) or "main"
    if not repo_name:
        return ""
    return f"repo:{repo_name.lower()}|branch:{branch_name.lower()}"


def convergence_state_for_scan(scan: dict) -> dict:
    state = public_scan_convergence_state(scan.get("convergenceState") or scan.get("convergence_state"))
    if not state:
        return {}
    expected_scope = convergence_scope_key(scan.get("repo"), scan.get("branch"))
    state_scope = public_issue_text(state.get("scopeKey"))
    if expected_scope and state_scope and state_scope.lower() != expected_scope:
        return {}
    if expected_scope and not state_scope:
        state = {**state, "scopeKey": expected_scope}
    return state


def convergence_state_from_worker_result(job: dict, body: dict) -> dict:
    state = public_scan_convergence_state(body.get("convergence_state") or body.get("convergenceState"))
    if not state:
        return {}
    expected_scope = convergence_scope_key(job.get("repo"), job.get("branch"))
    state_scope = public_issue_text(state.get("scopeKey"))
    if expected_scope and state_scope and state_scope.lower() != expected_scope:
        return {}
    if expected_scope and not state_scope:
        state = {**state, "scopeKey": expected_scope}
    return state


def worker_convergence_context_for_job(job: dict) -> dict:
    repo = clean_repository_full_name(job.get("repo"))
    branch = clean_github_access_text(job.get("branch")) or "main"
    scope_key = convergence_scope_key(repo, branch)
    user_id = public_issue_text(job.get("user_id"))
    scan_id = public_issue_text(job.get("scan_id"))
    if not scope_key:
        return {}
    candidates = []
    for scan in SCANS:
        if public_issue_text(scan.get("id")) == scan_id:
            continue
        if public_scan_status(scan.get("status")) != "done":
            continue
        if convergence_scope_key(scan.get("repo"), scan.get("branch")) != scope_key:
            continue
        scan_user_id = public_issue_text(scan.get("userId"))
        if user_id and scan_user_id and scan_user_id != user_id:
            continue
        rank = pull_request_timestamp(scan.get("completedAt")) or pull_request_timestamp(scan.get("createdAt")) or 0
        candidates.append((rank, scan))
    if not candidates:
        return {}
    _rank, latest_scan = sorted(candidates, key=lambda item: item[0])[-1]
    state = convergence_state_for_scan(latest_scan)
    if not state:
        return {}
    return {
        "protocol": CONVERGENCE_PROTOCOL_VERSION,
        "scope_key": state.get("scopeKey") or "",
        "previous_head_sha": state.get("headSha") or "",
        "open_findings": state.get("openFindings") or [],
        "source_stats": state.get("sourceStats") or {},
    }


def first_present(source: dict, *keys: str) -> object:
    for key in keys:
        if key in source:
            return source.get(key)
    return None


def public_scan_verification_counts(scan: dict) -> dict:
    scan_id = public_issue_text(scan.get("id")) if isinstance(scan, dict) else ""
    scan_user_id = public_issue_text(scan.get("userId")) if isinstance(scan, dict) else ""
    counts = {"verified": 0, "static_proof": 0, "potential_risk": 0, "unverified": 0}
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


def public_scan_verification_audit_input(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    rejected_reasons = []
    raw_reasons = source.get("rejectedReasons") if isinstance(source.get("rejectedReasons"), list) else []
    for item in raw_reasons:
        if not isinstance(item, dict):
            continue
        reason = public_issue_text(item.get("reason"))
        count = public_scan_count(item.get("count"))
        if reason and count:
            rejected_reasons.append({"reason": reason, "count": count})
    rejected_samples = []
    raw_samples = source.get("rejectedSamples") if isinstance(source.get("rejectedSamples"), list) else []
    for item in raw_samples:
        if not isinstance(item, dict):
            continue
        reason = public_issue_text(item.get("reason"))
        if not reason:
            continue
        sample = {"reason": reason}
        title = review._safe_text_lenient(item.get("title"))[:160]
        if title:
            sample["title"] = " ".join(title.split())
        if public_issue_text(item.get("severity")):
            sample["severity"] = review._safe_severity(item.get("severity"))
        if public_issue_text(item.get("category")):
            sample["category"] = review._safe_category(item.get("category"))
        file_path = public_issue_file(item.get("file"))
        if file_path:
            sample["file"] = file_path
        line = review._safe_non_negative_int(item.get("line"))
        if line:
            sample["line"] = line
        status = public_issue_text(item.get("verificationStatus")).lower()
        if status in ISSUE_VERIFICATION_STATUSES:
            sample["verificationStatus"] = status
        rejected_samples.append(sample)
    payload = {
        "candidateCount": public_scan_count(source.get("candidateCount") or source.get("candidate_count")),
        "reportedCount": public_scan_count(source.get("reportedCount") or source.get("reported_count")),
        "rejectedCount": public_scan_count(source.get("rejectedCount") or source.get("rejected_count")),
        "downgradedCount": public_scan_count(source.get("downgradedCount") or source.get("downgraded_count")),
        "verifiedCount": public_scan_count(source.get("verifiedCount") or source.get("verified_count")),
        "staticProofCount": public_scan_count(source.get("staticProofCount") or source.get("static_proof_count")),
        "potentialRiskCount": public_scan_count(source.get("potentialRiskCount") or source.get("potential_risk_count")),
        "unverifiedCount": public_scan_count(source.get("unverifiedCount") or source.get("unverified_count")),
        "summary": " ".join(review._safe_text_lenient(source.get("summary")).split()),
        "rejectedReasons": rejected_reasons[:10],
        "rejectedSamples": rejected_samples[:5],
    }
    reason_total = sum(item["count"] for item in payload["rejectedReasons"])
    payload["rejectedCount"] = max(payload["rejectedCount"], reason_total)
    return payload


def public_scan_verification_audit(scan: dict) -> dict:
    if not isinstance(scan, dict):
        scan = {}
    base = public_scan_verification_audit_input(scan.get("verificationAudit") or scan.get("verification_audit"))
    counts = public_scan_verification_counts(scan)
    reported_count = sum(counts.values())
    scan_id = public_issue_text(scan.get("id"))
    scan_user_id = public_issue_text(scan.get("userId"))
    downgraded_count = 0
    if scan_id:
        for issue in ISSUES:
            if public_issue_text(issue.get("scanId")) != scan_id:
                continue
            issue_user_id = public_issue_text(issue.get("userId"))
            if scan_user_id and issue_user_id and issue_user_id != scan_user_id:
                continue
            reported_status = public_issue_text(issue.get("reportedVerificationStatus")).lower()
            final_status = public_issue_verification_status(issue)
            if reported_status in ISSUE_VERIFICATION_STATUSES and reported_status != final_status:
                downgraded_count += 1
    rejected_count = base["rejectedCount"]
    candidate_count = max(base["candidateCount"], reported_count + rejected_count)
    final_downgraded_count = max(base["downgradedCount"], downgraded_count)
    summary = base["summary"] or f"{candidate_count} candidates evaluated; {reported_count} reported."
    if rejected_count and "rejected" not in summary.lower():
        summary = f"{summary.rstrip('.')}; {rejected_count} rejected before reporting."
    if final_downgraded_count and "downgrad" not in summary.lower():
        summary = f"{summary.rstrip('.')}; {final_downgraded_count} downgraded by evidence gates."
    return {
        "candidateCount": candidate_count,
        "reportedCount": reported_count,
        "rejectedCount": rejected_count,
        "downgradedCount": final_downgraded_count,
        "verifiedCount": counts["verified"],
        "staticProofCount": counts["static_proof"],
        "potentialRiskCount": counts["potential_risk"],
        "unverifiedCount": counts["unverified"],
        "rejectedReasons": base["rejectedReasons"],
        "rejectedSamples": base["rejectedSamples"],
        "summary": summary[:500],
    }


def public_scan_verification_audit_has_data(value: object) -> bool:
    audit = value if isinstance(value, dict) else {}
    return any(
        public_scan_count(audit.get(key))
        for key in (
            "candidateCount",
            "reportedCount",
            "rejectedCount",
            "downgradedCount",
            "verifiedCount",
            "staticProofCount",
            "potentialRiskCount",
            "unverifiedCount",
        )
    ) or bool(audit.get("rejectedReasons")) or bool(audit.get("rejectedSamples"))


def public_scan_audit_swarm_from_worker_body(body: dict, *, status: str = "") -> dict:
    source = body if isinstance(body, dict) else {}
    payload = public_scan_audit_swarm(source.get("audit_swarm") or source.get("auditSwarm"))
    issue_cards = source.get("issue_cards") if isinstance(source.get("issue_cards"), list) else []
    verification_results = (
        source.get("verification_results") if isinstance(source.get("verification_results"), list) else []
    )
    verification_audit = public_scan_verification_audit_input(
        source.get("verification_audit") or source.get("verificationAudit")
    )
    raw_payload = public_scan_audit_swarm(
        {
            "protocol": source.get("audit_protocol") or source.get("auditProtocol"),
            "stage": "report" if status == "done" else status,
            "summary": verification_audit.get("summary"),
            "counts": verification_audit,
            "issueCards": issue_cards,
            "verificationResults": verification_results,
            "evidenceBlocks": source.get("evidence_blocks") or source.get("evidenceBlocks"),
        }
    )
    if not payload:
        return raw_payload
    if not raw_payload:
        return payload
    merged = dict(payload)
    for key in ("issueCards", "verificationResults", "evidenceBlocks", "roles", "shards"):
        if raw_payload.get(key):
            if key == "evidenceBlocks" and merged.get(key):
                continue
            merged[key] = raw_payload[key]
    counts = dict(payload.get("counts") if isinstance(payload.get("counts"), dict) else {})
    for source_counts in (raw_payload.get("counts"), verification_audit):
        if not isinstance(source_counts, dict):
            continue
        for key, value in source_counts.items():
            count = public_scan_count(value)
            if count:
                counts[key] = max(public_scan_count(counts.get(key)), count)
    if counts:
        merged["counts"] = {key: value for key, value in counts.items() if public_scan_count(value)}
    return {key: value for key, value in merged.items() if value not in ("", [], {})}


def public_scan_audit_swarm(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    issue_cards = public_scan_audit_swarm_issue_cards(value.get("issueCards") or value.get("issue_cards"))
    verification_results = public_scan_audit_swarm_verification_results(
        value.get("verificationResults") or value.get("verification_results")
    )
    evidence_blocks = public_scan_audit_swarm_evidence_blocks(
        value.get("evidenceBlocks") or value.get("evidence_blocks"),
        issue_cards,
        verification_results,
    )
    roles = review._safe_text_list(value.get("roles"))[:12]
    roles.extend(item.get("agentRole", "") for item in issue_cards)
    roles.extend(item.get("verifierRole", "") for item in verification_results)
    roles.extend(item.get("role", "") for item in evidence_blocks)
    shards = review._safe_text_list(value.get("shards"))[:20]
    shards.extend(item.get("shardId", "") for item in issue_cards)
    shards.extend(item.get("shardId", "") for item in evidence_blocks)
    counts = public_scan_audit_swarm_counts(value.get("counts"), issue_cards, verification_results)
    if evidence_blocks:
        counts["evidenceBlocks"] = max(public_scan_count(counts.get("evidenceBlocks")), len(evidence_blocks))
    payload = {
        "protocol": public_issue_text(value.get("protocol")),
        "stage": public_issue_text(value.get("stage")).lower(),
        "adapter": public_issue_text(value.get("adapter")),
        "providerChain": review._safe_text_list(value.get("providerChain") or value.get("provider_chain"))[:5],
        "summary": " ".join(review._safe_text_lenient(value.get("summary")).split())[:800],
        "logsSummary": " ".join(review._safe_text_lenient(value.get("logsSummary") or value.get("logs_summary")).split())[
            :1000
        ],
        "counts": counts,
        "roles": list(dict.fromkeys(item for item in roles if item))[:12],
        "shards": list(dict.fromkeys(item for item in shards if item))[:20],
        "issueCards": issue_cards,
        "verificationResults": verification_results,
        "evidenceBlocks": evidence_blocks,
    }
    return {key: item for key, item in payload.items() if item not in ("", [], {})}


def public_scan_audit_swarm_counts(value: object, issue_cards: list[dict], verification_results: list[dict]) -> dict:
    source = value if isinstance(value, dict) else {}
    payload = {}
    for key in (
        "issueCards",
        "verificationResults",
        "candidateCount",
        "reportedCount",
        "rejectedCount",
        "downgradedCount",
        "verifiedCount",
        "staticProofCount",
        "potentialRiskCount",
        "unverifiedCount",
        "manifestCount",
        "toolCount",
        "verifierRunCount",
        "evidenceBlocks",
    ):
        count = public_scan_count(source.get(key))
        if count:
            payload[key] = count
    evidence_block_count = public_scan_count(source.get("evidenceBlocks") or source.get("evidence_blocks"))
    if evidence_block_count:
        payload["evidenceBlocks"] = max(public_scan_count(payload.get("evidenceBlocks")), evidence_block_count)
    if issue_cards:
        payload["issueCards"] = max(public_scan_count(payload.get("issueCards")), len(issue_cards))
    if verification_results:
        payload["verificationResults"] = max(
            public_scan_count(payload.get("verificationResults")),
            len(verification_results),
        )
    return payload


def public_scan_audit_swarm_evidence_blocks(
    value: object,
    issue_cards: list[dict],
    verification_results: list[dict],
) -> list[dict]:
    raw_blocks = value if isinstance(value, list) else []
    blocks = [
        block
        for block in (public_scan_audit_swarm_evidence_block(item) for item in raw_blocks)
        if block
    ]
    if not blocks:
        blocks = public_scan_audit_swarm_blocks_from_records(issue_cards, verification_results)
    return public_scan_audit_swarm_dedupe_blocks(blocks)[:40]


def public_scan_audit_swarm_blocks_from_records(
    issue_cards: list[dict],
    verification_results: list[dict],
) -> list[dict]:
    blocks = []
    for index, card in enumerate(issue_cards[:8]):
        issue_id = public_issue_text(card.get("issueId")) or f"audit-candidate-{index + 1}"
        common = {
            "issueId": issue_id,
            "severity": card.get("severity"),
            "category": card.get("category"),
            "role": card.get("agentRole"),
            "shardId": card.get("shardId"),
            "confidence": card.get("confidence"),
        }
        claim = public_issue_text(card.get("claim"))
        title = public_issue_text(card.get("title")) or f"Audit candidate {index + 1}"
        if claim:
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:claim",
                        "kind": "claim",
                        "title": title,
                        "summary": claim,
                        **common,
                    }
                )
            )
        for location_index, location in enumerate(card.get("locations") if isinstance(card.get("locations"), list) else []):
            if not isinstance(location, dict):
                continue
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:location:{location_index}",
                        "kind": "code_location",
                        "title": "Code location",
                        "summary": claim or title,
                        "file": location.get("file"),
                        "startLine": location.get("startLine"),
                        "endLine": location.get("endLine"),
                        **common,
                    }
                )
            )
        if not card.get("locations") and public_issue_text(card.get("file")):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:location:0",
                        "kind": "code_location",
                        "title": "Code location",
                        "summary": claim or title,
                        "file": card.get("file"),
                        "startLine": card.get("line"),
                        "endLine": card.get("line"),
                        **common,
                    }
                )
            )
        for evidence_index, evidence in enumerate(card.get("evidence") if isinstance(card.get("evidence"), list) else []):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:evidence:{evidence_index}",
                        "kind": "evidence",
                        "title": "Discovery evidence",
                        "summary": evidence,
                        **common,
                    }
                )
            )
        for check_index, check in enumerate(
            card.get("falsePositiveChecks") if isinstance(card.get("falsePositiveChecks"), list) else []
        ):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:false-positive:{check_index}",
                        "kind": "false_positive_check",
                        "title": "False-positive check",
                        "summary": check,
                        **common,
                    }
                )
            )
        for invariant_index, invariant in enumerate(
            card.get("violatedInvariants") if isinstance(card.get("violatedInvariants"), list) else []
        ):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:invariant:{invariant_index}",
                        "kind": "invariant",
                        "title": "Violated invariant",
                        "summary": invariant,
                        **common,
                    }
                )
            )
        suggested_test = public_issue_text(card.get("suggestedTest"))
        if suggested_test:
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{issue_id}:suggested-test",
                        "kind": "command",
                        "title": "Suggested test",
                        "summary": suggested_test,
                        "status": "suggested",
                        **common,
                    }
                )
            )
    for index, result in enumerate(verification_results[:12]):
        issue_id = public_issue_text(result.get("issueId"))
        key = issue_id or f"verification-{index + 1}"
        verdict = public_issue_text(result.get("verdict")).lower()
        common = {
            "issueId": issue_id,
            "role": result.get("verifierRole"),
            "verdict": verdict,
            "proofType": result.get("proofType"),
            "proofStrength": result.get("proofStrength"),
            "confidence": result.get("confidence"),
        }
        summary = public_issue_text(result.get("summary"))
        blocks.append(
            public_scan_audit_swarm_evidence_block(
                {
                    "id": f"{key}:verdict:{public_issue_text(result.get('verifierRole')) or index}",
                    "kind": "verifier_verdict",
                    "title": "Verifier verdict",
                    "summary": summary or f"{public_issue_text(result.get('verifierRole')) or 'verifier'} returned {verdict or 'a verdict'}.",
                    **common,
                }
            )
        )
        commands = result.get("commands") if isinstance(result.get("commands"), list) else []
        command = public_issue_text(result.get("command"))
        if command and command not in commands:
            commands = [command, *commands]
        for command_index, command_text in enumerate(commands[:3]):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{key}:command:{command_index}",
                        "kind": "command",
                        "title": "Verifier command",
                        "summary": summary,
                        "command": command_text,
                        "status": "executed",
                        **common,
                    }
                )
            )
        for evidence_index, evidence in enumerate(result.get("evidence") if isinstance(result.get("evidence"), list) else []):
            blocks.append(
                public_scan_audit_swarm_evidence_block(
                    {
                        "id": f"{key}:verification-evidence:{evidence_index}",
                        "kind": "evidence",
                        "title": "Verifier evidence",
                        "summary": evidence,
                        **common,
                    }
                )
            )
    return blocks


def public_scan_audit_swarm_evidence_block(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    kind = public_issue_text(value.get("kind")).lower()
    if kind not in AUDIT_SWARM_EVIDENCE_BLOCK_KINDS:
        kind = "evidence"
    title = " ".join(review._safe_text_lenient(value.get("title") or kind.replace("_", " ").title()).split())[:180]
    summary = " ".join(
        review._safe_text_lenient(value.get("summary") or value.get("text") or value.get("claim")).split()
    )[:900]
    block = {
        "id": public_issue_text(value.get("id") or value.get("blockId") or value.get("block_id")),
        "kind": kind,
        "title": title,
        "summary": summary,
    }
    for key in (
        "issueId",
        "severity",
        "category",
        "role",
        "shardId",
        "stage",
        "status",
        "verdict",
        "proofType",
        "command",
    ):
        snake_key = re.sub(r"(?<!^)([A-Z])", r"_\1", key).lower()
        text = public_issue_text(value.get(key) or value.get(snake_key))
        if key == "verdict" and text and text not in {"confirmed", "rejected", "inconclusive"}:
            text = ""
        if text:
            block[key] = text
    file_path = public_issue_file(value.get("file") or value.get("path"))
    if file_path:
        block["file"] = file_path
    start_line, end_line = public_scan_audit_swarm_line_range(value)
    if start_line:
        block["startLine"] = start_line
    if end_line:
        block["endLine"] = end_line
    proof_strength = public_scan_count(value.get("proofStrength") or value.get("proof_strength"))
    if proof_strength:
        block["proofStrength"] = proof_strength
    if "confidence" in value:
        try:
            confidence = float(value.get("confidence"))
        except (OverflowError, TypeError, ValueError):
            confidence = 0.0
        if confidence:
            block["confidence"] = max(0.0, min(1.0, confidence))
    items = public_scan_audit_swarm_text_items(value.get("items"))[:8]
    if items:
        block["items"] = items
    return {key: item for key, item in block.items() if item not in ("", [], {})}


def public_scan_audit_swarm_dedupe_blocks(blocks: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        key = (
            public_issue_text(block.get("kind")),
            public_issue_text(block.get("issueId")),
            public_issue_text(block.get("title")),
            public_issue_text(block.get("summary")),
            public_issue_text(block.get("command")),
            public_issue_text(block.get("file")),
            public_scan_count(block.get("startLine")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped


def public_scan_audit_swarm_issue_cards(value: object) -> list[dict]:
    raw_cards = value if isinstance(value, list) else []
    cards = []
    for index, item in enumerate(raw_cards):
        if not isinstance(item, dict):
            continue
        locations = public_scan_audit_swarm_locations(item)
        primary = locations[0] if locations else {}
        evidence = public_scan_audit_swarm_text_items(item.get("evidence"))[:5]
        false_positive_checks = review._safe_text_list(item.get("false_positive_checks") or item.get("falsePositiveChecks"))[
            :5
        ]
        invariants = review._safe_text_list(item.get("violated_invariants") or item.get("violatedInvariants"))[:5]
        issue_id = public_issue_text(item.get("issueId") or item.get("issue_id") or item.get("id"))
        title = " ".join(
            review._safe_text_lenient(item.get("title") or f"Audit candidate {index + 1}").split()
        )[:180]
        card = {
            "issueId": issue_id,
            "title": title,
            "severity": worker_audit_swarm_severity(item.get("severity")),
            "category": worker_audit_swarm_category(item),
            "shardId": public_issue_text(item.get("shardId") or item.get("shard_id")),
            "agentRole": public_issue_text(item.get("agentRole") or item.get("agent_role")),
            "confidence": worker_audit_swarm_confidence(item.get("confidence"), "candidate"),
            "file": public_issue_text(primary.get("file")),
            "line": public_scan_count(primary.get("startLine") or item.get("line")),
            "locations": locations,
            "claim": " ".join(
                review._safe_text_lenient(item.get("claim") or item.get("summary") or item.get("description")).split()
            )[:700],
            "evidence": evidence,
            "evidenceCount": max(
                public_scan_count(item.get("evidenceCount") or item.get("evidence_count")),
                len(evidence),
                len(item.get("evidence")) if isinstance(item.get("evidence"), list) else 0,
            ),
            "reproductionIdea": " ".join(
                review._safe_text_lenient(item.get("reproduction_idea") or item.get("reproductionIdea")).split()
            )[:700],
            "suggestedTest": " ".join(
                review._safe_text_lenient(item.get("suggested_test") or item.get("suggestedTest")).split()
            )[:700],
            "falsePositiveChecks": false_positive_checks,
            "violatedInvariants": invariants,
        }
        card = {key: field for key, field in card.items() if field not in ("", [], {})}
        if card:
            cards.append(card)
    return cards[:20]


def public_scan_audit_swarm_verification_results(value: object) -> list[dict]:
    raw_results = value if isinstance(value, list) else []
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        commands = review._safe_text_list(item.get("commands_run") or item.get("commandsRun") or item.get("commands"))[:5]
        command = public_issue_text(item.get("command"))
        if command and command not in commands:
            commands.insert(0, command)
        commands = commands[:5]
        evidence = public_scan_audit_swarm_text_items(item.get("evidence"))[:5]
        verdict = public_issue_text(item.get("verdict")).lower()
        if verdict not in {"confirmed", "rejected", "inconclusive"}:
            verdict = ""
        result = {
            "issueId": public_issue_text(item.get("issue_id") or item.get("issueId")),
            "verifierRole": public_issue_text(item.get("verifier_role") or item.get("verifierRole")),
            "verdict": verdict,
            "confidence": worker_audit_swarm_confidence(item.get("confidence"), verdict),
            "proofType": public_issue_text(item.get("proof_type") or item.get("proofType")),
            "proofStrength": public_scan_count(item.get("proof_strength") or item.get("proofStrength")),
            "summary": " ".join(
                review._safe_text_lenient(
                    item.get("result_summary") or item.get("resultSummary") or item.get("summary")
                ).split()
            )[:800],
            "commands": commands,
            "command": commands[0] if commands else "",
            "commandCount": max(
                public_scan_count(item.get("commandCount") or item.get("command_count")),
                len(commands),
            ),
            "evidence": evidence,
            "evidenceCount": max(
                public_scan_count(item.get("evidenceCount") or item.get("evidence_count")),
                len(evidence),
                len(item.get("evidence")) if isinstance(item.get("evidence"), list) else 0,
            ),
            "notesForFix": review._safe_text_list(item.get("notes_for_fix") or item.get("notesForFix"))[:5],
        }
        result = {key: field for key, field in result.items() if field not in ("", [], {})}
        if result:
            results.append(result)
    return results[:30]


def public_scan_audit_swarm_locations(card: dict) -> list[dict]:
    locations = []
    seen = set()
    raw_locations = card.get("locations") if isinstance(card.get("locations"), list) else []
    if not raw_locations and public_issue_text(card.get("file")):
        raw_locations = [{"file": card.get("file"), "line": card.get("line")}]
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = public_issue_file(item.get("file") or item.get("path"))
        if not file_path:
            continue
        start_line, end_line = public_scan_audit_swarm_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    return locations[:8]


def public_scan_audit_swarm_line_range(source: dict) -> tuple[int, int]:
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


def public_scan_audit_swarm_text_items(value: object) -> list[str]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for item in raw_items:
        if isinstance(item, dict):
            text = review._safe_text_lenient(item.get("summary") or item.get("text") or item.get("claim"))
        else:
            text = review._safe_text_lenient(item)
        text = " ".join(text.split())[:700]
        if text:
            items.append(text)
    return items


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
        evidence_items += len(evidence)
    preflight = public_scan.get("preflight") or {}
    log_artifact_count = len(audit_bundle_log_artifacts_from_preflight(preflight))
    bundle = {
        "schemaVersion": 1,
        "generatedAt": now(),
        "kind": "pullwise.audit_bundle",
        "scan": public_scan,
        "preflight": preflight,
        "verification": public_scan.get("verification") or public_scan_verification_counts(scan),
        "verificationAudit": public_scan.get("verificationAudit") or public_scan_verification_audit(scan),
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
    artifacts = [
        audit_bundle_artifact("README.md", "text/markdown", audit_bundle_readme_markdown(bundle)),
        audit_bundle_artifact("report.md", "text/markdown", audit_bundle_report_markdown(bundle)),
        audit_bundle_artifact("reproduction/commands.txt", "text/plain", audit_bundle_repro_commands_text(bundle)),
        audit_bundle_artifact("environment.json", "application/json", audit_bundle_environment_json(bundle)),
        audit_bundle_artifact("tool-versions.json", "application/json", audit_bundle_tool_versions_json(bundle)),
        audit_bundle_artifact("audit.json", "application/json", audit_bundle_json_text(bundle)),
    ]
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
    verification_audit = bundle.get("verificationAudit") if isinstance(bundle.get("verificationAudit"), dict) else {}
    return "\n".join(
        [
            "# Pullwise Audit Bundle",
            "",
            f"Repository: {public_issue_text(scan.get('repo')) or 'unknown'}",
            f"Branch: {public_issue_text(scan.get('branch')) or 'main'}",
            f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
            f"Generated at: {pull_request_timestamp(bundle.get('generatedAt')) or 0}",
            "",
            "This bundle is designed for evidence review. Start with report.md, inspect issues/*.md, then review reproduction/commands.txt as untrusted text.",
            "",
            "## Candidate Audit",
            "",
            f"- Candidates evaluated: {public_scan_count(verification_audit.get('candidateCount'))}",
            f"- Reported issues: {public_scan_count(verification_audit.get('reportedCount'))}",
            f"- Rejected before reporting: {public_scan_count(verification_audit.get('rejectedCount'))}",
            f"- Downgraded by evidence gates: {public_scan_count(verification_audit.get('downgradedCount'))}",
            "",
            "## Reproduction",
            "",
            "Captured reproduction commands are stored only in reproduction/commands.txt and issue markdown files.",
            "Treat every command as untrusted input. Review the repository, command, and environment before copying any command into a shell manually.",
            "Suggested patch artifacts are stored under patches/ when an issue includes safe before/after code evidence.",
            "Verifier stdout/stderr is withheld from the bundle; worker log paths may be listed as references only.",
            "Tool versions captured during preflight are stored in tool-versions.json.",
            "Artifact sizes and sha256 checksums are listed in artifact-manifest.json.",
            "",
        ]
    )


def audit_bundle_report_markdown(bundle: dict) -> str:
    scan = bundle.get("scan") if isinstance(bundle.get("scan"), dict) else {}
    evidence_summary = bundle.get("evidenceSummary") if isinstance(bundle.get("evidenceSummary"), dict) else {}
    verification = bundle.get("verification") if isinstance(bundle.get("verification"), dict) else {}
    verification_audit = bundle.get("verificationAudit") if isinstance(bundle.get("verificationAudit"), dict) else {}
    issues = bundle.get("issues") if isinstance(bundle.get("issues"), list) else []
    lines = [
        "# Repo Audit Report",
        "",
        f"Repo: {public_issue_text(scan.get('repo')) or 'unknown'}",
        f"Commit: {public_issue_text(scan.get('commit')) or 'pending'}",
        f"Scan: {public_issue_text(scan.get('id')) or 'unknown'}",
        "",
        "## Summary",
        "",
        f"- Issues: {public_scan_count(evidence_summary.get('issueCount'))}",
        f"- Evidence items: {public_scan_count(evidence_summary.get('evidenceItemCount'))}",
        f"- Reproduction commands: {public_scan_count(evidence_summary.get('reproductionCommandCount'))}",
        f"- Verifier log artifacts: {public_scan_count(evidence_summary.get('logArtifactCount'))}",
        f"- Verified: {public_scan_count(verification.get('verified'))}",
        f"- Static proof: {public_scan_count(verification.get('static_proof'))}",
        f"- Potential risk: {public_scan_count(verification.get('potential_risk'))}",
        f"- Unverified: {public_scan_count(verification.get('unverified'))}",
        "",
        "## Candidate Audit",
        "",
        f"- Candidates evaluated: {public_scan_count(verification_audit.get('candidateCount'))}",
        f"- Reported: {public_scan_count(verification_audit.get('reportedCount'))}",
        f"- Rejected: {public_scan_count(verification_audit.get('rejectedCount'))}",
        f"- Downgraded: {public_scan_count(verification_audit.get('downgradedCount'))}",
    ]
    rejected_samples = verification_audit.get("rejectedSamples") if isinstance(verification_audit.get("rejectedSamples"), list) else []
    for sample in rejected_samples[:5]:
        if not isinstance(sample, dict):
            continue
        reason = public_issue_text(sample.get("reason"))
        title = review._safe_text_lenient(sample.get("title"))
        if reason:
            lines.append(f"- Rejected sample: {reason}" + (f" - {title}" if title else ""))
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No issues were included in this bundle.")
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
            "# This scaffold is retained for compatibility and does not run captured commands.",
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
        "verificationAudit": bundle.get("verificationAudit") if isinstance(bundle.get("verificationAudit"), dict) else {},
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
    provider_chain = review._safe_text_list(value.get("providerChain"))[:5]
    if provider_chain:
        payload["providerChain"] = provider_chain
    environment = public_scan_preflight_environment(value.get("environment"))
    if environment:
        payload["environment"] = environment
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


