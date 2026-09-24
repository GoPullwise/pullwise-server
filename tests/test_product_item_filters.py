"""Saved Item filters must not infer labels from unrelated modules."""
import pytest

from pullwise_server.product_item_filters import filter_items


def _item(module, *, repository="repo", action="investigate_failure",
          facts=None, disposition="open", lifecycle="active"):
    return {"id": module, "module": module, "repositoryId": repository,
        "watchId": None, "actionTypes": [action], "sourceFacts": facts or {},
        "handling": {"disposition": disposition}, "lifecycle": lifecycle,
        "attentionState": "needs_action", "nextActors": []}


def test_item_filter_selects_action_lifecycle_and_disposition_together():
    items = [_item("pr", action="reply", disposition="done"),
             _item("ci", action="investigate_failure"),
             _item("updates", action="reply", disposition="done", lifecycle="source_closed")]
    assert filter_items(items, {"actionType": "reply", "disposition": "done",
                                "lifecycle": "active"}, "", include_view=True) == items[:1]


def test_pull_and_run_identity_filters_require_module_and_repository():
    items = [_item("pr", facts={"pullNumber": 12}),
             _item("ci", facts={"runId": "77"})]
    assert filter_items(items, {"module": "pr", "repositoryId": "repo",
                                "pullNumber": "12"}, "", include_view=True) == items[:1]
    assert filter_items(items, {"module": "ci", "repositoryId": "repo",
                                "runId": "77"}, "", include_view=True) == items[1:]
    for query in ({"pullNumber": "12"}, {"module": "ci", "repositoryId": "repo", "pullNumber": "12"},
                  {"module": "ci", "runId": "77"}):
        with pytest.raises(ValueError):
            filter_items(items, query, "", include_view=True)


def test_ci_stage_and_symptom_must_pair_within_one_window():
    paired = _item("ci", facts={"windows": [
        {"windowId": "install", "stage": "dependency_install",
         "symptoms": ["connection_timeout"]},
        {"windowId": "test", "stage": "test",
         "symptoms": ["assertion_failure"]}]})
    unclassified = _item("ci", facts={"windows": [
        {"windowId": "build", "stage": "build", "symptoms": []}]})
    unclassified["id"] = "ci-unclassified"
    items = [paired, unclassified]
    base = {"module": "ci"}
    assert filter_items(items, {**base, "ciStage": "dependency_install",
        "ciSymptom": "connection_timeout"}, "", include_view=True) == [paired]
    assert filter_items(items, {**base, "ciStage": "dependency_install",
        "ciSymptom": "assertion_failure"}, "", include_view=True) == []
    assert filter_items(items, {**base, "classificationState": "identified"},
        "", include_view=True) == [paired]
    assert filter_items(items, {**base, "classificationState": "unclassified"},
        "", include_view=True) == [unclassified]
    for query in ({"ciStage": "test"}, {**base, "ciSymptom": "invented"},
                  {**base, "classificationState": "unknown"}):
        with pytest.raises(ValueError, match="INVALID_CONFIGURATION"):
            filter_items(items, query, "", include_view=True)


def test_text_query_matches_authorized_item_title_without_scanning_evidence():
    first = {**_item("pr"), "id": "pr-cache", "title": "Clarify Cache Invalidation"}
    second = {**_item("pr"), "id": "pr-other", "title": "Review auth flow"}
    assert filter_items([first, second], {"module": "pr", "q": "cache"},
        "", include_view=True) == [first]
    with pytest.raises(ValueError, match="INVALID_CONFIGURATION"):
        filter_items([first], {"q": "x" * 201}, "", include_view=True)
