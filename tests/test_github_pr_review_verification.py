"""Formal review actions require current, bounded opinionated-review proof."""
from __future__ import annotations

import copy

import pytest

from pullwise_server.github_pr_reviews import GitHubPRReviewReader
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


def response(*, review_id="8", state="CHANGES_REQUESTED", reviewer_id=2, more=False):
    connection = {
        "nodes": [{"databaseId": review_id, "state": state,
                   "author": {"databaseId": reviewer_id}}],
        "pageInfo": {"hasNextPage": more, "endCursor": "next" if more else None},
    }
    return GitHubResponse(200, {"data": {"repository": {"databaseId": 42,
        "pullRequest": {"number": 7, "latestOpinionatedReviews": connection}}}}, {})


def test_effective_and_superseded_review_are_bound_to_reviewer_and_review_id():
    current = GitHubPRReviewReader(query_json=lambda *args, **kwargs: response())
    page = current.read(owner="owner", name="project", github_repository_id="42",
                        pull_number=7, token="fake")
    assert page.status(review_id="8", reviewer_id="2") == "effective"
    assert page.status(review_id="7", reviewer_id="2") == "superseded"
    assert page.status(review_id="8", reviewer_id="3") == "unknown"


def test_partial_page_never_proves_missing_review_was_superseded():
    page = GitHubPRReviewReader(query_json=lambda *args, **kwargs: response(more=True)).read(
        owner="owner", name="project", github_repository_id="42", pull_number=7, token="fake")
    assert page.status(review_id="8", reviewer_id="2") == "effective"
    assert page.status(review_id="7", reviewer_id="2") == "superseded"
    assert page.status(review_id="99", reviewer_id="3") == "unknown"


@pytest.mark.parametrize("mutation", [
    lambda value: value["data"]["repository"].update(databaseId=99),
    lambda value: value["data"]["repository"]["pullRequest"].update(number=8),
    lambda value: value["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"]["nodes"][0].update(databaseId=0),
    lambda value: value["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"]["nodes"][0].update(state="PENDING"),
])
def test_invalid_graphql_binding_never_confirms_review(mutation):
    payload = copy.deepcopy(response().payload)
    mutation(payload)
    with pytest.raises((ValueError, GitHubUnavailable)):
        GitHubPRReviewReader(query_json=lambda *args, **kwargs: GitHubResponse(200, payload, {})).read(
            owner="owner", name="project", github_repository_id="42", pull_number=7, token="fake")


def test_transport_uncertainty_remains_unavailable():
    with pytest.raises(GitHubUnavailable):
        GitHubPRReviewReader(query_json=lambda *args, **kwargs: GitHubResponse(429, {}, {})).read(
            owner="owner", name="project", github_repository_id="42", pull_number=7, token="fake")
