"""
Pullwise code review agent integration.

This module contains server-side finding normalization and protocol helpers.
Actual repository review work is performed by external pullwise-worker
processes that claim queued jobs from the server.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import time

from . import fix_workflow


VALID_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
VALID_FINDING_CATEGORIES = {
    "security": "Security",
    "performance": "Performance",
    "dependencies": "Dependencies",
    "quality": "Quality",
    "tests": "Tests",
    "docs": "Docs",
    "architecture": "Architecture",
}
VALID_VERIFICATION_STATUSES = {"verified", "static_proof", "potential_risk", "unverified"}
VALID_EVIDENCE_TYPES = {
    "code",
    "path",
    "trigger",
    "runtime_log",
    "test",
    "environment",
    "tool",
    "documentation",
    "fix_verification",
}
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

def normalize_findings(
    raw_findings: object,
    *,
    repo: str,
    branch: str,
    commit: str,
    user_id: str,
    scan_id: str,
    repo_path: str | None = None,
) -> list[dict]:
    """Normalize worker/provider findings into records ready to persist."""
    repo = _safe_repo_full_name(repo)
    branch = _safe_metadata_text(branch, "main")
    commit = _safe_metadata_text(commit, "pending")
    user_id = _safe_metadata_text(user_id)
    scan_id = _safe_metadata_text(scan_id)
    repo_path = _safe_metadata_text(repo_path) if repo_path is not None else None

    raw = raw_findings if isinstance(raw_findings, list) else []
    findings = []
    for finding in raw:
        if not isinstance(finding, dict):
            continue
        findings.append(
            _finalize_finding(
                finding,
                user_id=user_id,
                scan_id=scan_id,
                repo=repo,
                repo_path=repo_path,
            )
        )
    return findings


def _finalize_finding(
    finding: object,
    *,
    user_id: str,
    scan_id: str,
    repo: str,
    repo_path: str | None = None,
) -> dict:
    finding = finding if isinstance(finding, dict) else {}
    finding_id = _safe_text(finding.get("id"))
    file_path = _safe_finding_file(finding.get("file"), repo_path)
    affected_locations = _safe_affected_locations(finding.get("affectedLocations"), repo_path)
    if file_path and not affected_locations:
        line = _safe_non_negative_int(finding.get("line"))
        affected_locations = [{"file": file_path, "startLine": line, "endLine": line}]
    evidence = _safe_evidence(finding.get("evidence"), repo_path)
    reproduction = _safe_reproduction(finding.get("reproduction"), repo_path)
    bad_code = _safe_code_lines(finding.get("badCode"))
    good_code = _safe_code_lines(finding.get("goodCode"))
    auto_fix = _safe_auto_fix(
        finding,
        repo_path=repo_path,
        file_path=file_path,
        bad_code=bad_code,
        good_code=good_code,
    )
    if not auto_fix:
        bad_code = []
        good_code = []
    return {
        "id": finding_id or f"f_{secrets.token_urlsafe(6)}",
        "userId": user_id,
        "scanId": scan_id,
        "repo": repo,
        "status": "open",
        "severity": _safe_severity(finding.get("severity")),
        "category": _safe_category(finding.get("category")),
        "title": _safe_text(finding.get("title"), "Untitled finding"),
        "summary": _safe_text_lenient(finding.get("summary")),
        "impact": _safe_text_lenient(finding.get("impact")),
        "detectionReasoning": _safe_text_lenient(finding.get("detectionReasoning")),
        "reproductionPath": _safe_text_lenient(finding.get("reproductionPath")),
        "verificationStatus": _safe_verification_status(
            finding.get("verificationStatus"),
            affected_locations=affected_locations,
            evidence=evidence,
            reproduction=reproduction,
        ),
        "verificationSummary": _safe_text_lenient(finding.get("verificationSummary")),
        "affectedLocations": affected_locations,
        "evidence": evidence,
        "reproduction": reproduction,
        "whyNotFalsePositive": _safe_text_list(finding.get("whyNotFalsePositive")),
        "limitations": _safe_text_list(finding.get("limitations")),
        "file": file_path,
        "line": _safe_non_negative_int(finding.get("line")),
        "confidence": _safe_confidence(finding.get("confidence")),
        "confidenceRationale": _safe_text_lenient(finding.get("confidenceRationale")),
        "autoFix": auto_fix,
        "effort": _safe_text(finding.get("effort"), "-"),
        "fixBenefits": _safe_text_lenient(finding.get("fixBenefits")),
        "fixRisks": _safe_text_lenient(finding.get("fixRisks")),
        "tags": _safe_text_list(finding.get("tags")),
        "steps": _safe_text_list(finding.get("steps")),
        "badCode": bad_code,
        "goodCode": good_code,
        "references": _safe_references(finding.get("references")),
        "createdAt": int(time.time()),
    }


def _safe_text(value: object, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    if any(char in value for char in "\r\n\x00"):
        return default
    value = value.strip()
    if not value or any(char in value for char in "\r\n\x00"):
        return default
    return value


def _safe_text_lenient(value: object, default: str = "") -> str:
    """Sanitize text for issue content fields (summary, impact, etc.).

    Unlike ``_safe_text`` which rejects any string containing CR/LF/CRLF,
    this variant normalizes CRLF and CR to spaces while preserving LF.
    LLM providers frequently emit multi-line content in issue descriptions,
    and silently discarding that content leaves users with empty fields.

    CR and CRLF are still neutralized (replaced with spaces) to prevent
    HTTP header injection. Plain LF is safe in JSON API responses and
    HTML rendering contexts used by the issue detail view.
    """
    if not isinstance(value, str):
        return default
    if "\x00" in value:
        return default
    value = value.replace("\r\n", " ").replace("\r", " ").strip()
    if not value or "\x00" in value:
        return default
    return value


def _safe_metadata_text(value: object, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    if any(char in value for char in "\r\n\x00"):
        return default
    value = value.strip()
    if not value:
        return default
    return value


def _safe_repo_full_name(value: object) -> str:
    repo = _safe_metadata_text(value)
    return repo if _REPO_FULL_NAME_RE.match(repo) else ""


def _safe_finding_file(value: object, repo_path: str | None = None) -> str:
    path = _safe_text(value)
    if not path:
        return ""

    relative_path = _relative_file_inside_repo(path, repo_path) or path
    return fix_workflow.safe_issue_file(relative_path) or ""


def _relative_file_inside_repo(path: str, repo_path: str | None) -> str | None:
    if not repo_path or not os.path.isabs(path):
        return None

    root_abs = os.path.realpath(os.path.abspath(repo_path))
    file_abs = os.path.realpath(os.path.abspath(path))
    try:
        common = os.path.commonpath([root_abs, file_abs])
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(root_abs):
        return None
    return os.path.relpath(file_abs, root_abs).replace(os.sep, "/")


def _safe_auto_fix(
    finding: dict,
    *,
    repo_path: str | None,
    file_path: str,
    bad_code: list[dict],
    good_code: list[dict],
) -> bool:
    if finding.get("autoFix") is not True:
        return False
    if not file_path or not bad_code or not good_code:
        return False
    if not repo_path:
        return True

    try:
        preview = fix_workflow.preview_issue_fix(
            repo_path,
            {
                "id": "contract-check",
                "file": file_path,
                "autoFix": True,
                "badCode": bad_code,
                "goodCode": good_code,
            },
        )
    except (OSError, UnicodeError, ValueError):
        return False
    return preview.get("valid") is True


def _safe_severity(value: object) -> str:
    normalized = _safe_text(value).lower()
    return normalized if normalized in VALID_FINDING_SEVERITIES else "medium"


def _safe_category(value: object) -> str:
    normalized = _safe_text(value).lower()
    return VALID_FINDING_CATEGORIES.get(normalized, "Quality")


def _safe_non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, candidate)


def _safe_confidence(value: object) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(candidate):
        return 0.0
    return min(1.0, max(0.0, candidate))


def _safe_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _safe_text(item))]


def _safe_http_url(value: object) -> str:
    url = _safe_text(value)
    return url if url.startswith(("https://", "http://")) else ""


def _safe_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        candidate = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return candidate


def _safe_line_range(source: dict) -> tuple[int, int]:
    start = _safe_non_negative_int(
        source.get("startLine", source.get("start_line", source.get("line")))
    )
    end = _safe_non_negative_int(source.get("endLine", source.get("end_line", start)))
    if start and end and end < start:
        end = start
    if start and not end:
        end = start
    return start, end


def _safe_affected_locations(value: object, repo_path: str | None = None) -> list[dict]:
    if not isinstance(value, list):
        return []
    locations = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = _safe_finding_file(item.get("file"), repo_path)
        if not file_path:
            continue
        start_line, end_line = _safe_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    return locations[:10]


def _safe_evidence(value: object, repo_path: str | None = None) -> list[dict]:
    if not isinstance(value, list):
        return []
    evidence = []
    for item in value:
        if not isinstance(item, dict):
            continue
        evidence_type = _safe_text(item.get("type")).lower()
        if evidence_type not in VALID_EVIDENCE_TYPES:
            evidence_type = "code"
        label = _safe_text(item.get("label")) or evidence_type.replace("_", " ").title()
        summary = _safe_text_lenient(item.get("summary"))
        file_path = _safe_finding_file(item.get("file"), repo_path)
        start_line, end_line = _safe_line_range(item)
        command = _safe_text(item.get("command"))
        log_path = _safe_text(item.get("logPath", item.get("log_path")))
        url = _safe_http_url(item.get("url"))
        exit_code = _safe_optional_int(item.get("exitCode", item.get("exit_code")))
        output = _safe_text_lenient(item.get("output"))[:4000]
        if not any([summary, file_path, command, log_path, output, url, exit_code is not None]):
            continue
        record = {
            "type": evidence_type,
            "label": label,
            "summary": summary,
        }
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
        if output:
            record["output"] = output
        if url:
            record["url"] = url
        evidence.append(record)
    return evidence[:20]


def _safe_reproduction(value: object, repo_path: str | None = None) -> dict:
    source = value if isinstance(value, dict) else {}
    test_file = _safe_finding_file(source.get("testFile", source.get("test_file")), repo_path)
    return {
        "commands": _safe_text_list(source.get("commands")),
        "input": _safe_text_lenient(source.get("input")),
        "expected": _safe_text_lenient(source.get("expected")),
        "actual": _safe_text_lenient(source.get("actual")),
        "testFile": test_file,
        "logPath": _safe_text(source.get("logPath", source.get("log_path"))),
    }


def _safe_verification_status(
    value: object,
    *,
    affected_locations: list[dict],
    evidence: list[dict],
    reproduction: dict,
) -> str:
    status = _safe_text(value).lower()
    if status not in VALID_VERIFICATION_STATUSES:
        status = ""
    has_precise_location = any(location.get("file") and location.get("startLine") for location in affected_locations)
    has_reproduction_command = bool(reproduction.get("commands"))
    has_reproduction_output = has_reproduction_command and any(
        [reproduction.get("actual"), reproduction.get("logPath"), reproduction.get("testFile")]
    )
    has_runtime_evidence = has_reproduction_output or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and any(
            [
                item.get("command"),
                item.get("logPath"),
                item.get("file"),
                item.get("output"),
                item.get("exitCode") is not None,
            ]
        )
        for item in evidence
    )
    has_raw_runtime_output = has_reproduction_output or any(
        item.get("type") in {"runtime_log", "test", "fix_verification"}
        and any([item.get("logPath"), item.get("output")])
        for item in evidence
    )
    has_static_evidence = bool(affected_locations) or any(
        item.get("type") in {"code", "path", "trigger", "documentation", "tool"}
        and any([item.get("file"), item.get("summary"), item.get("command")])
        for item in evidence
    )
    verified_ready = (
        has_precise_location
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


def _safe_code_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if any(char in value for char in "\r\n\x00"):
        return None
    return value


def _safe_code_text_lenient(value: object) -> str | None:
    """Sanitize code text for issue evidence fields (badCode, goodCode).

    Unlike ``_safe_code_text`` which rejects any string containing CR/LF/CRLF,
    this variant normalizes CRLF and CR to LF while preserving LF.
    Code evidence frequently contains legitimate newlines within a single
    logical line (e.g. template literals, multi-line expressions).

    CR and CRLF are still neutralized to prevent HTTP header injection.
    Plain LF is safe in JSON API responses and HTML <pre> rendering.
    """
    if not isinstance(value, str):
        return None
    if "\x00" in value:
        return None
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _safe_code_lines(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        if not isinstance(item, dict) or (code := _safe_code_text_lenient(item.get("code"))) is None:
            continue
        raw_marker = item.get("t")
        marker = raw_marker if raw_marker in ("del", "add", None) else None
        lines.append({"ln": _safe_non_negative_int(item.get("ln")), "code": code, "t": marker})
    return lines


def _safe_references(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    references = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _safe_text(item.get("label"))
        url = _safe_text(item.get("url"))
        if label and url.startswith(("https://", "http://")):
            references.append({"label": label, "url": url})
    return references
