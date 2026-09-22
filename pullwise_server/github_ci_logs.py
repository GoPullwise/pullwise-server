"""Injected, read-only single-job log boundary; no default network transport.

The callback must implement GET, disable redirects/cookies/proxies, validate TLS
and public resolved peers, bound each chunk, and honor the monotonic ``deadline``
across DNS, connect, headers and reads. Checks here reject late results but cannot
interrupt a blocking callback. This is not a proven hard wall-clock transport or
Cloudflare runtime: live enablement requires that platform gate first.

Official endpoint: https://docs.github.com/en/rest/actions/workflow-jobs
Restricted storage family: https://github.blog/changelog/2023-01-31-github-actions-job-summary-updates/
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from .github_transport import GitHubUnavailable, _retry_at


@dataclass(frozen=True)
class CILogResult:
    text: str = field(default="", repr=False)
    coverage: str = "unavailable"
    reason: str | None = None


def redact_ci_log(text: str) -> str:
    """Known-pattern filtering only, not a guarantee of secret removal."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?im)^(.*?\b(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*[:=]).*$", r"\1 [REDACTED]", text)
    text = re.sub(r"(?i)\b(?:https?|ftp)://[^\s<>]+", "[REDACTED_URL]", text)
    text = re.sub(r"(?i)\b(?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*)[\"']?[ \t]*[:=][ \t]*[^\r\n]+", "[REDACTED_CREDENTIAL]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)",
                  lambda match: "[REDACTED_PRIVATE_KEY]" + "\n" * match[0].count("\n"), text)
    return text


def _download_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("location")
    url = urlsplit(value)
    host = url.hostname or ""
    if (url.scheme != "https" or url.username or url.password or url.fragment
            or url.netloc != host or not (host == "results-receiver.actions.githubusercontent.com"
            or re.fullmatch(r"productionresultssa[a-z0-9]*\.blob\.core\.windows\.net", host))):
        raise ValueError("location")
    return value


class GitHubCILogReader:
    def __init__(self, *, request: Callable | None = None, clock: Callable = time.monotonic,
                 wall_clock: Callable = time.time, max_bytes: int = 5 * 1024 * 1024,
                 total_seconds: float = 20):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 5 * 1024 * 1024:
            raise ValueError("GITHUB_CI_LOG_LIMIT_INVALID")
        if type(total_seconds) not in (int, float) or not math.isfinite(total_seconds) or not 0 < total_seconds <= 60:
            raise ValueError("GITHUB_CI_LOG_DEADLINE_INVALID")
        self.request, self.clock, self.wall_clock = request, clock, wall_clock
        self.max_bytes, self.total_seconds = max_bytes, total_seconds

    def __call__(self, verified_full_name, job_id, *, token):
        if (not isinstance(verified_full_name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", verified_full_name)
                or any(p in {".", ".."} for p in verified_full_name.split("/"))
                or type(job_id) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(job_id))
                or not isinstance(token, str) or not re.fullmatch(r"[!-~]*", token)):
            raise ValueError("GITHUB_CI_LOG_INPUT_INVALID")
        if self.request is None:
            return CILogResult(reason="transport_unconfigured")
        deadline = self.clock() + self.total_seconds
        url = f"https://api.github.com/repos/{verified_full_name}/actions/jobs/{job_id}/logs"
        body = bytearray()
        reason = None
        try:
            for hop in range(3):
                if self.clock() >= deadline:
                    return CILogResult(reason="deadline")
                headers = {"Accept": "text/plain", "Accept-Encoding": "identity", "User-Agent": "Pullwise/1.4"}
                if hop == 0:
                    headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
                    if token:
                        headers["Authorization"] = "Bearer " + token
                response = self.request(url, headers=headers, stream=True, allow_redirects=False, deadline=deadline)
                try:
                    if self.clock() >= deadline:
                        return CILogResult(reason="deadline")
                    metadata = {k.lower(): v for k, v in response.headers.items() if isinstance(k, str) and isinstance(v, str)}
                    status = response.status_code
                    if status == 429 or (status in {403, 503} and ("retry-after" in metadata or metadata.get("x-ratelimit-remaining") == "0")):
                        raise GitHubUnavailable("GITHUB_CI_LOG_RATE_LIMITED", retry_at=_retry_at(metadata, int(self.wall_clock())))
                    if status in {301, 302, 303, 307, 308}:
                        if hop == 0 and status != 302:
                            return CILogResult(reason="unavailable")
                        if hop == 2:
                            return CILogResult(reason="redirect_limit")
                        url = _download_url(metadata.get("location"))
                        continue
                    if hop == 0 or status != 200:
                        return CILogResult(reason="unavailable")
                    content_type = metadata.get("content-type", "text/plain").split(";")[0].strip().lower()
                    if content_type not in {"text/plain", "application/octet-stream"} or metadata.get("content-encoding", "identity").lower() != "identity":
                        return CILogResult(reason="invalid_payload")
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.clock() >= deadline:
                            reason = "deadline"
                            break
                        if not isinstance(chunk, bytes):
                            return CILogResult(reason="invalid_payload")
                        remaining = self.max_bytes - len(body)
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            reason = "size_limit"
                            break
                    if self.clock() >= deadline:
                        reason = "deadline"
                    break
                finally:
                    response.close()
        except GitHubUnavailable as error:
            raise GitHubUnavailable("GITHUB_CI_LOG_UNAVAILABLE", retry_at=error.retry_at) from None
        except Exception:
            reason = "unavailable"
        if reason:
            body = body[:body.rfind(b"\n") + 1]
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return CILogResult(reason="invalid_payload")
        if any(ord(c) < 32 and c not in "\t\r\n\x1b" for c in text) or text.startswith("PK\x03\x04"):
            return CILogResult(reason="invalid_payload")
        if token:
            text = text.replace(token, "[REDACTED_TOKEN]")
        text = redact_ci_log(text)
        if not text.strip():
            return CILogResult(reason=reason or "empty")
        return CILogResult(text=text, coverage="partial" if reason else "complete", reason=reason)


def select_ci_log_windows(result: CILogResult, *, job_id) -> list[dict]:
    """One deterministic bounded tail, without inventing step or stage identity."""
    if result.coverage not in {"complete", "partial"} or not result.text.strip():
        return []
    lines = result.text.splitlines(keepends=True)
    chosen = []
    size = 0
    for line in reversed(lines):
        encoded = len(line.encode("utf-8"))
        if size + encoded > 20 * 1024:
            break
        chosen.append(line)
        size += encoded
    text = "".join(reversed(chosen))
    if not text.strip():
        return []
    first, last = len(lines) - len(chosen) + 1, len(lines)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return [{"windowId": f"ci_log_{job_id}_{first}_{last}_{digest}", "stepNumber": None,
             "stepName": "", "stage": "unknown", "stageRuleVersion": "ci-log-unmapped/v1",
             "selectionRuleVersion": "ci-log-tail/v1", "startLine": first, "endLine": last,
             "coverage": "partial", "text": text, "symptoms": [], "evidenceIds": []}]
