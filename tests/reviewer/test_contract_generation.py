"""Generator, npm consumer, and build-integrity tests for R1-PI-03.

The target is exactly one dependency-free ESM package.  These tests prove the
three-file npm-only output set, deterministic generation, manifest-bound schema
bytes, closed package/export surfaces, real Node ESM fixture parity, and
missing/manual-edit detection.  The historical generated Python consumer is
explicitly outside this card's generator and checker targets.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "pullwise-review" / "v1"
SCRIPTS = ROOT / "scripts"
GENERATED = ROOT / "generated"
GENERATOR_PATH = SCRIPTS / "generate_reviewer_contract.py"
CHECK_PATH = SCRIPTS / "check_reviewer_contract.py"
PY_CONSUMER = GENERATED / "reviewer-contract-python" / "reviewer_contract.py"
NPM_INDEX = GENERATED / "reviewer-contract-npm" / "index.js"
NPM_SCHEMA = GENERATED / "reviewer-contract-npm" / "schema.json"
NPM_PACKAGE = GENERATED / "reviewer-contract-npm" / "package.json"

EXPECTED_MANIFEST_DIGEST = (
    "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e"
)
EXPECTED_SCHEMA_DIGEST = "39dd603502669542b9e16b30d60522796794014307ae0335b2f892d551f0c6dd"
EXPECTED_PROTECTED_PYTHON_DIGEST = (
    "0a998deec3f72134bfedd6ca4d5246aeb5fbad9cd9efdf691bda7d925349613b"
)
EXPECTED_PROTECTED_PYTHON_BYTES = 67_352
SCHEMA_RELATIVE = "shared/schemas/pullwise-review.schema.json"
OUTPUT_RELATIVE_PATHS = (
    "reviewer-contract-npm/package.json",
    "reviewer-contract-npm/index.js",
    "reviewer-contract-npm/schema.json",
)

EXPECTED_ESM_EXPORTS = {
    "CANONICALIZATION",
    "CONTRACT_VERSION",
    "FILES",
    "HTTP_STATUS_BY_ERROR_CODE",
    "MANIFEST_DIGEST",
    "REGISTRIES",
    "SCHEMA",
    "classifyErrorCode",
    "validateDefinition",
    "validateDocument",
}

def _normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator():
    return _load_module(GENERATOR_PATH, "generate_reviewer_contract")


def _build_bytes(contract_dir: Path) -> dict[str, bytes]:
    return _load_generator().build_outputs(contract_dir)


def _win_js_path(path: Path) -> str:
    return str(path).replace("\\", "/")


class GeneratorDeterminismTest(unittest.TestCase):
    def test_two_clean_generations_are_byte_identical(self) -> None:
        outputs_a = _build_bytes(CONTRACT)
        outputs_b = _build_bytes(CONTRACT)
        self.assertEqual(set(outputs_a), set(OUTPUT_RELATIVE_PATHS))
        self.assertEqual(set(outputs_b), set(OUTPUT_RELATIVE_PATHS))
        for relative_path in OUTPUT_RELATIVE_PATHS:
            with self.subTest(relative_path=relative_path):
                self.assertEqual(outputs_a[relative_path], outputs_b[relative_path])

    def test_generated_schema_json_is_byte_copy_of_shared_schema(self) -> None:
        outputs = _build_bytes(CONTRACT)
        source = (CONTRACT / SCHEMA_RELATIVE).read_bytes()
        self.assertEqual(EXPECTED_SCHEMA_DIGEST, hashlib.sha256(source).hexdigest())
        self.assertEqual(_normalize(source), _normalize(outputs["reviewer-contract-npm/schema.json"]))


class GeneratorSourceIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.contract_copy = Path(self._temp.name) / "contract"
        shutil.copytree(CONTRACT, self.contract_copy)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_pristine_copy_generates_identical_output(self) -> None:
        self.assertEqual(_build_bytes(self.contract_copy), _build_bytes(CONTRACT))

    def test_decoy_file_does_not_affect_output(self) -> None:
        (self.contract_copy / "unused-decoy.json").write_text(
            '{"schema_id": "pullwise-decoy/v1", "ignored": true}\n', encoding="utf-8"
        )
        self.assertEqual(_build_bytes(self.contract_copy), _build_bytes(CONTRACT))

    def test_missing_manifest_referenced_file_raises(self) -> None:
        (self.contract_copy / "registry.json").unlink()
        with self.assertRaises(RuntimeError):
            _build_bytes(self.contract_copy)

    def test_corrupt_manifest_digest_raises(self) -> None:
        manifest_path = self.contract_copy / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["manifest_digest"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            _build_bytes(self.contract_copy)


class CommittedArtifactFidelityTest(unittest.TestCase):
    def test_committed_generated_files_match_clean_generation(self) -> None:
        outputs = _build_bytes(CONTRACT)
        for relative_path, expected in outputs.items():
            with self.subTest(relative_path=relative_path):
                target = GENERATED / relative_path
                self.assertTrue(target.is_file(), f"missing generated file {relative_path}")
                self.assertEqual(_normalize(target.read_bytes()), _normalize(expected))

    def test_package_json_contract_binding(self) -> None:
        package = json.loads(NPM_PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "contract",
                "description",
                "exports",
                "files",
                "license",
                "name",
                "private",
                "type",
                "version",
            },
            set(package),
        )
        self.assertEqual("pullwise-review-contract", package["name"])
        self.assertEqual("module", package["type"])
        self.assertEqual("./index.js", package["exports"])
        self.assertEqual(["index.js", "schema.json"], package["files"])
        for dependency_field in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            self.assertNotIn(dependency_field, package)
        self.assertEqual("pullwise-review-consumer-npm/v1", package["contract"]["schema_id"])
        self.assertEqual("pullwise-review/v1", package["contract"]["contract_version"])
        self.assertEqual(EXPECTED_MANIFEST_DIGEST, package["contract"]["manifest_digest"])


class GenerationReportTest(unittest.TestCase):
    def test_report_digests_are_valid_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "out"
            report = Path(temp_dir) / "generation-report.json"
            gen = _load_generator()
            gen.generate(CONTRACT, out, report)
            body = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("pullwise-review-generation-report/v1", body["schema_id"])
            self.assertEqual(EXPECTED_MANIFEST_DIGEST, body["manifest_digest"])
            for relative_path in OUTPUT_RELATIVE_PATHS:
                with self.subTest(relative_path=relative_path):
                    expected = hashlib.sha256((out / relative_path).read_bytes()).hexdigest()
                    self.assertEqual(expected, body["outputs"][relative_path]["sha256"])

            report_two = Path(temp_dir) / "generation-report-2.json"
            gen.generate(CONTRACT, out, report_two)
            self.assertEqual(report.read_bytes(), report_two.read_bytes())


class NpmConsumerParityTest(unittest.TestCase):
    def test_npm_index_exposes_contract_api(self) -> None:
        source = NPM_INDEX.read_bytes()
        for marker in (
            b"export const CONTRACT_VERSION",
            b"export const HTTP_STATUS_BY_ERROR_CODE",
            b"export function validateDefinition",
            b"export function validateDocument",
            b"export function classifyErrorCode",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        for forbidden in (b"require(", b"module.exports", b"exports."):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        package = json.loads(NPM_PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual("./index.js", package["exports"])
        self.assertTrue(NPM_INDEX.is_file())

    def test_node_execution_parity(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for npm consumer parity")
        harness = """import fs from 'node:fs';
import * as c from '__INDEX_URL__';
const CD = '__CONTRACT__';
const fx = JSON.parse(fs.readFileSync(CD + '/fixtures/fixture-set.json', 'utf8'));
let fails = 0;
for (const f of fx.fixtures) {
  if (f.path.endsWith('.raw')) continue;
  const inst = JSON.parse(fs.readFileSync(CD + '/' + f.path, 'utf8'));
  const errs = c.validateDefinition(f.definition, inst);
  const cls = c.classifyErrorCode(errs);
  let ok;
  if (f.valid) ok = errs.length === 0;
  else ok = errs.length > 0 && cls === f.error_code;
  if (!ok) { fails++; console.log('PARITY FAIL ' + f.id + ' valid=' + f.valid + ' errs=' + errs.length + ' classify=' + cls); }
}
const expectedExports = __EXPECTED_EXPORTS__;
const actualExports = Object.keys(c).sort();
if (JSON.stringify(actualExports) !== JSON.stringify(expectedExports)) {
  fails++;
  console.log('EXPORT SURFACE FAIL actual=' + JSON.stringify(actualExports));
}
console.log('ALL_PARITY_PASS=' + (fails === 0));
process.exit(fails === 0 ? 0 : 1);
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            harness_path = Path(temp_dir) / "verify.mjs"
            harness_path.write_text(
                harness.replace("__INDEX_URL__", NPM_INDEX.as_uri())
                .replace("__CONTRACT__", _win_js_path(CONTRACT))
                .replace("__EXPECTED_EXPORTS__", json.dumps(sorted(EXPECTED_ESM_EXPORTS))),
                encoding="utf-8",
            )
            result = subprocess.run(
                [node, str(harness_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
            self.assertIn("ALL_PARITY_PASS=true", result.stdout)


class ManualEditDetectionTest(unittest.TestCase):
    def _run_check(self, generated: Path) -> int:
        return subprocess.run(
            [sys.executable, str(CHECK_PATH), "--generated", str(generated)],
            capture_output=True,
            text=True,
        ).returncode

    def test_pristine_committed_generation_passes(self) -> None:
        self.assertEqual(0, self._run_check(GENERATED))

    def test_every_missing_or_tampered_npm_output_fails(self) -> None:
        npm_paths = [Path(path) for path in OUTPUT_RELATIVE_PATHS]
        for relative_path in npm_paths:
            with self.subTest(relative_path=relative_path, mutation="missing"):
                with tempfile.TemporaryDirectory() as temp_dir:
                    mutated = Path(temp_dir) / "generated"
                    shutil.copytree(GENERATED, mutated)
                    (mutated / relative_path).unlink()
                    self.assertNotEqual(0, self._run_check(mutated))
            with self.subTest(relative_path=relative_path, mutation="tampered"):
                with tempfile.TemporaryDirectory() as temp_dir:
                    mutated = Path(temp_dir) / "generated"
                    shutil.copytree(GENERATED, mutated)
                    target = mutated / relative_path
                    target.write_bytes(target.read_bytes() + b"\n ")
                    self.assertNotEqual(0, self._run_check(mutated))

    def test_unexpected_npm_output_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mutated = Path(temp_dir) / "generated"
            shutil.copytree(GENERATED, mutated)
            (mutated / "reviewer-contract-npm" / "unexpected.txt").write_text(
                "not in the closed generated package\n", encoding="utf-8"
            )
            self.assertNotEqual(0, self._run_check(mutated))

    def test_protected_python_consumer_matches_frozen_identity(self) -> None:
        self.assertEqual(EXPECTED_PROTECTED_PYTHON_BYTES, PY_CONSUMER.stat().st_size)
        self.assertEqual(
            EXPECTED_PROTECTED_PYTHON_DIGEST,
            hashlib.sha256(PY_CONSUMER.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
