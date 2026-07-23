"""Build the sole Server-owned Agent-First current contract publication."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .agent_first_contract_bundle_npm import render_npm_wrapper
from .agent_first_contract_bundle_python import render_python_wrapper
from .agent_first_contract_bundle_registry import validate_semantic_registries
from .agent_first_contract_bundle_source import (
    ContractBundleError,
    FIXTURE_CLASSES,
    SEMANTIC_CYCLE_EXCEPTIONS,
    SEMANTIC_CYCLE_EXCEPTIONS_SHA256,
    canonical_bytes as _canonical_bytes,
    load_family as _load_family,
    load_json as _load_json,
    nonempty_ascii as _nonempty_ascii,
    reference_dag as _reference_dag,
    require_fields as _require_fields,
    sha256 as _sha256,
)


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
    "transport-abandonment",
    "tool-evidence",
    "change-set-patch",
    "change-set",
    "execution-profile",
    "execution-state",
    "source-state",
    "task-observation",
    "task-observation-manifests",
    "task-requirements",
    "effective-execution-policy",
    "task-request",
    "task-record",
    "task-attempt-owner",
    "task-completion-proposal",
    "quality-policy-plan",
    "task-verifier-input",
    "task-verifier-work",
    "task-attestation",
    "task-verification",
    "budget",
    "task-publication",
    "receipt-error",
    "gate-preparation",
    "pre-gate",
    "gate-input",
    "gate",
    "task-evidence",
    "task-result-identities",
    "task-result-reasons",
    "task-result-evidence",
    "task-result-outcomes",
    "task-result-outcome-success-variants",
    "task-result-outcome-stop-variants",
    "task-result",
    "task-result-core",
    "worker-debug-content",
    "worker-debug-transport",
    "task-result-transport",
)


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
        "semantic_cycle_exceptions": list(SEMANTIC_CYCLE_EXCEPTIONS),
        "semantic_cycle_exceptions_sha256": SEMANTIC_CYCLE_EXCEPTIONS_SHA256,
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
    npm_package = (
        _canonical_bytes(
            {
                "name": identity,
                "version": version,
                "type": "module",
                "exports": "./index.js",
                "files": ["index.js"],
                "pullwiseContentSha256": content_sha256,
                "pullwiseRootSha256": root_sha256,
            }
        )
        + b"\n"
    )
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
