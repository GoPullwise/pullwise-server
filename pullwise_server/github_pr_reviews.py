"""Bounded formal-review status proof from GitHub's latest opinionated reviews.

This is an injected read-only reference adapter. A missing/partial result is
unknown; it never withdraws a text request merely because a page omitted it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping

from .github_transport import GitHubResponse, GitHubUnavailable
from .github_pr_threads import _id


PR_LATEST_OPINIONATED_REVIEWS = """query PullwiseLatestOpinionatedReviews(
  $owner: String!, $name: String!, $number: Int!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    databaseId
    pullRequest(number: $number) {
      number
      latestOpinionatedReviews(first: $first) {
        nodes { databaseId: fullDatabaseId state author { ... on User { databaseId } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


@dataclass(frozen=True)
class PRReviewPage:
    by_reviewer: Mapping[str, tuple[str, str]]
    coverage: str

    def status(self, *, review_id: str, reviewer_id: str) -> str:
        latest = self.by_reviewer.get(reviewer_id)
        if latest is None:
            return "unknown"
        if latest[0] == review_id:
            return "effective" if latest[1] == "CHANGES_REQUESTED" else "dismissed" if latest[1] == "DISMISSED" else "unknown"
        return "superseded" if latest[1] in {"CHANGES_REQUESTED", "APPROVED"} else "unknown"


class GitHubPRReviewReader:
    def __init__(self, *, query_json: Callable, page_size: int = 100):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("GITHUB_PR_REVIEW_PAGE_SIZE_INVALID")
        self.query_json = query_json
        self.page_size = page_size

    def read(self, *, owner: str, name: str, github_repository_id: str,
             pull_number: int, token: str) -> PRReviewPage:
        if (any(not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part)
                for part in (owner, name)) or type(pull_number) is not int
                or not 1 <= pull_number <= 2**31 - 1 or not isinstance(token, str)):
            raise ValueError("GITHUB_PR_REVIEW_BINDING_INVALID")
        repository_id = _id(github_repository_id)
        try:
            response = self.query_json(PR_LATEST_OPINIONATED_REVIEWS,
                variables={"owner": owner, "name": name, "number": pull_number, "first": self.page_size},
                token=token)
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_PR_REVIEWS_UNAVAILABLE") from None
        if (not isinstance(response, GitHubResponse) or response.status != 200
                or not isinstance(response.payload, Mapping) or response.payload.get("errors")
                or not isinstance(response.payload.get("data"), Mapping)):
            raise GitHubUnavailable("GITHUB_PR_REVIEWS_UNAVAILABLE")
        repository = response.payload["data"].get("repository")
        if repository is None or isinstance(repository, Mapping) and repository.get("pullRequest") is None:
            raise GitHubUnavailable("GITHUB_PR_REVIEWS_UNAVAILABLE")
        if not isinstance(repository, Mapping) or _id(repository.get("databaseId")) != repository_id:
            raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
        pull = repository["pullRequest"]
        if not isinstance(pull, Mapping) or type(pull.get("number")) is not int or pull["number"] != pull_number:
            raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
        reviews = pull.get("latestOpinionatedReviews")
        if not isinstance(reviews, Mapping) or not isinstance(reviews.get("nodes"), list):
            raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
        nodes, info = reviews["nodes"], reviews.get("pageInfo")
        if (len(nodes) > self.page_size or not isinstance(info, Mapping)
                or type(info.get("hasNextPage")) is not bool):
            raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
        if info["hasNextPage"] and (not nodes or not isinstance(info.get("endCursor"), str) or not info["endCursor"]):
            raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
        by_reviewer = {}
        for node in nodes:
            if not isinstance(node, Mapping) or node.get("state") not in {"CHANGES_REQUESTED", "APPROVED", "DISMISSED"}:
                raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
            review_id = _id(node.get("databaseId"))
            author = node.get("author")
            # Bots/deleted users may have no stable User ID: keep their status unknown.
            if author is None or not isinstance(author, Mapping) or author.get("databaseId") is None:
                continue
            reviewer_id = _id(author["databaseId"])
            if reviewer_id in by_reviewer:
                raise ValueError("GITHUB_PR_REVIEW_RESPONSE_INVALID")
            by_reviewer[reviewer_id] = (review_id, node["state"])
        return PRReviewPage(by_reviewer, "partial" if info["hasNextPage"] else "complete")
