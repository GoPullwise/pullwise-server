import copy

import pytest

from pullwise_server.github_release_reader import GitHubReleaseReader
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


TARGET = {"module": "updates", "control_key": "watch:owner:42", "repository_id": "github:42",
          "github_repository_id": "42", "target_repository_id": None}
REPO = {"id": 42, "full_name": "owner/project", "private": False}
RELEASE = {"id": 7, "tag_name": "v1", "name": "Version 1", "body": "Full notes\n",
           "draft": False, "prerelease": False, "published_at": "2026-09-21T01:00:00Z",
           "created_at": "2020-01-01T00:00:00Z",
           "html_url": "https://github.com/owner/project/releases/tag/v1"}


def make_reader(payload=None, *, status=200, page_size=100, repo=None, link=None):
    calls = []
    def get_json(path, *, token):
        calls.append((path, token))
        if path in {"/repositories/42", "/repos/owner/project"}:
            return GitHubResponse(200, REPO if repo is None else repo, {})
        return GitHubResponse(status, [copy.deepcopy(RELEASE)] if payload is None else payload,
                              {"link": link} if link else {})
    return GitHubReleaseReader(get_json=get_json, token_for_target=lambda target: "secret-token",
                               page_size=page_size), calls


def read(reader, **kwargs):
    return reader.read_page(target=TARGET, cursor=None, high_watermark=None, event=None, **kwargs)


def test_maps_release_and_uses_validated_repository_routes():
    reader, calls = make_reader()
    page = read(reader)
    assert [call[0] for call in calls] == ["/repositories/42", "/repos/owner/project/releases?per_page=100&page=1", "/repos/owner/project"]
    assert page.sources[0]["repositoryId"] == "github:42"
    assert page.sources[0]["content"]["body"] == RELEASE["body"]
    assert page.sources[0]["sourceFacts"]["updatedAt"] is None
    assert page.high_watermark == RELEASE["published_at"]
    assert page.next_cursor is None
    assert "secret-token" not in repr(page)


def test_cursor_is_bound_to_scope_and_watermark_and_ignores_link_urls():
    reader, calls = make_reader(page_size=1, link='<https://api.github.com/repos/owner/project/releases?per_page=1&page=2>; rel="next"')
    first = read(reader)
    for target, watermark in [(dict(TARGET, control_key="other"), first.high_watermark), (TARGET, "")]:
        with pytest.raises(ValueError, match="GITHUB_RELEASE_CURSOR_INVALID"):
            reader.read_page(target=target, cursor=first.next_cursor, high_watermark=watermark, event=None)


@pytest.mark.parametrize("patch", [{"id": True}, {"id": -1}, {"draft": "false"}, {"prerelease": None},
                                    {"body": []}, {"tag_name": ""}, {"published_at": "yesterday"},
                                    {"html_url": "https://evil.invalid/release"}])
def test_rejects_invalid_authoritative_release_fields(patch):
    reader, _ = make_reader([dict(RELEASE, **patch)])
    with pytest.raises(ValueError, match="GITHUB_RELEASE_RESPONSE_INVALID"):
        read(reader)


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_http_failures_do_not_emit_deletion_or_response_details(status):
    reader, _ = make_reader({"message": "secret-token"}, status=status)
    with pytest.raises(GitHubUnavailable) as error:
        read(reader)
    assert "secret-token" not in str(error.value)


def test_unknown_publish_time_and_unverified_updated_field_do_not_gain_eligibility():
    reader, _ = make_reader([dict(RELEASE, published_at=None, updated_at="2026-09-22T00:00:00Z")])
    page = read(reader)
    assert page.high_watermark == ""
    assert page.sources[0]["sourceFacts"]["publishedAt"] is None
    assert page.sources[0]["sourceFacts"]["updatedAt"] is None


def test_event_refetches_exact_release_and_draft_closes_only_that_source():
    reader, calls = make_reader(dict(RELEASE, draft=True))
    page = reader.read_page(target=TARGET, cursor=None, high_watermark=None,
                            event={"event": "release", "resource_id": "7", "repository_id": "42"})
    assert calls[1][0] == "/repos/owner/project/releases/7"
    assert page.sources[0]["lifecycle"] == "source_closed"


def test_empty_page_never_creates_tombstones_and_retains_watermark():
    reader, _ = make_reader([])
    page = reader.read_page(target=TARGET, cursor=None, high_watermark=RELEASE["published_at"], event=None)
    assert page.sources == ()
    assert page.high_watermark == RELEASE["published_at"]


def test_duplicate_ids_fail_without_claiming_complete_scan():
    reader, _ = make_reader([RELEASE, RELEASE])
    with pytest.raises(ValueError, match="GITHUB_RELEASE_RESPONSE_INVALID"):
        read(reader)


@pytest.mark.parametrize("repo", [dict(REPO, id=99), dict(REPO, full_name="owner/../evil"), dict(REPO, private="false")])
def test_repository_identity_and_shape_fail_closed(repo):
    reader, calls = make_reader(repo=repo)
    with pytest.raises(ValueError, match="GITHUB_RELEASE_RESPONSE_INVALID"):
        read(reader)
    assert len(calls) == 1


def test_repository_reassignment_during_fetch_is_rejected():
    calls = []
    def get_json(path, *, token):
        calls.append(path)
        return GitHubResponse(200, REPO if len(calls) == 1 else [RELEASE] if len(calls) == 2 else dict(REPO, id=99), {})
    reader = GitHubReleaseReader(get_json=get_json, token_for_target=lambda target: "secret-token")
    with pytest.raises(ValueError, match="GITHUB_RELEASE_RESPONSE_INVALID"):
        read(reader)


@pytest.mark.parametrize("link", ['<https://evil.invalid/steal>; rel="next"',
                                  '<https://api.github.com/repos/owner/other/releases?page=2&per_page=100>; rel="next"',
                                  '<https://api.github.com/repos/owner/project/releases?page=1&per_page=100>; rel="next"'])
def test_untrusted_or_nonadvancing_next_links_are_rejected(link):
    reader, calls = make_reader(link=link)
    with pytest.raises(ValueError, match="GITHUB_RELEASE_PAGINATION_INVALID"):
        read(reader)
    assert all("evil.invalid" not in call[0] for call in calls)


def test_full_page_without_next_link_is_complete():
    reader, _ = make_reader(page_size=1)
    assert read(reader).next_cursor is None


def test_reads_next_page_with_bounded_requests_and_keeps_high_watermark():
    first_reader, _ = make_reader(page_size=1, link='<https://api.github.com/repos/owner/project/releases?per_page=1&page=2>; rel="next"')
    first = read(first_reader)
    second_reader, calls = make_reader([dict(RELEASE, id=8, published_at="2020-01-01T00:00:00Z")], page_size=1)
    second = second_reader.read_page(target=TARGET, cursor=first.next_cursor, high_watermark=first.high_watermark, event=None)
    assert len(calls) == 3
    assert calls[1][0].endswith("page=2")
    assert second.high_watermark == first.high_watermark
    assert second.next_cursor is None


def test_transport_retry_metadata_is_preserved_and_unknown_exception_sanitized():
    failure = GitHubUnavailable("GITHUB_RATE_LIMITED", retry_at=1000)
    def get_json(path, *, token):
        raise failure
    reader = GitHubReleaseReader(get_json=get_json, token_for_target=lambda target: "secret-token")
    with pytest.raises(GitHubUnavailable) as caught:
        read(reader)
    assert caught.value is failure
    def broken_token(target):
        raise RuntimeError("secret-token")
    reader = GitHubReleaseReader(get_json=get_json, token_for_target=broken_token)
    with pytest.raises(GitHubUnavailable) as caught:
        read(reader)
    assert "secret-token" not in str(caught.value)


def test_old_release_body_edit_keeps_original_authoritative_timestamp():
    from pullwise_server.product_discovery import _timestamp
    reader, _ = make_reader([dict(RELEASE, body="Edited notes", published_at="2020-01-01T00:00:00Z")])
    source = read(reader).sources[0]
    assert source["content"]["body"] == "Edited notes"
    assert _timestamp(source) == 1577836800


def test_shared_watch_rejects_private_upstream_before_reading_content():
    reader, calls = make_reader(repo=dict(REPO, private=True))
    with pytest.raises(GitHubUnavailable):
        reader.read_page(target=dict(TARGET, target_repository_id="github:9"), cursor=None, high_watermark=None, event=None)
    assert len(calls) == 1


@pytest.mark.parametrize("query", ["page=2&per_page=100&ignored=", "page=2&per_page=100&page=",
                                  "page=2&per_page=100&per_page="])
def test_next_links_reject_empty_extra_or_duplicate_parameters(query):
    reader, _ = make_reader(link=f'<https://api.github.com/repos/owner/project/releases?{query}>; rel="next"')
    with pytest.raises(ValueError, match="GITHUB_RELEASE_PAGINATION_INVALID"):
        read(reader)


def test_long_scan_can_advance_without_a_per_scan_page_cap():
    import json
    reader, _ = make_reader(link='<https://api.github.com/repos/owner/project/releases?per_page=100&page=2>; rel="next"')
    first = read(reader)
    cursor = json.loads(first.next_cursor)
    cursor["page"] = 200
    later_reader, calls = make_reader(link='<https://api.github.com/repos/owner/project/releases?per_page=100&page=201>; rel="next"')
    later = later_reader.read_page(target=TARGET, cursor=json.dumps(cursor), high_watermark=first.high_watermark, event=None)
    assert len(calls) == 3
    assert json.loads(later.next_cursor)["page"] == 201


def test_canonical_repository_id_next_link_advances_same_repository():
    # Official public /repos/cli/cli/releases response uses the canonical
    # /repositories/212613049/releases Link shape (sampled 2026-09-22).
    import json
    reader, _ = make_reader(link='<https://api.github.com/repositories/42/releases?per_page=100&page=2>; rel="next"')
    page = read(reader)
    assert json.loads(page.next_cursor)["page"] == 2


def test_canonical_next_link_cannot_switch_repository_identity():
    reader, calls = make_reader(link='<https://api.github.com/repositories/99/releases?per_page=100&page=2>; rel="next"')
    with pytest.raises(ValueError, match="GITHUB_RELEASE_PAGINATION_INVALID"):
        read(reader)
    assert all("/repositories/99" not in path for path, _ in calls)
