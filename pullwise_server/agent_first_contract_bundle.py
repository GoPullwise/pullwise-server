"""Build the single Server-owned Agent-First contract publication root."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import unicodedata


PACKAGE_SOURCE_ID = "agent-task-contract-package-source/v1"
ROOT_SCHEMA_ID = "agent-task-contract-root/v1"
BUNDLE_SCHEMA_ID = "agent-task-contract-bundle/v1"
REQUIRED_FAMILIES = (
    "authority-control",
    "tool-evidence",
    "budget",
    "receipt-error",
)
FIXTURE_CLASSES = ("golden", "negative", "idempotency", "fence", "crash")
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
    source_root = Path(source_root)
    package = _load_json(source_root / "package.json")
    _require_fields(
        package,
        {"schema_id", "package_identity", "package_version", "required_families"},
        "package_source_fields_invalid",
    )
    if package["schema_id"] != PACKAGE_SOURCE_ID:
        raise ContractBundleError("package_source_identity_invalid")
    identity = _nonempty_ascii(package["package_identity"], "package_identity_invalid")
    version = _nonempty_ascii(package["package_version"], "package_version_invalid")
    if package["required_families"] != list(REQUIRED_FAMILIES):
        raise ContractBundleError("required_family_set_invalid")

    family_root = source_root / "families"
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
    fixture_classes: set[str] = set()
    for family_id in REQUIRED_FAMILIES:
        family_path = family_root / f"{family_id}.json"
        if not family_path.exists():
            raise ContractBundleError(f"required_family_missing: {family_id}")
        family = _load_family(family_path, family_id, schema_owner, fixture_ids)
        families.append(family)
        fixture_classes.update(
            fixture["fixture_class"] for fixture in family["fixtures"]
        )
    if fixture_classes != set(FIXTURE_CLASSES):
        raise ContractBundleError("fixture_class_closure_invalid")

    references = _reference_dag(families, schema_owner)
    family_entries = []
    for family in families:
        family_entries.append(
            {
                "family_id": family["family_id"],
                "fixture_classes": sorted(
                    {fixture["fixture_class"] for fixture in family["fixtures"]}
                ),
                "fixture_ids": [fixture["fixture_id"] for fixture in family["fixtures"]],
                "schema_ids": [schema["$id"] for schema in family["schemas"]],
                "sha256": _sha256(_canonical_bytes(family)),
            }
        )
    root_without_digest = {
        "schema_id": ROOT_SCHEMA_ID,
        "package_identity": identity,
        "package_version": version,
        "required_families": list(REQUIRED_FAMILIES),
        "families": family_entries,
        "reference_dag": references,
    }
    root_sha256 = _sha256(_canonical_bytes(root_without_digest))
    root = {**root_without_digest, "root_sha256": root_sha256}
    document = {
        "schema_id": BUNDLE_SCHEMA_ID,
        "package_identity": identity,
        "package_version": version,
        "root_manifest": root,
        "families": families,
    }
    canonical = _canonical_bytes(document)
    content_sha256 = _sha256(canonical)
    python_wrapper = _render_python_wrapper(
        identity, version, root_sha256, content_sha256, canonical
    )
    npm_wrapper = _render_npm_wrapper(
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
        package_identity=identity,
        package_version=version,
        root_sha256=root_sha256,
        content_sha256=content_sha256,
        document=document,
        root_bytes=_canonical_bytes(root),
        canonical_bytes=canonical,
        python_wrapper=python_wrapper,
        npm_wrapper=npm_wrapper,
        npm_package=npm_package,
    )


def write_generated(repo_root: Path, *, check: bool = False) -> None:
    root = Path(repo_root)
    source = root / "contracts" / "agent-first" / "current" / "source"
    bundle = build_bundle(source)
    paths = generated_paths(root)
    outputs = {
        paths.root_manifest: bundle.root_bytes,
        paths.bundle: bundle.canonical_bytes,
        paths.python_wrapper: bundle.python_wrapper,
        paths.npm_index: bundle.npm_wrapper,
        paths.npm_package: bundle.npm_package,
    }
    stale: list[str] = []
    for path, payload in outputs.items():
        if check:
            if not path.is_file() or path.read_bytes() != payload:
                stale.append(path.relative_to(root).as_posix())
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
    schemas = source["schemas"]
    fixtures = source["fixtures"]
    if not isinstance(schemas, list) or not schemas:
        raise ContractBundleError(f"family_schemas_invalid: {expected_id}")
    if not isinstance(fixtures, list) or not fixtures:
        raise ContractBundleError(f"family_fixtures_invalid: {expected_id}")

    registry = []
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
        registry.append({"schema_id": schema_id, "sha256": _sha256(_canonical_bytes(schema))})
    if [item["$id"] for item in schemas] != sorted(item["$id"] for item in schemas):
        raise ContractBundleError(f"schema_order_invalid: {expected_id}")

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ContractBundleError(f"fixture_invalid: {expected_id}")
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
        if fixture["schema_id"] not in {item["$id"] for item in schemas}:
            raise ContractBundleError(f"fixture_schema_not_in_family: {fixture_id}")
        expected_code = fixture["expected_code"]
        if expected_code is not None and not isinstance(expected_code, str):
            raise ContractBundleError(f"fixture_expected_code_invalid: {fixture_id}")
        _canonical_bytes(fixture["document"])
    if [item["fixture_id"] for item in fixtures] != sorted(
        item["fixture_id"] for item in fixtures
    ):
        raise ContractBundleError(f"fixture_order_invalid: {expected_id}")
    return {
        "family_id": expected_id,
        "schemas": schemas,
        "registry": registry,
        "fixtures": fixtures,
    }


def _reference_dag(
    families: list[dict[str, object]], schema_owner: dict[str, str]
) -> list[dict[str, object]]:
    graph: dict[str, set[str]] = {}
    for family in families:
        for schema in family["schemas"]:
            schema_id = schema["$id"]
            refs = _references(schema)
            unknown = refs.difference(schema_owner)
            if unknown:
                raise ContractBundleError(
                    f"schema_reference_unknown: {schema_id}: {sorted(unknown)[0]}"
                )
            graph[schema_id] = refs
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
        found: set[str] = set()
        for item in value:
            found.update(_references(item))
        return found
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
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
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


def _render_python_wrapper(
    identity: str, version: str, root_sha: str, content_sha: str, canonical: bytes
) -> bytes:
    payload = base64.b64encode(canonical).decode("ascii")
    text = f'''"""Generated from the Server-owned Agent-First contract bundle; do not edit."""
from __future__ import annotations

import base64
import json

PACKAGE_IDENTITY = {identity!r}
PACKAGE_VERSION = {version!r}
ROOT_SHA256 = {root_sha!r}
CONTENT_SHA256 = {content_sha!r}
BUNDLE_BASE64 = {payload!r}

def bundle_bytes() -> bytes:
    return base64.b64decode(BUNDLE_BASE64, validate=True)

def bundle() -> dict[str, object]:
    return json.loads(bundle_bytes().decode("utf-8"))

def schema(schema_id: str) -> dict[str, object]:
    for family in bundle()["families"]:
        for document in family["schemas"]:
            if document["$id"] == schema_id:
                return document
    raise KeyError(schema_id)

def assert_pin(identity: str, version: str, content_sha256: str) -> None:
    if (identity, version, content_sha256) != (
        PACKAGE_IDENTITY, PACKAGE_VERSION, CONTENT_SHA256
    ):
        raise RuntimeError("CURRENT_PACKAGE_PIN_MISMATCH")

__all__ = [
    "BUNDLE_BASE64", "CONTENT_SHA256", "PACKAGE_IDENTITY", "PACKAGE_VERSION",
    "ROOT_SHA256", "assert_pin", "bundle", "bundle_bytes", "schema",
]
'''
    return text.encode("utf-8")


def _render_npm_wrapper(
    identity: str, version: str, root_sha: str, content_sha: str, canonical: bytes
) -> bytes:
    payload = base64.b64encode(canonical).decode("ascii")
    values = {
        "identity": json.dumps(identity),
        "version": json.dumps(version),
        "root": json.dumps(root_sha),
        "content": json.dumps(content_sha),
        "payload": json.dumps(payload),
    }
    text = f'''// Generated from the Server-owned Agent-First contract bundle; do not edit.
export const PACKAGE_IDENTITY = {values["identity"]};
export const PACKAGE_VERSION = {values["version"]};
export const ROOT_SHA256 = {values["root"]};
export const CONTENT_SHA256 = {values["content"]};
export const BUNDLE_BASE64 = {values["payload"]};

export function bundleBytes() {{
  const decoded = atob(BUNDLE_BASE64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}}

export function bundle() {{
  return JSON.parse(new TextDecoder().decode(bundleBytes()));
}}

export function schema(schemaId) {{
  for (const family of bundle().families) {{
    const document = family.schemas.find((candidate) => candidate.$id === schemaId);
    if (document) return document;
  }}
  throw new Error(`UNKNOWN_CONTRACT_SCHEMA: ${{schemaId}}`);
}}

export function assertPin(identity, version, contentSha256) {{
  if (
    identity !== PACKAGE_IDENTITY ||
    version !== PACKAGE_VERSION ||
    contentSha256 !== CONTENT_SHA256
  ) {{
    throw new Error("CURRENT_PACKAGE_PIN_MISMATCH");
  }}
}}
'''
    return text.encode("utf-8")


def _require_fields(value: object, expected: set[str], code: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractBundleError(code)


def _nonempty_ascii(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ContractBundleError(code)
    return value


def _sha256(value: bytes) -> str:
    digest = hashlib.sha256(value).hexdigest()
    assert SHA256_PATTERN.fullmatch(digest)
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    write_generated(args.repo_root, check=args.command == "check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
