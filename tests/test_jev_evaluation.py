"""Offline evaluation arithmetic using simulated outputs, never provider quality."""
import copy
import json
from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]
evaluate = runpy.run_path(str(ROOT / "scripts/evaluate-ledger-suggestions.py"))["evaluate"]


def recorded_rows():
    # The gold-label predictions here only test metric arithmetic and gates.
    # They are not real results and must never be saved as evaluation evidence.
    source = ROOT / "tests/fixtures/jev-ledger-synthetic-v2.jsonl"
    return [{**json.loads(line), "predictedCategoryId": json.loads(line)["expectedCategoryId"],
        "predictedTargetKind": json.loads(line)["expectedTargetKind"],
        "modelVersion": "jev-1.13.0", "questionVersion": "ledger-suggest-v2",
        "providerOutcome": "valid_response"} for line in source.read_text(encoding="utf-8").splitlines()]


def test_input_fixture_has_bilingual_labels_and_permutations_without_predictions():
    source = ROOT / "tests/fixtures/jev-ledger-synthetic-v2.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 36 and sum(row["language"] == "en" for row in rows) == 18
    assert sum(bool(row.get("variantOf")) for row in rows) == 6
    assert all(row["provenance"] == "agent-authored-synthetic" for row in rows)
    assert all("predictedCategoryId" not in row and "providerOutcome" not in row for row in rows)
    with pytest.raises(ValueError, match="recorded prediction"):
        evaluate(rows)


def test_per_field_uncertainty_preserves_other_correct_suggestion():
    result = evaluate(recorded_rows())
    for language in ("en", "zh"):
        metrics = result["byLanguage"][language]
        assert metrics["samples"] == 18
        assert metrics["category"]["correct"] == 16
        assert metrics["target"]["correct"] == 16
        assert metrics["category"]["expectedUncertain"] == 2
        assert metrics["category"]["uncertainRecall"] == 1
        assert metrics["category"]["selectiveAccuracy"] == 1
    assert result["optionOrder"] == {"pairs": 6, "consistent": 6, "agreement": 1.0}
    assert result["eligibleForReview"] is True


def test_confident_false_hint_on_unknown_label_blocks_review():
    rows = recorded_rows()
    rows[13]["predictedCategoryId"] = "cat_tools"
    result = evaluate(rows)
    assert result["category"]["falseHints"] == 1
    assert result["byLanguage"]["en"]["category"]["uncertainRecall"] == .5
    assert result["eligibleForReview"] is False


def test_high_aggregate_accuracy_does_not_hide_one_language_lack_of_coverage():
    rows = recorded_rows()
    for row in rows:
        if row["language"] == "zh":
            row["predictedCategoryId"] = None
    result = evaluate(rows)
    assert result["category"]["selectiveAccuracy"] == 1
    assert result["byLanguage"]["zh"]["category"]["clearCoverage"] == 0
    assert result["eligibleForReview"] is False


def test_order_changed_to_uncertain_counts_as_disagreement_and_blocks_review():
    rows = recorded_rows()
    rows[-1]["predictedTargetKind"] = None
    result = evaluate(rows)
    assert result["falsePromptRate"] == 0
    assert result["optionOrder"]["consistent"] == 5
    assert result["eligibleForReview"] is False


def test_missing_provider_evidence_cannot_pass_with_correct_simulated_predictions():
    rows = recorded_rows()
    del rows[0]["providerOutcome"]
    result = evaluate(rows)
    assert result["providerEvidenceComplete"] is False
    assert result["eligibleForReview"] is False


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[0].pop("expectedTargetKind"),
    lambda rows: rows[1].update(id=rows[0]["id"]),
    lambda rows: rows[-1].update(variantOf="missing-base"),
    lambda rows: rows[-1].update(expectedTargetKind="shared"),
])
def test_incomplete_or_mismatched_evaluation_rows_are_rejected(mutate):
    rows = copy.deepcopy(recorded_rows())
    mutate(rows)
    with pytest.raises(ValueError):
        evaluate(rows)
