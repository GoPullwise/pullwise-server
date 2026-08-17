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
START_MARKER = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_START -->"
END_MARKER = "<!-- PULLWISE_REVIEWER_CURRENT_AUTHORITY_END -->"
REQUIRED_URLS = (
    "https://app.notion.com/p/3b4e5c88f85f8128bd39dac3a7679c4a",
    "https://app.notion.com/p/3b4e5c88f85f818e933ecf3864c97469",
    "https://app.notion.com/p/b79ceacfedcd4d34a0d619c1790066c4",
    "https://app.notion.com/p/760a1698a86b404083662eeb1b637f64",
    "https://app.notion.com/p/3b5e5c88f85f81bc840ace8b8a65962e",
    "https://app.notion.com/p/3b5e5c88f85f81aeaeaef4621d211126",
    "https://app.notion.com/p/3b5e5c88f85f81d89deef714c8b23eeb",
    "https://app.notion.com/p/3b8e5c88f85f814d8296c6c60541946d",
    "https://app.notion.com/p/3b4e5c88f85f8192a488f6db72fa116b",
)
CURRENT_ROUTING_SHA256 = "9928a40c0cd22d15e3d8c9278b2ef15bcc83de6a015f55a68452c76f8a82e5c4"


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
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return result("INDETERMINATE", ["agents_file_not_utf8"])
    except OSError:
        return result("INDETERMINATE", ["agents_file_unreadable"])

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    errors: list[str] = []
    if not normalized.startswith(START_MARKER + "\n"):
        errors.append("missing_current_authority_block")
    end = normalized.find(END_MARKER)
    if end < 0:
        errors.append("unterminated_current_authority_block")
    else:
        block = normalized[: end + len(END_MARKER)]
        if any(url not in block for url in REQUIRED_URLS):
            errors.append("required_reference_missing")
        if (
            hashlib.sha256(block.encode("utf-8")).hexdigest()
            != CURRENT_ROUTING_SHA256
        ):
            errors.append("contradictory_block")
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
