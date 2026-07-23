"""Executable Python facade semantics for task publication documents."""

from __future__ import annotations


PYTHON_PUBLICATION = r'''
_ARTIFACT_CONTENT_TUPLES = (
    ("change_set", "change-set/v1", "application/json", "utf-8"),
    ("change_set_patch", "change-set-patch/v1", "application/json", "utf-8"),
    ("r0_read_result", "r0-read-result/v1", "application/json", "utf-8"),
    ("source_content", "source-content/v1", "application/json", "utf-8"),
    ("task_report", "task-report/v1", "application/json", "utf-8"),
)


def _rule_artifact_content_registry(value: dict[str, object]) -> None:
    _verify_embedded_digest("artifact-content-registry/v1", value)
    actual = tuple(
        (
            item["artifact_kind"],
            item["content_schema_id"],
            item["media_type"],
            item["encoding"],
        )
        for item in value["entries"]
    )
    _require(
        actual == _ARTIFACT_CONTENT_TUPLES,
        "ARTIFACT_CONTENT_REGISTRY_INVALID",
    )


def _rule_artifact_content_ref(value: dict[str, object]) -> None:
    expected = next(
        (
            item
            for item in _ARTIFACT_CONTENT_TUPLES
            if item[0] == value["artifact_kind"]
        ),
        None,
    )
    ref = value["ref"]
    _require(
        expected is not None
        and (
            ref["content_schema_id"], ref["media_type"], ref["encoding"]
        )
        == expected[1:],
        "ARTIFACT_CONTENT_TUPLE_INVALID",
    )


def _rule_budget_summary(value: dict[str, object]) -> None:
    _verify_embedded_digest("budget-summary/v1", value)
    if value["consumed_ms"] > value["elapsed_limit_ms"]:
        _fail(
            "BUDGET_SUMMARY_ELAPSED_INVALID",
            code="BUDGET_EXHAUSTED",
        )
    if value["calls_consumed"] > value["tool_call_limit"]:
        _fail(
            "BUDGET_SUMMARY_CALLS_INVALID",
            code="BUDGET_EXHAUSTED",
        )


def _rule_effect_ledger_snapshot(value: dict[str, object]) -> None:
    _verify_embedded_digest('effect-ledger-snapshot/v1', value)
    rows = value['rows']
    actual_counts = {
        'prepared': 0,
        'dispatched': 0,
        'committed': 0,
        'not_applied': 0,
        'rejected': 0,
        'unknown': 0,
    }
    for row in rows:
        actual_counts[row['state'].lower()] += 1
    _require(
        value['watermark'] == len(rows),
        'EFFECT_LEDGER_WATERMARK_INVALID',
        '$.watermark',
    )
    _require(
        value['state_counts'] == actual_counts,
        'EFFECT_LEDGER_STATE_COUNTS_INVALID',
        '$.state_counts',
    )
    _require(
        rows == sorted(rows, key=lambda item: item['effect_id']),
        'EFFECT_LEDGER_ROW_ORDER_INVALID',
        '$.rows',
    )
def _rule_task_report(value: dict[str, object]) -> None:
    _verify_embedded_digest("task-report/v1", value)
    _require(
        _ordered_unique(value["sections"], lambda item: item["section_id"]),
        "TASK_REPORT_SECTION_ORDER_INVALID",
        "$.sections",
    )
    for field, limit in (("title", 512), ("summary", 4096)):
        _require(
            len(value[field].encode("utf-8")) <= limit,
            "TASK_REPORT_UTF8_LIMIT_INVALID",
            f"$.{field}",
        )
    all_refs: list[dict[str, object]] = []
    for index, section in enumerate(value["sections"]):
        _require(
            len(section["title"].encode("utf-8")) <= 512
            and len(section["body"].encode("utf-8")) <= 65536,
            "TASK_REPORT_UTF8_LIMIT_INVALID",
            f"$.sections[{index}]",
        )
        _require(
            _ordered_unique(section["evidence_refs"], _ref_key),
            "TASK_REPORT_EVIDENCE_ORDER_INVALID",
            f"$.sections[{index}].evidence_refs",
        )
        all_refs.extend(section["evidence_refs"])
    verify_content_ref_set(all_refs)
'''


__all__ = ["PYTHON_PUBLICATION"]
