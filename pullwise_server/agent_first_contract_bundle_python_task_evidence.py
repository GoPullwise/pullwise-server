"""Generated Python facade semantics for final evidence closure."""

from __future__ import annotations


PYTHON_TASK_EVIDENCE = r'''
_TASK_EVIDENCE_IDENTITY_FIELDS = (
    "content_schema_id",
    "sha256",
    "size_bytes",
    "media_type",
    "encoding",
)


def _task_evidence_content_identity(
    ref: dict[str, object],
) -> tuple[object, ...]:
    return tuple(ref[field] for field in _TASK_EVIDENCE_IDENTITY_FIELDS)


def _task_evidence_has_content_alias(
    refs: list[dict[str, object]],
) -> bool:
    artifacts_by_content: dict[tuple[object, ...], str] = {}
    for ref in refs:
        identity = _task_evidence_content_identity(ref)
        artifact_id = ref["artifact_id"]
        previous = artifacts_by_content.setdefault(identity, artifact_id)
        if previous != artifact_id:
            return True
    return False


def _task_evidence_exact_edge(
    entries: list[dict[str, object]],
    schema_ids: set[str],
    expected: dict[str, object],
) -> bool:
    matches = [
        ref for ref in entries if ref["content_schema_id"] in schema_ids
    ]
    return (
        len(matches) == 1
        and canonical_document_bytes(matches[0])
        == canonical_document_bytes(expected)
    )


def _rule_evidence_closure_manifest(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(
        value["entry_count"] == len(entries),
        "EVIDENCE_CLOSURE_COUNT_INVALID",
        "$.entry_count",
    )
    _require(
        _ordered_unique(entries, _ref_key),
        "EVIDENCE_CLOSURE_ORDER_INVALID",
        "$.entries",
    )
    verify_content_ref_set(entries)
    _require(
        not _task_evidence_has_content_alias(entries),
        "EVIDENCE_CLOSURE_CONTENT_ALIAS",
        "$.entries",
    )
    edges = (
        (
            {"pre-gate-evidence-closure-manifest/v1"},
            value["pre_gate_evidence_closure_ref"],
        ),
        (
            {
                "gate-input-snapshot/v1",
                "terminalization-input-snapshot/v1",
            },
            value["input_snapshot_ref"],
        ),
        ({"gate-decision/v1"}, value["gate_decision_ref"]),
    )
    _require(
        all(
            _task_evidence_exact_edge(entries, schema_ids, expected)
            for schema_ids, expected in edges
        ),
        "EVIDENCE_CLOSURE_REQUIRED_EDGE_INVALID",
        "$.entries",
    )
    expected_digest = hashlib.sha256(
        canonical_document_bytes(entries)
    ).hexdigest()
    _require(
        value["evidence_closure_digest"] == expected_digest,
        "EVIDENCE_CLOSURE_DIGEST_INVALID",
        "$.evidence_closure_digest",
    )


def _task_evidence_ref_matches_document(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> bool:
    raw = canonical_document_bytes(document)
    return (
        ref["schema_id"] == "content-ref/v1"
        and ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def _task_evidence_expected_entries(
    manifest: dict[str, object],
    pre_gate_manifest: dict[str, object],
) -> list[dict[str, object]]:
    candidates = [
        *pre_gate_manifest["entries"],
        manifest["pre_gate_evidence_closure_ref"],
        manifest["input_snapshot_ref"],
        manifest["gate_decision_ref"],
    ]
    unique = {
        canonical_document_bytes(ref): ref
        for ref in candidates
    }
    return sorted(unique.values(), key=_ref_key)


def verify_evidence_closure_context(
    manifest: object,
    pre_gate_manifest: object,
) -> dict[str, object]:
    """Bind final closure to the direct PreGate manifest document."""
    validated = verify_document_digest(
        "evidence-closure-manifest/v1", manifest
    )
    pre_gate = verify_document_digest(
        "pre-gate-evidence-closure-manifest/v1", pre_gate_manifest
    )
    _require(
        validated["task_id"] == pre_gate["task_id"],
        "EVIDENCE_CLOSURE_INVALID",
        "$.task_id",
    )
    _require(
        _task_evidence_ref_matches_document(
            validated["pre_gate_evidence_closure_ref"],
            "pre-gate-evidence-closure-manifest/v1",
            pre_gate,
        ),
        "CAS_CORRUPT",
        "$.pre_gate_evidence_closure_ref",
    )
    expected = _task_evidence_expected_entries(validated, pre_gate)
    _require(
        [canonical_document_bytes(ref) for ref in validated["entries"]]
        == [canonical_document_bytes(ref) for ref in expected],
        "EVIDENCE_CLOSURE_INVALID",
        "$.entries",
    )
    return validated
'''


__all__ = ["PYTHON_TASK_EVIDENCE"]
