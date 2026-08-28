#!/usr/bin/env python
"""Deterministically generate the pullwise-review/v1 contract consumers.

Reads ONLY the frozen manifest (``contracts/pullwise-review/v1/manifest.json``)
and exactly the files it references, then emits the self-contained npm ESM
consumer for ``pullwise-review/v1``.  Every embedded value is serialized
canonically (sorted keys, compact separators), so two clean runs produce
byte-identical output.  No timestamp, absolute path, or environment value is
ever embedded.  The R1-PI-03 output is exactly three npm files; ``schema.json``
is a byte copy of the manifest-bound shared
JSON Schema so npm consumers validate against the same closed schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pullwise_server.reviewer.canonical import canonical_sha256  # noqa: E402

MANIFEST_FILENAME = "manifest.json"
DEFAULT_CONTRACT_DIR = _REPO_ROOT / "contracts" / "pullwise-review" / "v1"
OUTPUT_RELATIVE_PATHS = (
    "reviewer-contract-npm/package.json",
    "reviewer-contract-npm/index.js",
    "reviewer-contract-npm/schema.json",
)


def _json_blob(value) -> str:
    """Canonical compact JSON: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def read_frozen_contract(contract_dir: Path) -> tuple[dict, dict[str, bytes]]:
    """Read and verify the manifest plus every exact file it references."""
    manifest_path = Path(contract_dir) / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_id") != "pullwise-review-manifest/v1":
        raise RuntimeError(f"{manifest_path} is not a pullwise-review manifest")
    payload = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    declared = manifest.get("manifest_digest")
    actual = canonical_sha256(payload)
    if not isinstance(declared, str) or actual != declared:
        raise RuntimeError(
            f"manifest digest mismatch: declared {declared!r}, canonical payload hashes to {actual}"
        )
    files: dict[str, bytes] = {}
    for relative_path, spec in manifest["files"].items():
        target = Path(contract_dir) / relative_path
        try:
            data = target.read_bytes()
        except OSError as error:
            raise RuntimeError(
                f"manifest integrity violation for {relative_path}: {error}"
            ) from error
        if len(data) != spec["size_bytes"]:
            raise RuntimeError(
                f"manifest integrity violation for {relative_path}: size {len(data)} != "
                f"declared {spec['size_bytes']}"
            )
        if hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise RuntimeError(
                f"manifest integrity violation for {relative_path}: sha256 does not match"
            )
        files[relative_path] = data
    return manifest, files


# ---------------------------------------------------------------------------
# Generated npm consumer template.  ESM, dependency-free.  SCHEMA is loaded from
# ./schema.json (a byte copy of the manifest-bound shared JSON Schema).
# ---------------------------------------------------------------------------

NPM_INDEX_TEMPLATE = """\
/* Pullwise reviewer contract consumer - npm (generated).
 *
 * Deterministically generated from the frozen pullwise-review/v1 manifest by
 * scripts/generate_reviewer_contract.py.  Build output: manual edits fail
 * scripts/check_reviewer_contract.py.  Regenerate with:
 *
 *     python scripts/generate_reviewer_contract.py --out generated
 *
 * Dependency-free ESM with named exports only.  Exposes the closed registries,
 * HTTP status mapping, and closed-object validators; SCHEMA is loaded from
 * ./schema.json.  The keyword subset matches the frozen
 * schema's full inventory ($ref internal, oneOf, type, const, enum, pattern,
 * min/maxLength, min/maximum, min/maxItems, items, required, properties,
 * additionalProperties).  Strict decoding is the caller's boundary.
 */
import SCHEMA_VALUE from './schema.json' with { type: 'json' };

export const CONTRACT_VERSION = @@CONTRACT_VERSION@@;
export const CANONICALIZATION = @@CANONICALIZATION@@;
export const MANIFEST_DIGEST = @@MANIFEST_DIGEST@@;
export const FILES = @@FILES@@;
export const REGISTRIES = @@REGISTRIES@@;
export const HTTP_STATUS_BY_ERROR_CODE = @@HTTP_STATUS@@;
export const SCHEMA = SCHEMA_VALUE;

function tn(value) {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  if (typeof value === 'string') return 'string';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'object') return 'object';
  return 'unknown';
}

function has(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function validate(node, instance, path, errors, root) {
  if (has(node, '$ref')) {
    const ref = node.$ref;
    if (!ref.startsWith('#/$defs/')) { errors.push([path, 'unsupported external $ref ' + ref]); return; }
    const target = ref.slice('#/$defs/'.length);
    if (!has(root.$defs, target)) { errors.push([path, 'unresolved $ref ' + ref]); return; }
    validate(root.$defs[target], instance, path, errors, root);
    return;
  }
  if (has(node, 'oneOf')) {
    let matches = 0;
    for (let i = 0; i < node.oneOf.length; i++) {
      const be = [];
      validate(node.oneOf[i], instance, path + '#oneOf[' + i + ']', be, root);
      if (be.length === 0) matches++;
    }
    if (matches !== 1) errors.push([path, 'oneOf requires exactly one match, got ' + matches]);
    return;
  }
  if (has(node, 'type')) {
    const actual = tn(instance);
    const want = node.type;
    const ok = want === 'number' ? (actual === 'integer' || actual === 'number') : actual === want;
    if (!ok) { errors.push([path, 'expected type ' + want + ', got ' + actual]); return; }
  }
  if (has(node, 'const') && instance !== node.const) {
    errors.push([path, 'expected const ' + JSON.stringify(node.const) + ', got ' + JSON.stringify(instance)]);
  }
  if (has(node, 'enum') && !node.enum.includes(instance)) {
    errors.push([path, 'value ' + JSON.stringify(instance) + ' not in enum ' + JSON.stringify(node.enum)]);
  }
  if (has(node, 'pattern') && typeof instance === 'string') {
    if (!new RegExp('^(?:' + node.pattern + ')$').test(instance)) {
      errors.push([path, 'value ' + JSON.stringify(instance) + ' does not match pattern ' + node.pattern]);
    }
  }
  if (has(node, 'minLength') && typeof instance === 'string' && instance.length < node.minLength) {
    errors.push([path, 'len ' + instance.length + ' < minLength ' + node.minLength]);
  }
  if (has(node, 'maxLength') && typeof instance === 'string' && instance.length > node.maxLength) {
    errors.push([path, 'len ' + instance.length + ' > maxLength ' + node.maxLength]);
  }
  if (has(node, 'minimum') && typeof instance === 'number' && instance < node.minimum) {
    errors.push([path, instance + ' < minimum ' + node.minimum]);
  }
  if (has(node, 'maximum') && typeof instance === 'number' && instance > node.maximum) {
    errors.push([path, instance + ' > maximum ' + node.maximum]);
  }
  if (instance !== null && typeof instance === 'object' && !Array.isArray(instance)) {
    if (has(node, 'required')) {
      for (const name of node.required) {
        if (!has(instance, name)) errors.push([path, 'missing required property ' + JSON.stringify(name)]);
      }
    }
    if (has(node, 'properties')) {
      for (const name of Object.keys(node.properties)) {
        if (has(instance, name)) validate(node.properties[name], instance[name], path + '.' + name, errors, root);
      }
      if (node.additionalProperties === false) {
        for (const key of Object.keys(instance)) {
          if (!has(node.properties, key)) errors.push([path, 'additional property ' + JSON.stringify(key) + ' is not permitted']);
        }
      }
    }
  }
  if (Array.isArray(instance) && has(node, 'items')) {
    if (has(node, 'minItems') && instance.length < node.minItems) errors.push([path, 'len ' + instance.length + ' < minItems ' + node.minItems]);
    if (has(node, 'maxItems') && instance.length > node.maxItems) errors.push([path, 'len ' + instance.length + ' > maxItems ' + node.maxItems]);
    for (let i = 0; i < instance.length; i++) validate(node.items, instance[i], path + '[' + i + ']', errors, root);
  }
}

export function validateDefinition(definition, instance) {
  if (!has(SCHEMA.$defs, definition)) return [['$defs', 'unknown definition ' + definition]];
  const errors = [];
  validate(SCHEMA.$defs[definition], instance, '$', errors, SCHEMA);
  return errors;
}

export function validateDocument(instance) {
  return validateDefinition('Document', instance);
}

export function classifyErrorCode(errors) {
  for (const entry of errors) {
    const p = entry[0];
    if (p === '$.evidence.path' || p.startsWith('$.evidence.path.') || p.startsWith('$.evidence.path#')) {
      return 'EVIDENCE_INVALID';
    }
  }
  return 'REQUEST_INVALID';
}
"""


def build_outputs(contract_dir: Path) -> dict[str, bytes]:
    """Return {output_relative_path: bytes} for a deterministic generation."""
    manifest, files = read_frozen_contract(contract_dir)
    for required in (
        "registry.json",
        "shared/schemas/pullwise-review.schema.json",
        "canonical-json-v1.md",
    ):
        if required not in files:
            raise RuntimeError(f"manifest does not reference {required}")

    registry = json.loads(files["registry.json"].decode("utf-8"))
    js_replacements = {
        "@@CONTRACT_VERSION@@": _json_blob(manifest["contract_version"]),
        "@@CANONICALIZATION@@": _json_blob(manifest["canonicalization"]),
        "@@MANIFEST_DIGEST@@": _json_blob(manifest["manifest_digest"]),
        "@@FILES@@": _json_blob(manifest["files"]),
        "@@REGISTRIES@@": _json_blob(registry["registries"]),
        "@@HTTP_STATUS@@": _json_blob(registry["http_status_by_error_code"]),
    }

    index_text = NPM_INDEX_TEMPLATE
    for token, value in js_replacements.items():
        index_text = index_text.replace(token, value)

    package_json = {
        "name": "pullwise-review-contract",
        "version": "1.0.0",
        "description": "Deterministic npm contract consumer for pullwise-review/v1 (generated; do not edit).",
        "type": "module",
        "exports": "./index.js",
        "files": ["index.js", "schema.json"],
        "private": True,
        "license": "UNLICENSED",
        "contract": {
            "schema_id": "pullwise-review-consumer-npm/v1",
            "contract_version": manifest["contract_version"],
            "canonicalization": manifest["canonicalization"],
            "manifest_digest": manifest["manifest_digest"],
        },
    }

    return {
        "reviewer-contract-npm/package.json": _json_blob(package_json).encode("utf-8") + b"\n",
        "reviewer-contract-npm/index.js": index_text.encode("utf-8"),
        "reviewer-contract-npm/schema.json": files["shared/schemas/pullwise-review.schema.json"],
    }


def write_outputs(out: Path, outputs: dict[str, bytes]) -> dict[str, str]:
    """Write every output with exact bytes; return {path: sha256 hex}."""
    digest_map: dict[str, str] = {}
    for relative_path, data in outputs.items():
        target = Path(out) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest_map[relative_path] = hashlib.sha256(data).hexdigest()
    return digest_map


def generate(contract_dir: Path, out: Path, report: Path | None = None) -> dict[str, str]:
    """Run a deterministic generation; optionally write a regeneration report."""
    outputs = build_outputs(contract_dir)
    digest_map = write_outputs(out, outputs)
    if report is not None:
        manifest, _ = read_frozen_contract(contract_dir)
        report_body = {
            "schema_id": "pullwise-review-generation-report/v1",
            "contract_version": "pullwise-review/v1",
            "canonicalization": "pullwise-canonical-json/v1",
            "manifest_digest": manifest["manifest_digest"],
            "outputs": {
                relative_path: {
                    "sha256": digest_map[relative_path],
                    "size_bytes": len(outputs[relative_path]),
                }
                for relative_path in sorted(outputs)
            },
        }
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_bytes(_json_blob(report_body).encode("utf-8") + b"\n")
    return digest_map


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate_reviewer_contract.py",
        description="Deterministically generate the npm-only ESM pullwise-review/v1 consumer.",
    )
    parser.add_argument(
        "--contract-dir",
        type=Path,
        default=DEFAULT_CONTRACT_DIR,
        help="directory containing manifest.json (default: frozen pullwise-review/v1)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory to receive the three files under reviewer-contract-npm/",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="optional path for the deterministic regeneration report JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    digest_map = generate(args.contract_dir, args.out, args.report)
    for relative_path in OUTPUT_RELATIVE_PATHS:
        print(f"generated {relative_path} sha256:{digest_map[relative_path]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
