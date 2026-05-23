from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
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
    raise unittest.SkipTest("No POSIX shell is available for launcher tests.")


def shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    shell = shell_executable()
    converted = subprocess.run(
        [shell, "-lc", 'cygpath -u "$1"', "_", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return converted or str(path)


def write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def tar_contents(path: Path) -> list[str]:
    return subprocess.run(
        ["tar", "-tzf", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


class LauncherContractsTest(unittest.TestCase):
    def run_launcher(self, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [shell_executable(), shell_path(project_root() / "launcher.sh"), *args],
            cwd=project_root(),
            env=merged_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def write_fake_runtime(self, root: Path) -> tuple[Path, Path, Path]:
        venv_python = root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        write_executable(
            venv_python,
            """
            #!/usr/bin/env sh
            if [ "$1" = "--version" ]; then
              echo "Python 3.10.12"
              exit 0
            fi
            echo "fake python invoked: $*" >&2
            exit 0
            """,
        )

        fake_git = root / "bin" / "git"
        fake_codex = root / "bin" / "codex"
        fake_journalctl = root / "bin" / "journalctl"
        fake_git.parent.mkdir(parents=True)
        write_executable(
            fake_git,
            """
            #!/usr/bin/env sh
            echo "git version 2.43.0"
            """,
        )
        write_executable(
            fake_codex,
            """
            #!/usr/bin/env sh
            echo "codex 1.0.0"
            """,
        )
        write_executable(
            fake_journalctl,
            """
            #!/usr/bin/env sh
            echo "journalctl args: $*"
            """,
        )
        return venv_python, fake_git, fake_codex

    def write_fake_chgrp(self, root: Path) -> tuple[Path, Path]:
        fake_chgrp = root / "bin" / "chgrp"
        fake_chgrp.parent.mkdir(parents=True, exist_ok=True)
        log_file = root / "chgrp.log"
        write_executable(
            fake_chgrp,
            """
            #!/usr/bin/env sh
            printf '%s\\n' "$*" >> "$PULLWISE_CHGRP_LOG"
            """,
        )
        return fake_chgrp, log_file

    def write_production_env(self, root: Path, *, allowed_origins: str = "https://app.example.com") -> Path:
        env_file = root / "server.env"
        env_file.write_text(
            textwrap.dedent(
                f"""
                PULLWISE_MODE=production
                PULLWISE_HOST=0.0.0.0
                PULLWISE_PORT=3010
                PULLWISE_APP_URL=https://app.example.com
                PULLWISE_ALLOWED_ORIGINS={allowed_origins}
                PULLWISE_API_BASE_URL=https://app.example.com/api
                PULLWISE_DB_PATH={shell_path(root / "data" / "pullwise.sqlite3")}
                PULLWISE_LOG_DIR={shell_path(root / "logs")}
                PULLWISE_CHECKOUT_ROOT={shell_path(root / "checkouts")}
                PULLWISE_COOKIE_SECURE=true
                PULLWISE_GITHUB_CLIENT_ID=client_id
                PULLWISE_GITHUB_CLIENT_SECRET=client_secret
                PULLWISE_GITHUB_APP_SLUG=pullwise
                PULLWISE_GITHUB_APP_ID=123
                PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64=cHJpdmF0ZS1rZXk=
                PULLWISE_GITHUB_OAUTH_SCOPE=read:user user:email
                PULLWISE_REVIEW_PROVIDER=codex
                PULLWISE_MAX_CONCURRENT_SCANS=1
                PULLWISE_MAX_CONCURRENT_SCANS_PER_USER=1
                """
            ).strip()
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return env_file

    def write_relative_migration_env(self, root: Path) -> Path:
        env_file = root / "server.env"
        env_file.write_text(
            textwrap.dedent(
                """
                PULLWISE_MODE=production
                PULLWISE_HOST=0.0.0.0
                PULLWISE_PORT=3010
                PULLWISE_APP_URL=https://app.example.com
                PULLWISE_ALLOWED_ORIGINS=https://app.example.com
                PULLWISE_API_BASE_URL=https://app.example.com/api
                PULLWISE_DB_PATH=data/pullwise.sqlite3
                PULLWISE_LOG_DIR=logs
                PULLWISE_CHECKOUT_ROOT=checkouts
                PULLWISE_COOKIE_SECURE=true
                PULLWISE_GITHUB_CLIENT_ID=client_id
                PULLWISE_GITHUB_CLIENT_SECRET=client_secret
                PULLWISE_GITHUB_APP_SLUG=pullwise
                PULLWISE_GITHUB_APP_ID=123
                PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=secrets/github-app-private-key.pem
                PULLWISE_REVIEW_PROVIDER=codex
                PULLWISE_MAX_CONCURRENT_SCANS=1
                PULLWISE_MAX_CONCURRENT_SCANS_PER_USER=1
                """
            ).strip()
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return env_file

    def base_launcher_env(self, root: Path) -> dict[str, str]:
        venv_python, fake_git, fake_codex = self.write_fake_runtime(root)
        os_release = root / "os-release"
        os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
        return {
            "PULLWISE_SYSTEM_ENV_FILE": shell_path(self.write_production_env(root)),
            "PULLWISE_LOCAL_ENV_FILE": shell_path(root / ".env.local"),
            "PULLWISE_SYSTEMD_DIR": shell_path(root / "systemd"),
            "PULLWISE_VENV_DIR": shell_path(venv_python.parents[1]),
            "PULLWISE_GIT_BIN": shell_path(fake_git),
            "PULLWISE_CODEX_BIN": shell_path(fake_codex),
            "PULLWISE_JOURNALCTL_BIN": shell_path(root / "bin" / "journalctl"),
            "PULLWISE_OS_RELEASE_FILE": shell_path(os_release),
            "PULLWISE_LAUNCHER_TESTING": "1",
        }

    def write_minimal_service(self, root: Path, env_file: str) -> Path:
        service_dir = root / "systemd"
        service_dir.mkdir(exist_ok=True)
        service_file = service_dir / "pullwise-server.service"
        service_file.write_text(
            f"[Service]\nEnvironmentFile={env_file}\nExecStart=/venv/bin/python -m pullwise_server\n",
            encoding="utf-8",
            newline="\n",
        )
        return service_file

    def test_help_lists_management_commands(self) -> None:
        result = self.run_launcher(["help"])

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        for command in [
            "setup",
            "sync-env",
            "install-service",
            "render-service",
            "start",
            "stop",
            "restart",
            "status",
            "health",
            "logs",
            "doctor",
            "audit",
            "config",
        ]:
            self.assertIn(command, result.stdout)

    def test_doctor_accepts_complete_ubuntu_production_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("doctor checks passed", result.stdout)
        self.assertIn("Ubuntu 22.04", result.stdout)

    def test_doctor_rejects_missing_systemd_service_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)

            result = self.run_launcher(["doctor"], env)

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("systemd service is not installed", result.stderr + result.stdout)

    def test_doctor_rejects_wildcard_origin_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(self.write_production_env(root, allowed_origins="*"))

            result = self.run_launcher(["doctor"], env)

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("PULLWISE_ALLOWED_ORIGINS", result.stderr + result.stdout)
        self.assertIn("must not contain wildcard", result.stderr + result.stdout)

    def test_config_loads_env_values_with_spaces_without_sourcing_as_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self.base_launcher_env(Path(tmp))

            result = self.run_launcher(["config"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("PULLWISE_GITHUB_OAUTH_SCOPE=read:user user:email", result.stdout)
        self.assertIn("PULLWISE_GITHUB_CLIENT_SECRET=<set>", result.stdout)
        self.assertNotIn("client_secret", result.stdout)

    def test_sync_env_copies_project_local_env_to_system_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            local_env = root / ".env.local"
            system_env = root / "etc" / "pullwise" / "server.env"
            local_env.write_text(
                "PULLWISE_MODE=production\nPULLWISE_GITHUB_OAUTH_SCOPE=read:user user:email\n",
                encoding="utf-8",
                newline="\n",
            )
            env["PULLWISE_LOCAL_ENV_FILE"] = shell_path(local_env)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(system_env)

            result = self.run_launcher(["sync-env"], env)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(system_env.exists())
            self.assertEqual(local_env.read_text(encoding="utf-8"), system_env.read_text(encoding="utf-8"))
            self.assertIn("server.env", result.stdout)

    def test_sync_env_sets_system_env_group_for_service_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            fake_chgrp, chgrp_log = self.write_fake_chgrp(root)
            local_env = root / ".env.local"
            system_env = root / "etc" / "pullwise" / "server.env"
            local_env.write_text("PULLWISE_MODE=production\n", encoding="utf-8", newline="\n")
            env["PULLWISE_LOCAL_ENV_FILE"] = shell_path(local_env)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(system_env)
            env["PULLWISE_CHGRP_BIN"] = shell_path(fake_chgrp)
            env["PULLWISE_CHGRP_LOG"] = shell_path(chgrp_log)
            env["PULLWISE_SERVICE_GROUP"] = "pullwise"

            result = self.run_launcher(["sync-env"], env)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertIn(f"pullwise {shell_path(system_env)}", chgrp_log.read_text(encoding="utf-8"))

    def test_render_service_uses_system_environment_file_and_python_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_SERVICE_USER"] = "pullwise"
            env["PULLWISE_SERVICE_GROUP"] = "pullwise"

            result = self.run_launcher(["render-service"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn(f"EnvironmentFile={env['PULLWISE_SYSTEM_ENV_FILE']}", result.stdout)
        self.assertIn("ExecStart=", result.stdout)
        self.assertIn("-m pullwise_server", result.stdout)
        self.assertNotIn("--host", result.stdout)
        self.assertNotIn("--port", result.stdout)
        self.assertIn("Restart=always", result.stdout)
        self.assertIn("User=pullwise", result.stdout)

    def test_install_service_dry_run_shows_sync_and_systemctl_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            local_env = root / ".env.local"
            local_env.write_text("PULLWISE_MODE=production\n", encoding="utf-8", newline="\n")
            env["PULLWISE_LOCAL_ENV_FILE"] = shell_path(local_env)

            result = self.run_launcher(["install-service", "--dry-run"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("sync-env", result.stdout)
        self.assertIn("systemctl daemon-reload", result.stdout)
        self.assertIn("systemctl enable pullwise-server", result.stdout)

    def test_start_dry_run_prints_systemd_command_when_service_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            (systemd_dir / "pullwise-server.service").write_text("[Service]\n", encoding="utf-8")

            result = self.run_launcher(["start", "--dry-run"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("systemctl start pullwise-server", result.stdout)

    def test_start_dry_run_can_still_print_direct_server_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self.base_launcher_env(Path(tmp))
            env["PULLWISE_MANAGER"] = "direct"

            result = self.run_launcher(["start", "--dry-run"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("-m pullwise_server --host 0.0.0.0 --port 3010", result.stdout)
        self.assertIn("dry-run", result.stdout)

    def test_logs_journal_tails_systemd_journal_for_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["logs", "journal", "--follow"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("journalctl args:", result.stdout)
        self.assertIn("-u pullwise-server", result.stdout)
        self.assertIn("-n 120", result.stdout)
        self.assertIn("-f", result.stdout)

    def test_export_packages_env_state_logs_checkouts_and_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_APP_DIR"] = shell_path(root)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(self.write_relative_migration_env(root))
            (root / "data").mkdir()
            (root / "data" / "pullwise.sqlite3").write_text("sqlite", encoding="utf-8")
            (root / "logs").mkdir()
            (root / "logs" / "pullwise-2026-05-23.log").write_text("log", encoding="utf-8")
            (root / "checkouts" / "usr" / "scan" / "repo").mkdir(parents=True)
            (root / "checkouts" / "usr" / "scan" / "repo" / "README.md").write_text("repo", encoding="utf-8")
            (root / "secrets").mkdir()
            (root / "secrets" / "github-app-private-key.pem").write_text("pem", encoding="utf-8")
            (root / ".pullwise" / "extra").mkdir(parents=True)
            (root / ".pullwise" / "extra" / "artifact.txt").write_text("artifact", encoding="utf-8")
            archive = root / "pullwise-export.tar.gz"

            result = self.run_launcher(["export", shell_path(archive)], env)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(archive.exists())
            contents = tar_contents(archive)
            for expected in [
                "manifest.env",
                "config/server.env",
                "data/pullwise.sqlite3",
                "logs/pullwise-2026-05-23.log",
                "checkouts/usr/scan/repo/README.md",
                "secrets/github-app-private-key.pem",
                "pullwise-state/extra/artifact.txt",
            ]:
                self.assertIn(expected, contents)

    def test_import_restores_package_and_renders_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "source"
            source.mkdir()
            source_env = self.base_launcher_env(source)
            source_env["PULLWISE_APP_DIR"] = shell_path(source)
            source_env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(self.write_relative_migration_env(source))
            (source / "data").mkdir()
            (source / "data" / "pullwise.sqlite3").write_text("sqlite", encoding="utf-8")
            (source / "logs").mkdir()
            (source / "logs" / "pullwise-2026-05-23.log").write_text("log", encoding="utf-8")
            (source / "checkouts" / "usr" / "scan").mkdir(parents=True)
            (source / "checkouts" / "usr" / "scan" / "repo.txt").write_text("repo", encoding="utf-8")
            (source / "secrets").mkdir()
            (source / "secrets" / "github-app-private-key.pem").write_text("pem", encoding="utf-8")
            archive = workspace / "pullwise-export.tar.gz"

            export_result = self.run_launcher(["export", shell_path(archive)], source_env)
            self.assertEqual(0, export_result.returncode, export_result.stderr + export_result.stdout)

            dest = workspace / "dest"
            dest.mkdir()
            dest_env = self.base_launcher_env(dest)
            dest_env["PULLWISE_APP_DIR"] = shell_path(dest)
            dest_env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(dest / "etc" / "pullwise" / "server.env")
            dest_env["PULLWISE_SYSTEMD_DIR"] = shell_path(dest / "systemd")
            fake_chgrp, chgrp_log = self.write_fake_chgrp(dest)
            dest_env["PULLWISE_CHGRP_BIN"] = shell_path(fake_chgrp)
            dest_env["PULLWISE_CHGRP_LOG"] = shell_path(chgrp_log)
            dest_env["PULLWISE_SERVICE_GROUP"] = "pullwise"

            import_result = self.run_launcher(["import", shell_path(archive)], dest_env)

            self.assertEqual(0, import_result.returncode, import_result.stderr + import_result.stdout)
            self.assertTrue((dest / "etc" / "pullwise" / "server.env").exists())
            self.assertEqual("sqlite", (dest / "data" / "pullwise.sqlite3").read_text(encoding="utf-8"))
            self.assertEqual("log", (dest / "logs" / "pullwise-2026-05-23.log").read_text(encoding="utf-8"))
            self.assertEqual("repo", (dest / "checkouts" / "usr" / "scan" / "repo.txt").read_text(encoding="utf-8"))
            self.assertEqual("pem", (dest / "secrets" / "github-app-private-key.pem").read_text(encoding="utf-8"))
            self.assertTrue((dest / "systemd" / "pullwise-server.service").exists())
            chgrp_calls = chgrp_log.read_text(encoding="utf-8")
            self.assertIn(f"pullwise {shell_path(dest / 'etc' / 'pullwise' / 'server.env')}", chgrp_calls)
            self.assertIn(f"pullwise {shell_path(dest / 'secrets' / 'github-app-private-key.pem')}", chgrp_calls)

    def test_import_rejects_archive_with_path_traversal_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                env_bytes = b"PULLWISE_MODE=production\n"
                env_info = tarfile.TarInfo("config/server.env")
                env_info.size = len(env_bytes)
                tar.addfile(env_info, io.BytesIO(env_bytes))

                payload = b"escape"
                escape_info = tarfile.TarInfo("../escaped.txt")
                escape_info.size = len(payload)
                tar.addfile(escape_info, io.BytesIO(payload))

            dest = root / "dest"
            dest.mkdir()
            env = self.base_launcher_env(dest)
            env["PULLWISE_APP_DIR"] = shell_path(dest)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(dest / "etc" / "pullwise" / "server.env")

            result = self.run_launcher(["import", shell_path(archive)], env)

            self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertIn("unsafe archive member", result.stderr + result.stdout)
            self.assertFalse((root / "escaped.txt").exists())

    def test_import_rejects_archive_with_link_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "link.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                env_bytes = b"PULLWISE_MODE=production\n"
                env_info = tarfile.TarInfo("config/server.env")
                env_info.size = len(env_bytes)
                tar.addfile(env_info, io.BytesIO(env_bytes))

                link_info = tarfile.TarInfo("logs/latest")
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = "/etc/passwd"
                tar.addfile(link_info)

            dest = root / "dest"
            dest.mkdir()
            env = self.base_launcher_env(dest)
            env["PULLWISE_APP_DIR"] = shell_path(dest)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(dest / "etc" / "pullwise" / "server.env")

            result = self.run_launcher(["import", shell_path(archive)], env)

            self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertIn("unsafe archive member", result.stderr + result.stdout)
            self.assertFalse((dest / "logs" / "latest").exists())


if __name__ == "__main__":
    unittest.main()
