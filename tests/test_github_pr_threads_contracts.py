"""Contract tests for bounded, read-only GraphQL review-thread observations."""
from copy import deepcopy

import pytest

from pullwise_server.github_pr_threads import GitHubPRThreadReader, PR_THREADS_QUERY, PR_THREAD_COMMENTS_QUERY
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


def connection(nodes, *, more=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def payload():
    parent = {"number": 7, "repository": {"databaseId": 11}}
    comment = {"databaseId": "101", "replyTo": None, "pullRequest": parent}
    thread = {"id": "PRRT_a", "isResolved": False, "isOutdated": True,
              "pullRequest": parent, "comments": connection([comment])}
    return {"data": {"repository": {"databaseId": 11, "pullRequest": {
        "number": 7, "reviewThreads": connection([thread])}}}}


def read(value=None, **kwargs):
    value = payload() if value is None else value
    reader = GitHubPRThreadReader(query_json=lambda *a, **k: GitHubResponse(200, value))
    return reader.read(owner="octo", name="repo", github_repository_id="11",
                       pull_number=7, token="private-test-token", **kwargs)


def threads(value):
    return value["data"]["repository"]["pullRequest"]["reviewThreads"]


def test_verified_thread_comment_mapping_and_fixed_query():
    calls = []
    def query(document, *, variables, token):
        calls.append((document, variables, token))
        return GitHubResponse(200, payload())
    page = GitHubPRThreadReader(query_json=query).read(
        owner="octo", name="repo", github_repository_id="11", pull_number=7, token="secret")
    assert page.comment_facts["101"] == {"threadId": "PRRT_a", "isResolved": False,
        "isOutdated": True, "inReplyToId": None, "threadCoverage": "complete", "associationVerified": True}
    assert page.coverage == "complete" and page.next_cursor is None
    assert calls == [(PR_THREADS_QUERY, {"owner": "octo", "name": "repo", "number": 7,
                                        "first": 100, "after": None, "commentAfter": None}, "secret")]
    assert "mutation" not in PR_THREADS_QUERY and "databaseId: fullDatabaseId" in PR_THREADS_QUERY


@pytest.mark.parametrize("level", ["threads", "comments", "both"])
def test_two_level_pagination_never_claims_complete(level):
    value = payload()
    for conn in ([threads(value)] if level == "threads" else
                 [threads(value)["nodes"][0]["comments"]] if level == "comments" else
                 [threads(value), threads(value)["nodes"][0]["comments"]]):
        conn["pageInfo"] = {"hasNextPage": True, "endCursor": "cursor1"}
    page = read(value)
    assert page.coverage == "partial"
    assert page.next_cursor is not None
    assert page.threads[0]["commentsNextCursor"] == ("cursor1" if level in {"comments", "both"} else None)


def test_continuation_is_not_a_complete_snapshot():
    value = payload()
    threads(value)["pageInfo"] = {"hasNextPage": True, "endCursor": "old-cursor"}
    assert read(after=read(value).next_cursor).coverage == "partial"


def test_empty_page_does_not_synthesize_deleted_or_resolved_comments():
    value = payload()
    threads(value)["nodes"] = []
    assert read(value).comment_facts == {}


@pytest.mark.parametrize("mutation", [
    lambda v: v["data"]["repository"].update(databaseId=12),
    lambda v: v["data"]["repository"]["pullRequest"].update(number=8),
    lambda v: threads(v)["nodes"][0]["pullRequest"].update(number=8),
    lambda v: threads(v)["nodes"][0]["pullRequest"]["repository"].update(databaseId=12),
    lambda v: threads(v)["nodes"][0].update(isResolved="false"),
    lambda v: threads(v)["nodes"][0].update(isOutdated=None),
    lambda v: threads(v)["nodes"][0].update(id=""),
    lambda v: threads(v)["nodes"][0]["comments"]["nodes"][0].update(databaseId=True),
    lambda v: threads(v)["nodes"][0]["comments"]["nodes"][0].update(replyTo={"databaseId": "101"}),
    lambda v: threads(v)["nodes"].append(deepcopy(threads(v)["nodes"][0])),
    lambda v: threads(v)["nodes"][0]["comments"]["nodes"].append(deepcopy(threads(v)["nodes"][0]["comments"]["nodes"][0])),
    lambda v: threads(v)["pageInfo"].update(hasNextPage=True, endCursor=None),
    lambda v: threads(v)["pageInfo"].update(hasNextPage="false"),
    lambda v: threads(v).update(nodes=[None]),
])
def test_invalid_identity_or_shape_is_rejected(mutation):
    value = payload()
    mutation(value)
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_RESPONSE_INVALID"):
        read(value)


def test_reply_parent_link_is_preserved_without_inventing_parent_presence():
    value = payload()
    threads(value)["nodes"][0]["comments"]["nodes"][0]["replyTo"] = {"databaseId": "99"}
    assert read(value).comment_facts["101"]["inReplyToId"] == "99"


def test_partial_graphql_errors_and_missing_repository_are_unavailable():
    for value in ({"errors": [{"message": "secret"}], **payload()}, {"data": {"repository": None}}):
        with pytest.raises(GitHubUnavailable, match="GITHUB_PR_THREADS_UNAVAILABLE"):
            read(value)


def test_transport_retry_deadline_is_preserved_and_other_errors_sanitized():
    for error in (GitHubUnavailable(retry_at=12345), RuntimeError("secret")):
        def fail(*a, **k):
            raise error
        with pytest.raises(GitHubUnavailable) as caught:
            GitHubPRThreadReader(query_json=fail).read(owner="o", name="r",
                github_repository_id="11", pull_number=7, token="secret")
        assert "secret" not in str(caught.value)
        assert caught.value.retry_at == (12345 if isinstance(error, GitHubUnavailable) else None)


def test_nested_comments_are_drained_before_next_thread_page():
    value = payload()
    threads(value)["pageInfo"] = {"hasNextPage": True, "endCursor": "threads-next"}
    threads(value)["nodes"][0]["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "comments-next"}
    cursor = read(value).next_cursor
    node = deepcopy(threads(payload())["nodes"][0])
    node["comments"]["nodes"][0].update(databaseId="102", replyTo={"databaseId": "101"})
    calls = []
    def query(document, *, variables, token):
        calls.append((document, variables))
        return GitHubResponse(200, {"data": {"node": node}})
    page = GitHubPRThreadReader(query_json=query).read(owner="octo", name="repo",
        github_repository_id="11", pull_number=7, token="secret", after=cursor)
    assert calls == [(PR_THREAD_COMMENTS_QUERY, {"id": "PRRT_a", "first": 100, "commentAfter": "comments-next"})]
    assert page.comment_facts["102"]["inReplyToId"] == "101"
    assert page.comment_facts["102"]["threadCoverage"] == "partial"
    assert page.coverage == "partial" and page.next_cursor
    assert read(after=page.next_cursor).next_cursor is None


@pytest.mark.parametrize("field,value", [("owner", "other"), ("name", "other"),
    ("github_repository_id", "12"), ("pull_number", 8)])
def test_cursor_cannot_cross_identity(field, value):
    original = payload()
    threads(original)["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    args = dict(owner="octo", name="repo", github_repository_id="11", pull_number=7,
                token="secret", after=read(original).next_cursor)
    args[field] = value
    def forbidden(*a, **k):
        pytest.fail("Invalid cursor must fail before network")
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_CURSOR_INVALID"):
        GitHubPRThreadReader(query_json=forbidden).read(**args)


@pytest.mark.parametrize("cursor", ["garbage", "{}", "[]", "null", '{"scope": []}'])
def test_malformed_cursor_rejected(cursor):
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_CURSOR_INVALID"):
        read(after=cursor)


def test_comment_continuation_revalidates_thread_and_parent_identity():
    value = payload()
    threads(value)["nodes"][0]["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    cursor = read(value).next_cursor
    for field, val in [("id", "other"), ("pullRequest", {"number": 8, "repository": {"databaseId": 11}})]:
        node = deepcopy(threads(payload())["nodes"][0])
        node[field] = val
        with pytest.raises(ValueError, match="GITHUB_PR_THREAD_RESPONSE_INVALID"):
            read({"data": {"node": node}}, after=cursor)


@pytest.mark.parametrize("size", [0, 101, True, None])
def test_invalid_page_size(size):
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_PAGE_SIZE_INVALID"):
        GitHubPRThreadReader(query_json=lambda *a: None, page_size=size)


def test_reply_relationship_cannot_cross_observed_threads():
    value = payload()
    other = deepcopy(threads(value)["nodes"][0])
    other["id"] = "PRRT_b"
    other["comments"]["nodes"][0].update(databaseId="102", replyTo={"databaseId": "101"})
    threads(value)["nodes"].append(other)
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_RESPONSE_INVALID"):
        read(value)


def test_reply_cycles_are_rejected():
    value = payload()
    comments = threads(value)["nodes"][0]["comments"]["nodes"]
    other = deepcopy(comments[0])
    comments[0]["replyTo"] = {"databaseId": "102"}
    other.update(databaseId="102", replyTo={"databaseId": "101"})
    comments.append(other)
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_RESPONSE_INVALID"):
        read(value)


@pytest.mark.parametrize("level", ["threads", "comments"])
def test_page_limit_applies_to_both_connections(level):
    value = payload()
    nodes = threads(value)["nodes"] if level == "threads" else threads(value)["nodes"][0]["comments"]["nodes"]
    nodes.extend(deepcopy(nodes[0]) for _ in range(100))
    with pytest.raises(ValueError, match="GITHUB_PR_THREAD_RESPONSE_INVALID"):
        read(value)
