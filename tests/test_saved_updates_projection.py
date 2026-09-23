from pullwise_server.update_filter import project_saved_updates


SIGNALS = ("migration_stated", "deprecation_stated", "breaking_change_stated", "security_fix_stated")


def assessment(units):
    answers, bindings = {}, {}
    for index, values in enumerate(units):
        for field, choice in zip(("relevance", *SIGNALS), values):
            key = f"u{index}_{field}"
            answers[key] = {"choice": choice, "confidence": 0.95}
            bindings[key] = {"changeUnitId": f"cu{index}", "evidenceIds": [f"ev{index}"]}
    return {"questionVersion": "updates-filter/v3", "answers": answers, "bindings": bindings}


def test_unrelated_unit_signal_cannot_be_borrowed_by_relevant_unit():
    result = project_saved_updates(assessment([
        ("relevant", "absent", "absent", "absent", "absent"),
        ("not_relevant", "present", "absent", "absent", "absent"),
    ]), {"state": "complete", "selectedUnits": 2, "rawSourcePartial": False})
    assert result["relevance"] == "relevant"
    assert result["updateSignals"]["migration_stated"] == "unclear"


def test_partial_negative_is_never_release_negative():
    result = project_saved_updates(assessment([
        ("not_relevant", "absent", "absent", "absent", "absent"),
    ]), {"state": "partial", "selectedUnits": 1})
    assert result["relevance"] == "unclear"
    assert all(value == "unclear" for value in result["updateSignals"].values())


def test_fully_unrelated_release_has_inapplicable_signals_not_absence():
    result = project_saved_updates(assessment([
        ("not_relevant", "absent", "absent", "absent", "absent"),
    ]), {"state": "complete", "selectedUnits": 1, "rawSourcePartial": False})
    assert result["relevance"] == "not_relevant"
    assert all(value is None for value in result["updateSignals"].values())


def test_partial_positive_keeps_its_evidence():
    result = project_saved_updates(assessment([
        ("relevant", "present", "absent", "absent", "absent"),
    ]), {"state": "partial", "selectedUnits": 1})
    assert result["updateSignals"]["migration_stated"] == "present"
    assert result["units"][0]["evidenceIds"] == ["ev0"]


def test_incomplete_question_set_is_not_a_negative_classification():
    saved = assessment([("not_relevant", "absent", "absent", "absent", "absent")])
    del saved["answers"]["u0_migration_stated"]
    assert project_saved_updates(saved, {"state": "complete", "selectedUnits": 1}) is None
