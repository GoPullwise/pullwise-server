#!/usr/bin/env python
"""Deterministically generate the pullwise-review/v1 contract consumers.

Reads ONLY the frozen manifest (``contracts/pullwise-review/v1/manifest.json``)
and exactly the files it references, then emits the self-contained Python and
npm consumers for ``pullwise-review/v1``.  Every embedded value is serialized
canonically (sorted keys, compact separators), so two clean runs produce
byte-identical output.  No timestamp, absolute path, or environment value is
ever embedded.  The write set of card R1-03 names the four generated files;
``schema.json`` in the npm package is a byte copy of the manifest-bound shared
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
    "reviewer-contract-python/reviewer_contract.py",
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


def _py_scalar(value) -> str:
    """JSON scalar -> a valid Python source literal."""
    return _json_blob(value)


def _py_json(value) -> str:
    """JSON value -> Python expression `json.loads('<json blob>')`.

    Required because JSON booleans/null are not valid Python literals; the blob
    is embedded as an escaped string literal and decoded at import time.
    """
    return "json.loads(" + json.dumps(_json_blob(value)) + ")"


def read_frozen_contract(contract_dir: Path) -> tuple[dict, dict[str, bytes]]:
    """Read the manifest and the exact bytes of every file it references.

    Only ``manifest.json`` is read directly; every other byte the generator
    embeds comes from a file the manifest lists, verified byte-for-byte against
    the manifest's declared SHA-256 and size.  The canonical manifest digest is
    recomputed and must match the declared one.
    """
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
# Generated Python consumer template.  The module is stdlib-only; the keyword
# subset matches the full inventory the frozen schema actually uses ($ref
# internal, oneOf, type, const, enum, pattern, min/maxLength, min/maximum,
# min/maxItems, items, required, properties, additionalProperties).
# ---------------------------------------------------------------------------

PYTHON_CONSUMER_TEMPLATE = """\
\"\"\"Pullwise reviewer contract consumer - Python (generated).

Deterministically generated from the frozen pullwise-review/v1 manifest by
scripts/generate_reviewer_contract.py.  Build output: manual edits fail
scripts/check_reviewer_contract.py.  Regenerate with::

    python scripts/generate_reviewer_contract.py --out generated

Self-contained (stdlib only): exposes the closed registries, the HTTP status
mapping, the manifest binding, and the shared JSON Schema, plus closed-object
validators.  Strict JSON decoding and canonicalization are the caller's
transport boundary (pullwise-canonical-json/v1); the validators operate on
already-decoded instances.
\"\"\"

from __future__ import annotations

import json
import re
from typing import Any

CONTRACT_VERSION = @@CONTRACT_VERSION@@
CANONICALIZATION = @@CANONICALIZATION@@
MANIFEST_DIGEST = @@MANIFEST_DIGEST@@
FILES = @@FILES@@
REGISTRIES = @@REGISTRIES@@
HTTP_STATUS_BY_ERROR_CODE = @@HTTP_STATUS@@
SCHEMA = @@SCHEMA@@
CANONICALIZATION_SPEC = @@CANONICAL_SPEC@@


def _tn(value: Any) -> str:
    \"\"\"JSON type name of a decoded instance.\"\"\"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _validate(node, instance, path, errors, root) -> None:
    \"\"\"Append (json_path, message) violations of instance against node.\"\"\"
    ref = node.get("$ref")
    if ref is not None:
        if not ref.startswith("#/$defs/"):
            errors.append((path, f"unsupported external $ref {ref}"))
            return
        target = ref[len("#/$defs/"):]
        if target not in root["$defs"]:
            errors.append((path, f"unresolved $ref {ref}"))
            return
        _validate(root["$defs"][target], instance, path, errors, root)
        return

    if "oneOf" in node:
        matches = 0
        for index, branch in enumerate(node["oneOf"]):
            branch_errors: list = []
            _validate(branch, instance, f"{path}#oneOf[{index}]", branch_errors, root)
            if not branch_errors:
                matches += 1
        if matches != 1:
            errors.append((path, f"oneOf requires exactly one match, got {matches}"))
        return

    if "type" in node:
        actual = _tn(instance)
        want = node["type"]
        ok = actual in ("integer", "number") if want == "number" else actual == want
        if not ok:
            errors.append((path, f"expected type {want}, got {actual}"))
            return

    if "const" in node and instance != node["const"]:
        errors.append((path, f"expected const {node['const']!r}, got {instance!r}"))
    if "enum" in node and instance not in node["enum"]:
        errors.append((path, f"value {instance!r} not in enum {node['enum']}"))
    if "pattern" in node and isinstance(instance, str):
        if re.fullmatch(node["pattern"], instance) is None:
            errors.append((path, f"value {instance!r} does not match pattern {node['pattern']}"))
    if "minLength" in node and isinstance(instance, str) and len(instance) < node["minLength"]:
        errors.append((path, f"len {len(instance)} < minLength {node['minLength']}"))
    if "maxLength" in node and isinstance(instance, str) and len(instance) > node["maxLength"]:
        errors.append((path, f"len {len(instance)} > maxLength {node['maxLength']}"))
    if "minimum" in node and isinstance(instance, (int, float)) and instance < node["minimum"]:
        errors.append((path, f"{instance} < minimum {node['minimum']}"))
    if "maximum" in node and isinstance(instance, (int, float)) and instance > node["maximum"]:
        errors.append((path, f"{instance} > maximum {node['maximum']}"))

    if isinstance(instance, dict):
        if "required" in node:
            for name in node["required"]:
                if name not in instance:
                    errors.append((path, f"missing required property {name!r}"))
        if "properties" in node:
            for name, prop_schema in node["properties"].items():
                if name in instance:
                    _validate(prop_schema, instance[name], f"{path}.{name}", errors, root)
            if node.get("additionalProperties") is False:
                for key in instance:
                    if key not in node["properties"]:
                        errors.append((path, f"additional property {key!r} is not permitted"))

    if isinstance(instance, list) and "items" in node:
        if "minItems" in node and len(instance) < node["minItems"]:
            errors.append((path, f"len {len(instance)} < minItems {node['minItems']}"))
        if "maxItems" in node and len(instance) > node["maxItems"]:
            errors.append((path, f"len {len(instance)} > maxItems {node['maxItems']}"))
        for index, item in enumerate(instance):
            _validate(node["items"], item, f"{path}[{index}]", errors, root)


def validate_definition(definition: str, instance: Any) -> list:
    \"\"\"Return (json_path, message) violations against a $defs definition.\"\"\"
    if definition not in SCHEMA["$defs"]:
        return [("$defs", f"unknown definition {definition}")]
    errors: list = []
    _validate(SCHEMA["$defs"][definition], instance, "$", errors, SCHEMA)
    return errors


def validate_document(instance: Any) -> list:
    \"\"\"Return violations against the Document root definition.\"\"\"
    return validate_definition("Document", instance)


def classify_error_code(errors: list) -> str:
    \"\"\"Map violations to REQUEST_INVALID or EVIDENCE_INVALID.

    A violation rooted at the Finding evidence path is EVIDENCE_INVALID; every
    other validation failure is REQUEST_INVALID.
    \"\"\"
    for path, _message in errors:
        if (
            path == "$.evidence.path"
            or path.startswith("$.evidence.path.")
            or path.startswith("$.evidence.path#")
        ):
            return "EVIDENCE_INVALID"
    return "REQUEST_INVALID"
"""


# ---------------------------------------------------------------------------
# Generated npm consumer template.  CommonJS, dependency-free.  SCHEMA is loaded
# from ./schema.json (a byte copy of the manifest-bound shared JSON Schema).
# ---------------------------------------------------------------------------

NPM_INDEX_TEMPLATE = """\
'use strict';
/* Pullwise reviewer contract consumer - npm (generated).
 *
 * Deterministically generated from the frozen pullwise-review/v1 manifest by
 * scripts/generate_reviewer_contract.py.  Build output: manual edits fail
 * scripts/check_reviewer_contract.py.  Regenerate with:
 *
 *     python scripts/generate_reviewer_contract.py --out generated
 *
 * Dependency-free CommonJS.  Exposes the same closed registries, HTTP status
 * mapping, and closed-object validators as the generated Python consumer;
 * SCHEMA is loaded from ./schema.json.  The keyword subset matches the frozen
 * schema's full inventory ($ref internal, oneOf, type, const, enum, pattern,
 * min/maxLength, min/maximum, min/maxItems, items, required, properties,
 * additionalProperties).  Strict decoding is the caller's boundary.
 */
const SCHEMA = require('./schema.json');

const CONTRACT_VERSION = @@CONTRACT_VERSION@@;
const CANONICALIZATION = @@CANONICALIZATION@@;
const MANIFEST_DIGEST = @@MANIFEST_DIGEST@@;
const FILES = @@FILES@@;
const REGISTRIES = @@REGISTRIES@@;
const HTTP_STATUS_BY_ERROR_CODE = @@HTTP_STATUS@@;

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

function validateDefinition(definition, instance) {
  if (!has(SCHEMA.$defs, definition)) return [['$defs', 'unknown definition ' + definition]];
  const errors = [];
  validate(SCHEMA.$defs[definition], instance, '$', errors, SCHEMA);
  return errors;
}

function validateDocument(instance) {
  return validateDefinition('Document', instance);
}

function classifyErrorCode(errors) {
  for (const entry of errors) {
    const p = entry[0];
    if (p === '$.evidence.path' || p.startsWith('$.evidence.path.') || p.startsWith('$.evidence.path#')) {
      return 'EVIDENCE_INVALID';
    }
  }
  return 'REQUEST_INVALID';
}

module.exports = {
  CONTRACT_VERSION,
  CANONICALIZATION,
  MANIFEST_DIGEST,
  FILES,
  REGISTRIES,
  HTTP_STATUS_BY_ERROR_CODE,
  SCHEMA,
  validateDefinition,
  validateDocument,
  classifyErrorCode,
};
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
    schema = json.loads(files["shared/schemas/pullwise-review.schema.json"].decode("utf-8"))
    canonical_spec = files["canonical-json-v1.md"].decode("utf-8")

    py_replacements = {
        "@@CONTRACT_VERSION@@": _py_scalar(manifest["contract_version"]),
        "@@CANONICALIZATION@@": _py_scalar(manifest["canonicalization"]),
        "@@MANIFEST_DIGEST@@": _py_scalar(manifest["manifest_digest"]),
        "@@FILES@@": _py_json(manifest["files"]),
        "@@REGISTRIES@@": _py_json(registry["registries"]),
        "@@HTTP_STATUS@@": _py_json(registry["http_status_by_error_code"]),
        "@@SCHEMA@@": _py_json(schema),
        "@@CANONICAL_SPEC@@": _py_scalar(canonical_spec),
    }
    js_replacements = {
        "@@CONTRACT_VERSION@@": _json_blob(manifest["contract_version"]),
        "@@CANONICALIZATION@@": _json_blob(manifest["canonicalization"]),
        "@@MANIFEST_DIGEST@@": _json_blob(manifest["manifest_digest"]),
        "@@FILES@@": _json_blob(manifest["files"]),
        "@@REGISTRIES@@": _json_blob(registry["registries"]),
        "@@HTTP_STATUS@@": _json_blob(registry["http_status_by_error_code"]),
    }

    python_text = PYTHON_CONSUMER_TEMPLATE
    index_text = NPM_INDEX_TEMPLATE
    for token, value in py_replacements.items():
        python_text = python_text.replace(token, value)
    for token, value in js_replacements.items():
        index_text = index_text.replace(token, value)

    package_json = {
        "name": "pullwise-review-contract",
        "version": "1.0.0",
        "description": "Deterministic npm contract consumer for pullwise-review/v1 (generated; do not edit).",
        "main": "index.js",
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
        "reviewer-contract-python/reviewer_contract.py": python_text.encode("utf-8"),
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
        description="Deterministically generate the pullwise-review/v1 contract consumers.",
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
        help="directory to receive reviewer-contract-python/ and reviewer-contract-npm/",
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
