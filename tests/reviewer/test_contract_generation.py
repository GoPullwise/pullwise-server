"""Generator, consumer, and build-integrity tests for the R1-03 write set.

Exercises scripts/generate_reviewer_contract.py and the four generated consumer
artifacts under generated/: byte-determinism across runs, source isolation to
the frozen manifest, committed-artifact fidelity, closed-registry and validator
parity with both generated consumers, and manual-edit detection by
scripts/check_reviewer_contract.py.  Generated files may be CRLF in the working
tree (core.autocrlf), so every byte comparison normalizes CRLF to LF.
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

from pullwise_server.reviewer.canonical import decode_strict_json


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
SCHEMA_RELATIVE = "shared/schemas/pullwise-review.schema.json"
OUTPUT_RELATIVE_PATHS = (
    "reviewer-contract-python/reviewer_contract.py",
    "reviewer-contract-npm/package.json",
    "reviewer-contract-npm/index.js",
    "reviewer-contract-npm/schema.json",
)

EXPECTED_FIXTURE_CODES = {
    "VALID-MINIMAL-SCAN": ("CreateScanRequest", True),
    "VALID-FULL-CANDIDATE": ("ResultCandidate", True),
    "VALID-ISSUE-STATUS": ("UpdateIssueStatusCommand", True),
    "CONTRACT-UNKNOWN-ENUM": ("CreateScanRequest", False),
    "CONTRACT-EXTRA-TENANT": ("ResultCandidate", False),
    "CONTRACT-PATH-TRAVERSAL": ("Finding", False),
    "CONTRACT-FLOAT": ("ResultCandidate", False),
    "CONTRACT-DUPLICATE-KEY": ("UpdateIssueStatusCommand", False),
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
        self.assertEqual("pullwise-review-contract", package["name"])
        self.assertEqual("index.js", package["main"])
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


class PythonConsumerParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.consumer = _load_module(PY_CONSUMER, "reviewer_contract")
        cls.registry = json.loads((CONTRACT / "registry.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))
        cls.fixture_set = json.loads((CONTRACT / "fixtures/fixture-set.json").read_text(encoding="utf-8"))

    def test_registry_parity(self) -> None:
        self.assertEqual(self.registry["registries"], self.consumer.REGISTRIES)
        self.assertEqual(
            self.registry["http_status_by_error_code"],
            self.consumer.HTTP_STATUS_BY_ERROR_CODE,
        )

    def test_manifest_binding(self) -> None:
        self.assertEqual("pullwise-review/v1", self.consumer.CONTRACT_VERSION)
        self.assertEqual(
            self.manifest["canonicalization"], self.consumer.CANONICALIZATION
        )
        self.assertEqual(EXPECTED_MANIFEST_DIGEST, self.consumer.MANIFEST_DIGEST)
        self.assertEqual(self.manifest["files"], self.consumer.FILES)

    def test_embedded_schema_is_the_frozen_shared_schema(self) -> None:
        frozen = json.loads((CONTRACT / SCHEMA_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(frozen, self.consumer.SCHEMA)

    def test_valid_fixtures_validate(self) -> None:
        for fixture in self.fixture_set["fixtures"]:
            if not fixture["valid"]:
                continue
            with self.subTest(fixture=fixture["id"]):
                instance = decode_strict_json((CONTRACT / fixture["path"]).read_bytes())
                violations = self.consumer.validate_definition(fixture["definition"], instance)
                self.assertEqual([], violations)

    def test_invalid_fixtures_classify_to_catalogued_code(self) -> None:
        for fixture in self.fixture_set["fixtures"]:
            if fixture["valid"]:
                continue
            with self.subTest(fixture=fixture["id"]):
                expected_code = fixture["error_code"]
                raw = (CONTRACT / fixture["path"]).read_bytes()
                try:
                    instance = decode_strict_json(raw)
                except ValueError:
                    self.assertEqual("REQUEST_INVALID", expected_code)
                    continue
                violations = self.consumer.validate_definition(fixture["definition"], instance)
                self.assertTrue(violations, "invalid fixture must produce violations")
                self.assertEqual(expected_code, self.consumer.classify_error_code(violations))

    def test_fixture_catalogue_codes_match_frozen_registry(self) -> None:
        status_map = self.registry["http_status_by_error_code"]
        for fixture in self.fixture_set["fixtures"]:
            if fixture["valid"]:
                continue
            with self.subTest(fixture=fixture["id"]):
                self.assertIn(fixture["error_code"], status_map)
                self.assertEqual(
                    status_map[fixture["error_code"]],
                    {"REQUEST_INVALID": 400, "EVIDENCE_INVALID": 422}[fixture["error_code"]],
                )


class NpmConsumerParityTest(unittest.TestCase):
    def test_npm_index_exposes_contract_api(self) -> None:
        source = NPM_INDEX.read_bytes()
        for marker in (
            b"module.exports",
            b"validateDefinition",
            b"validateDocument",
            b"classifyErrorCode",
            b"HTTP_STATUS_BY_ERROR_CODE",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        package = json.loads(NPM_PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual("index.js", package["main"])
        self.assertTrue((GENERATED / "reviewer-contract-npm" / package["main"]).is_file())

    def test_node_execution_parity(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node not available in this environment")
        harness = """'use strict';
const fs = require('fs');
const c = require('__INDEX__');
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
console.log('ALL_PARITY_PASS=' + (fails === 0));
process.exit(fails === 0 ? 0 : 1);
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            harness_path = Path(temp_dir) / "verify.js"
            harness_path.write_text(
                harness.replace("__INDEX__", _win_js_path(NPM_INDEX))
                .replace("__CONTRACT__", _win_js_path(CONTRACT)),
                encoding="utf-8",
            )
            result = subprocess.run(
                [shutil.which("node"), str(harness_path)],
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

    def test_manual_edit_to_generated_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mutated = Path(temp_dir) / "generated"
            shutil.copytree(GENERATED, mutated)
            target = mutated / "reviewer-contract-python" / "reviewer_contract.py"
            original = target.read_bytes()
            target.write_bytes(original + b"\n# tampered\n")
            self.assertNotEqual(0, self._run_check(mutated))
            target.write_bytes(original)
            self.assertEqual(0, self._run_check(mutated))


if __name__ == "__main__":
    unittest.main()
