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
'''


__all__ = ["PYTHON_SOURCE_EXECUTION_OBSERVATION"]
