from __future__ import annotations

import os
import tempfile
import unittest

from pullwise_server.fix_workflow import apply_issue_fix, preview_issue_fix


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
        for file_path in ["../secrets.env", "C:secrets.env"]:
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


if __name__ == "__main__":
    unittest.main()
