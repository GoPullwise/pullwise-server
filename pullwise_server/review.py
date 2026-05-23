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
import os
import secrets
import shutil
import subprocess
import tempfile
import time


CHECKOUT_PROVIDERS = {"claude_code", "codex"}
DEFAULT_PROVIDER = "disabled"


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
      "file": "repo-relative path; '' if cross-cutting",
      "line": <integer, 1-indexed; 0 if multi-file>,
      "confidence": <float 0.0..1.0>,
      "autoFix": <bool>,
      "effort": "<rough estimate, e.g. '5 min', '1 hour'>",
      "tags": ["<kebab-case>", ...],
      "steps": ["<concrete action>", ...],
      "badCode":  [ {"ln": <int>, "code": "<line>", "t": "del" | null} ],
      "goodCode": [ {"ln": <int>, "code": "<line>", "t": "add" | null} ],
      "references": [ {"label": "<short>", "url": "<url>"} ]
    }
  ]
}

`badCode`, `goodCode`, `references` are optional. Include `badCode`/`goodCode`
only when `autoFix` is true and you can produce a deterministic patch.

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
    chosen = selected_provider(provider)
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

    return [
        _finalize_finding(finding, user_id=user_id, scan_id=scan_id, repo=repo)
        for finding in raw
    ]


def selected_provider(provider: str | None = None) -> str:
    return (provider or os.environ.get("PULLWISE_REVIEW_PROVIDER", DEFAULT_PROVIDER)).strip().lower()


def provider_requires_checkout(provider: str | None = None) -> bool:
    return selected_provider(provider) in CHECKOUT_PROVIDERS


def _finalize_finding(finding: dict, *, user_id: str, scan_id: str, repo: str) -> dict:
    return {
        "id": finding.get("id") or f"f_{secrets.token_urlsafe(6)}",
        "userId": user_id,
        "scanId": scan_id,
        "repo": repo,
        "status": "open",
        "severity": finding.get("severity") or "medium",
        "category": finding.get("category") or "Quality",
        "title": finding.get("title") or "Untitled finding",
        "summary": finding.get("summary") or "",
        "impact": finding.get("impact") or "",
        "file": finding.get("file") or "",
        "line": int(finding.get("line") or 0),
        "confidence": float(finding.get("confidence") or 0.7),
        "autoFix": bool(finding.get("autoFix")),
        "effort": finding.get("effort") or "—",
        "tags": list(finding.get("tags") or []),
        "steps": list(finding.get("steps") or []),
        "badCode": list(finding.get("badCode") or []),
        "goodCode": list(finding.get("goodCode") or []),
        "references": list(finding.get("references") or []),
        "createdAt": int(time.time()),
    }


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
            "file": "lib/payments.ts",
            "line": 14,
            "confidence": 0.97,
            "autoFix": True,
            "effort": "5 min",
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
            "file": "src/screens/dashboard.tsx",
            "line": 88,
            "confidence": 0.86,
            "autoFix": False,
            "effort": "20 min",
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
            "file": "package.json",
            "line": 28,
            "confidence": 1.0,
            "autoFix": True,
            "effort": "1 min",
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
    timeout = int(os.environ.get("PULLWISE_REVIEW_TIMEOUT_SECONDS", "600"))
    resolved_cmd = [_resolve_cli_executable(executable, bin_env_var), *cmd[1:]]
    try:
        completed = subprocess.run(
            resolved_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReviewProviderError(
            f"{provider_label} CLI is not installed or not on PATH. "
            f"Install it, confirm `{executable} --version` works, then restart Pullwise."
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
    finding = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "id": {"type": "string"},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
            "category": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "impact": {"type": "string"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "confidence": {"type": "number"},
            "autoFix": {"type": "boolean"},
            "effort": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "string"}},
        },
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
    open_brace = text.find("{")
    if open_brace > 0:
        text = text[open_brace:]
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewProviderError(
            "Review provider did not return valid JSON findings. "
            f"Detail: {_cli_output_snippet(text, None)}"
        ) from exc
    findings = document.get("findings") if isinstance(document, dict) else None
    return findings if isinstance(findings, list) else []
