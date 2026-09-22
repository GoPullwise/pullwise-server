"""One bounded read-only GraphQL request per review-thread observation.

The injected callback owns transport bounds and credentials never enter cursors.
Connection completion is coverage, not an atomic snapshot or deletion evidence.
Fields follow https://docs.github.com/en/graphql/reference/pulls; comment IDs
use the non-deprecated BigInt field aliased to databaseId for REST association.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping

from .github_transport import GitHubResponse, GitHubUnavailable


_THREAD_FIELDS = """
  id isResolved isOutdated
  pullRequest { number repository { databaseId } }
  comments(first: $first, after: $commentAfter) {
    nodes { databaseId: fullDatabaseId replyTo { databaseId: fullDatabaseId }
            pullRequest { number repository { databaseId } } }
    pageInfo { hasNextPage endCursor }
  }
"""
PR_THREADS_QUERY = """query PullwiseReviewThreads(
  $owner: String!, $name: String!, $number: Int!, $first: Int!,
  $after: String, $commentAfter: String) {
  repository(owner: $owner, name: $name) {
    databaseId
    pullRequest(number: $number) {
      number
      reviewThreads(first: $first, after: $after) {
        nodes { """ + _THREAD_FIELDS + """ }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
PR_THREAD_COMMENTS_QUERY = """query PullwiseReviewThreadComments(
  $id: ID!, $first: Int!, $commentAfter: String) {
  node(id: $id) { ... on PullRequestReviewThread { """ + _THREAD_FIELDS + """ } }
}"""


@dataclass(frozen=True)
class PRThreadPage:
    comment_facts: Mapping[str, dict]
    threads: tuple[dict, ...]
    next_cursor: str | None
    coverage: str
    github_repository_id: str | None = None
    pull_number: int | None = None


def _invalid():
    raise ValueError("GITHUB_PR_THREAD_RESPONSE_INVALID")


def _id(value):
    if (type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value))
            or int(value) > 2**63 - 1):
        _invalid()
    return str(value)


def _opaque(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and all(ord(c) >= 32 for c in value)


def _parent(value, repository_id, number):
    if (not isinstance(value, Mapping) or type(value.get("number")) is not int
            or value["number"] != number or not isinstance(value.get("repository"), Mapping)
            or _id(value["repository"].get("databaseId")) != repository_id):
        _invalid()


def _connection(value, size, previous=None):
    if not isinstance(value, Mapping) or not isinstance(value.get("nodes"), list):
        _invalid()
    nodes, info = value["nodes"], value.get("pageInfo")
    if (len(nodes) > size or not isinstance(info, Mapping)
            or type(info.get("hasNextPage")) is not bool):
        _invalid()
    cursor = info.get("endCursor")
    if cursor is not None and not _opaque(cursor):
        _invalid()
    if info["hasNextPage"] and (not nodes or cursor is None or cursor == previous):
        _invalid()
    return nodes, cursor if info["hasNextPage"] else None


class GitHubPRThreadReader:
    def __init__(self, *, query_json: Callable, page_size: int = 100):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("GITHUB_PR_THREAD_PAGE_SIZE_INVALID")
        self.query_json = query_json
        self.page_size = page_size

    def read(self, *, owner: str, name: str, github_repository_id: str,
             pull_number: int, token: str, after: str | None = None) -> PRThreadPage:
        repository_id = _id(github_repository_id)
        if (any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", s)
                for s in (owner, name)) or type(pull_number) is not int
                or not 1 <= pull_number <= 2**31 - 1 or not isinstance(token, str)):
            raise ValueError("GITHUB_PR_THREAD_BINDING_INVALID")
        scope = hashlib.sha256(json.dumps([owner, name, repository_id, pull_number,
                                           self.page_size]).encode()).hexdigest()
        state = {"scope": scope, "threadAfter": None, "pending": []}
        if after is not None:
            try:
                if not isinstance(after, str) or len(after) > 220000:
                    raise ValueError
                state = json.loads(after)
                if (not isinstance(state, dict) or set(state) != {"scope", "threadAfter", "pending"}
                        or state["scope"] != scope or not isinstance(state["pending"], list)
                        or len(state["pending"]) > self.page_size
                        or (state["threadAfter"] is not None and not _opaque(state["threadAfter"]))
                        or any(not isinstance(p, list) or len(p) != 2 or not all(_opaque(s) for s in p)
                               for p in state["pending"])
                        or len({p[0] for p in state["pending"]}) != len(state["pending"])
                        or not (state["pending"] or state["threadAfter"])):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise ValueError("GITHUB_PR_THREAD_CURSOR_INVALID") from None
        pending = list(state["pending"])
        continuation = pending.pop(0) if pending else None
        if continuation:
            document = PR_THREAD_COMMENTS_QUERY
            variables = {"id": continuation[0], "first": self.page_size, "commentAfter": continuation[1]}
        else:
            document = PR_THREADS_QUERY
            variables = {"owner": owner, "name": name, "number": pull_number,
                         "first": self.page_size, "after": state["threadAfter"], "commentAfter": None}
        try:
            response = self.query_json(document, variables=variables, token=token)
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_PR_THREADS_UNAVAILABLE") from None
        if (not isinstance(response, GitHubResponse) or response.status != 200
                or not isinstance(response.payload, Mapping) or response.payload.get("errors")
                or not isinstance(response.payload.get("data"), Mapping)):
            raise GitHubUnavailable("GITHUB_PR_THREADS_UNAVAILABLE")
        data = response.payload["data"]
        if continuation:
            node = data.get("node")
            if node is None:
                raise GitHubUnavailable("GITHUB_PR_THREADS_UNAVAILABLE")
            if not isinstance(node, Mapping) or node.get("id") != continuation[0]:
                _invalid()
            nodes, next_thread = [node], state["threadAfter"]
        else:
            repo = data.get("repository")
            if repo is None or isinstance(repo, Mapping) and repo.get("pullRequest") is None:
                raise GitHubUnavailable("GITHUB_PR_THREADS_UNAVAILABLE")
            if not isinstance(repo, Mapping) or _id(repo.get("databaseId")) != repository_id:
                _invalid()
            pull = repo.get("pullRequest")
            if not isinstance(pull, Mapping) or type(pull.get("number")) is not int or pull["number"] != pull_number:
                _invalid()
            nodes, next_thread = _connection(pull.get("reviewThreads"), self.page_size, state["threadAfter"])
        facts, threads, seen = {}, [], set()
        for thread in nodes:
            if (not isinstance(thread, Mapping) or not _opaque(thread.get("id"))
                    or thread["id"] in seen or type(thread.get("isResolved")) is not bool
                    or type(thread.get("isOutdated")) is not bool):
                _invalid()
            seen.add(thread["id"])
            _parent(thread.get("pullRequest"), repository_id, pull_number)
            comments, next_comment = _connection(thread.get("comments"), self.page_size,
                                                 continuation[1] if continuation else None)
            coverage = "partial" if next_comment or continuation else "complete"
            for comment in comments:
                if not isinstance(comment, Mapping) or "replyTo" not in comment:
                    _invalid()
                _parent(comment.get("pullRequest"), repository_id, pull_number)
                cid = _id(comment.get("databaseId"))
                reply = comment["replyTo"]
                if reply is not None and not isinstance(reply, Mapping):
                    _invalid()
                parent_id = _id(reply.get("databaseId")) if reply is not None else None
                if cid in facts or parent_id == cid:
                    _invalid()
                facts[cid] = {"threadId": thread["id"], "isResolved": thread["isResolved"],
                              "isOutdated": thread["isOutdated"], "inReplyToId": parent_id,
                              "threadCoverage": coverage, "associationVerified": True}
            threads.append({"id": thread["id"], "isResolved": thread["isResolved"],
                            "isOutdated": thread["isOutdated"], "commentsCoverage": coverage,
                            "commentsNextCursor": next_comment})
            if next_comment:
                pending.append([thread["id"], next_comment])
        checked = set()
        for cid, fact in facts.items():
            parent = facts.get(fact["inReplyToId"])
            if parent is not None and parent["threadId"] != fact["threadId"]:
                _invalid()
            chain = set()
            current = cid
            while current in facts and current not in checked:
                if current in chain:
                    _invalid()
                chain.add(current)
                current = facts[current]["inReplyToId"]
            checked.update(chain)
        cursor = json.dumps({"scope": scope, "threadAfter": next_thread, "pending": pending},
                            separators=(",", ":")) if pending or next_thread else None
        return PRThreadPage(facts, tuple(threads), cursor, "partial" if after or cursor else "complete",
                            repository_id, pull_number)
