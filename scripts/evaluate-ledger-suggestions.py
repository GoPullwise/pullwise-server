#!/usr/bin/env python3
"""Evaluate recorded Jev results offline; never call a provider or invent predictions.

JSONL contains language, expectedCategoryId/expectedTargetKind and the independent
predictedCategoryId/predictedTargetKind (null means no actionable hint). Null gold
labels explicitly require uncertainty. Optional id/variantOf links an option-order
variant to its base. Only aggregate metrics are printed.
"""
import argparse
import json
from pathlib import Path


def evaluate(rows):
    rows = list(rows)
    if not rows:
        raise ValueError("evaluation requires recorded results")
    def field_counts():
        return {"labeled": 0, "clear": 0, "clearSuggested": 0, "suggested": 0,
                "correct": 0, "falseHints": 0, "expectedUncertain": 0,
                "correctlyWithheld": 0}
    counts = {language: {"samples": 0, "category": field_counts(), "target": field_counts()}
              for language in ("en", "zh")}
    indexed = {}
    false_prompt = uncertain = 0
    for row in rows:
        language = row.get("language")
        if language not in counts:
            raise ValueError("language must be en or zh")
        counts[language]["samples"] += 1
        row_false = False
        row_uncertain = False
        for field, suffix in (("category", "CategoryId"), ("target", "TargetKind")):
            expected_key, predicted_key = "expected" + suffix, "predicted" + suffix
            if expected_key not in row or predicted_key not in row:
                raise ValueError("missing gold label or recorded prediction")
            expected, predicted = row[expected_key], row[predicted_key]
            if field == "target":
                if expected not in {None, "project", "shared"} or predicted not in {None, "project", "shared"}:
                    raise ValueError("invalid target label")
            elif any(value is not None and (not isinstance(value, str) or not value)
                     for value in (expected, predicted)):
                raise ValueError("invalid category label")
            bucket = counts[language][field]
            bucket["labeled"] += 1
            bucket["clear"] += expected is not None
            bucket["expectedUncertain"] += expected is None
            bucket["suggested"] += predicted is not None
            bucket["clearSuggested"] += expected is not None and predicted is not None
            bucket["correct"] += predicted is not None and predicted == expected
            bucket["correctlyWithheld"] += expected is None and predicted is None
            wrong = predicted is not None and predicted != expected
            bucket["falseHints"] += wrong
            row_false |= wrong
            row_uncertain |= predicted is None
        false_prompt += row_false
        uncertain += row_uncertain
        sample_id = row.get("id")
        if sample_id is not None:
            if not isinstance(sample_id, str) or not sample_id or sample_id in indexed:
                raise ValueError("evaluation ids must be unique non-empty strings")
            indexed[sample_id] = row
    order_pairs = order_consistent = 0
    for row in rows:
        base_id = row.get("variantOf")
        if base_id is None:
            continue
        base = indexed.get(base_id)
        if (base is None or base is row or base.get("variantOf") is not None
                or base["language"] != row["language"]
                or any(base["expected" + field] != row["expected" + field]
                       for field in ("CategoryId", "TargetKind"))):
            raise ValueError("option-order variant has no matching base labels")
        order_pairs += 1
        order_consistent += all(base["predicted" + field] == row["predicted" + field]
                                for field in ("CategoryId", "TargetKind"))
    def summarize(bucket):
        def rate(numerator, denominator):
            return round(numerator / denominator, 4) if denominator else None
        return {**bucket, "selectiveAccuracy": rate(bucket["correct"], bucket["suggested"]),
                "clearCoverage": rate(bucket["clearSuggested"], bucket["clear"]),
                "clearAccuracy": rate(bucket["correct"], bucket["clear"]),
                "uncertainRecall": rate(bucket["correctlyWithheld"], bucket["expectedUncertain"])}
    metrics = {language: {"samples": bucket["samples"],
                          **{field: summarize(bucket[field]) for field in ("category", "target")}}
               for language, bucket in counts.items()}
    total = len(rows)
    totals = {field: summarize({key: sum(bucket[field][key] for bucket in counts.values())
                               for key in field_counts()}) for field in ("category", "target")}
    result = {"samples": total, "byLanguage": metrics, "category": totals["category"],
        "target": totals["target"], "falsePromptRate": round(false_prompt / total, 4),
        "uncertainRate": round(uncertain / total, 4),
        "optionOrder": {"pairs": order_pairs, "consistent": order_consistent,
                        "agreement": round(order_consistent / order_pairs, 4) if order_pairs else None},
        "provenance": sorted({row.get("provenance", "unspecified") for row in rows}),
        "providerEvidenceComplete": all(row.get("modelVersion") == "jev-1.13.0"
            and row.get("questionVersion") == "ledger-suggest-v2"
            and row.get("providerOutcome") == "valid_response" for row in rows)}
    # This gate reviews the fixed small synthetic suite only. It is not evidence
    # of customer-data quality, production activation or calibrated error rates.
    result["eligibleForReview"] = (result["providerEvidenceComplete"] and false_prompt == 0
        and order_pairs >= 6 and order_consistent == order_pairs
        and all(bucket["samples"] >= 15 and all(
            bucket[field]["clear"] >= 12
            and (bucket[field]["clearCoverage"] or 0) >= 0.85
            and bucket[field]["expectedUncertain"] >= 1
            and bucket[field]["uncertainRecall"] == 1.0
            for field in ("category", "target")) for bucket in metrics.values()))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(evaluate(rows), sort_keys=True))


if __name__ == "__main__":
    main()
