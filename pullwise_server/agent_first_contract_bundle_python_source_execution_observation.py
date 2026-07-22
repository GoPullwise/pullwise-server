"""Python facade semantics for source, execution, and observation state."""

from __future__ import annotations


PYTHON_SOURCE_EXECUTION_OBSERVATION = r'''
def _seo_require(
    condition: bool,
    detail: str,
    path: str = "$",
    code: str | None = None,
) -> None:
    if not condition:
        _fail(detail, path, code)


def _seo_verify_embedded_digest(
    schema_id: str, value: dict[str, object]
) -> None:
    spec = schema(schema_id)["x-pullwise-digest"]
    field, domain = spec["field"], spec["domain"]
    unsigned = {key: item for key, item in value.items() if key != field}
    digest_input = (
        domain.encode("utf-8")
        + b"\0"
        + canonical_document_bytes(unsigned)
    )
    _seo_require(
        value[field] == hashlib.sha256(digest_input).hexdigest(),
        "CONTRACT_DIGEST_MISMATCH",
        f"$.{field}",
    )


def _seo_tree_entry_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected = {
        "file": {"path", "type", "size_bytes", "sha256", "executable"},
        "symlink": {"path", "type", "target"},
        "gitlink": {"path", "type", "commit_sha"},
    }.get(value.get("type"))
    return expected is not None and set(value) == expected


def _seo_ref_matches_document(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> bool:
    raw = canonical_document_bytes(document)
    expected = {
        "content_schema_id": schema_id,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }
    return all(ref.get(key) == item for key, item in expected.items())


def _seo_change_entry_path(entry: dict[str, object]) -> str:
    branch = entry.get("after") or entry.get("before")
    return branch["path"]


def _rule_change_set_complete(value: dict[str, object]) -> None:
    all_paths: list[str] = []
    for group in ("added", "modified", "deleted", "type_changed"):
        paths: list[str] = []
        for index, item in enumerate(value[group]):
            expected = {"after"} if group == "added" else {"before"}
            if group in {"modified", "type_changed"}:
                expected = {"before", "after"}
            path = f"$.{group}[{index}]"
            _seo_require(
                set(item) == expected,
                "CHANGE_SET_ENTRY_SHAPE_INVALID",
                path,
            )
            _seo_require(
                all(_seo_tree_entry_valid(entry) for entry in item.values()),
                "CHANGE_SET_ENTRY_SHAPE_INVALID",
                path,
            )
            before, after = item.get("before"), item.get("after")
            if before is not None and after is not None:
                _seo_require(
                    before["path"] == after["path"],
                    "CHANGE_SET_PATH_INVALID",
                    path,
                )
                _seo_require(
                    canonical_document_bytes(before)
                    != canonical_document_bytes(after),
                    "CHANGE_SET_EMPTY_MUTATION",
                    path,
                )
                _seo_require(
                    (before["type"] == after["type"])
                    == (group == "modified"),
                    "CHANGE_SET_TYPE_BRANCH_INVALID",
                    path,
                )
            paths.append(_seo_change_entry_path(item))
        _seo_require(
            paths == sorted(paths, key=lambda item: item.encode("utf-8")),
            "CHANGE_SET_ORDER_INVALID",
            f"$.{group}",
        )
        all_paths.extend(paths)
    _seo_require(bool(all_paths), "CHANGE_SET_EMPTY")
    _seo_require(
        len(all_paths) == len(set(all_paths)),
        "CHANGE_SET_PATH_OVERLAP",
    )
    _seo_require(
        value["original_source_state_id"] != value["final_source_state_id"],
        "CHANGE_SET_STATE_UNCHANGED",
    )
    _seo_verify_embedded_digest("change-set/v1", value)


def _seo_expected_change_groups(
    original: dict[str, object], final: dict[str, object]
) -> dict[str, list[dict[str, object]]]:
    before = {entry["path"]: entry for entry in original["entries"]}
    after = {entry["path"]: entry for entry in final["entries"]}
    groups: dict[str, list[dict[str, object]]] = {
        "added": [],
        "modified": [],
        "deleted": [],
        "type_changed": [],
    }
    for path in sorted(set(before) | set(after), key=lambda item: item.encode("utf-8")):
        old, new = before.get(path), after.get(path)
        if old is None:
            groups["added"].append({"after": new})
        elif new is None:
            groups["deleted"].append({"before": old})
        elif canonical_document_bytes(old) != canonical_document_bytes(new):
            group = "modified" if old["type"] == new["type"] else "type_changed"
            groups[group].append({"before": old, "after": new})
    return groups


def verify_change_set_context(
    change_set: object,
    original_source_tree: object,
    final_source_tree: object,
    patch: object,
) -> dict[str, object]:
    """Bind a change set to its two source trees and exact patch content."""
    checked = verify_document_digest("change-set/v1", change_set)
    original = verify_document_digest(
        "source-tree-manifest/v1", original_source_tree
    )
    final = verify_document_digest("source-tree-manifest/v1", final_source_tree)
    patch_value = verify_document_digest("change-set-patch/v1", patch)
    _seo_require(
        checked["original_source_state_id"] == original["source_state_id"],
        "CHANGE_SET_CONTEXT_INVALID",
        "$.original_source_state_id",
    )
    _seo_require(
        checked["final_source_state_id"] == final["source_state_id"],
        "CHANGE_SET_CONTEXT_INVALID",
        "$.final_source_state_id",
    )
    expected = _seo_expected_change_groups(original, final)
    for group in ("added", "modified", "deleted", "type_changed"):
        _seo_require(
            canonical_document_bytes(checked[group])
            == canonical_document_bytes(expected[group]),
            "CHANGE_SET_CONTEXT_INVALID",
            f"$.{group}",
        )
    _seo_require(
        _seo_ref_matches_document(
            checked["patch_ref"], "change-set-patch/v1", patch_value
        ),
        "CAS_CORRUPT",
        "$.patch_ref",
    )
    return checked


def _seo_ordered_unique(values: list[object], key) -> bool:
    keys = [key(item) for item in values]
    return keys == sorted(keys) and len(keys) == len(set(keys))


def _seo_content_ref_key(value: dict[str, object]) -> tuple[str, str, str]:
    return (value["content_schema_id"], value["artifact_id"], value["sha256"])


def _rule_execution_state_manifest(value: dict[str, object]) -> None:
    _seo_require(
        _seo_ordered_unique(value["toolchain"], lambda item: item["tool_id"]),
        "EXECUTION_TOOLCHAIN_ORDER_INVALID", "$.toolchain",
    )
    _seo_require(
        _seo_ordered_unique(value["config_and_fixtures"], _seo_content_ref_key),
        "EXECUTION_CONFIG_ORDER_INVALID", "$.config_and_fixtures",
    )
    _seo_require(
        _seo_ordered_unique(value["services"], lambda item: item["service_id"]),
        "EXECUTION_SERVICE_ORDER_INVALID", "$.services",
    )
    _seo_require(
        _seo_ordered_unique(value["environment"], lambda item: item["key"]),
        "EXECUTION_ENVIRONMENT_ORDER_INVALID", "$.environment",
    )
    for index, item in enumerate(value["environment"]):
        expected = (
            {"kind", "key", "value"}
            if item["kind"] == "value"
            else {"kind", "key", "secret_key_id", "secret_version"}
        )
        _seo_require(
            set(item) == expected,
            "EXECUTION_ENVIRONMENT_SHAPE_INVALID",
            f"$.environment[{index}]",
        )
    unsigned = {
        key: item for key, item in value.items()
        if key not in {"execution_state_id", "manifest_digest"}
    }
    _seo_require(
        value["execution_state_id"]
        == hashlib.sha256(canonical_document_bytes(unsigned)).hexdigest(),
        "EXECUTION_STATE_ID_INVALID", "$.execution_state_id",
    )
    _seo_verify_embedded_digest("execution-state-manifest/v1", value)


def verify_execution_state_context(
    manifest: object,
    source_tree: object,
    execution_profile: object,
) -> dict[str, object]:
    """Bind execution state to its exact source state and runtime profile."""
    checked = verify_document_digest("execution-state-manifest/v1", manifest)
    source = verify_document_digest("source-tree-manifest/v1", source_tree)
    profile = verify_document_digest("execution-profile/v1", execution_profile)
    _seo_require(
        checked["source_state_id"] == source["source_state_id"],
        "EXECUTION_STATE_CONTEXT_INVALID", "$.source_state_id",
    )
    _seo_require(
        checked["execution_profile_digest"] == profile["profile_digest"],
        "EXECUTION_STATE_CONTEXT_INVALID", "$.execution_profile_digest",
    )
    _seo_require(
        _seo_ref_matches_document(
            checked["execution_profile_ref"], "execution-profile/v1", profile
        ),
        "CAS_CORRUPT", "$.execution_profile_ref",
    )
    return checked


def _rule_source_selection_policy_complete(value: dict[str, object]) -> None:
    excluded = value["excluded_control_roots"]
    ephemeral = value["ephemeral_patterns"]
    _seo_require(
        value["include"] == "all_repository_regular_files",
        "SOURCE_SELECTION_INCLUDE_INVALID", "$.include",
    )
    _seo_require(
        ".git/" in excluded and ".pullwise-worker/" in excluded,
        "SOURCE_SELECTION_CONTROL_ROOT_MISSING", "$.excluded_control_roots",
    )
    _seo_require(
        _seo_ordered_unique(excluded, lambda item: item.encode("utf-8")),
        "SOURCE_SELECTION_ORDER_INVALID", "$.excluded_control_roots",
    )
    _seo_require(
        _seo_ordered_unique(ephemeral, lambda item: item.encode("utf-8")),
        "SOURCE_SELECTION_ORDER_INVALID", "$.ephemeral_patterns",
    )
    _seo_verify_embedded_digest("source-selection-policy/v1", value)


def _seo_case_collision_key(value: str) -> str:
    return value.upper().lower()


def _rule_source_tree_manifest(value: dict[str, object]) -> None:
    entries = value["entries"]
    _seo_require(
        _seo_ordered_unique(entries, lambda item: item["path"].encode("utf-8")),
        "SOURCE_TREE_ORDER_INVALID", "$.entries",
    )
    for index, entry in enumerate(entries):
        _seo_require(
            _seo_tree_entry_valid(entry),
            "SOURCE_TREE_ENTRY_INVALID", f"$.entries[{index}]",
        )
    folded = [_seo_case_collision_key(entry["path"]) for entry in entries]
    _seo_require(
        len(folded) == len(set(folded)),
        "SOURCE_TREE_CASE_COLLISION", "$.entries",
    )
    total = sum(
        entry["size_bytes"] for entry in entries if entry["type"] == "file"
    )
    _seo_require(
        value["entry_count"] == len(entries),
        "SOURCE_TREE_COUNT_INVALID", "$.entry_count",
    )
    _seo_require(
        value["total_bytes"] == total,
        "SOURCE_TREE_SIZE_INVALID", "$.total_bytes",
    )
    identity = {
        "base_revision": value["base_revision"],
        "selection_policy_digest": value["selection_policy_digest"],
        "entries": entries,
    }
    _seo_require(
        value["source_state_id"]
        == hashlib.sha256(canonical_document_bytes(identity)).hexdigest(),
        "SOURCE_STATE_ID_INVALID", "$.source_state_id",
    )
    _seo_verify_embedded_digest("source-tree-manifest/v1", value)


def verify_source_tree_context(
    manifest: object,
    selection_policy: object,
) -> dict[str, object]:
    """Bind a source tree to the exact selection policy used to build it."""
    checked = verify_document_digest("source-tree-manifest/v1", manifest)
    policy = verify_document_digest(
        "source-selection-policy/v1", selection_policy
    )
    _seo_require(
        checked["selection_policy_digest"] == policy["policy_digest"],
        "SOURCE_TREE_CONTEXT_INVALID", "$.selection_policy_digest",
    )
    _seo_require(
        _seo_ref_matches_document(
            checked["selection_policy_ref"],
            "source-selection-policy/v1",
            policy,
        ),
        "CAS_CORRUPT", "$.selection_policy_ref",
    )
    return checked


def _seo_observation_manifest_common(
    value: dict[str, object], *, pre_verifier: bool
) -> None:
    entries = value["entries"]
    _seo_require(
        value["entry_count"] == len(entries),
        "OBSERVATION_MANIFEST_COUNT_INVALID", "$.entry_count",
    )
    _seo_require(
        _seo_ordered_unique(
            entries,
            lambda item: (item["observation_seq"], item["observation_id"]),
        ),
        "OBSERVATION_MANIFEST_ORDER_INVALID", "$.entries",
    )
    if pre_verifier:
        allowed = {"task_owner", "domain_reviewer"}
        for index, entry in enumerate(entries):
            _seo_require(
                entry["actor"]["kind"] in allowed,
                "OBSERVATION_MANIFEST_ACTOR_INVALID",
                f"$.entries[{index}].actor.kind",
            )
    else:
        _seo_require(
            any(
                entry["actor"]["kind"] == "quality_verifier"
                for entry in entries
            ),
            "OBSERVATION_MANIFEST_VERIFIER_MISSING", "$.entries",
        )


def _rule_observation_manifest_complete(value: dict[str, object]) -> None:
    _seo_observation_manifest_common(value, pre_verifier=False)
    _seo_verify_embedded_digest("observation-manifest/v1", value)


def _rule_pre_verifier_observation_manifest(
    value: dict[str, object]
) -> None:
    _seo_observation_manifest_common(value, pre_verifier=True)
    _seo_verify_embedded_digest(
        "pre-verifier-observation-manifest/v1", value
    )


def verify_observation_manifest_extension(
    manifest: object,
    pre_verifier_manifest: object,
) -> dict[str, object]:
    """Verify that final observations strictly and exactly extend the pre-set."""
    final = verify_document_digest("observation-manifest/v1", manifest)
    pre = verify_document_digest(
        "pre-verifier-observation-manifest/v1", pre_verifier_manifest
    )
    for field in ("task_id", "proposal_id", "attempt_id", "native_epoch"):
        _seo_require(
            final[field] == pre[field],
            "OBSERVATION_MANIFEST_IDENTITY_INVALID", f"$.{field}",
        )
    _seo_require(
        final["manifest_id"] != pre["manifest_id"],
        "OBSERVATION_MANIFEST_IDENTITY_INVALID", "$.manifest_id",
    )
    _seo_require(
        _seo_ref_matches_document(
            final["pre_verifier_observation_manifest_ref"],
            "pre-verifier-observation-manifest/v1",
            pre,
        ),
        "CAS_CORRUPT", "$.pre_verifier_observation_manifest_ref",
    )
    prefix_length = len(pre["entries"])
    _seo_require(
        len(final["entries"]) > prefix_length,
        "OBSERVATION_MANIFEST_EXTENSION_INVALID", "$.entries",
    )
    _seo_require(
        canonical_document_bytes(final["entries"][:prefix_length])
        == canonical_document_bytes(pre["entries"]),
        "OBSERVATION_MANIFEST_EXTENSION_INVALID", "$.entries",
    )
    for index, entry in enumerate(
        final["entries"][prefix_length:], prefix_length
    ):
        _seo_require(
            entry["actor"]["kind"] == "quality_verifier",
            "OBSERVATION_MANIFEST_EXTENSION_INVALID",
            f"$.entries[{index}].actor.kind",
        )
    return final
'''


__all__ = ["PYTHON_SOURCE_EXECUTION_OBSERVATION"]
