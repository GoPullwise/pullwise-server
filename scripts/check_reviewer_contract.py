#!/usr/bin/env python
"""Verify the committed npm pullwise-review contract consumer is pristine.

Regenerates exactly the three npm outputs from the frozen manifest and compares
them byte-for-byte (after CRLF normalization, so the check is robust to
core.autocrlf) against the files committed under ``--generated``.  A missing or
manually edited npm file fails with exit 1; operational failures (unreadable
manifest, missing generator) exit 2; a fully pristine generation exits 0.  The
historical generated Python consumer is deliberately outside this target.

Usage:
    python scripts/check_reviewer_contract.py [--generated DIR] [--contract-dir DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = _REPO_ROOT / "scripts" / "generate_reviewer_contract.py"
DEFAULT_GENERATED = _REPO_ROOT / "generated"
DEFAULT_CONTRACT_DIR = _REPO_ROOT / "contracts" / "pullwise-review" / "v1"
NPM_OUTPUT_DIRECTORY = "reviewer-contract-npm"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_OPERATIONAL = 2


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_reviewer_contract", GENERATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load generator module {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def check(generated: Path, contract_dir: Path) -> list[str]:
    """Return a list of divergence problems; empty means the build is pristine."""
    gen = _load_generator()
    outputs = gen.build_outputs(contract_dir)
    problems: list[str] = []
    expected_paths = set(outputs)
    npm_root = Path(generated) / NPM_OUTPUT_DIRECTORY
    actual_paths = {
        path.relative_to(generated).as_posix()
        for path in npm_root.rglob("*")
        if path.is_file()
    } if npm_root.is_dir() else set()
    for relative_path in sorted(actual_paths - expected_paths):
        problems.append(f"unexpected generated npm file: {relative_path}")
    for relative_path, expected in sorted(outputs.items()):
        target = Path(generated) / relative_path
        if not target.is_file():
            problems.append(f"missing generated file: {relative_path}")
            continue
        if _normalize(target.read_bytes()) != _normalize(expected):
            problems.append(
                f"generated file diverges from the frozen manifest: {relative_path}"
            )
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_reviewer_contract.py",
        description="Verify the committed npm pullwise-review consumer matches a fresh generation.",
    )
    parser.add_argument(
        "--generated",
        type=Path,
        default=DEFAULT_GENERATED,
        help="directory holding reviewer-contract-npm/",
    )
    parser.add_argument(
        "--contract-dir",
        type=Path,
        default=DEFAULT_CONTRACT_DIR,
        help="frozen contract directory containing manifest.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        problems = check(args.generated, args.contract_dir)
    except Exception as error:  # noqa: BLE001 - exit code contract is the API
        print(f"error: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return EXIT_MISMATCH
    print("ok: generated npm consumer matches the frozen manifest")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
