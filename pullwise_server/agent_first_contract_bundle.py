"""Build the sole Server-owned Agent-First current contract publication."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import unicodedata

from .agent_first_contract_bundle_npm import render_npm_wrapper
from .agent_first_contract_bundle_python import render_python_wrapper
from .agent_first_contract_bundle_registry import validate_semantic_registries


PACKAGE_SOURCE_ID = "agent-task-contract-package-source/v1"
ROOT_SCHEMA_ID = "agent-task-contract-root/v1"
BUNDLE_SCHEMA_ID = "agent-task-contract-bundle/v1"
SOURCE_AUTHORITY = "server_owned_package"
PUBLICATION_MODEL = "logical_bundle_generated_wrappers"
ROOT_KIND = "exhaustive_layered_atomic_root"
CANONICALIZATION = "pullwise-canonical-json/v1"
REQUIRED_FAMILIES = (
    "core",
    "authority-control",
    "tool-evidence",
    "budget",
    "receipt-error",
    "gate",
)
FIXTURE_CLASSES = ("golden", "negative", "idempotency", "fence", "crash")
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$")
SAFE_INTEGER = (1 << 53) - 1


class ContractBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedPaths:
    root_manifest: Path
    bundle: Path
    python_wrapper: Path
    npm_index: Path
    npm_package: Path


@dataclass(frozen=True)
class PublishedBundle:
    package_identity: str
    package_version: str
    root_sha256: str
    content_sha256: str
    document: dict[str, object]
    root_bytes: bytes
    canonical_bytes: bytes
    python_wrapper: bytes
    npm_wrapper: bytes
    npm_package: bytes


def generated_paths(repo_root: Path) -> GeneratedPaths:
    root = Path(repo_root)
    published = root / "contracts" / "agent-first" / "current" / "published"
    npm = root / "generated" / "agent-task-contract-npm"
    return GeneratedPaths(
        root_manifest=published / "root-manifest.json",
        bundle=published / "contract-bundle.json",
        python_wrapper=root / "pullwise_server" / "_generated_agent_task_contract.py",
        npm_index=npm / "index.js",
        npm_package=npm / "package.json",
    )


def build_bundle(source_root: Path) -> PublishedBundle:
    package = _load_json(Path(source_root) / "package.json")
    _require_fields(
        package,
        {
            "schema_id",
            "package_identity",
            "package_version",
            "source_authority",
            "publication_model",
            "root_kind",
            "canonicalization",
            "required_families",
        },
        "package_source_fields_invalid",
    )
    expected_metadata = {
        "schema_id": PACKAGE_SOURCE_ID,
        "source_authority": SOURCE_AUTHORITY,
        "publication_model": PUBLICATION_MODEL,
        "root_kind": ROOT_KIND,
        "canonicalization": CANONICALIZATION,
        "required_families": list(REQUIRED_FAMILIES),
    }
    for key, expected in expected_metadata.items():
        if package[key] != expected:
            raise ContractBundleError(f"package_source_{key}_invalid")
    identity = _nonempty_ascii(package["package_identity"], "package_identity_invalid")
    version = _nonempty_ascii(package["package_version"], "package_version_invalid")

    family_root = Path(source_root) / "families"
    existing = {
        path.stem
        for path in family_root.glob("*.json")
        if path.is_file() and not path.is_symlink()
    }
    unexpected = existing.difference(REQUIRED_FAMILIES)
    if unexpected:
        raise ContractBundleError(f"unexpected_family: {sorted(unexpected)[0]}")

    families: list[dict[str, object]] = []
    schema_owner: dict[str, str] = {}
    fixture_ids: set[str] = set()
    for family_id in REQUIRED_FAMILIES:
        family_path = family_root / f"{family_id}.json"
        if not family_path.exists():
            raise ContractBundleError(f"required_family_missing: {family_id}")
        families.append(
            _load_family(family_path, family_id, schema_owner, fixture_ids)
        )
    fixture_classes = {
        item["fixture_class"]
        for family in families
        for item in family["fixtures"]
    }
    if fixture_classes != set(FIXTURE_CLASSES):
        raise ContractBundleError("fixture_class_closure_invalid")
    validate_semantic_registries(families, schema_owner, ContractBundleError)

    reference_dag = _reference_dag(families, schema_owner)
    schema_registry = [
        item for family in families for item in family["schema_registry"]
    ]
    fixture_registry = [
        item for family in families for item in family["fixture_registry"]
    ]
    family_entries = [
        {
            "family_id": family["family_id"],
            "schema_ids": [item["$id"] for item in family["schemas"]],
            "fixture_ids": [item["fixture_id"] for item in family["fixtures"]],
            "sha256": _sha256(_canonical_bytes(family)),
        }
        for family in families
    ]
    root_body = {
        "schema_id": ROOT_SCHEMA_ID,
        "source_authority": SOURCE_AUTHORITY,
        "publication_model": PUBLICATION_MODEL,
        "root_kind": ROOT_KIND,
        "canonicalization": CANONICALIZATION,
        "package_identity": identity,
        "package_version": version,
        "required_families": list(REQUIRED_FAMILIES),
        "fixture_classes": list(FIXTURE_CLASSES),
        "families": family_entries,
        "schema_registry": schema_registry,
        "fixture_registry": fixture_registry,
        "reference_dag": reference_dag,
    }
    root_sha256 = _sha256(_canonical_bytes(root_body))
    root = {**root_body, "root_sha256": root_sha256}
    document = {
        "schema_id": BUNDLE_SCHEMA_ID,
        "package_identity": identity,
        "package_version": version,
        "root_manifest": root,
        "families": families,
    }
    canonical = _canonical_bytes(document)
    content_sha256 = _sha256(canonical)
    python_wrapper = render_python_wrapper(
        identity, version, root_sha256, content_sha256, canonical
    )
    npm_wrapper = render_npm_wrapper(
        identity, version, root_sha256, content_sha256, canonical
    )
    npm_package = _canonical_bytes(
        {
            "name": identity,
            "version": version,
            "type": "module",
            "exports": "./index.js",
            "files": ["index.js"],
            "pullwiseContentSha256": content_sha256,
            "pullwiseRootSha256": root_sha256,
        }
    ) + b"\n"
    return PublishedBundle(
        identity,
        version,
        root_sha256,
        content_sha256,
        document,
        _canonical_bytes(root),
        canonical,
        python_wrapper,
        npm_wrapper,
        npm_package,
    )


def write_generated(
    repo_root: Path,
    *,
    check: bool = False,
    worker_root: Path | None = None,
    web_root: Path | None = None,
) -> None:
    root = Path(repo_root)
    bundle = build_bundle(root / "contracts" / "agent-first" / "current" / "source")
    paths = generated_paths(root)
    outputs = {
        paths.root_manifest: bundle.root_bytes,
        paths.bundle: bundle.canonical_bytes,
        paths.python_wrapper: bundle.python_wrapper,
        paths.npm_index: bundle.npm_wrapper,
        paths.npm_package: bundle.npm_package,
    }
    if worker_root is not None:
        outputs[
            Path(worker_root) / "pullwise_worker" / "_generated_agent_task_contract.py"
        ] = bundle.python_wrapper
    if web_root is not None:
        npm_root = (
            Path(web_root)
            / "vendor"
            / "generated"
            / "agent-task-contract-npm"
        )
        outputs[npm_root / "index.js"] = bundle.npm_wrapper
        outputs[npm_root / "package.json"] = bundle.npm_package
    stale: list[str] = []
    for path, payload in outputs.items():
        if check:
            if not path.is_file() or path.read_bytes() != payload:
                stale.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if stale:
        raise ContractBundleError("generated_artifact_stale: " + ", ".join(stale))


def _load_family(
    path: Path,
    expected_id: str,
    schema_owner: dict[str, str],
    fixture_ids: set[str],
) -> dict[str, object]:
    source = _load_json(path)
    _require_fields(source, {"family_id", "schemas", "fixtures"}, "family_fields_invalid")
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
        if schema_id in schema_owner:
            raise ContractBundleError(f"schema_duplicate: {schema_id}")
        schema_owner[schema_id] = expected_id
        schema_registry.append(
            {
                "schema_id": schema_id,
                "family_id": expected_id,
                "references": sorted(_references(schema)),
                "sha256": _sha256(_canonical_bytes(schema)),
            }
        )
    if [item["$id"] for item in schemas] != sorted(item["$id"] for item in schemas):
        raise ContractBundleError(f"schema_order_invalid: {expected_id}")

    fixture_registry = []
    family_schema_ids = {item["$id"] for item in schemas}
    for fixture in fixtures:
        _require_fields(
            fixture,
            {"fixture_id", "fixture_class", "schema_id", "document", "expected_code"},
            f"fixture_fields_invalid: {expected_id}",
        )
        fixture_id = _nonempty_ascii(fixture["fixture_id"], "fixture_identity_invalid")
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
                "sha256": _sha256(_canonical_bytes(fixture)),
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


def _reference_dag(
    families: list[dict[str, object]], schema_owner: dict[str, str]
) -> list[dict[str, object]]:
    graph = {
        schema["$id"]: _references(schema)
        for family in families
        for schema in family["schemas"]
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
        for target in graph[schema_id]:
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
        }
        for schema_id in sorted(graph)
    ]


def _references(value: object) -> set[str]:
    if isinstance(value, dict):
        found = {
            item for key, item in value.items() if key == "$ref" and isinstance(item, str)
        }
        for item in value.values():
            found.update(_references(item))
        return found
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value), set())
    return set()


def _load_json(path: Path) -> dict[str, object]:
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
    _canonical_bytes(value)
    return value


def _canonical_bytes(value: object) -> bytes:
    _validate_value(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _validate_value(value: object) -> None:
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
            _validate_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise ContractBundleError("canonical_key_invalid")
            _validate_value(item)
        return
    raise ContractBundleError("canonical_type_invalid")


def _require_fields(value: object, expected: set[str], code: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractBundleError(code)


def _nonempty_ascii(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ContractBundleError(code)
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--worker-root", type=Path)
    parser.add_argument("--web-root", type=Path)
    args = parser.parse_args(argv)
    write_generated(
        args.repo_root,
        check=args.command == "check",
        worker_root=args.worker_root,
        web_root=args.web_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
