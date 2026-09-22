"""Bounded Actions metadata reader with injected HTTP and credential resolution.

Each call reads one page of jobs in one explicit run attempt. Success is native
run state, never a failure source or recovery proof. Optional injected log reads
use one-job pages; default metadata-only failures retain unavailable evidence.
REST references: https://docs.github.com/en/rest/actions/workflow-runs and
https://docs.github.com/en/rest/actions/workflow-jobs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .github_sources import ci_failure_source
from .github_ci_logs import CILogResult, redact_ci_log, select_ci_log_windows
from .github_transport import GitHubResponse, GitHubUnavailable
from .product_discovery import FactPage


_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_STATUSES = {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
_CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale", "startup_failure"}


def _invalid():
    raise ValueError("GITHUB_CI_RESPONSE_INVALID")


def _id(value):
    if type(value) not in (int, str) or not _ID.fullmatch(str(value)):
        _invalid()
    return str(value)


def _positive(value):
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        _invalid()
    return value


def _time(value, *, required=False):
    if value is None and not required:
        return None
    try:
        if not isinstance(value, str) or len(value) > 40:
            _invalid()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _invalid()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        _invalid()


def _status(record):
    if (not isinstance(record.get("status"), str) or record["status"] not in _STATUSES
            or (record.get("conclusion") is not None and not isinstance(record["conclusion"], str))
            or record.get("conclusion") not in _CONCLUSIONS | {None}
            or (record["status"] != "completed" and record.get("conclusion") is not None)
            or (record["status"] == "completed" and record.get("conclusion") is None)):
        _invalid()


class GitHubCIReader:
    def __init__(self, *, get_json: Callable, token_for_target: Callable, page_size: int = 100,
                 log_reader: Callable | None = None):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("GITHUB_CI_PAGE_SIZE_INVALID")
        self.get_json = get_json
        self.token_for_target = token_for_target
        self.page_size = page_size
        if log_reader is not None and page_size != 1:
            raise ValueError("GITHUB_CI_LOG_PAGE_LIMIT")
        self.log_reader = log_reader

    def _get(self, path, token):
        try:
            response = self.get_json(path, token=token)
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_CI_UNAVAILABLE") from None
        if not isinstance(response, GitHubResponse) or response.status != 200:
            raise GitHubUnavailable("GITHUB_CI_UNAVAILABLE")
        return response

    @staticmethod
    def _repository(payload, repository_id):
        if (not isinstance(payload, Mapping) or type(payload.get("id")) is not int
                or _id(payload["id"]) != repository_id or type(payload.get("private")) is not bool
                or not isinstance(payload.get("full_name"), str) or not _NAME.fullmatch(payload["full_name"])
                or any(part in {".", ".."} for part in payload["full_name"].split("/"))):
            _invalid()
        return payload["full_name"]

    @staticmethod
    def _next(headers, path, canonical, size, page):
        if not isinstance(headers, Mapping):
            _invalid()
        link = headers.get("link", "")
        if not isinstance(link, str) or len(link) > 8192:
            _invalid()
        following = None
        for part in link.split(","):
            if not part.strip():
                continue
            match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
            if match is None:
                _invalid()
            # Validate every URL, even a non-followed relation.
            try:
                url = urlsplit(match[1])
                query = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
                if (url.scheme != "https" or url.netloc != "api.github.com" or url.fragment
                        or url.path not in {path, canonical} or set(query) != {"page", "per_page"}
                        or query["per_page"] != [str(size)] or len(query["page"]) != 1
                        or not _ID.fullmatch(query["page"][0])):
                    _invalid()
                _positive(int(query["page"][0]))
                if match[2] == "next":
                    if following is not None or query["page"] != [str(page + 1)]:
                        _invalid()
                    following = _positive(page + 1)
            except (ValueError, TypeError):
                _invalid()
        return following

    @staticmethod
    def _records(payload, key, size):
        if (not isinstance(payload, Mapping) or not isinstance(payload.get(key), list)
                or len(payload[key]) > size or type(payload.get("total_count")) is not int
                or payload["total_count"] < len(payload[key])):
            _invalid()
        return payload[key]

    @staticmethod
    def _run(run, repository_id, run_id=None, attempt=None):
        if not isinstance(run, Mapping) or not isinstance(run.get("repository"), Mapping):
            _invalid()
        if (_id(run["repository"].get("id")) != repository_id
                or (run_id is not None and _id(run.get("id")) != run_id)
                or (attempt is not None and run.get("run_attempt") != attempt)):
            _invalid()
        _id(run.get("id"))
        _positive(run.get("run_attempt"))
        _id(run.get("workflow_id"))
        _time(run.get("updated_at"), required=True)
        _status(run)
        if not isinstance(run.get("head_sha"), str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", run["head_sha"]):
            _invalid()
        pulls = run.get("pull_requests")
        if not isinstance(pulls, list) or len(pulls) > 100:
            _invalid()
        normalized = []
        for pull in pulls:
            if not isinstance(pull, Mapping):
                _invalid()
            number = _positive(pull.get("number"))
            shas = []
            for name in ("head", "base"):
                ref = pull.get(name)
                if not isinstance(ref, Mapping) or not isinstance(ref.get("sha"), str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", ref["sha"]):
                    _invalid()
                shas.append(ref["sha"])
            normalized.append({"number": number, "headSha": shas[0], "baseSha": shas[1]})
        return normalized

    @staticmethod
    def _job(job, run_id, attempt, full_name):
        if not isinstance(job, Mapping) or _id(job.get("run_id")) != run_id:
            _invalid()
        _id(job.get("id"))
        if "run_attempt" in job and _positive(job["run_attempt"]) != attempt:
            _invalid()
        _status(job)
        if not isinstance(job.get("name"), str) or len(job["name"]) > 4096:
            _invalid()
        completed = _time(job.get("completed_at"), required=job["status"] == "completed")
        steps = job.get("steps")
        if not isinstance(steps, list) or len(steps) > 1000:
            _invalid()
        normalized, numbers = [], set()
        for step in steps:
            if not isinstance(step, Mapping):
                _invalid()
            number = _positive(step.get("number"))
            if number in numbers or not isinstance(step.get("name"), str) or len(step["name"]) > 4096:
                _invalid()
            numbers.add(number)
            _status(step)
            value = {key: step[key] for key in ("number", "name", "status", "conclusion") if key in step}
            for key in ("started_at", "completed_at"):
                if key in step:
                    value[key] = _time(step[key])
            normalized.append(value)
        # Construct a stable GitHub page URL, never preserve arbitrary payload links.
        return {"id": job["id"], "run_id": job["run_id"], "name": job["name"],
                "status": job["status"], "conclusion": job.get("conclusion"), "completed_at": completed,
                "steps": normalized, "html_url": f"https://github.com/{full_name}/actions/runs/{run_id}/job/{job['id']}"}

    def read_page(self, *, target: Mapping, cursor: str | None,
                  high_watermark: str | None, event: Mapping | None) -> FactPage:
        if (target.get("module") != "ci" or not isinstance(target.get("control_key"), str)
                or not target["control_key"] or not isinstance(target.get("repository_id"), str)
                or not target["repository_id"]):
            raise ValueError("GITHUB_CI_BINDING_INVALID")
        repository_id = _id(target.get("github_repository_id"))
        watermark = _time(high_watermark, required=True) if high_watermark else ""
        scope = hashlib.sha256(json.dumps([target["control_key"], target["repository_id"], repository_id,
                                          self.page_size], separators=(",", ":")).encode()).hexdigest()
        position = {"runPage": 1, "runId": None, "attempt": None, "jobsPage": 1, "nextRunPage": None}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                saved = json.loads(cursor)
                if (not isinstance(saved, dict) or set(saved) != {"scope", "watermark", "position"}
                        or saved["scope"] != scope or saved["watermark"] != watermark
                        or not isinstance(saved["position"], dict) or set(saved["position"]) != set(position)):
                    raise ValueError
                position = saved["position"]
                _positive(position["runPage"])
                _positive(position["jobsPage"])
                if position["runId"] is not None:
                    _id(position["runId"])
                    _positive(position["attempt"])
                elif position["attempt"] is not None or position["jobsPage"] != 1 or position["nextRunPage"] is not None:
                    raise ValueError
                if position["nextRunPage"] is not None and position["nextRunPage"] != position["runPage"] + 1:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("GITHUB_CI_CURSOR_INVALID") from None
        resource_id = None
        if event is not None:
            if (cursor is not None or event.get("event") not in {"workflow_run", "workflow_job"}
                    or _id(event.get("repository_id")) != repository_id):
                raise ValueError("GITHUB_CI_BINDING_INVALID")
            resource_id = _id(event.get("resource_id"))
        try:
            token = self.token_for_target(dict(target))
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_CI_UNAVAILABLE") from None
        if not isinstance(token, str) or not token:
            raise GitHubUnavailable("GITHUB_CI_UNAVAILABLE")
        repository = self._get(f"/repositories/{repository_id}", token).payload
        full_name = self._repository(repository, repository_id)
        base, canonical = f"/repos/{full_name}/actions", f"/repositories/{repository_id}/actions"
        expected_job = None
        direct_job = None
        run = None
        if event is not None:
            if event["event"] == "workflow_job":
                job = self._get(f"{base}/jobs/{resource_id}", token).payload
                if not isinstance(job, Mapping) or _id(job.get("id")) != resource_id:
                    _invalid()
                run_id = _id(job.get("run_id"))
                expected_job = resource_id
            else:
                run_id = resource_id
            run = self._get(f"{base}/runs/{run_id}", token).payload
            self._run(run, repository_id, run_id)
            position["runId"], position["attempt"] = run_id, run["run_attempt"]
            if expected_job and "run_attempt" in job:
                position["attempt"] = _positive(job["run_attempt"])
                direct_job = job
        elif position["runId"] is None:
            response = self._get(f"{base}/runs?per_page=1&page={position['runPage']}", token)
            runs = self._records(response.payload, "workflow_runs", 1)
            position["nextRunPage"] = self._next(response.headers, f"{base}/runs", f"{canonical}/runs", 1, position["runPage"])
            if runs:
                run = runs[0]
                self._run(run, repository_id)
                position["runId"], position["attempt"] = _id(run["id"]), run["run_attempt"]
            elif position["nextRunPage"] is not None:
                _invalid()
        sources, states = [], []
        next_position = None
        if position["runId"] is not None:
            run_id, attempt = position["runId"], position["attempt"]
            run = self._get(f"{base}/runs/{run_id}/attempts/{attempt}", token).payload
            pulls = self._run(run, repository_id, run_id, attempt)
            jobs_path = f"/runs/{run_id}/attempts/{attempt}/jobs"
            if direct_job is not None:
                # The job endpoint itself binds this job to a concrete attempt.
                # A singleton event snapshot makes no claim about the other jobs.
                jobs, next_jobs = [direct_job], None
            else:
                response = self._get(f"{base}{jobs_path}?per_page={self.page_size}&page={position['jobsPage']}", token)
                jobs = self._records(response.payload, "jobs", self.page_size)
                next_jobs = self._next(response.headers, base + jobs_path, canonical + jobs_path, self.page_size, position["jobsPage"])
                if next_jobs is None and response.payload["total_count"] > (position["jobsPage"] - 1) * self.page_size + len(jobs):
                    _invalid()
            normalized, ids = [], set()
            for job in jobs:
                value = self._job(job, run_id, attempt, full_name)
                job_id = _id(value["id"])
                if job_id in ids:
                    _invalid()
                ids.add(job_id)
                normalized.append({"jobId": job_id, "name": value["name"], "status": value["status"],
                                   "conclusion": value["conclusion"], "completedAt": value["completed_at"], "steps": value["steps"]})
                source = ci_failure_source(repository_id=target["repository_id"], run=run, job=value, evidence_windows=[])
                if source is not None:
                    if self.log_reader is not None:
                        try:
                            log = self.log_reader(full_name, job_id, token=token)
                            if not isinstance(log, CILogResult) or len(log.text.encode("utf-8")) > 5 * 1024 * 1024:
                                raise ValueError("INVALID_LOG_RESULT")
                            log = CILogResult(text=redact_ci_log(log.text), coverage=log.coverage)
                            windows = select_ci_log_windows(log, job_id=job_id)
                        except GitHubUnavailable as error:
                            raise GitHubUnavailable("GITHUB_CI_LOG_UNAVAILABLE", retry_at=error.retry_at) from None
                        except Exception:
                            windows = []
                        source = ci_failure_source(repository_id=target["repository_id"], run=run, job=value,
                                                   evidence_windows=windows)
                        source["sourceFacts"]["logCoverage"] = {
                            "state": "partial" if windows else "unavailable",
                            "selectionRuleVersion": "ci-log-tail/v1",
                            "reason": None if windows else "selection_empty_or_unavailable",
                        }
                    sources.append(source)
            if expected_job is not None and expected_job not in ids:
                # Cannot prove membership in this bounded attempt page; retry safely.
                raise GitHubUnavailable("GITHUB_CI_JOB_MEMBERSHIP_UNAVAILABLE")
            updated = _time(run["updated_at"], required=True)
            if not watermark or datetime.fromisoformat(updated.replace("Z", "+00:00")) > datetime.fromisoformat(watermark.replace("Z", "+00:00")):
                watermark = updated
            states.append({"repositoryId": target["repository_id"], "runId": run_id, "runAttempt": attempt,
                           "status": run["status"], "conclusion": run.get("conclusion"), "workflowId": _id(run["workflow_id"]),
                           "headSha": run["head_sha"], "updatedAt": updated, "pullRequests": pulls, "jobs": normalized,
                           "coverage": {"jobsComplete": direct_job is None and position["jobsPage"] == 1 and next_jobs is None,
                                        "jobsPage": position["jobsPage"], "nextJobsPage": next_jobs}})
            if event is None:
                if next_jobs is not None:
                    next_position = dict(position, jobsPage=next_jobs)
                elif attempt > 1:
                    next_position = dict(position, attempt=attempt - 1, jobsPage=1)
                elif position["nextRunPage"] is not None:
                    next_position = {"runPage": position["nextRunPage"], "runId": None, "attempt": None, "jobsPage": 1, "nextRunPage": None}
        after = self._get(f"/repos/{full_name}", token).payload
        if self._repository(after, repository_id) != full_name or after["private"] != repository["private"]:
            _invalid()
        next_cursor = None if next_position is None else json.dumps(
            {"scope": scope, "watermark": watermark, "position": next_position}, separators=(",", ":"))
        return FactPage(tuple(sources), next_cursor, watermark, run_states=tuple(states))
