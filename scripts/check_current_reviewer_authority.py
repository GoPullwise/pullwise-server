#!/usr/bin/env python3
"""Fail closed unless this repository routes Reviewer work to current authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys


REPOSITORY = "server"
ROOT = Path(__file__).resolve().parents[1]
# Current checked-in target; no external authority registry is required.
TARGET_START_MARKER = "<!-- PULLWISE_REVIEWER_TARGET_START -->"
TARGET_END_MARKER = "<!-- PULLWISE_REVIEWER_TARGET_END -->"
TARGET_BLOCK_SHA256 = "4d6c70c6e661eadb241140bb56f111866088c3a87c55ec244c8e957bc08bcc13"


def result(status: str, errors: list[str], sha256: str | None = None) -> dict:
    return {
        "schema_id": "pullwise-current-reviewer-ci-authority-report/v1",
        "repository": REPOSITORY,
        "status": status,
        "path": "AGENTS.md",
        "errors": sorted(set(errors)),
        "sha256": sha256,
    }


def validate() -> dict:
    path = ROOT / "AGENTS.md"
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return result("INDETERMINATE", ["agents_file_not_regular"])
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return result("INDETERMINATE", ["agents_file_not_utf8"])
    except OSError:
        return result("INDETERMINATE", ["agents_file_unreadable"])

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    errors: list[str] = []
    if not normalized.startswith(TARGET_START_MARKER + "\n"):
        errors.append("target_block_not_first")
    if normalized.count(TARGET_START_MARKER) != 1 or normalized.count(TARGET_END_MARKER) != 1:
        errors.append("target_block_count_mismatch")
    else:
        start = normalized.index(TARGET_START_MARKER)
        stop = normalized.index(TARGET_END_MARKER) + len(TARGET_END_MARKER)
        if normalized[stop:stop + 1] != "\n":
            errors.append("target_block_missing_trailing_lf")
        actual = hashlib.sha256(normalized[start:stop + 1].encode("utf-8")).hexdigest()
        if actual != TARGET_BLOCK_SHA256:
            errors.append("target_block_mismatch")
    return result(
        "FAIL" if errors else "PASS",
        errors,
        hashlib.sha256(raw).hexdigest(),
    )


def main() -> int:
    report = validate()
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
