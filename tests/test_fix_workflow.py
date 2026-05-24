from __future__ import annotations

import os
import tempfile
import unittest

from pullwise_server.fix_workflow import (
    apply_issue_fix,
    code_lines,
    invalid,
    preview_issue_fix,
    replacement_preview,
    safe_issue_file,
    safe_join,
)


VALID_PREVIEW_KEYS = {
    "issueId",
    "autoFixable",
    "valid",
    "repository",
    "branch",
    "file",
    "diff",
    "summary",
}
INVALID_PREVIEW_KEYS = {
    "issueId",
    "autoFixable",
    "valid",
    "message",
}
UNSAFE_PATHS = [
    "",
    "/tmp/secret",
    "src/./auth.py",
    "src/../secrets.env",
    "C:\\secrets.env",
    "C:secrets.env",
    ".git/config",
    ".GIT/hooks/post-checkout",
    "src/.git/config",
]


class FixWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        os.makedirs(os.path.join(self.tmpdir.name, "src"), exist_ok=True)
        self.auth_path = os.path.join(self.tmpdir.name, "src", "auth.py")
        self.write_auth(
            "def login(next_url):\n"
            "    return redirect(next_url)\n"
        )

    def write_auth(self, content: str) -> None:
        with open(self.auth_path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def read_auth(self) -> str:
        with open(self.auth_path, encoding="utf-8") as handle:
            return handle.read()

    def write_auth_bytes(self, content: bytes) -> None:
        with open(self.auth_path, "wb") as handle:
            handle.write(content)

    def read_auth_bytes(self) -> bytes:
        with open(self.auth_path, "rb") as handle:
            return handle.read()

    def issue(self, **overrides: object) -> dict:
        data = {
            "id": "f_123",
            "repo": "owner/repo",
            "branch": "main",
            "file": "src/auth.py",
            "autoFix": True,
            "badCode": ["return redirect(next_url)"],
            "goodCode": ["return redirect(safe_redirect(next_url))"],
        }
        data.update(overrides)
        return data

    def test_preview_rejects_non_auto_fixable_issue(self) -> None:
        result = preview_issue_fix(
            self.tmpdir.name,
            self.issue(autoFix=False, autoFixable=False),
        )

        self.assertFalse(result["valid"])
        self.assertEqual(set(result), INVALID_PREVIEW_KEYS)
        self.assertFalse(result["autoFixable"])
        self.assertEqual(result["issueId"], "f_123")
        self.assertIn("auto-fixable", result["message"])

    def test_preview_rejects_unsafe_paths(self) -> None:
        for file_path in UNSAFE_PATHS:
            with self.subTest(file_path=file_path):
                result = preview_issue_fix(
                    self.tmpdir.name,
                    self.issue(file=file_path),
                )

                self.assertFalse(result["valid"])
                self.assertEqual(set(result), INVALID_PREVIEW_KEYS)
                self.assertTrue(result["autoFixable"])
                self.assertIn("Unsafe", result["message"])

    def test_preview_rejects_missing_old_block(self) -> None:
        self.write_auth(
            "def login(next_url):\n"
            "    return redirect('/dashboard')\n"
        )

        result = preview_issue_fix(self.tmpdir.name, self.issue())

        self.assertFalse(result["valid"])
        self.assertEqual(set(result), INVALID_PREVIEW_KEYS)
        self.assertTrue(result["autoFixable"])
        self.assertIn("not found", result["message"])

    def test_preview_rejects_ambiguous_old_block(self) -> None:
        self.write_auth(
            "def login(next_url):\n"
            "    return redirect(next_url)\n"
            "\n"
            "def continue_login(next_url):\n"
            "    return redirect(next_url)\n"
        )

        result = preview_issue_fix(self.tmpdir.name, self.issue())

        self.assertFalse(result["valid"])
        self.assertEqual(set(result), INVALID_PREVIEW_KEYS)
        self.assertTrue(result["autoFixable"])
        self.assertIn("more than once", result["message"])

    def test_preview_returns_unified_diff_for_valid_fix(self) -> None:
        result = preview_issue_fix(self.tmpdir.name, self.issue())

        self.assertTrue(result["valid"])
        self.assertEqual(set(result), VALID_PREVIEW_KEYS)
        self.assertTrue(result["autoFixable"])
        self.assertEqual(result["issueId"], "f_123")
        self.assertEqual(result["repository"], "owner/repo")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["file"], "src/auth.py")
        self.assertEqual(result["summary"], "1 file changed")
        self.assertIn("--- a/src/auth.py", result["diff"])
        self.assertIn("+++ b/src/auth.py", result["diff"])
        self.assertIn("-    return redirect(next_url)", result["diff"])
        self.assertIn("+    return redirect(safe_redirect(next_url))", result["diff"])

    def test_preview_uses_repository_when_repo_is_missing(self) -> None:
        issue = self.issue(repository="fallback/repo")
        issue.pop("repo")

        result = preview_issue_fix(self.tmpdir.name, issue)

        self.assertTrue(result["valid"])
        self.assertEqual(set(result), VALID_PREVIEW_KEYS)
        self.assertEqual(result["repository"], "fallback/repo")

    def test_apply_writes_valid_fix_and_returns_preview_metadata(self) -> None:
        result = apply_issue_fix(self.tmpdir.name, self.issue())

        self.assertTrue(result["valid"])
        self.assertEqual(set(result), VALID_PREVIEW_KEYS)
        self.assertTrue(result["autoFixable"])
        self.assertEqual(result["file"], "src/auth.py")
        self.assertEqual(
            self.read_auth(),
            "def login(next_url):\n"
            "    return redirect(safe_redirect(next_url))\n",
        )
        self.assertIn("--- a/src/auth.py", result["diff"])

    def test_apply_preserves_nested_relative_indentation(self) -> None:
        self.write_auth(
            "def handler(next_url):\n"
            "    if next_url:\n"
            "        return redirect(next_url)\n"
            "    return redirect('/home')\n"
        )

        result = apply_issue_fix(
            self.tmpdir.name,
            self.issue(
                badCode=[
                    "if next_url:",
                    "    return redirect(next_url)",
                ],
                goodCode=[
                    "if safe_redirect(next_url):",
                    "    return redirect(safe_redirect(next_url))",
                ],
            ),
        )

        self.assertTrue(result["valid"])
        self.assertEqual(
            self.read_auth(),
            "def handler(next_url):\n"
            "    if safe_redirect(next_url):\n"
            "        return redirect(safe_redirect(next_url))\n"
            "    return redirect('/home')\n",
        )

    def test_apply_preserves_existing_line_endings(self) -> None:
        self.write_auth_bytes(
            b"def login(next_url):\r\n"
            b"    return redirect(next_url)\r\n"
            b"# keep lf\n"
        )

        result = apply_issue_fix(self.tmpdir.name, self.issue())

        self.assertTrue(result["valid"])
        self.assertEqual(
            self.read_auth_bytes(),
            b"def login(next_url):\r\n"
            b"    return redirect(safe_redirect(next_url))\r\n"
            b"# keep lf\n",
        )

    def test_invalid_returns_minimal_invalid_payload(self) -> None:
        result = invalid({"id": "f_123", "autoFix": True}, "Not safe.")

        self.assertEqual(set(result), INVALID_PREVIEW_KEYS)
        self.assertEqual(
            result,
            {
                "issueId": "f_123",
                "autoFixable": True,
                "valid": False,
                "message": "Not safe.",
            },
        )

    def test_code_lines_accepts_code_dicts_and_raw_strings(self) -> None:
        self.assertEqual(
            code_lines([
                {"ln": 10, "code": "first"},
                "second\nthird",
            ]),
            ["first", "second", "third"],
        )

    def test_code_lines_returns_empty_when_any_code_is_not_string(self) -> None:
        self.assertEqual(code_lines([{"code": "ok"}, {"code": 123}]), [])

    def test_replacement_preview_rejects_multiple_matches(self) -> None:
        result = replacement_preview("return value\nreturn value\n", ["return value"], ["return safe_value"])

        self.assertFalse(result["ok"])
        self.assertIn("more than once", result["message"])

    def test_replacement_preview_returns_updated_content_for_valid_replacement(self) -> None:
        result = replacement_preview(
            "def login(next_url):\n"
            "    return redirect(next_url)\n",
            ["return redirect(next_url)"],
            ["return redirect(safe_redirect(next_url))"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["updatedContent"],
            "def login(next_url):\n"
            "    return redirect(safe_redirect(next_url))\n",
        )

    def test_safe_issue_file_accepts_normal_repo_relative_path(self) -> None:
        self.assertEqual(safe_issue_file("src/auth.py"), "src/auth.py")

    def test_safe_issue_file_rejects_unsafe_path_variants(self) -> None:
        for file_path in UNSAFE_PATHS:
            with self.subTest(file_path=file_path):
                self.assertIsNone(safe_issue_file(file_path))

    def test_safe_join_returns_path_under_root_for_valid_input(self) -> None:
        result = safe_join(self.tmpdir.name, "src/auth.py")

        self.assertIsNotNone(result)
        self.assertEqual(
            os.path.normcase(os.path.commonpath([os.path.abspath(self.tmpdir.name), result or ""])),
            os.path.normcase(os.path.abspath(self.tmpdir.name)),
        )
        self.assertTrue((result or "").endswith(os.path.join("src", "auth.py")))

    def test_safe_join_rejects_unsafe_or_escaping_input(self) -> None:
        for file_path in [*UNSAFE_PATHS, "../secrets.env"]:
            with self.subTest(file_path=file_path):
                self.assertIsNone(safe_join(self.tmpdir.name, file_path))


if __name__ == "__main__":
    unittest.main()
