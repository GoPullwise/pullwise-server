from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def shell_executable() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    raise unittest.SkipTest("bash is required for git-watch tests.")


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


def write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipIf(os.name == "nt", "git-watch shell contracts run on POSIX CI")
class GitWatchContractsTest(unittest.TestCase):
    def symlink_tool(self, root: Path, name: str) -> None:
        target = shutil.which(name)
        if not target:
            raise unittest.SkipTest(f"{name} is required for this shell contract")
        link = root / "bin" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(target)

    def write_fake_apt_get(self, root: Path) -> tuple[Path, Path]:
        fake_apt = root / "bin" / "apt-get"
        fake_apt.parent.mkdir(parents=True, exist_ok=True)
        apt_log = root / "apt.log"
        write_executable(
            fake_apt,
            """
            #!/usr/bin/env sh
            printf '%s\\n' "$*" >> "$PULLWISE_WATCH_APT_LOG"
            if [ "$1" = "install" ]; then
              cat > "$PULLWISE_FAKE_BIN/git" <<'GIT'
#!/usr/bin/env sh
exit 1
GIT
              chmod +x "$PULLWISE_FAKE_BIN/git"
            fi
            exit 0
            """,
        )
        return fake_apt, apt_log

    def create_remote_and_clone(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        run_git(["init"], source)
        run_git(["checkout", "-b", "main"], source)
        run_git(["config", "user.email", "test@example.com"], source)
        run_git(["config", "user.name", "Test User"], source)
        (source / ".gitignore").write_text(".pullwise/\n", encoding="utf-8")
        (source / "README.md").write_text("initial\n", encoding="utf-8")
        run_git(["add", ".gitignore", "README.md"], source)
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

    def test_installs_missing_git_on_ubuntu_2204_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            for tool in ["date", "dirname", "id", "mkdir", "sed", "tee", "tr", "sh", "chmod", "cat"]:
                self.symlink_tool(root, tool)
            fake_apt, apt_log = self.write_fake_apt_get(root)
            os_release = root / "os-release"
            os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(root / "bin"),
                    "PULLWISE_WATCH_APP_DIR": str(app),
                    "PULLWISE_WATCH_LOG_FILE": str(app / ".pullwise" / "git-watch.log"),
                    "PULLWISE_WATCH_LOCK_DIR": str(app / ".pullwise" / "git-watch.lock"),
                    "PULLWISE_WATCH_APT_GET_BIN": str(fake_apt),
                    "PULLWISE_WATCH_APT_LOG": str(apt_log),
                    "PULLWISE_FAKE_BIN": str(root / "bin"),
                    "PULLWISE_WATCH_OS_RELEASE_FILE": str(os_release),
                }
            )

            result = subprocess.run(
                [shell_executable(), str(project_root() / "git-watch.sh"), "--once"],
                cwd=app,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            apt_calls = apt_log.read_text(encoding="utf-8")

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("update", apt_calls)
        self.assertIn("install -y --no-install-recommends git", apt_calls)
