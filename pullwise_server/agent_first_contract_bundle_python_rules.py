"""Python facade document-rule handlers for source and evidence documents."""

from __future__ import annotations


PYTHON_RULES = r'''
from datetime import datetime
import unicodedata


def _require(condition: bool, detail: str, path: str = "$", code: str | None = None) -> None:
    if not condition:
        _fail(detail, path, code)


def _ordered_unique(values: list[object], key) -> bool:
    keys = [key(item) for item in values]
    return keys == sorted(keys) and len(keys) == len(set(keys))


def _sorted_unique(values: list[object]) -> bool:
    return values == sorted(values) and len(values) == len(set(values))


def _ref_key(value: dict[str, object]) -> tuple[object, ...]:
    return (
        value["content_schema_id"],
        value["artifact_id"],
        value["sha256"],
    )


def _artifact_ref_key(value: dict[str, object]) -> str:
    ref = value.get("ref", value)
    return ref["artifact_id"]


def _verify_embedded_digest(schema_id: str, value: dict[str, object]) -> None:
    spec = schema(schema_id).get("x-pullwise-digest")
    if not isinstance(spec, dict):
        return
    field, domain = spec["field"], spec["domain"]
    presented = value[field]
    unsigned = {key: item for key, item in value.items() if key != field}
    raw = domain.encode("utf-8") + b"\0" + canonical_document_bytes(unsigned)
    _require(
        presented == hashlib.sha256(raw).hexdigest(),
        "CONTRACT_DIGEST_MISMATCH",
        f"$.{field}",
    )


def _rule_server_authority_envelope(value: dict[str, object]) -> None:
    grant = value["grant"]
    _verify_embedded_digest("agent-worker-grant/v1", grant)
    deadline_fields = ("absolute_deadline_at", "terminalization_reserve_ms")
    _require(
        all(value[field] == grant[field] for field in deadline_fields),
        "AUTHORITY_GRANT_BINDING_MISMATCH",
        code="AUTHORITY_INPUT_UNTRUSTED",
    )


def _rule_transport_abandonment_record(value: dict[str, object]) -> None:
    _require(
        value["abandoned_task_version"] == value["previous_task_version"] + 1,
        "AUTHORITY_SUCCESSOR_VERSION_INVALID",
        "$.abandoned_task_version",
        "AUTHORITY_INPUT_UNTRUSTED",
    )


def _decode_canonical_base64(value: str, path: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        _fail("SOURCE_CONTENT_BASE64_INVALID", path)
    _require(
        base64.b64encode(raw).decode("ascii") == value,
        "SOURCE_CONTENT_BASE64_NONCANONICAL",
        path,
    )
    return raw


def _rule_binary_content(value: dict[str, object], data_field: str = "data_base64") -> None:
    raw = _decode_canonical_base64(value[data_field], f"$.{data_field}")
    _require(len(raw) == value["size_bytes"], "SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes")
    _require(
        hashlib.sha256(raw).hexdigest() == value["byte_sha256"],
        "SOURCE_CONTENT_SHA256_MISMATCH",
        "$.byte_sha256",
    )


def _rule_change_set_patch(value: dict[str, object]) -> None:
    _rule_binary_content(value)
    _verify_embedded_digest("change-set-patch/v1", value)


def _valid_tree_entry(entry: dict[str, object]) -> bool:
    expected = {
        "file": {"path", "type", "size_bytes", "sha256", "executable"},
        "symlink": {"path", "type", "target"},
        "gitlink": {"path", "type", "commit_sha"},
    }.get(entry.get("type"))
    return expected is not None and set(entry) == expected


def _rule_change_set(value: dict[str, object]) -> None:
    paths: list[str] = []
    for group in ("added", "modified", "deleted", "type_changed"):
        group_paths: list[str] = []
        for index, item in enumerate(value[group]):
            expected = {"after"} if group == "added" else {"before"}
            if group in {"modified", "type_changed"}:
                expected = {"before", "after"}
            _require(set(item) == expected, "CHANGE_SET_ENTRY_SHAPE_INVALID", f"$.{group}[{index}]")
            _require(
                all(_valid_tree_entry(entry) for entry in item.values()),
                "CHANGE_SET_ENTRY_SHAPE_INVALID",
                f"$.{group}[{index}]",
            )
            before, after = item.get("before"), item.get("after")
            path = (after or before)["path"]
            if before is not None and after is not None:
                _require(before["path"] == after["path"], "CHANGE_SET_PATH_INVALID", f"$.{group}[{index}]")
                _require(before != after, "CHANGE_SET_EMPTY_MUTATION", f"$.{group}[{index}]")
                _require(
                    (before["type"] == after["type"]) == (group == "modified"),
                    "CHANGE_SET_TYPE_BRANCH_INVALID",
                    f"$.{group}[{index}]",
                )
            group_paths.append(path)
        _require(
            group_paths == sorted(group_paths, key=lambda item: item.encode("utf-8")),
            "CHANGE_SET_ORDER_INVALID",
            f"$.{group}",
        )
        paths.extend(group_paths)
    _require(bool(paths), "CHANGE_SET_EMPTY")
    _require(len(paths) == len(set(paths)), "CHANGE_SET_PATH_OVERLAP")
    _require(
        value["original_source_state_id"] != value["final_source_state_id"],
        "CHANGE_SET_STATE_UNCHANGED",
    )


def _rule_execution_profile(value: dict[str, object]) -> None:
    _require(value["operating_system"] == "linux", "EXECUTION_PROFILE_OS_INVALID")
    _require(value["cpu_architecture"] in {"aarch64", "x86_64"}, "EXECUTION_PROFILE_ARCH_INVALID")
    _require(
        value["image_identity"].startswith("sha256:")
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_identity"]) is not None,
        "EXECUTION_PROFILE_IMAGE_MUTABLE",
    )


def _rule_execution_state(value: dict[str, object]) -> None:
    _require(_ordered_unique(value["toolchain"], lambda item: item["tool_id"]), "EXECUTION_TOOLCHAIN_ORDER_INVALID")
    _require(_ordered_unique(value["config_and_fixtures"], _ref_key), "EXECUTION_CONFIG_ORDER_INVALID")
    _require(_ordered_unique(value["services"], lambda item: item["service_id"]), "EXECUTION_SERVICE_ORDER_INVALID")
    _require(_ordered_unique(value["environment"], lambda item: item["key"]), "EXECUTION_ENVIRONMENT_ORDER_INVALID")
    for index, item in enumerate(value["environment"]):
        expected = {"kind", "key", "value"} if item["kind"] == "value" else {
            "kind", "key", "secret_key_id", "secret_version"
        }
        _require(set(item) == expected, "EXECUTION_ENVIRONMENT_SHAPE_INVALID", f"$.environment[{index}]")
    unsigned = {
        key: item
        for key, item in value.items()
        if key not in {"execution_state_id", "manifest_digest"}
    }
    _require(
        value["execution_state_id"] == canonical_document_sha256(unsigned),
        "EXECUTION_STATE_ID_INVALID",
        "$.execution_state_id",
    )


def _rule_source_selection_policy(value: dict[str, object]) -> None:
    excluded, ephemeral = value["excluded_control_roots"], value["ephemeral_patterns"]
    _require(value["include"] == "all_repository_regular_files", "SOURCE_SELECTION_INCLUDE_INVALID")
    _require(".git/" in excluded and ".pullwise-worker/" in excluded, "SOURCE_SELECTION_CONTROL_ROOT_MISSING")
    _require(_ordered_unique(excluded, lambda item: item.encode("utf-8")), "SOURCE_SELECTION_ORDER_INVALID")
    _require(_ordered_unique(ephemeral, lambda item: item.encode("utf-8")), "SOURCE_SELECTION_ORDER_INVALID")


def _rule_source_tree(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(_ordered_unique(entries, lambda item: item["path"].encode("utf-8")), "SOURCE_TREE_ORDER_INVALID")
    _require(all(_valid_tree_entry(item) for item in entries), "SOURCE_TREE_ENTRY_INVALID")
    folded = [item["path"].lower() for item in entries]
    _require(len(folded) == len(set(folded)), "SOURCE_TREE_CASE_COLLISION")
    total = sum(item["size_bytes"] for item in entries if item["type"] == "file")
    _require(value["entry_count"] == len(entries), "SOURCE_TREE_COUNT_INVALID")
    _require(value["total_bytes"] == total, "SOURCE_TREE_SIZE_INVALID")
    identity = {
        "base_revision": value["base_revision"],
        "selection_policy_digest": value["selection_policy_digest"],
        "entries": entries,
    }
    _require(
        value["source_state_id"] == canonical_document_sha256(identity),
        "SOURCE_STATE_ID_INVALID",
        "$.source_state_id",
    )


def _timestamp_millis(value: object) -> int | None:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
        value,
    ) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    elapsed = parsed - datetime(1970, 1, 1)
    return (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )


def _rule_actor(value: dict[str, object]) -> None:
    session_kinds = {
        "task_owner", "quality_verifier", "domain_reviewer",
        "explorer", "troubleshooter", "implementer",
    }
    if value["kind"] in session_kinds:
        _require(
            isinstance(value["session_id"], str)
            and re.fullmatch(r"sess_[0-9a-f]{32}", value["session_id"]) is not None,
            "ACTOR_SESSION_INVALID",
        )
    else:
        _require(value["session_id"] is None, "ACTOR_SESSION_INVALID")


def _rule_observation(value: dict[str, object]) -> None:
    _rule_actor(value["actor"])
    status = value["status"]
    started, completed = _timestamp_millis(value["started_at"]), _timestamp_millis(value["completed_at"])
    if status == "policy_denied":
        _require(
            all(value[key] is None for key in ("started_at", "completed_at", "duration_ms", "exit_code")),
            "OBSERVATION_STATUS_MATRIX_INVALID",
        )
        _require(
            value["result_ref"]["availability"] == "available"
            and value["result_ref"]["ref"]["content_schema_id"] == "error-response/v1"
            and value["source_state_before_id"] == value["source_state_after_id"]
            and value["execution_state_id"] is None,
            "OBSERVATION_STATUS_MATRIX_INVALID",
        )
    else:
        _require(
            started is not None and completed is not None and completed >= started
            and value["duration_ms"] == completed - started,
            "OBSERVATION_TIME_INVALID",
        )
        if status == "succeeded":
            _require(value["result_ref"]["availability"] == "available", "OBSERVATION_RESULT_REQUIRED")
    _require(value["partial_side_effect"] is False, "OBSERVATION_PARTIAL_SIDE_EFFECT")


def _rule_observation_manifest(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(value["entry_count"] == len(entries), "OBSERVATION_MANIFEST_COUNT_INVALID")
    _require(
        _ordered_unique(entries, lambda item: (item["observation_seq"], item["observation_id"])),
        "OBSERVATION_MANIFEST_ORDER_INVALID",
    )
    if value["schema_id"] == "pre-verifier-observation-manifest/v1":
        allowed = {"task_owner", "legacy_domain_reviewer"}
        _require(all(item["actor"]["kind"] in allowed for item in entries), "OBSERVATION_MANIFEST_ACTOR_INVALID")
    else:
        _require(any(item["actor"]["kind"] == "quality_verifier" for item in entries), "OBSERVATION_MANIFEST_VERIFIER_MISSING")


def _rule_completion_proposal(value: dict[str, object]) -> None:
    _require(_sorted_unique(value["execution_state_ids"]), "PROPOSAL_EXECUTION_STATE_ORDER_INVALID")
    _require(_ordered_unique(value["artifact_refs"], _artifact_ref_key), "PROPOSAL_ARTIFACT_ORDER_INVALID")
    _require(_ordered_unique(value["requirement_claims"], lambda item: item["requirement_id"]), "PROPOSAL_CLAIM_ORDER_INVALID")
    for item in value["requirement_claims"]:
        _require(_sorted_unique(item["evidence_ids"]), "PROPOSAL_EVIDENCE_ORDER_INVALID")
    _require(_sorted_unique(value["known_gaps"]), "PROPOSAL_GAP_ORDER_INVALID")
    _require(_sorted_unique(value["residual_risks"]), "PROPOSAL_RISK_ORDER_INVALID")
    if value["outcome_requested"] == "NO_CHANGE_NEEDED":
        _require(value["change_set_ref"] is None, "PROPOSAL_NO_CHANGE_SET_INVALID")
        _require(
            value["original_source_state_id"] == value["final_source_state_id"],
            "PROPOSAL_NO_CHANGE_STATE_INVALID",
        )


def _rule_verifier_input(value: dict[str, object]) -> None:
    _require(value["owner_conclusion_excluded"] is True, "VERIFIER_OWNER_CONCLUSION_INCLUDED")
    _require(_ordered_unique(value["artifact_refs"], _artifact_ref_key), "VERIFIER_ARTIFACT_ORDER_INVALID")
    _require(_ordered_unique(value["engineering_rule_refs"], _ref_key), "VERIFIER_RULE_ORDER_INVALID")
    _require(_sorted_unique(value["requirement_ids"]), "VERIFIER_REQUIREMENT_ORDER_INVALID")


def _valid_assessments(values: list[dict[str, object]]) -> bool:
    if not _ordered_unique(values, lambda item: item["requirement_id"]):
        return False
    return all(
        _sorted_unique(item["evidence_ids"])
        and _sorted_unique(item["limitations"])
        and (item["verdict"] != "PASS" or not item["limitations"])
        for item in values
    )


def _rule_verifier_work(value: dict[str, object]) -> None:
    _require(value["sandbox_mode"] == "read_only_or_cow", "VERIFIER_SANDBOX_INVALID")
    for field in ("counterexamples_searched", "own_observation_ids", "limitations"):
        _require(_sorted_unique(value[field]), "VERIFIER_WORK_ORDER_INVALID", f"$.{field}")
    _require(bool(value["own_observation_ids"]), "VERIFIER_OBSERVATION_REQUIRED")
    _require(_valid_assessments(value["provisional_requirement_assessments"]), "VERIFIER_ASSESSMENT_INVALID")


def _rule_attestation(value: dict[str, object]) -> None:
    _require(_sorted_unique(value["execution_state_ids"]), "ATTESTATION_EXECUTION_ORDER_INVALID")
    _require(_sorted_unique(value["own_observation_ids"]) and bool(value["own_observation_ids"]), "ATTESTATION_OBSERVATION_INVALID")
    verdicts = value["requirement_verdicts"]
    _require(_valid_assessments(verdicts), "ATTESTATION_VERDICT_INVALID")
    present = {item["verdict"] for item in verdicts}
    expected = next((item for item in ("POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE") if item in present), "PASS")
    _require(value["run_status"] == expected, "ATTESTATION_RUN_STATUS_INVALID")


def _rule_attestation_manifest(value: dict[str, object]) -> None:
    attestations = value["attestations"]
    _require(value["attestation_count"] == len(attestations), "ATTESTATION_MANIFEST_COUNT_INVALID")
    _require(_ordered_unique(attestations, lambda item: (item["slot_id"], item["attestation_id"])), "ATTESTATION_MANIFEST_ORDER_INVALID")
    slots = {item["slot_id"] for item in attestations}
    ids = {item["attestation_id"] for item in attestations}
    aggregates = value["requirement_aggregates"]
    _require(_ordered_unique(aggregates, lambda item: item["requirement_id"]), "ATTESTATION_AGGREGATE_ORDER_INVALID")
    for item in aggregates:
        _require(_sorted_unique(item["required_slot_ids"]), "ATTESTATION_REQUIRED_SLOT_ORDER_INVALID")
        _require(_sorted_unique(item["attestation_ids"]), "ATTESTATION_ID_ORDER_INVALID")
        _require(set(item["attestation_ids"]).issubset(ids), "ATTESTATION_ID_UNKNOWN")
        if set(item["required_slot_ids"]).difference(slots):
            _require(item["verdict"] == "UNVERIFIABLE", "ATTESTATION_MISSING_SLOT_INVALID")


def _rule_evidence_closure(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(value["entry_count"] == len(entries), "EVIDENCE_CLOSURE_COUNT_INVALID")
    _require(_ordered_unique(entries, _ref_key), "EVIDENCE_CLOSURE_ORDER_INVALID")
    forbidden = {
        "evidence-closure-manifest/v1", "task-result-core/v1",
        "task-result/v1", "worker-debug-fragment/v1",
    }
    _require(all(item["content_schema_id"] not in forbidden for item in entries), "EVIDENCE_CLOSURE_BACK_EDGE")
    verify_content_ref_set(entries)
    keys = {_ref_key(item) for item in entries}
    required = {
        _ref_key(value["pre_gate_evidence_closure_ref"]),
        _ref_key(value["input_snapshot_ref"]),
        _ref_key(value["gate_decision_ref"]),
    }
    _require(required.issubset(keys), "EVIDENCE_CLOSURE_REQUIRED_EDGE_MISSING")
    _require(value["evidence_closure_digest"] == canonical_document_sha256(entries), "EVIDENCE_CLOSURE_DIGEST_INVALID")
'''


__all__ = ["PYTHON_RULES"]
