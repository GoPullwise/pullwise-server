"""Saved visualization counts use Item identities and current scopes."""
import pytest

from pullwise_server.product_visualizations import (pr_actions_visualization,
    ci_failures_visualization, updates_releases_visualization)


def _item(identifier, *, number, actions, state="needs_action"):
    return {"id": identifier, "module": "pr", "repositoryId": "repo-1",
        "sourceFacts": {"pullNumber": number}, "actionTypes": actions,
        "attentionState": state, "nextActors": [], "lastSyncedAt": None}


def test_pr_matrix_counts_one_item_with_two_actions_and_paginates_rows():
    items = [_item("one", number=1, actions=["change_requested", "reply_needed"]),
             _item("two", number=1, actions=["reply_needed"]),
             _item("three", number=2, actions=["review_requested"])]
    params = {"module": "pr", "repositoryId": "repo-1", "view": "all", "limit": "1"}
    first = pr_actions_visualization(items, [], params, owner_id="owner",
        now=1800000000, request_id="req-1")
    assert first["totalCount"] == 3 and first["data"]["rowsTotal"] == 2
    assert first["hasMore"] is True and first["nextCursor"]
    row = first["data"]["rows"][0]
    assert row["totalCount"] == 2
    counts = {cell["key"]: cell["count"] for cell in row["cells"]}
    assert counts["change_requested"] == 1 and counts["reply_needed"] == 2
    drilldown = next(cell["drilldown"] for cell in row["cells"]
                     if cell["key"] == "change_requested")
    assert drilldown == {"resource": "items", "filters": {
        "module": "pr", "repositoryId": "repo-1", "view": "all",
        "pullNumber": "1", "actionType": "change_requested"}}
    second = pr_actions_visualization(items, [], {**params, "cursor": first["nextCursor"]},
        owner_id="owner", now=1800000000, request_id="req-2")
    assert second["totalCount"] == 3 and second["data"]["rowsTotal"] == 2
    assert second["data"]["rows"][0]["pullNumber"] == 2
    assert second["hasMore"] is False
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        pr_actions_visualization(items, [], {**params, "repositoryId": "other",
            "cursor": first["nextCursor"]}, owner_id="owner", now=1800000000,
            request_id="req-3")
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        pr_actions_visualization(items, [], {**params, "cursor": first["nextCursor"]},
            owner_id="other-owner", now=1800000000, request_id="req-4")
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        pr_actions_visualization(items, [], {**params, "cursor": first["nextCursor"]},
            owner_id="owner", visibility_key='{"repositoryIds":["other"]}',
            now=1800000000, request_id="req-4b")
    action_scope = pr_actions_visualization(items[:2], [],
        {**params, "actionType": "reply_needed"}, owner_id="owner",
        now=1800000000, request_id="req-5")
    assert [cell["key"] for cell in action_scope["data"]["rows"][0]["cells"]] == ["reply_needed"]


def test_ci_matrix_pairs_stage_and_symptom_inside_one_window():
    items = [{"id": "ci-one", "module": "ci", "repositoryId": "repo-1",
        "sourceFacts": {"runId": "7", "runAttempt": 1, "jobId": "9",
            "windows": [
                {"windowId": "install", "stage": "dependency_install",
                 "symptoms": ["connection_timeout"]},
                {"windowId": "test", "stage": "test",
                 "symptoms": ["assertion_failure"]}]}},
        {"id": "ci-two", "module": "ci", "repositoryId": "repo-1",
         "sourceFacts": {"runId": "7", "runAttempt": 1, "jobId": "10",
             "windows": [{"windowId": "build", "stage": "build", "symptoms": []}]}}]
    result = ci_failures_visualization(items, [], {"module": "ci", "repositoryId": "repo-1"},
        now=1800000000, request_id="req-ci")
    assert result["countUnit"] == "ci_job_attempt"
    assert result["totalCount"] == 2
    assert result["data"]["unclassifiedCount"] == 1
    cells = {(cell["rowKey"], cell["columnKey"]): cell
             for cell in result["data"]["cells"]}
    assert cells[("dependency_install", "connection_timeout")]["count"] == 1
    assert cells[("test", "assertion_failure")]["count"] == 1
    assert cells[("dependency_install", "assertion_failure")]["count"] == 0
    assert cells[("dependency_install", "connection_timeout")]["drilldown"] == {
        "resource": "items", "filters": {"module": "ci", "repositoryId": "repo-1",
            "view": "all", "ciStage": "dependency_install",
            "ciSymptom": "connection_timeout"}}


def test_updates_table_keeps_release_watch_rows_without_items_or_analysis():
    source = {"id": "release-1", "type": "release", "repositoryId": "upstream",
        "sourceFacts": {"releaseId": "77", "tagName": "v2", "name": "SDK 2",
            "publishedAt": "2026-09-20T00:00:00Z"},
        "sourceUrl": "https://github.com/acme/sdk/releases/tag/v2",
        "lastSyncedAt": "2026-09-21T00:00:00Z", "completeness": "partial",
        "contexts": [
            {"id": "context-a", "watchId": "watch-a", "itemId": None,
             "contextVersion": 2, "processingStatus": "assessed",
             "contextStale": False, "coverage": {"state": "partial",
                 "selectedUnits": 1, "totalUnits": 3, "limitations": ["input_limit"]},
             "relevance": "relevant", "updateSignals": {
                 "migration_stated": "present", "deprecation_stated": None},
             "units": [{"evidenceIds": ["ev-a"], "relevance": "relevant",
                 "updateSignals": {"migration_stated": "present"}}]},
            {"id": "context-b", "watchId": "watch-b", "itemId": None,
             "contextVersion": 1, "processingStatus": "analysis_disabled",
             "contextStale": False, "coverage": {"state": "complete",
                 "selectedUnits": 0, "totalUnits": 0, "limitations": []},
             "relevance": None, "updateSignals": {}}]}
    result = updates_releases_visualization([source],
        {"module": "updates"}, owner_id="owner", now=1800000000,
        request_id="req-updates")
    assert result["countUnit"] == "release_watch"
    assert result["totalCount"] == 2
    rows = {row["watchId"]: row for row in result["data"]["rows"]}
    assert rows["watch-a"]["relevance"] == "relevant"
    assert rows["watch-a"]["updateSignals"]["migration_stated"] == "present"
    assert rows["watch-a"]["evidenceIds"] == ["ev-a"]
    assert rows["watch-b"]["relevance"] is None
    assert all(value is None for value in rows["watch-b"]["updateSignals"].values())
    assert rows["watch-b"]["itemId"] is None
    first = updates_releases_visualization([source], {"limit": "1"},
        owner_id="owner", now=1800000000, request_id="req-page")
    assert first["hasMore"] and len(first["data"]["rows"]) == 1
    second = updates_releases_visualization([source], {"limit": "1",
        "cursor": first["nextCursor"]}, owner_id="owner", now=1800000000,
        request_id="req-page-2")
    assert second["totalCount"] == 2 and len(second["data"]["rows"]) == 1
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        updates_releases_visualization([source], {"limit": "1", "watchId": "watch-a",
            "cursor": first["nextCursor"]}, owner_id="owner", now=1800000000,
            request_id="req-invalid")
