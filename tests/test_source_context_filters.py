import pytest

from pullwise_server.product_api import _apply_source_restrictions, _filter_sources


def source():
    return {"id": "release", "type": "release", "repositoryId": "upstream", "contexts": [
        {"id": "a", "watchId": "watch-a", "relevance": "relevant",
         "updateSignals": {"migration_stated": "present"}, "assessments": [{"id": "private-a"}]},
        {"id": "b", "watchId": "watch-b", "relevance": "not_relevant", "updateSignals": {}},
    ]}


def test_watch_restriction_removes_other_contexts_not_just_other_sources():
    result = _apply_source_restrictions([source()], {"watchIds": ["watch-b"]})
    assert [context["id"] for context in result[0]["contexts"]] == ["b"]


def test_shared_watch_requires_both_target_and_watch_restrictions():
    shared = source()
    shared["contexts"] = [{**shared["contexts"][0], "targetRepositoryId": "target"}]
    assert _apply_source_restrictions([shared], {"repositoryIds": ["other"], "watchIds": ["watch-a"]}) == []
    assert _apply_source_restrictions([shared], {"repositoryIds": ["target"]}) == [shared]


def test_watch_filter_does_not_borrow_another_context_signal():
    assert _filter_sources([source()], {"module": "updates", "watchId": "watch-b",
                                        "updateSignal": "migration_stated"}) == []
    result = _filter_sources([source()], {"module": "updates", "watchId": "watch-a",
                                         "relevance": "relevant", "updateSignal": "migration_stated"})
    assert [context["id"] for context in result[0]["contexts"]] == ["a"]


@pytest.mark.parametrize("params", [{"module": "ci", "relevance": "relevant"},
                                     {"module": "updates", "relevance": "safe"},
                                     {"module": "updates", "updateSignal": "safe"}])
def test_invalid_classification_filters_are_rejected(params):
    with pytest.raises(ValueError, match="INVALID_CONFIGURATION"):
        _filter_sources([source()], params)
