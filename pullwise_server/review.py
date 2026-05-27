"""
Pullwise code review agent integration.

This module is the integration point for engineering code review. The active
provider is selected by `PULLWISE_REVIEW_PROVIDER`:

- `mock`: synthetic findings for explicit local wire-up only.
- `claude_code`: subprocess the Claude Code CLI in the working tree.
- `codex`: subprocess the Codex CLI in the working tree.

Real providers run the agent against a checked-out working tree and parse a
strict JSON document from stdout. Swap providers without touching worker or
HTTP layers.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from . import fix_workflow, scan_logging


CHECKOUT_PROVIDERS = {"claude_code", "codex"}
DEFAULT_PROVIDER = "disabled"
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
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ReviewProviderError(RuntimeError):
    """Expected review-provider failure that should be shown as a scan error."""


SYSTEM_PROMPT = """\
You are Pullwise's code review agent. Read the repository you are placed in
and emit a structured report of issues. You are running in an isolated
sandbox; assume read-only access to the working tree and no network access
unless explicitly granted.

# Scope (cover all that apply, weight emissions by severity x confidence)

- Security: hardcoded secrets, SQL/command injection, XSS, SSRF, path
  traversal, weak crypto, broken auth, unsafe deserialization, unsafe
  template rendering.
- Performance: N+1 queries, render loops, blocking I/O on request paths,
  unbounded memory growth, missing indexes, redundant network calls.
- Dependencies: known CVEs, abandoned/unmaintained packages, lockfile drift,
  duplicate transitives, version pins on EOL major versions.
- Quality: stale closures, race conditions, swallowed errors, dead code,
  duplicated logic, mis-typed boundaries, leaky abstractions.
- Tests: missing coverage on core/risky paths, mocked integrations that hide
  divergence, brittle assertions, flaky tests.
- Docs: outdated public API examples, missing required setup steps,
  contradictions between README and actual behavior.
- Architecture: layering violations, circular imports, god modules,
  cross-cutting concerns leaked into domain code.

# Output contract

Emit a single JSON document and nothing else. No markdown, no preamble, no
trailing prose. Schema:

{
  "findings": [
    {
      "id": "f_<8 url-safe chars>",
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "category": "Security" | "Performance" | "Dependencies" | "Quality" | "Tests" | "Docs" | "Architecture",
      "title": "<= 70 chars, imperative voice",
      "summary": "1-2 sentences: what is wrong",
      "impact": "1 sentence: consequence if not fixed",
      "detectionReasoning": "2-3 sentences: WHY this was flagged. What pattern, data flow, or evidence triggered detection. Reference the specific code path analyzed.",
      "reproductionPath": "1-2 sentences: how the user can verify this issue. Include trigger conditions, required environment, or example input that exposes the problem.",
      "file": "repo-relative path; '' if cross-cutting",
      "line": <integer, 1-indexed; 0 if multi-file>,
      "confidence": <float 0.0..1.0>,
      "confidenceRationale": "1 sentence: why this confidence level. What uncertainty or edge case reduced the score (e.g. 'may be intentional in test fixtures').",
      "autoFix": <bool>,
      "effort": "<rough estimate, e.g. '5 min', '1 hour'>",
      "fixBenefits": "1-2 sentences: what concretely improves after the fix. Quantify when possible (e.g. 'reduces P95 latency by ~200ms').",
      "fixRisks": "1-2 sentences: what could go wrong when applying this fix. Breaking changes, migration needs, behavior changes, or downstream consumers affected.",
      "tags": ["<kebab-case>", ...],
      "steps": ["<concrete action>", ...],
      "badCode":  [ {"ln": <int>, "code": "<line>", "t": "del" | null} ],
      "goodCode": [ {"ln": <int>, "code": "<line>", "t": "add" | null} ],
      "references": [ {"label": "<short>", "url": "<url>"} ]
    }
  ]
}

Always include `badCode`, `goodCode`, and `references` as arrays; use empty
arrays when not applicable. Populate `badCode`/`goodCode` only when `autoFix`
is true and you can produce a deterministic patch. For `autoFix: true`,
`badCode` must be one exact contiguous old block as it appears in `file`, and
`goodCode` must be the complete replacement block. Do not skip unchanged lines
inside the old block.

`detectionReasoning`, `reproductionPath`, `confidenceRationale`, `fixBenefits`,
and `fixRisks` are required string fields. Use an empty string only when the
information is genuinely unavailable. These fields help users decide whether
to trust and act on the finding.

# Severity rubric

- critical: exploit-ready security hole, data loss risk, or production outage trigger.
- high: meaningful correctness/security/performance issue, fix this sprint.
- medium: code quality issue with measurable developer or user impact.
- low: minor improvement.
- info: advisory only; not directly actionable.

# Confidence rubric

- >= 0.95: unambiguous pattern, repo-specific evidence.
- 0.80-0.94: strong signal; may have false-positive edge cases.
- 0.60-0.79: heuristic match; mark as 'review needed'.
- < 0.60: do not emit.

# Constraints

- Output JSON only. Any non-JSON character before or after the document
  invalidates the response.
- Be specific. Cite the file and line where the evidence lives. If you
  cannot point to a line, do not emit the finding.
- Do not invent CVE IDs or library versions. Quote `package.json` /
  `requirements.txt` / `go.mod` when claiming a dependency is vulnerable.
- Do not emit duplicates. If two files share the same root cause, emit one
  finding and list the additional files in the summary.
- Cap each run at 25 findings. Pick the highest severity x confidence first.
"""


USER_PROMPT_TEMPLATE = """\
Repository: {repo}
Branch: {branch}
Commit: {commit}
Working tree: {repo_path}

Review the working tree at the path above and emit the JSON report described
in the system instructions. Stop when you have either covered all scopes or
hit the 25-finding cap.
"""


def run_review(
    *,
    repo: str,
    branch: str,
    commit: str,
    user_id: str,
    scan_id: str,
    repo_path: str | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Dispatch to a review provider and return findings ready to persist.

    Each finding is a dict shaped to drop straight into the ISSUES blob:
    `userId`, `scanId`, `repo`, `status`, `createdAt` are filled here so the
    worker does not need to reshape provider output.
    """
    repo = _safe_repo_full_name(repo)
    branch = _safe_metadata_text(branch, "main")
    commit = _safe_metadata_text(commit, "pending")
    user_id = _safe_metadata_text(user_id)
    scan_id = _safe_metadata_text(scan_id)
    repo_path = _safe_metadata_text(repo_path) if repo_path is not None else None
    chosen = selected_provider(provider)
    started_at = time.monotonic()
    scan_logging.log_event(
        "review_provider_started",
        scanId=scan_id,
        userId=user_id,
        repo=repo,
        branch=branch,
        commit=commit,
        provider=chosen,
        checkoutRequired=provider_requires_checkout(chosen),
        repoPath=repo_path,
    )
    try:
        if chosen == "disabled":
            raise RuntimeError(
                "Code review provider is not configured. Set PULLWISE_REVIEW_PROVIDER "
                "to claude_code or codex for real scans. Use mock only for explicit local wire-up."
            )
        if chosen == "mock":
            raw = _run_mock(repo=repo, branch=branch, commit=commit)
        elif chosen == "claude_code":
            raw = _run_claude_code(repo=repo, branch=branch, commit=commit, repo_path=repo_path)
        elif chosen == "codex":
            raw = _run_codex(repo=repo, branch=branch, commit=commit, repo_path=repo_path)
        else:
            raise ValueError(f"Unknown PULLWISE_REVIEW_PROVIDER: {chosen}")

        findings = []
        auto_fix_downgraded = 0
        for finding in raw:
            if not isinstance(finding, dict):
                continue
            finalized = _finalize_finding(
                finding,
                user_id=user_id,
                scan_id=scan_id,
                repo=repo,
                repo_path=repo_path,
            )
            if finding.get("autoFix") is True and finalized["autoFix"] is False:
                auto_fix_downgraded += 1
            findings.append(finalized)
        scan_logging.log_event(
            "review_provider_completed",
            scanId=scan_id,
            userId=user_id,
            repo=repo,
            branch=branch,
            commit=commit,
            provider=chosen,
            rawFindingCount=len(raw),
            finalizedFindingCount=len(findings),
            autoFixDowngradedCount=auto_fix_downgraded,
            durationMs=int((time.monotonic() - started_at) * 1000),
        )
        return findings
    except Exception as exc:
        scan_logging.log_event(
            "review_provider_failed",
            scanId=scan_id,
            userId=user_id,
            repo=repo,
            branch=branch,
            commit=commit,
            provider=chosen,
            durationMs=int((time.monotonic() - started_at) * 1000),
            error=str(exc)[:500],
        )
        raise


def selected_provider(provider: str | None = None) -> str:
    return (provider or os.environ.get("PULLWISE_REVIEW_PROVIDER", DEFAULT_PROVIDER)).strip().lower()


def provider_requires_checkout(provider: str | None = None) -> bool:
    return selected_provider(provider) in CHECKOUT_PROVIDERS


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


# ── mock provider ─────────────────────────────────────────────────────────


def _run_mock(*, repo: str, branch: str, commit: str) -> list[dict]:
    """Synthetic findings used for frontend wire-up and offline development."""
    time.sleep(0.4)
    return [
        {
            "severity": "critical",
            "category": "Security",
            "title": "Hardcoded API key in client bundle",
            "summary": "A Stripe-shaped secret is referenced from a module that ships in the browser bundle.",
            "impact": "Any visitor can extract the key from the deployed JS and incur charges on the account.",
            "detectionReasoning": "Detected `sk_live_` prefix in a string literal at lib/payments.ts:14. Traced the import chain: lib/payments.ts is imported by src/App.tsx, which is the client entry point. This module is included in the browser bundle via the Vite build config.",
            "reproductionPath": "Run `pnpm build && grep -r sk_live_ dist/` — the secret will appear in the minified client JS bundle.",
            "file": "lib/payments.ts",
            "line": 14,
            "confidence": 0.97,
            "confidenceRationale": "The `sk_live_` prefix is unambiguous and the import chain to the client entry point is direct. Minor uncertainty: a bundler plugin could theoretically strip the constant, but none is configured.",
            "autoFix": True,
            "effort": "5 min",
            "fixBenefits": "Eliminates the ability for any visitor to extract the secret key from the deployed bundle, preventing unauthorized charges.",
            "fixRisks": "If any client-side code path currently depends on the Stripe instance being available in the browser, those paths will break. Verify no client components call `stripe.*` directly.",
            "tags": ["secrets", "stripe", "client-side"],
            "steps": [
                "Move the Stripe instance behind a server-only module and import it from server code paths.",
                "Move the secret to .env / hosted secret store and rotate the existing key in the Stripe Dashboard.",
                "Add a CI secret scanner (e.g. gitleaks) to prevent regression.",
            ],
            "badCode": [
                {"ln": 13, "code": 'import Stripe from "stripe";', "t": "del"},
                {"ln": 14, "code": 'export const stripe = new Stripe("sk_live_51N...4dQp");', "t": "del"},
            ],
            "goodCode": [
                {"ln": 13, "code": 'import "server-only";', "t": "add"},
                {"ln": 14, "code": 'import Stripe from "stripe";', "t": "add"},
                {"ln": 15, "code": "export const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);", "t": "add"},
            ],
            "references": [
                {"label": "Stripe — Securing your secret keys", "url": "https://stripe.com/docs/keys"},
            ],
        },
        {
            "severity": "high",
            "category": "Performance",
            "title": "Dashboard triggers N+1 fetch",
            "summary": "Each row issues its own HTTP request inside the render loop.",
            "impact": "First contentful paint regresses from 320 ms to ~3 s on lists with 100+ rows.",
            "detectionReasoning": "Found a `useEffect` with a `fetch()` call inside a `.map()` render loop at dashboard.tsx:88. Each row component mounts independently and fires its own GET /projects/stats?id=X request. With 100 rows, this produces 100 sequential HTTP requests.",
            "reproductionPath": "Open the dashboard with a workspace that has 50+ projects. Open DevTools Network tab and observe the waterfall of /projects/stats requests.",
            "file": "src/screens/dashboard.tsx",
            "line": 88,
            "confidence": 0.86,
            "confidenceRationale": "The pattern is clearly an N+1 in the render loop, but the actual performance impact depends on the server's response time and whether HTTP/2 multiplexing is enabled.",
            "autoFix": False,
            "effort": "20 min",
            "fixBenefits": "Collapsing 100 individual requests into one batched call reduces P95 dashboard load time from ~3s to ~400ms for large workspaces.",
            "fixRisks": "The batched endpoint GET /projects/stats?ids=... must be added to the backend API. If the backend does not support batch queries yet, this fix requires a server-side change.",
            "tags": ["n+1", "react-query"],
            "steps": [
                "Collapse the per-row fetch into a single GET /projects/stats?ids=...",
                "Use TanStack Query useQueries with a 60s staleTime to dedupe.",
            ],
            "references": [
                {"label": "TanStack Query — useQueries", "url": "https://tanstack.com/query/v5/docs/react/reference/useQueries"},
            ],
        },
        {
            "severity": "medium",
            "category": "Dependencies",
            "title": "axios pinned to a version with a known CVE",
            "summary": "package.json pins axios@0.21.1 which is impacted by CVE-2023-45857 (SSRF via redirect).",
            "impact": "Server-side calls that follow user-controlled URLs become SSRF-able.",
            "detectionReasoning": "package.json:28 pins `axios` at `0.21.1`. Cross-referenced with the NVD database: CVE-2023-45857 affects all axios versions below 1.6.0. The vulnerability allows SSRF through unvalidated redirect targets.",
            "reproductionPath": "If the application makes server-side HTTP requests using axios (e.g. via an API proxy), craft a request that returns a 302 redirect to an internal IP. Axios will follow the redirect without validation.",
            "file": "package.json",
            "line": 28,
            "confidence": 1.0,
            "confidenceRationale": "The pinned version and CVE are both unambiguous. The vulnerability is well-documented and the fix version is clearly specified in the advisory.",
            "autoFix": True,
            "effort": "1 min",
            "fixBenefits": "Eliminates the SSRF vector through redirect following. Also picks up 2 years of bug fixes and performance improvements in axios 1.x.",
            "fixRisks": "axios 1.x has breaking changes: the default Content-Type for POST requests changed, and the response interceptor signature is slightly different. Review any custom interceptors after upgrading.",
            "tags": ["cve", "npm"],
            "steps": [
                "Bump axios to ^1.6.7 in package.json.",
                "Run pnpm dedupe to flatten the transitive copy.",
            ],
            "references": [
                {"label": "CVE-2023-45857", "url": "https://nvd.nist.gov/vuln/detail/CVE-2023-45857"},
            ],
        },
    ]


# ── claude code provider (stub — wire when ready) ─────────────────────────


def _run_claude_code(*, repo: str, branch: str, commit: str, repo_path: str | None) -> list[dict]:
    """Subprocess the Claude Code CLI and parse its JSON response.

    Wire-up checklist:
      1. Install: `npm install -g @anthropic-ai/claude-code`.
      2. Log in with the Claude Code CLI as the OS user running Pullwise.
      3. Ensure the worker has GitHub App credentials; it clones the selected
         repository and passes repo_path before invoking review.
      4. Verify the CLI accepts the system prompt via `--append-system-prompt`
         on your installed version; adjust the flag if not.
    """
    if not repo_path:
        raise ValueError("Claude Code provider requires repo_path (a checked-out working tree).")

    cmd = [
        "claude",
        "--print",
        "--output-format", "json",
        "--append-system-prompt", SYSTEM_PROMPT,
        USER_PROMPT_TEMPLATE.format(repo=repo, branch=branch, commit=commit, repo_path=repo_path),
    ]
    completed = _run_cli_provider(
        provider_label="Claude Code",
        executable="claude",
        bin_env_var="PULLWISE_CLAUDE_BIN",
        login_command="claude login",
        cmd=cmd,
        cwd=repo_path,
    )
    return _parse_findings_json(completed.stdout)


# ── codex provider ────────────────────────────────────────────────────────


def _run_codex(*, repo: str, branch: str, commit: str, repo_path: str | None) -> list[dict]:
    """Subprocess the Codex CLI and parse its JSON response.

    Wire-up checklist:
      1. Install Codex per its docs and confirm `codex --version` works.
      2. Log in with the Codex CLI as the OS user running Pullwise.
      3. Ensure the worker has GitHub App credentials; it clones the selected
         repository and passes repo_path before invoking review.
      4. Uses official non-interactive `codex exec` mode with read-only
         sandboxing plus a JSON schema for the final response.
    """
    if not repo_path:
        raise ValueError("Codex provider requires repo_path (a checked-out working tree).")

    with tempfile.TemporaryDirectory(prefix="pullwise-codex-") as tmpdir:
        schema_path = os.path.join(tmpdir, "findings.schema.json")
        output_path = os.path.join(tmpdir, "findings.json")
        with open(schema_path, "w", encoding="utf-8") as schema_file:
            json.dump(_findings_schema(), schema_file)

        prompt = "\n\n".join(
            [
                SYSTEM_PROMPT,
                USER_PROMPT_TEMPLATE.format(repo=repo, branch=branch, commit=commit, repo_path=repo_path),
            ]
        )
        cmd = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--config",
            'model_reasoning_effort="xhigh"',
            "--sandbox",
            "read-only",
            "--output-schema",
            schema_path,
            "--output-last-message",
            output_path,
            prompt,
        ]
        completed = _run_cli_provider(
            provider_label="Codex",
            executable="codex",
            bin_env_var="PULLWISE_CODEX_BIN",
            login_command="codex login",
            cmd=cmd,
            cwd=repo_path,
        )
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as output_file:
                return _parse_findings_json(output_file.read())
        return _parse_findings_json(completed.stdout)


def _run_cli_provider(
    *,
    provider_label: str,
    executable: str,
    bin_env_var: str,
    login_command: str,
    cmd: list[str],
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    timeout = _review_timeout_seconds()
    resolved_cmd = [_resolve_cli_executable(executable, bin_env_var), *cmd[1:]]
    try:
        completed = subprocess.run(
            resolved_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReviewProviderError(
            f"{provider_label} CLI is not installed or not on PATH. "
            f"Install it, confirm `{executable} --version` works, then restart Pullwise."
        ) from exc
    except PermissionError as exc:
        cli_path = resolved_cmd[0]
        raise ReviewProviderError(
            f"{provider_label} CLI is not executable by the Pullwise service user: {cli_path}. "
            f"Set {bin_env_var} to a readable executable path, confirm `{executable} --version` "
            "works as the same OS user/session that runs Pullwise, then restart Pullwise."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        elapsed = int(exc.timeout or timeout)
        raise ReviewProviderError(
            f"{provider_label} review timed out after {elapsed} seconds. "
            "Increase PULLWISE_REVIEW_TIMEOUT_SECONDS or retry with a smaller repository."
        ) from exc

    if completed.returncode == 0:
        return completed

    detail = _cli_output_snippet(completed.stdout, completed.stderr)
    if _looks_like_cli_auth_failure(detail):
        raise ReviewProviderError(
            f"{provider_label} CLI is not authenticated. Run `{login_command}` "
            "as the same OS user/session that runs Pullwise, then retry the scan. "
            f"Detail: {detail}"
        )
    raise ReviewProviderError(
        f"{provider_label} review failed (exit {completed.returncode}). Detail: {detail}"
    )


def _review_timeout_seconds() -> int:
    try:
        timeout = int(os.environ.get("PULLWISE_REVIEW_TIMEOUT_SECONDS", "600"))
    except (TypeError, ValueError):
        return 600
    return timeout if timeout > 0 else 600


def _resolve_cli_executable(executable: str, bin_env_var: str) -> str:
    configured = os.environ.get(bin_env_var, "").strip()
    if configured:
        return configured
    return shutil.which(executable) or executable


def _cli_output_snippet(stdout: str | None, stderr: str | None) -> str:
    text = "\n".join(part.strip() for part in [stderr or "", stdout or ""] if part and part.strip())
    return (text or "no output")[:500]


def _looks_like_cli_auth_failure(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "not logged in",
        "not authenticated",
        "authentication",
        "unauthorized",
        "login required",
        "please login",
        "please log in",
        "api key",
    ]
    return any(marker in lowered for marker in markers)


def _findings_schema() -> dict:
    code_line = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ln": {"type": "integer"},
            "code": {"type": "string"},
            "t": {"type": ["string", "null"], "enum": ["del", "add", None]},
        },
        "required": ["ln", "code", "t"],
    }
    reference = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string"},
            "url": {"type": "string"},
        },
        "required": ["label", "url"],
    }
    finding_properties = {
        "id": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
        "category": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "impact": {"type": "string"},
        "detectionReasoning": {"type": "string"},
        "reproductionPath": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "confidence": {"type": "number"},
        "confidenceRationale": {"type": "string"},
        "autoFix": {"type": "boolean"},
        "effort": {"type": "string"},
        "fixBenefits": {"type": "string"},
        "fixRisks": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "badCode": {"type": "array", "items": code_line},
        "goodCode": {"type": "array", "items": code_line},
        "references": {"type": "array", "items": reference},
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": finding_properties,
        "required": list(finding_properties),
    }
    return {
        "type": "object",
        "required": ["findings"],
        "additionalProperties": False,
        "properties": {"findings": {"type": "array", "items": finding, "maxItems": 25}},
    }


def _parse_findings_json(raw: str) -> list[dict]:
    """Tolerate output wrapped in a code fence or with leading log lines."""
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        document = _first_findings_document(text)
    except json.JSONDecodeError as exc:
        raise ReviewProviderError(
            "Review provider did not return valid JSON findings. "
            f"Detail: {_cli_output_snippet(text, None)}"
        ) from exc
    findings = document.get("findings") if isinstance(document, dict) else None
    return findings if isinstance(findings, list) else []


def _first_findings_document(text: str) -> dict:
    decoder = json.JSONDecoder()
    first_error = None
    malformed_findings_document = None
    index = 0
    while index < len(text):
        index = text.find("{", index)
        if index < 0:
            break
        try:
            document, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            index += 1
            continue
        if _is_findings_document(document):
            return document
        if _is_malformed_findings_document(document) and malformed_findings_document is None:
            malformed_findings_document = document
        index += max(1, end)
    if malformed_findings_document is not None:
        return malformed_findings_document
    if first_error:
        raise first_error
    raise json.JSONDecodeError("No JSON object found", text, 0)


def _is_findings_document(document: object) -> bool:
    if not isinstance(document, dict):
        return False
    if "event" in document:
        return False
    return isinstance(document.get("findings"), list)


def _is_malformed_findings_document(document: object) -> bool:
    return isinstance(document, dict) and "event" not in document and "findings" in document
