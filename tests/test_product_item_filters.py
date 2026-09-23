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
