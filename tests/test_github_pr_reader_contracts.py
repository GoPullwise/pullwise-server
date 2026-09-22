import copy
import json

import pytest

from pullwise_server.github_pr_reader import GitHubPRReader
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


TARGET = {"module": "pr", "control_key": "repo:42:pr", "repository_id": "github:42", "github_repository_id": "42"}
REPO = {"id": 42, "full_name": "owner/project", "private": False}
TIME = "2026-09-21T01:00:00Z"
PR = {"id": 700, "number": 7, "state": "open", "title": "Fix", "body": "Description",
      "updated_at": TIME, "created_at": TIME, "merged_at": None, "draft": False,
      "user": {"id": 1, "login": "author"}, "head": {"sha": "abc"},
      "base": {"repo": {"id": 42}}, "requested_reviewers": [{"id": 2, "login": "reviewer"}],
      "requested_teams": [], "html_url": "https://github.com/owner/project/pull/7"}
REVIEW = {"id": 8, "state": "CHANGES_REQUESTED", "body": "", "submitted_at": TIME,
          "user": {"id": 2, "login": "reviewer"}, "pull_request_url": "https://api.github.com/repos/owner/project/pulls/7",
          "html_url": "https://github.com/owner/project/pull/7#pullrequestreview-8"}
COMMENT = {"id": 9, "body": "Please fix\n```py\nx = 1\n```", "created_at": TIME, "updated_at": TIME,
           "user": {"id": 2, "login": "reviewer"}, "issue_url": "https://api.github.com/repos/owner/project/issues/7",
           "html_url": "https://github.com/owner/project/pull/7#issuecomment-9"}
INLINE = dict(COMMENT, id=10, pull_request_url=REVIEW["pull_request_url"],
              html_url="https://github.com/owner/project/pull/7#discussion_r10", pull_request_review_id=8,
              path="a.py", diff_hunk="@@ -1 +1 @@", in_reply_to_id=6)


def reader(*, replacement=None, link=None, page_size=100, thread_reader=None):
    calls = []
    def get(path, *, token):
        calls.append(path)
        if path in {"/repositories/42", "/repos/owner/project"}:
            payload = REPO
        elif path.split("?")[0] == "/repos/owner/project/pulls":
            payload = [PR]
        elif path == "/repos/owner/project/pulls/7":
            payload = PR
        elif "/reviews" in path:
            payload = [REVIEW] if "?" in path else REVIEW
        elif "/issues/" in path:
            payload = [COMMENT] if "?" in path else COMMENT
        else:
            payload = [INLINE] if "?" in path else INLINE
        result = GitHubResponse(200, copy.deepcopy(payload), {"link": link} if link and "reviews?" in path and path.endswith("page=1") else {})
        return replacement(path, result) if replacement else result
    return GitHubPRReader(get_json=get, token_for_target=lambda _: "secret", page_size=page_size,
                         thread_reader=thread_reader), calls


def read(adapter, cursor=None, high_watermark=None, event=None, target=TARGET):
    return adapter.read_page(target=target, cursor=cursor, high_watermark=high_watermark, event=event)


def advance(adapter, page):
    return read(adapter, page.next_cursor, page.high_watermark)


def test_scan_maps_four_sources_without_inventing_thread_state():
    adapter, calls = reader()
    pages = [read(adapter)]
    for _ in range(3):
        pages.append(advance(adapter, pages[-1]))
    state, review, comment, inline = [page.sources[0] for page in pages]
    assert [source["sourceType"] for source in (state, review, comment, inline)] == ["pr_state", "pr_review_body", "pr_comment", "pr_review_comment"]
    assert state["processingMode"] == "rules_only"
    assert state["sourceFacts"]["requestedReviewers"][0]["githubId"] == "2"
    assert review["sourceFacts"]["updatedAt"] is None
    assert review["sourceFacts"]["submittedAt"] == TIME
    assert review["processingMode"] == "rules_only"
    assert review["ruleActions"] == []
    assert comment["content"]["body"] == COMMENT["body"]
    assert inline["sourceFacts"]["threadId"] is None
    assert inline["sourceFacts"]["isResolved"] is None
    assert inline["sourceFacts"]["isOutdated"] is None
    assert inline["completeness"] == "partial"
    assert inline["sourceFacts"]["inReplyToId"] == "6"
    assert pages[-1].next_cursor is None
    assert all(len(page.sources) <= 100 for page in pages)
    assert len(calls) == 15


def test_cursor_rejects_other_scope_watermark_and_stage_before_io():
    adapter, calls = reader()
    page = read(adapter)
    size = len(calls)
    for target, watermark in [(dict(TARGET, control_key="other"), page.high_watermark), (TARGET, "")]:
        with pytest.raises(ValueError, match="CURSOR_INVALID"):
            read(adapter, page.next_cursor, watermark, target=target)
    saved = json.loads(page.next_cursor)
    saved["stage"] = "evil"
    with pytest.raises(ValueError, match="CURSOR_INVALID"):
        read(adapter, json.dumps(saved), page.high_watermark)
    assert len(calls) == size


def test_canonical_pagination_does_not_follow_arbitrary_urls():
    adapter, calls = reader(link='<https://api.github.com/repositories/42/pulls/7/reviews?per_page=100&page=2>; rel="next"')
    first = read(adapter)
    second = advance(adapter, first)
    advance(adapter, second)
    assert "/repos/owner/project/pulls/7/reviews?per_page=100&page=2" in calls


@pytest.mark.parametrize("link", [
    '<https://evil.invalid/pulls/7/reviews?per_page=100&page=2>; rel="next"',
    '<https://api.github.com/repositories/99/pulls/7/reviews?per_page=100&page=2>; rel="next"',
    '<https://api.github.com/repositories/42/pulls/7/reviews?per_page=100&page=3>; rel="next"',
])
def test_bad_pagination_rejected(link):
    adapter, _ = reader(link=link)
    with pytest.raises(ValueError, match="PAGINATION_INVALID"):
        advance(adapter, read(adapter))


@pytest.mark.parametrize("event,resource", [("pull_request", "700"), ("pull_request_review", "8"),
    ("issue_comment", "9"), ("pull_request_review_comment", "10")])
def test_events_refetch_single_authoritative_resource(event, resource):
    adapter, calls = reader()
    page = read(adapter, event={"event": event, "repository_id": "42", "resource_id": resource, "pull_number": 7})
    assert len(page.sources) == 1
    assert page.next_cursor is None
    assert not any("?" in call for call in calls)


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_failures_never_infer_deletion(status):
    adapter, _ = reader(replacement=lambda path, response: GitHubResponse(status, {}, {}) if "pulls?" in path else response)
    with pytest.raises(GitHubUnavailable):
        read(adapter)


def test_repository_name_reuse_is_rejected_after_fetch():
    adapter, _ = reader(replacement=lambda path, response: GitHubResponse(200, dict(REPO, id=99), {}) if path == "/repos/owner/project" else response)
    with pytest.raises(ValueError):
        read(adapter)


def test_retry_deadline_survives_transport_failure():
    def fail(path, response):
        raise GitHubUnavailable("RATE_LIMIT", retry_at=12345)
    adapter, _ = reader(replacement=fail)
    with pytest.raises(GitHubUnavailable) as caught:
        read(adapter)
    assert caught.value.retry_at == 12345


def test_thread_event_does_not_acknowledge_unavailable_graphql_facts():
    adapter, calls = reader()
    with pytest.raises(GitHubUnavailable, match="REQUIRES_GRAPHQL"):
        read(adapter, event={"event": "pull_request_review_thread", "repository_id": "42", "resource_id": "88", "pull_number": 7})
    assert calls == []


def test_poll_empty_page_does_not_infer_deleted_sources():
    adapter, _ = reader(replacement=lambda path, result: GitHubResponse(200, [], {}) if "pulls?" in path else result)
    page = read(adapter)
    assert page.sources == ()
    assert page.next_cursor is None


def test_issue_comment_event_must_belong_to_refetched_pr():
    adapter, _ = reader(replacement=lambda path, result: GitHubResponse(200, dict(COMMENT, issue_url="https://api.github.com/repos/owner/project/issues/99"), {}) if path.endswith("issues/comments/9") else result)
    with pytest.raises(ValueError):
        read(adapter, event={"event": "issue_comment", "repository_id": "42", "resource_id": "9", "pull_number": 7})


@pytest.mark.parametrize("patch", [{"id": True}, {"number": True}, {"updated_at": "bad"},
    {"base": {"repo": {"id": 99}}}, {"requested_reviewers": [None]}, {"body": {}}, {"draft": "false"}])
def test_invalid_pull_fields_fail_closed(patch):
    adapter, _ = reader(replacement=lambda path, result: GitHubResponse(200, [dict(PR, **patch)], {}) if "pulls?" in path else result)
    with pytest.raises(ValueError):
        read(adapter)


def test_next_pull_page_is_retained_while_child_collections_are_read():
    link = '<https://api.github.com/repositories/42/pulls?per_page=20&state=open&sort=updated&direction=desc&page=2>; rel="next"'
    adapter, calls = reader(replacement=lambda path, result: GitHubResponse(200, result.payload, {"link": link}) if "pulls?" in path and path.endswith("page=1") else result)
    page = read(adapter)
    for _ in range(3):
        page = advance(adapter, page)
    state = json.loads(page.next_cursor)
    assert state["stage"] == "pulls"
    assert state["page"] == 2
    advance(adapter, page)
    assert any("pulls?" in path and path.endswith("page=2") for path in calls)


def test_comment_event_preserves_authoritatively_closed_parent_state():
    adapter, _ = reader(replacement=lambda path, result: GitHubResponse(200, dict(PR, state="closed", merged_at=TIME), {}) if path == "/repos/owner/project/pulls/7" else result)
    page = read(adapter, event={"event": "issue_comment", "repository_id": "42", "resource_id": "9", "pull_number": 7})
    assert page.sources[0]["lifecycle"] == "source_closed"
    assert page.sources[0]["sourceFacts"]["pullState"] == "closed"


def thread_payload(*, repository=42, number=7, cid="10", more=True):
    return {"data": {"repository": {"databaseId": repository, "pullRequest": {
        "number": number, "reviewThreads": {"nodes": [{"id": "PRRT_thread", "isResolved": True,
        "isOutdated": False, "pullRequest": {"number": number, "repository": {"databaseId": repository}},
        "comments": {"nodes": [{"databaseId": cid, "replyTo": {"databaseId": "6"},
            "pullRequest": {"number": number, "repository": {"databaseId": repository}}}],
            "pageInfo": {"hasNextPage": more, "endCursor": "comments_next" if more else None}}}],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}


def inline_event(adapter):
    return read(adapter, event={"event": "pull_request_review_comment", "repository_id": "42",
                                "resource_id": "10", "pull_number": 7})


def test_optional_threads_enrich_matching_partial_inline_page_once():
    from pullwise_server.github_pr_threads import GitHubPRThreadReader
    queries = []
    def query(document, **kwargs):
        queries.append(kwargs)
        return GitHubResponse(200, thread_payload())
    adapter, calls = reader(thread_reader=GitHubPRThreadReader(query_json=query))
    page = read(adapter)
    for _ in range(2):
        page = advance(adapter, page)
    assert queries == []
    page = advance(adapter, page)
    facts = page.sources[0]["sourceFacts"]
    assert len(queries) == 1 and len(calls) == 15
    assert facts["threadId"] == "PRRT_thread" and facts["isResolved"] is True
    assert facts["isOutdated"] is False and facts["inReplyToId"] == "6"
    assert facts["threadCoverage"] == "partial" and facts["associationVerified"] is True
    assert page.sources[0]["completeness"] == "partial"


@pytest.mark.parametrize("patch", [{"repository": 99}, {"number": 8}, {"cid": "99"}])
def test_unmatched_or_wrong_scope_graphql_does_not_change_comment(patch):
    from pullwise_server.github_pr_threads import GitHubPRThreadReader
    adapter, _ = reader(thread_reader=GitHubPRThreadReader(query_json=lambda *a, **k:
                       GitHubResponse(200, thread_payload(**patch))))
    source = inline_event(adapter).sources[0]
    assert source["sourceFacts"]["threadId"] is None
    assert source["sourceFacts"]["isResolved"] is None
    assert source["sourceFacts"]["inReplyToId"] == "6"
    assert source["lifecycle"] == "active"


@pytest.mark.parametrize("error", [GitHubUnavailable(), RuntimeError("secret")])
def test_unavailable_threads_preserve_rest_facts_without_error_details(error):
    class Threads:
        def read(self, **kwargs):
            raise error
    adapter, _ = reader(thread_reader=Threads())
    source = inline_event(adapter).sources[0]
    assert source["sourceFacts"]["threadCoverage"] == "thread_unavailable"
    assert source["sourceFacts"]["threadId"] is None and source["lifecycle"] == "active"
    assert "secret" not in json.dumps(source)


def test_thread_rate_limit_preserves_retry_deadline():
    class Threads:
        def read(self, **kwargs):
            raise GitHubUnavailable(retry_at=12345)
    adapter, _ = reader(thread_reader=Threads())
    with pytest.raises(GitHubUnavailable) as caught:
        inline_event(adapter)
    assert caught.value.retry_at == 12345


def test_mismatched_bound_page_is_not_applied():
    from pullwise_server.github_pr_threads import PRThreadPage
    class Threads:
        def read(self, **kwargs):
            return PRThreadPage({"10": {"threadId": "wrong", "isResolved": True,
                "isOutdated": False, "inReplyToId": "6", "threadCoverage": "complete",
                "associationVerified": True}}, (), None, "complete", "99", 7)
    adapter, _ = reader(thread_reader=Threads())
    facts = inline_event(adapter).sources[0]["sourceFacts"]
    assert facts["threadId"] is None and facts["isResolved"] is None
