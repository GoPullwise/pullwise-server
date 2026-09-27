#!/usr/bin/env python3
"""Offline labeled Jev suggestion evaluation; no network or customer data output.

JSONL fields: language (en/zh), expectedCategoryId, expectedTargetKind,
predictedCategoryId, predictedTargetKind, and uncertainty (boolean).
IDs should be anonymized. Only aggregate metrics are printed.
"""
import argparse
import json
from pathlib import Path


def evaluate(rows):
    counts = {"en": 0, "zh": 0}
    correct_category = correct_target = uncertain = false_prompt = 0
    for row in rows:
        language = row.get("language")
        if language not in counts:
            raise ValueError("language must be en or zh")
        counts[language] += 1
        expected_category = row.get("expectedCategoryId")
        expected_target = row.get("expectedTargetKind")
        if (not isinstance(expected_category, str) or not expected_category
                or expected_target not in {"project", "shared"}):
            raise ValueError("missing expected category or target")
        if row.get("uncertainty") is True:
            uncertain += 1
            continue
        category = row.get("predictedCategoryId")
        target = row.get("predictedTargetKind")
        correct_category += category == expected_category
        correct_target += target == expected_target
        false_prompt += bool(category and category != expected_category) or bool(
            target and target != expected_target)
    total = sum(counts.values())
    assert total
    result = {"samples": total, "byLanguage": counts,
        "categoryAccuracy": round(correct_category / total, 4),
        "targetAccuracy": round(correct_target / total, 4),
        "falsePromptRate": round(false_prompt / total, 4),
        "uncertainRate": round(uncertain / total, 4)}
    # A small synthetic smoke fixture can exercise the harness, but cannot
    # authorize production. Real anonymized labeled samples are required.
    result["eligibleForReview"] = (all(value >= 15 for value in counts.values())
        and result["categoryAccuracy"] >= 0.85 and result["targetAccuracy"] >= 0.85
        and result["falsePromptRate"] <= 0.05)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.samples.read_text().splitlines() if line.strip()]
    print(json.dumps(evaluate(rows), sort_keys=True))


if __name__ == "__main__":
    main()
