"""Deterministic issue fix preview and apply helpers."""

from __future__ import annotations

import difflib
import os
import re


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def preview_issue_fix(repo_path: str, issue: dict) -> dict:
    if not is_auto_fixable(issue):
        return invalid(issue, "Issue is not auto-fixable.")

    relative_path = safe_issue_file(issue.get("file"))
    if not relative_path:
        return invalid(issue, "Unsafe issue file path.")

    bad_lines = code_lines(issue.get("badCode"))
    good_lines = code_lines(issue.get("goodCode"))
    if not bad_lines or not good_lines:
        return invalid(issue, "Auto-fix requires non-empty badCode and goodCode.")

    target_path = safe_join(repo_path, relative_path)
    if not target_path:
        return invalid(issue, "Unsafe issue file path.")

    try:
        with open(target_path, encoding="utf-8", newline="") as handle:
            original = handle.read()
    except FileNotFoundError:
        return invalid(issue, "Issue file was not found.")

    preview = replacement_preview(original, bad_lines, good_lines)
    if not preview["ok"]:
        return invalid(issue, preview["message"])

    updated = preview["updatedContent"]
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )
    return {
        "valid": True,
        "issueId": safe_preview_text(issue.get("id")),
        "autoFixable": True,
        "repository": safe_preview_repository(issue),
        "branch": safe_preview_text(issue.get("branch")),
        "file": relative_path,
        "diff": diff,
        "summary": "1 file changed",
    }


def apply_issue_fix(repo_path: str, issue: dict) -> dict:
    preview = preview_issue_fix(repo_path, issue)
    if not preview.get("valid"):
        return preview

    target_path = safe_join(repo_path, preview["file"])
    if not target_path:
        return invalid(issue, "Unsafe issue file path.")

    try:
        with open(target_path, encoding="utf-8", newline="") as handle:
            original = handle.read()
    except FileNotFoundError:
        return invalid(issue, "Issue file was not found.")

    replacement = replacement_preview(
        original,
        code_lines(issue.get("badCode")),
        code_lines(issue.get("goodCode")),
    )
    if not replacement["ok"]:
        return invalid(issue, replacement["message"])

    with open(target_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(replacement["updatedContent"])
    return preview


def invalid(issue: dict, message: str) -> dict:
    return {
        "issueId": safe_preview_text(issue.get("id")) if isinstance(issue, dict) else "",
        "autoFixable": is_auto_fixable(issue),
        "valid": False,
        "message": message,
    }


def code_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    lines: list[str] = []
    for item in value:
        raw = item.get("code") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            return []
        split = raw.splitlines()
        lines.extend(split or [""])
    return lines


def safe_preview_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()


def safe_preview_repository(issue: dict) -> str:
    for key in ("repo", "repository"):
        value = safe_preview_text(issue.get(key))
        if value and _REPO_FULL_NAME_RE.match(value):
            return value
    return ""


def is_auto_fixable(issue: object) -> bool:
    if not isinstance(issue, dict):
        return False
    return issue.get("autoFix") is True or issue.get("autoFixable") is True


def replacement_preview(original: str, bad_lines: list[str], good_lines: list[str]) -> dict:
    if not bad_lines or not good_lines:
        return {"ok": False, "message": "Auto-fix requires non-empty badCode and goodCode."}

    records = [_split_line(line) for line in original.splitlines(keepends=True)]
    contents = [content for content, _ending in records]
    matches: list[tuple[int, list[str]]] = []
    width = len(bad_lines)

    for index in range(0, len(contents) - width + 1):
        candidate = contents[index:index + width]
        replacement = _replacement_lines(candidate, bad_lines, good_lines)
        if replacement is not None:
            matches.append((index, replacement))

    if not matches:
        return {"ok": False, "message": "Old block was not found."}
    if len(matches) > 1:
        return {"ok": False, "message": "Old block appears more than once."}

    index, replacement = matches[0]
    old_endings = [ending for _content, ending in records[index:index + width]]
    endings = _replacement_endings(old_endings, len(replacement), _default_newline(original))
    replacement_records = list(zip(replacement, endings, strict=True))
    updated_records = records[:index] + replacement_records + records[index + width:]
    updated = "".join(content + ending for content, ending in updated_records)
    return {"ok": True, "updatedContent": updated}


def safe_issue_file(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    raw = value.strip()
    normalized = raw.replace("\\", "/")
    if (
        not raw
        or "\x00" in raw
        or _WINDOWS_DRIVE_RE.match(raw)
        or os.path.isabs(raw)
        or normalized.startswith("/")
        or normalized.startswith("//")
        or raw.startswith("\\")
    ):
        return None

    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if any(part.casefold() == ".git" for part in parts):
        return None
    return "/".join(parts)


def safe_join(root: str, relative_path: str) -> str | None:
    if not isinstance(root, str) or not root.strip():
        return None

    safe_path = safe_issue_file(relative_path)
    if not safe_path:
        return None

    root_abs = os.path.realpath(os.path.abspath(root))
    candidate = os.path.realpath(os.path.abspath(os.path.join(root_abs, *safe_path.split("/"))))
    try:
        common = os.path.commonpath([root_abs, candidate])
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(root_abs):
        return None
    git_dir = os.path.realpath(os.path.abspath(os.path.join(root_abs, ".git")))
    if path_contains_or_is(git_dir, candidate):
        return None
    return candidate


def path_contains_or_is(parent: str, candidate: str) -> bool:
    try:
        common = os.path.commonpath([os.path.normcase(parent), os.path.normcase(candidate)])
    except ValueError:
        return False
    return common == os.path.normcase(parent)


def _replacement_lines(candidate: list[str], bad_lines: list[str], good_lines: list[str]) -> list[str] | None:
    candidate_indent = _common_outer_indent(candidate)
    normalized_candidate = _remove_outer_indent(candidate, candidate_indent)
    normalized_bad = _remove_outer_indent(bad_lines, _common_outer_indent(bad_lines))
    if normalized_candidate != normalized_bad:
        return None

    relative_good = _remove_outer_indent(good_lines, _common_outer_indent(good_lines))
    return _add_outer_indent(relative_good, candidate_indent)


def _common_outer_indent(lines: list[str]) -> str:
    indents = [_leading_whitespace(line) for line in lines if line.strip()]
    if not indents:
        return ""

    common = indents[0]
    for indent in indents[1:]:
        while common and not indent.startswith(common):
            common = common[:-1]
        if not common:
            break
    return common


def _remove_outer_indent(lines: list[str], indent: str) -> list[str]:
    if not indent:
        return [line if line.strip() else "" for line in lines]
    return [line[len(indent):] if line.strip() and line.startswith(indent) else "" for line in lines]


def _add_outer_indent(lines: list[str], indent: str) -> list[str]:
    return [indent + line if line.strip() else "" for line in lines]


def _leading_whitespace(value: str) -> str:
    return value[:len(value) - len(value.lstrip(" \t"))]


def _split_line(value: str) -> tuple[str, str]:
    if value.endswith("\r\n"):
        return value[:-2], "\r\n"
    if value.endswith("\n"):
        return value[:-1], "\n"
    if value.endswith("\r"):
        return value[:-1], "\r"
    return value, ""


def _replacement_endings(old_endings: list[str], replacement_count: int, default_newline: str) -> list[str]:
    if replacement_count == len(old_endings):
        return list(old_endings)
    if replacement_count == 0:
        return []
    return [default_newline] * (replacement_count - 1) + [old_endings[-1] if old_endings else default_newline]


def _default_newline(value: str) -> str:
    return "\r\n" if "\r\n" in value else "\n"
