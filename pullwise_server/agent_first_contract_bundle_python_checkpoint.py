"""Python facade semantics for dual-layer committed checkpoints."""

from __future__ import annotations


PYTHON_CHECKPOINT = r'''
def _checkpoint_current_package(value: dict[str, object]) -> None:
    _require(
        _json_equal(value["package"], package_tuple()),
        "CHECKPOINT_PACKAGE_MISMATCH",
        "$.package",
    )


def _rule_machine_checkpoint(value: dict[str, object]) -> None:
    _checkpoint_current_package(value)


def _rule_semantic_checkpoint(value: dict[str, object]) -> None:
    _checkpoint_current_package(value)
    summary = value["owner_summary"]
    for field in (
        "completed_requirement_ids",
        "next_requirement_ids",
        "unresolved_question_ids",
        "residual_risk_ids",
    ):
        _require(
            _sorted_unique(summary[field]),
            "SEMANTIC_CHECKPOINT_SUMMARY_ORDER_INVALID",
            f"$.owner_summary.{field}",
        )
    _require(
        set(summary["completed_requirement_ids"]).isdisjoint(
            summary["next_requirement_ids"]
        ),
        "SEMANTIC_CHECKPOINT_REQUIREMENT_OVERLAP",
        "$.owner_summary",
    )
    _require(
        _sorted_unique(value["pending_interaction_ids"])
        and summary["unresolved_question_ids"]
        == value["pending_interaction_ids"],
        "SEMANTIC_CHECKPOINT_INTERACTION_MISMATCH",
        "$.pending_interaction_ids",
    )
    _require(
        len(summary["objective_restated"].encode("utf-8")) <= 16384,
        "UTF8_BYTE_LIMIT_INVALID",
        "$.owner_summary.objective_restated",
    )
    evidence = verify_content_ref_set(value["evidence_refs"])
    _require(
        _ordered_unique(evidence, _ref_key),
        "SEMANTIC_CHECKPOINT_EVIDENCE_ORDER_INVALID",
        "$.evidence_refs",
    )


def _rule_committed_checkpoint_manifest(value: dict[str, object]) -> None:
    _checkpoint_current_package(value)
    generation = value["generation"]
    predecessor_valid = (
        value["previous_generation"] == generation - 1
        and (
            (generation == 1 and value["previous_manifest_hash"] is None)
            or (generation > 1 and value["previous_manifest_hash"] is not None)
        )
    )
    _require(
        predecessor_valid,
        "CHECKPOINT_PREDECESSOR_INVALID",
        "$.previous_generation",
    )
    _require(
        value["committed_task_version"]
        == value["committed_from_task_version"] + 1,
        "CHECKPOINT_TASK_VERSION_INVALID",
        "$.committed_task_version",
    )


def _checkpoint_ref_matches(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> bool:
    raw = canonical_document_bytes(document)
    return (
        ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def verify_committed_checkpoint_context(
    manifest: object,
    machine_state: object,
    semantic_state: object,
    previous_manifest: object | None = None,
) -> dict[str, object]:
    committed = verify_document_digest(
        "committed-checkpoint-manifest/v1", manifest
    )
    machine = verify_document_digest("machine-checkpoint/v1", machine_state)
    semantic = verify_document_digest("semantic-checkpoint/v1", semantic_state)
    _require(
        _json_equal(committed["package"], machine["package"])
        and _json_equal(committed["package"], semantic["package"]),
        "CHECKPOINT_PACKAGE_MISMATCH",
        "$.package",
    )
    _require(
        committed["task_id"] == machine["task_id"] == semantic["task_id"],
        "CHECKPOINT_TASK_BINDING_MISMATCH",
        "$.task_id",
    )
    _require(
        committed["generation"]
        == machine["generation"]
        == semantic["generation"],
        "CHECKPOINT_GENERATION_MISMATCH",
        "$.generation",
    )
    _require(
        machine["task_version"]
        == semantic["task_version"]
        == committed["committed_from_task_version"],
        "CHECKPOINT_TASK_VERSION_MISMATCH",
        "$.committed_from_task_version",
    )
    _require(
        committed["attempt_id"] == machine["attempt_id"]
        and committed["native_epoch"] == machine["native_epoch"]
        and committed["owner_epoch"]
        == machine["owner_epoch"]
        == semantic["owner_epoch"]
        and machine["owner_id"] == semantic["owner_id"],
        "CHECKPOINT_AUTHORITY_BINDING_MISMATCH",
        "$.attempt_id",
    )
    _require(
        _checkpoint_ref_matches(
            committed["machine_state_ref"], "machine-checkpoint/v1", machine
        ),
        "CHECKPOINT_STATE_REF_MISMATCH",
        "$.machine_state_ref",
    )
    _require(
        _checkpoint_ref_matches(
            committed["semantic_state_ref"], "semantic-checkpoint/v1", semantic
        ),
        "CHECKPOINT_STATE_REF_MISMATCH",
        "$.semantic_state_ref",
    )
    for field in (
        "budget_watermark",
        "effect_watermark",
        "observation_watermark",
        "event_seq",
    ):
        _require(
            committed[field] == machine[field],
            "CHECKPOINT_WATERMARK_MISMATCH",
            f"$.{field}",
        )
    _require(
        committed["created_at"] == machine["created_at"] == semantic["created_at"],
        "CHECKPOINT_TIMESTAMP_MISMATCH",
        "$.created_at",
    )

    if committed["generation"] == 1:
        _require(
            previous_manifest is None,
            "CHECKPOINT_CHAIN_UNEXPECTED_PREVIOUS",
            "$.previous_manifest_hash",
        )
        return committed
    _require(
        previous_manifest is not None,
        "CHECKPOINT_CHAIN_PREVIOUS_REQUIRED",
        "$.previous_manifest_hash",
    )
    previous = verify_document_digest(
        "committed-checkpoint-manifest/v1", previous_manifest
    )
    _require(
        _json_equal(previous["package"], committed["package"])
        and previous["task_id"] == committed["task_id"]
        and previous["generation"] == committed["previous_generation"]
        and previous["manifest_hash"] == committed["previous_manifest_hash"],
        "CHECKPOINT_CHAIN_MISMATCH",
        "$.previous_manifest_hash",
    )
    _require(
        previous["committed_task_version"]
        == committed["committed_from_task_version"],
        "CHECKPOINT_CHAIN_TASK_VERSION_MISMATCH",
        "$.committed_from_task_version",
    )
    monotonic_fields = (
        "budget_watermark",
        "effect_watermark",
        "observation_watermark",
        "event_seq",
    )
    _require(
        all(committed[field] >= previous[field] for field in monotonic_fields),
        "CHECKPOINT_WATERMARK_REGRESSION",
        "$.event_seq",
    )
    _require(
        committed["created_at"] >= previous["created_at"],
        "CHECKPOINT_TIMESTAMP_REGRESSION",
        "$.created_at",
    )
    return committed
'''


__all__ = ["PYTHON_CHECKPOINT"]
