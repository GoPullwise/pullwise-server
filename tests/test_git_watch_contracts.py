from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def shell_executable() -> str:
    shell = shutil.which("sh")
    if shell:
        return shell
    bash = shutil.which("bash")
    if bash:
        return bash
    raise unittest.SkipTest("No POSIX shell is available for git-watch tests.")


def git_executable() -> str:
    git = shutil.which("git")
    if git:
        return git
    raise unittest.SkipTest("git is required for git-watch tests.")


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git_executable(), *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@unittest.skipIf(os.name == "nt", "git-watch shell contracts run on POSIX CI")
class GitWatchContractsTest(unittest.TestCase):
    def create_remote_and_clone(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        run_git(["init"], source)
        run_git(["checkout", "-b", "main"], source)
        run_git(["config", "user.email", "test@example.com"], source)
        run_git(["config", "user.name", "Test User"], source)
        (source / "README.md").write_text("initial\n", encoding="utf-8")
        run_git(["add", "README.md"], source)
        run_git(["commit", "-m", "initial"], source)

        remote = root / "origin.git"
        run_git(["clone", "--bare", str(source), str(remote)], root)
        app = root / "app"
        run_git(["clone", str(remote), str(app)], root)
        run_git(["checkout", "main"], app)
        return app

    def run_watcher(self, app: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env.update(
            {
                "PULLWISE_WATCH_APP_DIR": str(app),
                "PULLWISE_WATCH_LOG_FILE": str(app / ".pullwise" / "git-watch.log"),
                "PULLWISE_WATCH_LOCK_DIR": str(app / ".pullwise" / "git-watch.lock"),
                "PULLWISE_WATCH_RUN_SETUP": "false",
                "PULLWISE_WATCH_RUN_TESTS": "false",
                "PULLWISE_WATCH_RUN_HEALTH": "false",
            }
        )
        merged_env.update(env)
        return subprocess.run(
            [shell_executable(), str(project_root() / "git-watch.sh"), "--once"],
            cwd=app,
            env=merged_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_retries_deploy_when_head_is_current_but_not_marked_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = self.create_remote_and_clone(root)
            deploy_log = app / ".pullwise" / "deploy.log"

            failed = self.run_watcher(
                app,
                {
                    "PULLWISE_WATCH_RUN_TESTS": "true",
                    "PULLWISE_WATCH_TEST_COMMAND": "exit 23",
                },
            )

            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertFalse((app / ".pullwise" / "git-watch.deployed-head").exists())

            succeeded = self.run_watcher(
                app,
                {
                    "PULLWISE_WATCH_RESTART_COMMAND": f"printf restart >> {deploy_log}",
                },
            )

            self.assertEqual(succeeded.returncode, 0, succeeded.stdout + succeeded.stderr)
            self.assertEqual(deploy_log.read_text(encoding="utf-8"), "restart")
            deployed_head = (app / ".pullwise" / "git-watch.deployed-head").read_text(encoding="utf-8").strip()
            self.assertEqual(deployed_head, run_git(["rev-parse", "HEAD"], app).stdout.strip())
