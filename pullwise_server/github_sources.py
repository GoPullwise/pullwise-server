from __future__ import annotations

import re
from typing import Mapping, Sequence
from urllib.parse import quote

import requests

from .product_domain import formal_review_actions


_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def resolve_upstream_repository(
    owner: str,
    repository: str,
    *,
    token: str = "",
) -> dict:
    if not isinstance(owner, str) or not _REPOSITORY_PART.fullmatch(owner.strip()):
        raise ValueError("INVALID_UPSTREAM_REPOSITORY")
    if not isinstance(repository, str) or not _REPOSITORY_PART.fullmatch(repository.strip()):
        raise ValueError("INVALID_UPSTREAM_REPOSITORY")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Pullwise/1.4",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    url = f"https://api.github.com/repos/{quote(owner.strip(), safe='')}/{quote(repository.strip(), safe='')}"
    response = requests.get(url, headers=headers, timeout=(5, 10), allow_redirects=False)
    if response.status_code == 404:
        raise ValueError("GITHUB_REPOSITORY_NOT_FOUND")
    if response.status_code in {401, 403}:
        raise ValueError("GITHUB_REPOSITORY_UNAVAILABLE")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping) or isinstance(payload.get("id"), bool) or payload.get("id") is None:
        raise ValueError("GITHUB_REPOSITORY_INVALID_RESPONSE")
    return {
        "id": f"github:{payload['id']}",
        "private": bool(payload.get("private")),
        "fullName": _text(payload.get("full_name")) or f"{owner.strip()}/{repository.strip()}",
    }


def _identifier(value: object, field: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a stable identifier")
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a stable identifier")
    return text


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _actor(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    actor_id = value.get("id")
    if isinstance(actor_id, bool) or actor_id is None:
        return None
    return {"githubId": str(actor_id), "login": _text(value.get("login"))}


def pr_review_source(
    *,
    repository_id: str,
    pull_number: int,
    review: Mapping[str, object],
) -> dict:
    repository_id = _identifier(repository_id, "repository_id")
    if isinstance(pull_number, bool) or not isinstance(pull_number, int) or pull_number < 1:
        raise ValueError("pull_number must be a positive integer")
    review_id = _identifier(review.get("id"), "review.id")
    state = _text(review.get("state")).strip().upper()
    body = _text(review.get("body"))
    actions = formal_review_actions(review_state=state, body=body)
    return {
        "sourceId": f"source_pr_review_body_{review_id}",
        "sourceType": "pr_review_body",
        "externalKey": f"github:pr_review_body:{review_id}",
        "repositoryId": repository_id,
        "content": {"body": body},
        "sourceFacts": {
            "pullNumber": pull_number,
            "reviewId": review_id,
            "reviewState": state,
            "reviewer": _actor(review.get("user")),
            "submittedAt": review.get("submitted_at"),
            "updatedAt": review.get("updated_at"),
        },
        "sourceUrl": _text(review.get("html_url")),
        "processingMode": "rules_only" if not body.strip() else "model",
        "completeness": "complete",
        "lifecycle": "active",
        "ruleActions": list(actions),
    }


def pr_issue_comment_source(
    *,
    repository_id: str,
    issue: Mapping[str, object],
    comment: Mapping[str, object],
) -> dict | None:
    if not isinstance(issue.get("pull_request"), Mapping):
        return None
    repository_id = _identifier(repository_id, "repository_id")
    pull_number = issue.get("number")
    if isinstance(pull_number, bool) or not isinstance(pull_number, int) or pull_number < 1:
        raise ValueError("issue.number must identify a pull request")
    comment_id = _identifier(comment.get("id"), "comment.id")
    return {
        "sourceId": f"source_pr_comment_{comment_id}",
        "sourceType": "pr_comment",
        "externalKey": f"github:pr_comment:{comment_id}",
        "repositoryId": repository_id,
        "content": {"body": _text(comment.get("body"))},
        "sourceFacts": {
            "pullNumber": pull_number,
            "commentId": comment_id,
            "author": _actor(comment.get("user")),
            "createdAt": comment.get("created_at"),
            "updatedAt": comment.get("updated_at"),
        },
        "sourceUrl": _text(comment.get("html_url")),
        "processingMode": "model",
        "completeness": "complete",
        "lifecycle": "active",
        "ruleActions": [],
    }


def ci_failure_source(
    *,
    repository_id: str,
    run: Mapping[str, object],
    job: Mapping[str, object],
    evidence_windows: Sequence[Mapping[str, object]],
) -> dict | None:
    conclusion = _text(job.get("conclusion")).strip().lower()
    if conclusion not in {"failure", "timed_out"}:
        return None
    repository_id = _identifier(repository_id, "repository_id")
    run_id = _identifier(run.get("id"), "run.id")
    run_attempt = run.get("run_attempt")
    job_id = _identifier(job.get("id"), "job.id")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
        raise ValueError("run.run_attempt must be a positive integer")
    windows = []
    for window in evidence_windows:
        window_id = _identifier(window.get("windowId"), "windowId")
        windows.append(
            {
                "windowId": window_id,
                "stepNumber": window.get("stepNumber"),
                "stepName": _text(window.get("stepName")),
                "stage": _text(window.get("stage")) or "unknown",
                "stageRuleVersion": _text(window.get("stageRuleVersion")),
                "text": _text(window.get("text")),
                "symptoms": [],
                "evidenceIds": [],
            }
        )
        for field in ("startLine", "endLine", "selectionRuleVersion", "coverage"):
            if field in window:
                windows[-1][field] = window[field]
    return {
        "sourceId": f"source_ci_{repository_id}_{run_id}_{run_attempt}_{job_id}",
        "sourceType": "ci_failure",
        "externalKey": f"github:ci_failure:{repository_id}:{run_id}:{run_attempt}:{job_id}",
        "repositoryId": repository_id,
        "content": {"windows": windows},
        "sourceFacts": {
            "runId": run_id,
            "runAttempt": run_attempt,
            "jobId": job_id,
            "workflowId": str(run.get("workflow_id") or ""),
            "headSha": _text(run.get("head_sha")),
            "jobName": _text(job.get("name")),
            "conclusion": conclusion,
            "completedAt": job.get("completed_at"),
            "steps": list(job.get("steps") or []),
            "windows": windows,
            "recovery": None,
            "recoveryStatus": "unknown",
            "recoveryReason": "identity_support_not_verified",
        },
        "sourceUrl": _text(job.get("html_url")) or _text(run.get("html_url")),
        "processingMode": "model",
        "completeness": "partial" if windows else "unavailable",
        "lifecycle": "active",
        "ruleActions": ["investigate_failure"],
    }


def release_source(*, upstream_repository_id: str, release: Mapping[str, object]) -> dict:
    upstream_repository_id = _identifier(upstream_repository_id, "upstream_repository_id")
    release_id = _identifier(release.get("id"), "release.id")
    lifecycle = "source_closed" if bool(release.get("draft")) else "active"
    return {
        "sourceId": f"source_release_{upstream_repository_id}_{release_id}",
        "sourceType": "release",
        "externalKey": f"github:release:{upstream_repository_id}:{release_id}",
        "repositoryId": upstream_repository_id,
        "content": {
            "tagName": _text(release.get("tag_name")),
            "name": _text(release.get("name")),
            "body": _text(release.get("body")),
        },
        "sourceFacts": {
            "releaseId": release_id,
            "tagName": _text(release.get("tag_name")),
            "name": _text(release.get("name")),
            "publishedAt": release.get("published_at"),
            "updatedAt": release.get("updated_at"),
            "draft": bool(release.get("draft")),
            "prerelease": bool(release.get("prerelease")),
        },
        "sourceUrl": _text(release.get("html_url")),
        "processingMode": "model",
        "completeness": "complete",
        "lifecycle": lifecycle,
        "ruleActions": [],
    }
