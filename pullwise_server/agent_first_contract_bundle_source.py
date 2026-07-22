"""Load, canonicalize, and close Agent-First contract family source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import unicodedata

from .agent_first_contract_bundle_registry import validate_supported_schema


FIXTURE_CLASSES = ("golden", "negative", "idempotency", "fence", "crash")
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$")
SAFE_INTEGER = (1 << 53) - 1
MAX_FAMILY_LINES = 600
MAX_FAMILY_LINE_LENGTH = 200
SEMANTIC_CYCLE_EXCEPTIONS = (
    {
        "schema_id": "task-charter/v1",
        "kind": "content_ref_target",
        "path": "$.properties.previous_charter_ref.oneOf[0]",
        "target_schema_id": "task-charter/v1",
    },
    {
        "schema_id": "task-record/v1",
        "kind": "content_ref_target",
        "path": "$.properties.result_ref.oneOf[0]",
        "target_schema_id": "task-result/v1",
    },
)
INTERNAL_CONSTRAINT_SCHEMA_IDS = frozenset(
    {
        "task-result-blocked-variant/v1",
        "task-result-cancelled-variant/v1",
        "task-result-completed-variant/v1",
        "task-result-completed-with-waivers-variant/v1",
        "task-result-failed-variant/v1",
        "task-result-no-change-needed-variant/v1",
        "task-result-partial-variant/v1",
    }
)


class ContractBundleError(RuntimeError):
    pass


def load_family(
    path: Path,
    expected_id: str,
    schema_owner: dict[str, str],
    fixture_ids: set[str],
) -> dict[str, object]:
    _validate_family_readability(path)
    source = load_json(path)
    require_fields(source, {"family_id", "schemas", "fixtures"}, "family_fields_invalid")
    if source["family_id"] != expected_id:
        raise ContractBundleError(f"family_identity_invalid: {expected_id}")
    schemas, fixtures = source["schemas"], source["fixtures"]
    if not isinstance(schemas, list) or not schemas:
        raise ContractBundleError(f"family_schemas_invalid: {expected_id}")
    if not isinstance(fixtures, list) or not fixtures:
        raise ContractBundleError(f"family_fixtures_invalid: {expected_id}")

    schema_registry = []
    for schema in schemas:
        if not isinstance(schema, dict):
            raise ContractBundleError(f"schema_invalid: {expected_id}")
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not SCHEMA_ID_PATTERN.fullmatch(schema_id):
            raise ContractBundleError(f"schema_identity_invalid: {expected_id}")
        if schema.get("$schema") != SCHEMA_DRAFT:
            raise ContractBundleError(f"schema_draft_invalid: {schema_id}")
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ContractBundleError(f"schema_closed_object_required: {schema_id}")
        validate_supported_schema(schema, ContractBundleError)
        if schema_id in schema_owner:
            raise ContractBundleError(
                f"schema_duplicate: {schema_id}: "
                f"{schema_owner[schema_id]}: {expected_id}"
            )
        schema_owner[schema_id] = expected_id
        edges = schema_edges(schema)
        schema_registry.append(
            {
                "schema_id": schema_id,
                "family_id": expected_id,
                "role": (
                    "internal_constraint"
                    if schema_id in INTERNAL_CONSTRAINT_SCHEMA_IDS
                    else "public_document"
                ),
                "references": sorted({item["target_schema_id"] for item in edges}),
                "edges": edges,
                "sha256": sha256(canonical_bytes(schema)),
            }
        )
    if [item["$id"] for item in schemas] != sorted(item["$id"] for item in schemas):
        raise ContractBundleError(f"schema_order_invalid: {expected_id}")

    fixture_registry = []
    family_schema_ids = {item["$id"] for item in schemas}
    for fixture in fixtures:
        require_fields(
            fixture,
            {"fixture_id", "fixture_class", "schema_id", "document", "expected_code"},
            f"fixture_fields_invalid: {expected_id}",
        )
        fixture_id = nonempty_ascii(fixture["fixture_id"], "fixture_identity_invalid")
        if fixture_id in fixture_ids:
            raise ContractBundleError(f"fixture_duplicate: {fixture_id}")
        fixture_ids.add(fixture_id)
        if fixture["fixture_class"] not in FIXTURE_CLASSES:
            raise ContractBundleError(f"fixture_class_invalid: {fixture_id}")
        if fixture["schema_id"] not in family_schema_ids:
            raise ContractBundleError(f"fixture_schema_not_in_family: {fixture_id}")
        if fixture["expected_code"] is not None and not isinstance(
            fixture["expected_code"], str
        ):
            raise ContractBundleError(f"fixture_expected_code_invalid: {fixture_id}")
        fixture_registry.append(
            {
                "fixture_id": fixture_id,
                "family_id": expected_id,
                "schema_id": fixture["schema_id"],
                "fixture_class": fixture["fixture_class"],
                "expected_code": fixture["expected_code"],
                "sha256": sha256(canonical_bytes(fixture)),
            }
        )
    if [item["fixture_id"] for item in fixtures] != sorted(
        item["fixture_id"] for item in fixtures
    ):
        raise ContractBundleError(f"fixture_order_invalid: {expected_id}")
    return {
        "family_id": expected_id,
        "schemas": schemas,
        "schema_registry": schema_registry,
        "fixtures": fixtures,
        "fixture_registry": fixture_registry,
    }


def reference_dag(
    families: list[dict[str, object]], schema_owner: dict[str, str]
) -> list[dict[str, object]]:
    edge_graph = {
        schema["$id"]: schema_edges(schema)
        for family in families
        for schema in family["schemas"]
    }
    graph = {
        schema_id: {item["target_schema_id"] for item in edges}
        for schema_id, edges in edge_graph.items()
    }
    for schema_id, refs in graph.items():
        unknown = refs.difference(schema_owner)
        if unknown:
            raise ContractBundleError(
                f"schema_reference_unknown: {schema_id}: {sorted(unknown)[0]}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(schema_id: str) -> None:
        if schema_id in visiting:
            raise ContractBundleError(f"schema_reference_cycle: {schema_id}")
        if schema_id in visited:
            return
        visiting.add(schema_id)
        for edge in edge_graph[schema_id]:
            target = edge["target_schema_id"]
            exception = {"schema_id": schema_id, **edge}
            if exception in SEMANTIC_CYCLE_EXCEPTIONS:
                continue
            visit(target)
        visiting.remove(schema_id)
        visited.add(schema_id)

    for schema_id in sorted(graph):
        visit(schema_id)
    return [
        {
            "schema_id": schema_id,
            "family_id": schema_owner[schema_id],
            "references": sorted(graph[schema_id]),
            "edges": edge_graph[schema_id],
        }
        for schema_id in sorted(graph)
    ]


def schema_edges(value: object) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            target = item.get("$ref")
            if isinstance(target, str):
                found.append(
                    {"kind": "schema_ref", "path": path, "target_schema_id": target}
                )
            annotations = (
                (
                    "x-pullwise-content-schema-id",
                    "x-pullwise-content-schema-ids",
                    "content_ref_target",
                ),
                (
                    "x-pullwise-availability-content-schema-id",
                    "x-pullwise-availability-content-schema-ids",
                    "availability_ref_target",
                ),
            )
            for singular, plural, kind in annotations:
                targets: list[object] = []
                if singular in item:
                    targets.append(item[singular])
                if plural in item and isinstance(item[plural], list):
                    targets.extend(item[plural])
                for annotated in targets:
                    if isinstance(annotated, str):
                        found.append(
                            {
                                "kind": kind,
                                "path": path,
                                "target_schema_id": annotated,
                            }
                        )
            for key, child in item.items():
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, "$")
    return sorted(
        {canonical_bytes(item): item for item in found}.values(),
        key=lambda item: (item["path"], item["kind"], item["target_schema_id"]),
    )


def references(value: object) -> set[str]:
    return {item["target_schema_id"] for item in schema_edges(value)}


def _validate_family_readability(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractBundleError(f"source_json_invalid: {path.name}") from exc
    if len(lines) > MAX_FAMILY_LINES:
        raise ContractBundleError(f"family_line_count_invalid: {path.stem}")
    for line_number, line in enumerate(lines, 1):
        if len(line) > MAX_FAMILY_LINE_LENGTH:
            raise ContractBundleError(
                f"family_line_length_invalid: {path.stem}:{line_number}"
            )


def load_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ContractBundleError(f"source_missing: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ContractBundleError(f"source_not_regular: {path.name}")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ContractBundleError(f"json_duplicate_key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractBundleError(f"source_json_invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ContractBundleError(f"source_object_required: {path.name}")
    canonical_bytes(value)
    return value


def canonical_bytes(value: object) -> bytes:
    validate_value(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def validate_value(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -SAFE_INTEGER <= value <= SAFE_INTEGER:
            raise ContractBundleError("canonical_integer_unsafe")
        return
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise ContractBundleError("canonical_string_not_nfc")
        return
    if isinstance(value, list):
        for item in value:
            validate_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise ContractBundleError("canonical_key_invalid")
            validate_value(item)
        return
    raise ContractBundleError("canonical_type_invalid")


def require_fields(value: object, expected: set[str], code: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractBundleError(code)


def nonempty_ascii(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ContractBundleError(code)
    return value


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


SEMANTIC_CYCLE_EXCEPTIONS_SHA256 = sha256(
    canonical_bytes(list(SEMANTIC_CYCLE_EXCEPTIONS))
)
