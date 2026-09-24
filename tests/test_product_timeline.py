import json

from pullwise_server.product_timeline import item_timeline


def test_only_verified_ci_successor_creates_relation_to_execution():
    item = {"id": "item-ci", "itemVersion": 1, "revision": 1,
        "repositoryId": "repo-1", "sources": [{"sourceId": "failure",
            "sourceVersion": "sv-1", "sourceRevision": 1}],
        "sourceFacts": {"recoveryStatus": "verified", "recovery": {
            "kind": "same_run_retry_succeeded", "runId": "run-1",
            "runAttempt": 2, "jobId": "job-2",
            "matchRuleVersion": "ci-successor/v1"}},
        "handlingHistory": []}
    versions = [{"item_version": 1, "observed_at": 1800000000,
        "sources_json": json.dumps(item["sources"]),
        "snapshot_json": json.dumps({"assessments": []})}]
    result = item_timeline(item, versions, owner_id="owner", visibility_key="",
        limit=50, cursor=None, request_id="req")
    assert result["relations"] == [{"kind": "same_run_retry_succeeded",
        "fromEventId": "item-ci:iv:1:observed", "fromLoaded": True,
        "toExecution": {"repositoryId": "repo-1", "runId": "run-1",
            "runAttempt": 2, "jobId": "job-2", "loaded": False},
        "matchRuleVersion": "ci-successor/v1"}]
    item["sourceFacts"]["recoveryStatus"] = "unknown"
    assert item_timeline(item, versions, owner_id="owner", visibility_key="",
        limit=50, cursor=None, request_id="req")["relations"] == []


def test_journaled_thread_resolution_attaches_to_matching_item_version():
    item = {"id": "item-pr", "itemVersion": 2, "revision": 2,
        "sources": [{"sourceId": "comment", "sourceVersion": "sv-2",
            "sourceRevision": 2}], "sourceFacts": {}, "handlingHistory": []}
    versions = [
        {"item_version": 1, "observed_at": 1800000000,
         "sources_json": json.dumps([{"sourceId": "comment", "sourceVersion": "sv-1",
             "sourceRevision": 1}]), "snapshot_json": "{}"},
        {"item_version": 2, "observed_at": 1800000002,
         "sources_json": json.dumps(item["sources"]), "snapshot_json": "{}"},
    ]
    source_events = [{"id": "source-event-1", "source_id": "comment",
        "source_revision": 2, "event_type": "thread_resolved",
        "occurred_at": None, "observed_at": 1800000002}]
    result = item_timeline(item, versions, owner_id="owner", visibility_key="",
        limit=50, cursor=None, request_id="req", source_events=source_events)
    event = next(event for event in result["items"] if event["eventType"] == "thread_resolved")
    assert event["itemVersion"] == 2
    assert event["occurredAt"] is None and event["timeBasis"] == "observed"
    assert event["sourceRefs"] == item["sources"]
