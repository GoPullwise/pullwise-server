"""Only verified GitHub fact transitions enter the append-only event journal."""
from contextlib import closing
from datetime import datetime, timezone

from pullwise_server.product_store import ProductStore
from pullwise_server.product_source_events import source_transition_events


def _upsert(store, *, resolved, coverage="complete", observed_at):
    return store.upsert_source_snapshot(source_id="comment-1",
        source_type="pr_review_comment", external_key="github:comment:1",
        repository_id="repo-1", content={"body": "Please check this"},
        source_facts={"threadId": "thread-1", "associationVerified": True,
            "threadCoverage": coverage, "isResolved": resolved},
        source_url="https://github.com/acme/api/pull/1#discussion_r1",
        processing_mode="model", completeness="complete", lifecycle="active",
        observed_at=observed_at)


def test_verified_thread_transition_is_journaled_once_with_observed_time(tmp_path):
    store = ProductStore(tmp_path / "domain.db")
    store.initialize()
    _upsert(store, resolved=False, observed_at=1800000000)
    _upsert(store, resolved=False, observed_at=1800000001)
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM source_fact_events").fetchone()[0] == 0
    _upsert(store, resolved=True, observed_at=1800000002)
    with closing(store.connect()) as db:
        event = db.execute("SELECT source_revision,event_type,occurred_at,observed_at "
            "FROM source_fact_events").fetchone()
        assert tuple(event) == (2, "thread_resolved", None, 1800000002)
    _upsert(store, resolved=True, observed_at=1800000003)
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM source_fact_events").fetchone()[0] == 1


def test_partial_thread_coverage_does_not_invent_resolution_event(tmp_path):
    store = ProductStore(tmp_path / "domain.db")
    store.initialize()
    _upsert(store, resolved=False, observed_at=1800000000)
    _upsert(store, resolved=True, coverage="partial", observed_at=1800000001)
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM source_fact_events").fetchone()[0] == 0


def test_journaled_resolution_reaches_authorized_item_timeline(tmp_path):
    store = ProductStore(tmp_path / "domain.db")
    store.initialize()
    context_id = "repo:repo-1"
    first = _upsert(store, resolved=False, observed_at=1800000000)
    store.set_source_context(source_id="comment-1", context_id=context_id,
        context_version=1, configuration_revision=1, authorization_revision=1,
        authorization_valid_until=1900000000, accessible=True,
        billing_owner_id="owner", watch_id=None, processing_status="rules_only",
        analysis_enabled=False, context_stale=False,
        coverage={"state": "complete", "selectedUnits": 0,
            "rawSourcePartial": False, "limitations": []})
    item = store.create_item(context_id=context_id, unit_type="pr_comment",
        unit_key="comment-1")

    def publish(source, *, resolved, expected_revision, observed_at):
        return store.publish_item_snapshot(item_id=item["id"],
            expected_item_revision=expected_revision,
            sources=[{"sourceId": "comment-1", "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"]}],
            context_fences=[{"sourceId": "comment-1", "contextId": context_id,
                "contextVersion": 1, "configurationRevision": 1,
                "authorizationRevision": 1}],
            snapshot={"module": "pr", "repositoryId": "repo-1",
                "watchId": None, "title": "Thread", "actionTypes": ["reply_needed"],
                "attentionState": "needs_action", "lifecycle": "active",
                "sourceFacts": {"threadResolved": resolved}},
            observed_at=observed_at)

    version_one = publish(first, resolved=False,
        expected_revision=item["revision"], observed_at=1800000000)
    second = _upsert(store, resolved=True, observed_at=1800000002)
    publish(second, resolved=True, expected_revision=version_one["revision"],
        observed_at=1800000002)
    timeline = store.item_timeline_for_billing_owner("owner", item["id"],
        request_id="req")
    assert timeline is not None
    event = next(event for event in timeline["items"] if event["eventType"] == "thread_resolved")
    assert event["itemVersion"] == 2
    assert event["sourceRefs"][0]["sourceRevision"] == 2


def test_only_authoritative_followup_facts_name_specific_github_events():
    assert source_transition_events("pr_comment",
        {"updatedAt": "2026-09-20T00:00:00Z"},
        {"updatedAt": "2026-09-21T00:00:00Z"},
        previous_content_hash="old", current_content_hash="new") == ("comment_edited",)
    assert source_transition_events("pr_comment", {}, {},
        previous_content_hash="old", current_content_hash="new") == ()
    assert source_transition_events("pr_state", {"state": "open"},
        {"state": "closed", "mergedAt": "2026-09-21T00:00:00Z"}) == ("pr_merged",)
    assert source_transition_events("release", {"releaseId": "5",
        "updatedAt": "2026-09-20T00:00:00Z"},
        {"releaseId": "5", "updatedAt": "2026-09-21T00:00:00Z"}) == ("release_edited",)
    assert source_transition_events("release", {"releaseId": "5"},
        {"releaseId": "5"}, previous_lifecycle="active",
        current_lifecycle="source_deleted") == ("source_deleted",)


def test_confirmed_comment_edit_keeps_github_occurrence_time(tmp_path):
    store = ProductStore(tmp_path / "domain.db")
    store.initialize()

    def observe(body, updated_at, observed_at):
        store.upsert_source_snapshot(source_id="comment", source_type="pr_comment",
            external_key="github:comment:9", repository_id="repo-1",
            content={"body": body}, source_facts={"updatedAt": updated_at},
            source_url="https://github.com/acme/api/issues/1#issuecomment-9",
            processing_mode="model", completeness="complete", lifecycle="active",
            observed_at=observed_at)

    observe("before", "2026-09-20T00:00:00Z", 1800000000)
    observe("after", "2026-09-21T00:00:00Z", 1800000002)
    with closing(store.connect()) as db:
        event = db.execute("SELECT event_type,occurred_at,observed_at "
            "FROM source_fact_events").fetchone()
    expected = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
    assert tuple(event) == ("comment_edited", expected, 1800000002)
