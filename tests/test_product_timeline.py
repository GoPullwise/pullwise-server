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
