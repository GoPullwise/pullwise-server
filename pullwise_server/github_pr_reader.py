"""Bounded, injected REST PR fact reader (not a live runtime composition).

Each call reads one fact page, bracketed by repository identity verification.
Polling traverses open PRs in pages of twenty, then each PR's reviews and two
comment collections. This is incremental coverage, never an atomic snapshot.
An optional GraphQL reader enriches matching inline comments with thread facts.
It reads only the first bounded thread page; its continuation cursor is not yet
joined to the REST scan. Unmatched comments retain unknown thread state.
Review submitted_at is a creation fact, not an edit clock. No model work occurs.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .github_release_reader import GitHubReleaseReader, _id, _time
from .github_pr_threads import PRThreadPage
from .github_sources import _actor, pr_issue_comment_source, pr_review_source
from .github_transport import GitHubUnavailable
from .product_discovery import FactPage


_STAGES = ("pulls", "reviews", "discussion", "inline")
_EVENTS = {"pull_request": "pulls", "pull_request_review": "reviews",
           "issue_comment": "discussion", "pull_request_review_comment": "inline"}


def _invalid():
    raise ValueError("GITHUB_PR_RESPONSE_INVALID")


def _positive(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        _invalid()
    return value


class GitHubPRReader(GitHubReleaseReader):
    def __init__(self, *, get_json: Callable, token_for_target: Callable, page_size: int = 100,
                 thread_reader=None):
        super().__init__(get_json=get_json, token_for_target=token_for_target, page_size=page_size)
        self.thread_reader = thread_reader

    def _enrich_threads(self, sources, repository_id, name, number, token):
        if self.thread_reader is None or not sources:
            return
        owner, repo_name = name.split("/", 1)
        try:
            page = self.thread_reader.read(owner=owner, name=repo_name,
                github_repository_id=repository_id, pull_number=number, token=token, after=None)
            if (not isinstance(page, PRThreadPage) or page.github_repository_id != repository_id
                    or type(page.pull_number) is not int or page.pull_number != number
                    or page.coverage not in {"complete", "partial"}
                    or not isinstance(page.comment_facts, Mapping)):
                raise ValueError
            updates = []
            for source in sources:
                cid = source["externalKey"].removeprefix("github:pr_review_comment:")
                facts = page.comment_facts.get(cid)
                if facts is None:
                    continue
                if (not isinstance(facts, Mapping) or facts.get("associationVerified") is not True
                        or not isinstance(facts.get("threadId"), str) or not facts["threadId"]
                        or type(facts.get("isResolved")) is not bool or type(facts.get("isOutdated")) is not bool
                        or facts.get("threadCoverage") not in {"complete", "partial"}
                        or "inReplyToId" not in facts
                        or facts["inReplyToId"] != source["sourceFacts"]["inReplyToId"]):
                    raise ValueError
                update = {key: facts[key] for key in ("threadId", "isResolved", "isOutdated",
                    "inReplyToId", "threadCoverage", "associationVerified")}
                if page.coverage == "partial":
                    update["threadCoverage"] = "partial"
                updates.append((source, update))
        except GitHubUnavailable as error:
            if error.retry_at is not None:
                raise
            updates = None
        except Exception:
            updates = None
        if updates is None:
            for source in sources:
                source["sourceFacts"]["threadCoverage"] = "thread_unavailable"
            return
        for source, facts in updates:
            source["sourceFacts"].update(facts)

    def _link(self, headers, path, canonical, query, page):
        link = headers.get("link", "") if isinstance(headers, Mapping) else None
        if not isinstance(link, str) or len(link) > 8192:
            raise ValueError("GITHUB_PR_PAGINATION_INVALID")
        result = None
        for part in link.split(","):
            if not part.strip():
                continue
            match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
            if match is None:
                raise ValueError("GITHUB_PR_PAGINATION_INVALID")
            if match[2] != "next":
                continue
            try:
                url = urlsplit(match[1])
                params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                expected = dict(query, page=[str(page + 1)])
                if (result is not None or url.scheme != "https" or url.netloc != "api.github.com"
                        or url.path not in {path, canonical} or url.fragment or params != expected
                        or page >= 2**31 - 1):
                    raise ValueError
            except ValueError:
                raise ValueError("GITHUB_PR_PAGINATION_INVALID") from None
            result = page + 1
        return result

    @staticmethod
    def _record(record, full_name, number=None):
        if not isinstance(record, Mapping):
            _invalid()
        _positive(record.get("id"))
        for key in ("body", "title"):
            if record.get(key) is not None and not isinstance(record[key], str):
                _invalid()
        url = record.get("html_url")
        prefix = f"https://github.com/{full_name}/pull/"
        if not isinstance(url, str) or not url.startswith(prefix):
            _invalid()
        tail = url[len(prefix):]
        if not re.fullmatch(r"[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?", tail):
            _invalid()
        if number is not None and int(tail.split("#")[0]) != number:
            _invalid()

    def _pull(self, record, target, name):
        self._record(record, name)
        number = _positive(record.get("number"))
        self._record(record, name, number)
        if (record.get("state") not in {"open", "closed"} or type(record.get("draft")) is not bool
                or not isinstance(record.get("base"), Mapping)
                or not isinstance(record["base"].get("repo"), Mapping)
                or str(record["base"]["repo"].get("id")) != str(target["github_repository_id"])):
            _invalid()
        reviewers, teams = record.get("requested_reviewers"), record.get("requested_teams")
        if not isinstance(reviewers, list) or not isinstance(teams, list):
            _invalid()
        if any(not isinstance(actor, Mapping) or type(actor.get("id")) is not int or actor["id"] < 1 for actor in reviewers + teams):
            _invalid()
        updated = _time(record.get("updated_at"))
        if updated is None:
            _invalid()
        merged = _time(record.get("merged_at"))
        rid = str(record["id"])
        return {"sourceId": f"source_pr_state_{rid}", "sourceType": "pr_state",
                "externalKey": f"github:pr_state:{rid}", "repositoryId": target["repository_id"],
                "content": {"title": record.get("title") or "", "body": record.get("body") or ""},
                "sourceFacts": {"pullId": rid, "pullNumber": number, "state": record["state"],
                    "draft": record["draft"], "mergedAt": merged, "updatedAt": updated,
                    "author": _actor(record.get("user")), "headSha": (record.get("head") or {}).get("sha"),
                    "requestedReviewers": [_actor(actor) for actor in reviewers],
                    "requestedTeams": [{"githubId": str(actor["id"]), "slug": actor.get("slug", "")} for actor in teams],
                    "coverage": {"reviewThreads": "unavailable", "collections": "incremental"}},
                "sourceUrl": record["html_url"], "processingMode": "rules_only", "completeness": "partial",
                "lifecycle": "source_closed" if record["state"] == "closed" else "active", "ruleActions": []}

    def _comment(self, record, stage, target, name, number):
        self._record(record, name, number)
        key = "issue_url" if stage == "discussion" else "pull_request_url"
        segment = "issues" if stage == "discussion" else "pulls"
        if record.get(key) != f"https://api.github.com/repos/{name}/{segment}/{number}":
            _invalid()
        clean = dict(record)
        if stage == "reviews":
            if record.get("state") not in {"PENDING", "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                _invalid()
            clean.update(submitted_at=_time(record.get("submitted_at")), updated_at=None)
            source = pr_review_source(repository_id=target["repository_id"], pull_number=number, review=clean)
            source["sourceFacts"]["formalReviewStatus"] = "unknown"
            # A page cannot establish whether a later review supersedes this one.
            source["sourceFacts"]["reviewHistoryCoverage"] = "incremental"
            source["ruleActions"] = []
            return source
        clean.update(created_at=_time(record.get("created_at")), updated_at=_time(record.get("updated_at")))
        if clean["created_at"] is None or clean["updated_at"] is None:
            _invalid()
        source = pr_issue_comment_source(repository_id=target["repository_id"],
                                        issue={"number": number, "pull_request": {}}, comment=clean)
        if stage == "inline":
            cid = str(record["id"])
            source.update(sourceId=f"source_pr_review_comment_{cid}", sourceType="pr_review_comment",
                          externalKey=f"github:pr_review_comment:{cid}", completeness="partial")
            reply = record.get("in_reply_to_id")
            if reply is not None:
                _positive(reply)
            review = record.get("pull_request_review_id")
            if review is not None:
                _positive(review)
            source["sourceFacts"].update(threadId=None, isResolved=None, isOutdated=None,
                threadCoverage="unavailable_rest", inReplyToId=str(reply) if reply else None,
                reviewId=str(review) if review else None, path=record.get("path"),
                diffHunk=record.get("diff_hunk"), commitId=record.get("commit_id"),
                originalCommitId=record.get("original_commit_id"), line=record.get("line"),
                originalLine=record.get("original_line"))
        return source

    def read_page(self, *, target: Mapping, cursor: str | None, high_watermark: str | None,
                  event: Mapping | None) -> FactPage:
        if (target.get("module") != "pr" or not isinstance(target.get("control_key"), str)
                or not target["control_key"] or not isinstance(target.get("repository_id"), str)
                or not target["repository_id"]):
            raise ValueError("GITHUB_PR_BINDING_INVALID")
        repository_id = _id(target.get("github_repository_id"))
        watermark = _time(high_watermark) if high_watermark else ""
        scope = hashlib.sha256(json.dumps([target["control_key"], target["repository_id"], repository_id,
                                          self.page_size, "open-updated-desc-v1"], separators=(",", ":")).encode()).hexdigest()
        state = {"scope": scope, "watermark": watermark, "stage": "pulls", "page": 1,
                 "pulls": [], "nextPullPage": None}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                saved = json.loads(cursor)
                if (not isinstance(saved, dict) or set(saved) != set(state) or saved["scope"] != scope
                        or saved["watermark"] != watermark or saved["stage"] not in _STAGES
                        or type(saved["page"]) is not int or not 1 <= saved["page"] <= 2**31 - 1
                        or not isinstance(saved["pulls"], list) or len(saved["pulls"]) > 20
                        or any(type(n) is not int or not 1 <= n <= 2**63 - 1 for n in saved["pulls"])
                        or len(set(saved["pulls"])) != len(saved["pulls"])
                        or (saved["stage"] == "pulls") != (not saved["pulls"])
                        or (saved["nextPullPage"] is not None and (type(saved["nextPullPage"]) is not int
                            or not 2 <= saved["nextPullPage"] <= 2**31 - 1))):
                    raise ValueError
                state = saved
            except (ValueError, TypeError):
                raise ValueError("GITHUB_PR_CURSOR_INVALID") from None
        resource = number = None
        if event is not None:
            if cursor is not None or str(event.get("repository_id")) != repository_id:
                raise ValueError("GITHUB_PR_BINDING_INVALID")
            if event.get("event") == "pull_request_review_thread":
                raise GitHubUnavailable("GITHUB_PR_THREAD_REQUIRES_GRAPHQL")
            if event.get("event") not in _EVENTS:
                raise ValueError("GITHUB_PR_BINDING_INVALID")
            resource = _id(event.get("resource_id"))
            number = _positive(event.get("pull_number"))
            state["stage"] = _EVENTS[event["event"]]
        try:
            token = self.token_for_target(dict(target))
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_PR_UNAVAILABLE") from None
        if not isinstance(token, str):
            raise GitHubUnavailable("GITHUB_PR_UNAVAILABLE")
        repository = self._get(f"/repositories/{repository_id}", token).payload
        name = self._repository(repository, repository_id)
        stage, page = state["stage"], state["page"]
        root = f"/repos/{name}"
        if number is None and state["pulls"]:
            number = state["pulls"][0]
        paths = {"pulls": "/pulls", "reviews": f"/pulls/{number}/reviews",
                 "discussion": f"/issues/{number}/comments", "inline": f"/pulls/{number}/comments"}
        suffix = paths[stage]
        size = min(20, self.page_size) if stage == "pulls" else self.page_size
        params = {"per_page": [str(size)]}
        if stage == "pulls":
            params.update(state=["open"], sort=["updated"], direction=["desc"])
        if event:
            pull = self._get(f"{root}/pulls/{number}", token).payload
            self._pull(pull, target, name)
            if pull["number"] != number:
                _invalid()
            if stage == "pulls":
                records = [pull]
            else:
                event_path = {"reviews": f"/pulls/{number}/reviews/{resource}",
                              "discussion": f"/issues/comments/{resource}", "inline": f"/pulls/comments/{resource}"}[stage]
                records = [self._get(root + event_path, token).payload]
            if not isinstance(records[0], Mapping) or str(records[0].get("id")) != resource:
                _invalid()
            next_page = None
        else:
            if stage != "pulls":
                pull = self._get(f"{root}/pulls/{number}", token).payload
                self._pull(pull, target, name)
                if pull["number"] != number:
                    _invalid()
            query = "&".join(f"{key}={values[0]}" for key, values in params.items())
            response = self._get(f"{root}{suffix}?{query}&page={page}", token)
            records = response.payload
            next_page = self._link(response.headers, root + suffix, f"/repositories/{repository_id}{suffix}", params, page)
        if not isinstance(records, list) or len(records) > size:
            _invalid()
        sources = []
        for record in records:
            source = self._pull(record, target, name) if stage == "pulls" else self._comment(record, stage, target, name, number)
            if stage != "pulls":
                source["sourceFacts"]["pullState"] = pull["state"]
                if pull["state"] == "closed":
                    source["lifecycle"] = "source_closed"
            sources.append(source)
            changed = source["sourceFacts"].get("updatedAt") or source["sourceFacts"].get("submittedAt")
            if changed and changed > watermark:
                watermark = changed
        if len({source["sourceId"] for source in sources}) != len(sources):
            _invalid()
        if stage == "inline":
            self._enrich_threads(sources, repository_id, name, number, token)
        after = self._get(root, token).payload
        if self._repository(after, repository_id) != name or after["private"] != repository["private"]:
            _invalid()
        state["watermark"] = watermark
        if event:
            return FactPage(tuple(sources), None, watermark)
        if stage == "pulls":
            state.update(pulls=[record["number"] for record in records], nextPullPage=next_page, stage="reviews", page=1)
            if not records:
                state.update(stage="pulls", page=next_page or 1)
        elif next_page:
            state["page"] = next_page
        elif stage != "inline":
            state.update(stage=_STAGES[_STAGES.index(stage) + 1], page=1)
        else:
            state["pulls"] = state["pulls"][1:]
            state.update(stage="reviews" if state["pulls"] else "pulls", page=1 if state["pulls"] else state["nextPullPage"] or 1)
        done = not state["pulls"] and state["nextPullPage"] is None
        return FactPage(tuple(sources), None if done else json.dumps(state, separators=(",", ":")), watermark)
