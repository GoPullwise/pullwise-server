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


def require_shell_path_converter(shell: str) -> None:
    if os.name != "nt":
        return
    result = subprocess.run(
        [shell, "-lc", "command -v cygpath"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise unittest.SkipTest("launcher tests require cygpath on Windows")


def shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    shell = shell_executable()
    require_shell_path_converter(shell)
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


@unittest.skipIf(os.name == "nt", "launcher shell contracts run on POSIX CI; Windows sh/cygpath is too slow for local full-suite gating")
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

    def write_fake_runtime(self, root: Path) -> tuple[Path, Path]:
        venv_python = root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
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
            fake_journalctl,
            """
            #!/usr/bin/env sh
            echo "journalctl args: $*"
            """,
        )
        return venv_python, fake_git

    def write_fake_long_running_python(self, root: Path) -> Path:
        venv_python = root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        write_executable(
            venv_python,
            """
            #!/usr/bin/env sh
            if [ "$1" = "--version" ]; then
              echo "Python 3.10.12"
              exit 0
            fi
            printf 'fake server invoked: %s\\n' "$*" >> "$PULLWISE_FAKE_SERVER_LOG"
            while :; do
              sleep 1
            done
            """,
        )
        return venv_python

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

    def write_fake_systemctl(self, root: Path) -> tuple[Path, Path]:
        fake_systemctl = root / "bin" / "systemctl"
        fake_systemctl.parent.mkdir(parents=True, exist_ok=True)
        log_file = root / "systemctl.log"
        write_executable(
            fake_systemctl,
            """
            #!/usr/bin/env sh
            printf '%s\\n' "$*" >> "$PULLWISE_SYSTEMCTL_LOG"
            echo "systemctl args: $*"
            """,
        )
        return fake_systemctl, log_file

    def write_fake_curl(self, root: Path, *, exit_code: int = 0, output: str = '{"ok":true}') -> Path:
        fake_curl = root / "bin" / "curl"
        fake_curl.parent.mkdir(parents=True, exist_ok=True)
        write_executable(
            fake_curl,
            f"""
            #!/usr/bin/env sh
            printf '%s\\n' "$*" >> "$PULLWISE_CURL_LOG"
            printf '%s' '{output}'
            exit {exit_code}
            """,
        )
        return fake_curl

    def symlink_tool(self, root: Path, name: str) -> None:
        target = shutil.which(name)
        if not target:
            raise unittest.SkipTest(f"{name} is required for this shell contract")
        link = root / "bin" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(target)

    def write_fake_root_id(self, root: Path) -> None:
        write_executable(
            root / "bin" / "id",
            """
            #!/usr/bin/env sh
            if [ "$1" = "-u" ]; then
              echo 0
              exit 0
            fi
            exit 1
            """,
        )

    def write_fake_apt_get_for_launcher(self, root: Path) -> tuple[Path, Path]:
        fake_apt = root / "bin" / "apt-get"
        fake_apt.parent.mkdir(parents=True, exist_ok=True)
        apt_log = root / "apt.log"
        write_executable(
            fake_apt,
            """
            #!/usr/bin/env sh
            printf '%s\\n' "$*" >> "$PULLWISE_APT_LOG"
            if [ "$1" = "install" ]; then
              shift
              [ "$1" = "-y" ] && shift
              [ "$1" = "--no-install-recommends" ] && shift
              for package in "$@"; do
                case "$package" in
                  python3.10)
                    cat > "$PULLWISE_FAKE_BIN/python3.10" <<'PY'
#!/usr/bin/env sh
if [ "$1" = "--version" ]; then
  echo "Python 3.10.12"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ] && [ "$3" = "--help" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'VENV'
#!/usr/bin/env sh
if [ "$1" = "--version" ]; then
  echo "Python 3.10.12"
  exit 0
fi
exit 0
VENV
  chmod +x "$3/bin/python"
  exit 0
fi
exit 0
PY
                    chmod +x "$PULLWISE_FAKE_BIN/python3.10"
                    ;;
                  curl)
                    cat > "$PULLWISE_FAKE_BIN/curl" <<'CURL'
#!/usr/bin/env sh
printf '{"ok":true}'
CURL
                    chmod +x "$PULLWISE_FAKE_BIN/curl"
                    ;;
                esac
              done
            fi
            exit 0
            """,
        )
        return fake_apt, apt_log

    def health_env(self, root: Path, *, curl_exit_code: int = 0) -> dict[str, str]:
        env = self.base_launcher_env(root)
        curl_log = root / "curl.log"
        env["PULLWISE_CURL_BIN"] = shell_path(self.write_fake_curl(root, exit_code=curl_exit_code))
        env["PULLWISE_CURL_LOG"] = shell_path(curl_log)
        return env

    def write_state_encryption_key(self, root: Path) -> Path:
        key_file = root / "secrets" / "state-encryption-key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            key_file.chmod(0o600)
        key_file.write_text("01" * 32 + "\n", encoding="ascii", newline="\n")
        key_file.chmod(0o400)
        return key_file

    def write_production_env(self, root: Path, *, allowed_origins: str = "https://app.example.com") -> Path:
        env_file = root / "server.env"
        state_key = self.write_state_encryption_key(root)
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
                PULLWISE_STATE_ENCRYPTION_KEY_PATH={shell_path(state_key)}
                PULLWISE_COOKIE_SECURE=true
                PULLWISE_RATE_LIMIT_ENABLED=true
                PULLWISE_RATE_LIMIT_REQUESTS=600
                PULLWISE_RATE_LIMIT_WINDOW_SECONDS=60
                PULLWISE_GITHUB_CLIENT_ID=client_id
                PULLWISE_GITHUB_CLIENT_SECRET=client_secret
                PULLWISE_GITHUB_APP_SLUG=pullwise
                PULLWISE_GITHUB_APP_ID=123
                PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64=cHJpdmF0ZS1rZXk=
                PULLWISE_GITHUB_OAUTH_SCOPE=read:user user:email
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
                PULLWISE_RATE_LIMIT_ENABLED=true
                PULLWISE_RATE_LIMIT_REQUESTS=600
                PULLWISE_RATE_LIMIT_WINDOW_SECONDS=60
                PULLWISE_GITHUB_CLIENT_ID=client_id
                PULLWISE_GITHUB_CLIENT_SECRET=client_secret
                PULLWISE_GITHUB_APP_SLUG=pullwise
                PULLWISE_GITHUB_APP_ID=123
                PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=secrets/github-app-private-key.pem
                PULLWISE_STATE_ENCRYPTION_KEY_PATH=secrets/state-encryption-key
                """
            ).strip()
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return env_file

    def base_launcher_env(self, root: Path) -> dict[str, str]:
        venv_python, fake_git = self.write_fake_runtime(root)
        os_release = root / "os-release"
        os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
        return {
            "PULLWISE_SYSTEM_ENV_FILE": shell_path(self.write_production_env(root)),
            "PULLWISE_LOCAL_ENV_FILE": shell_path(root / ".env.local"),
            "PULLWISE_SYSTEMD_DIR": shell_path(root / "systemd"),
            "PULLWISE_VENV_DIR": shell_path(venv_python.parents[1]),
            "PULLWISE_GIT_BIN": shell_path(fake_git),
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
            "init-env",
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

    def test_launcher_file_is_directly_executable(self) -> None:
        mode = (project_root() / "launcher.sh").stat().st_mode

        self.assertTrue(mode & stat.S_IXUSR)

    def test_health_returns_success_when_curl_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.health_env(root)

            result = self.run_launcher(["health"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn('{"ok":true}', result.stdout)

    def test_health_returns_failure_when_curl_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.health_env(root, curl_exit_code=7)

            result = self.run_launcher(["health"], env)

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("health check failed", result.stderr)

    def test_setup_installs_missing_python_packages_on_ubuntu_2204(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tool in ["dirname", "pwd", "mkdir", "chmod", "sed", "tr", "sh", "cat"]:
                self.symlink_tool(root, tool)
            self.write_fake_root_id(root)
            fake_apt, apt_log = self.write_fake_apt_get_for_launcher(root)
            os_release = root / "os-release"
            os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            env = {
                "PATH": shell_path(root / "bin"),
                "PULLWISE_APT_GET_BIN": shell_path(fake_apt),
                "PULLWISE_APT_LOG": shell_path(apt_log),
                "PULLWISE_FAKE_BIN": shell_path(root / "bin"),
                "PULLWISE_OS_RELEASE_FILE": shell_path(os_release),
                "PULLWISE_VENV_DIR": shell_path(root / "venv"),
                "PULLWISE_LOCAL_ENV_FILE": shell_path(root / ".env.local"),
                "PULLWISE_SYSTEM_ENV_FILE": shell_path(root / "server.env"),
            }

            result = self.run_launcher(["setup"], env)
            apt_calls = apt_log.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("update", apt_calls)
        self.assertIn("install -y --no-install-recommends python3.10 python3.10-venv python3-pip", apt_calls)
        self.assertIn("setup complete", result.stdout)

    def test_health_installs_missing_curl_on_ubuntu_2204(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os_release = root / "os-release"
            os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            env = self.base_launcher_env(root)
            fake_apt, apt_log = self.write_fake_apt_get_for_launcher(root)
            for tool in ["dirname", "pwd", "sed", "tr", "sh", "cat", "chmod"]:
                self.symlink_tool(root, tool)
            self.write_fake_root_id(root)
            env.update(
                {
                    "PATH": shell_path(root / "bin"),
                    "PULLWISE_APT_GET_BIN": shell_path(fake_apt),
                    "PULLWISE_APT_LOG": shell_path(apt_log),
                    "PULLWISE_FAKE_BIN": shell_path(root / "bin"),
                    "PULLWISE_OS_RELEASE_FILE": shell_path(os_release),
                }
            )

            result = self.run_launcher(["health"], env)
            apt_calls = apt_log.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("install -y --no-install-recommends curl", apt_calls)
        self.assertIn('{"ok":true}', result.stdout)

    def test_init_env_creates_local_template_and_guides_required_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            local_env = root / ".env.local"
            env["PULLWISE_APP_DIR"] = shell_path(project_root())
            env["PULLWISE_LOCAL_ENV_FILE"] = shell_path(local_env)

            result = self.run_launcher(["init-env"], env)
            created = local_env.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("PULLWISE_MODE=production", created)
        self.assertIn("PULLWISE_GITHUB_CLIENT_ID", created)
        self.assertIn("HTTP/runtime", result.stdout)
        self.assertIn("GitHub OAuth/App", result.stdout)
        self.assertIn("External workers", result.stdout)
        self.assertIn("doctor", result.stdout)

    def test_init_env_does_not_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            local_env = root / ".env.local"
            local_env.write_text("CUSTOM=1\n", encoding="utf-8", newline="\n")
            env["PULLWISE_APP_DIR"] = shell_path(project_root())
            env["PULLWISE_LOCAL_ENV_FILE"] = shell_path(local_env)

            result = self.run_launcher(["init-env"], env)
            current = local_env.read_text(encoding="utf-8")

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual("CUSTOM=1\n", current)
        self.assertIn("already exists", result.stderr + result.stdout)

    def test_doctor_accepts_complete_ubuntu_production_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("doctor checks passed", result.stdout)

    def test_doctor_accepts_admin_managed_billing_catalog_without_product_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env_file = Path(env["PULLWISE_SYSTEM_ENV_FILE"])
            with env_file.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("PULLWISE_CREEM_API_KEY=creem_123\n")
                handle.write("PULLWISE_CREEM_WEBHOOK_SECRET=whsec_123\n")
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertNotIn("PULLWISE_CREEM_PRO_PRODUCT_IDS", result.stderr + result.stdout)

    def test_doctor_accepts_admin_managed_scan_limits_without_scan_limit_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
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

    def test_doctor_rejects_disabled_api_rate_limit_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_RATE_LIMIT_ENABLED"] = "false"
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("PULLWISE_RATE_LIMIT_ENABLED", result.stderr + result.stdout)

    def test_doctor_rejects_missing_state_encryption_key_in_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            state_key = root / "secrets" / "state-encryption-key"
            state_key.unlink()
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])

            result = self.run_launcher(["doctor"], env)

        self.assertNotEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("PULLWISE_STATE_ENCRYPTION_KEY_PATH", result.stderr + result.stdout)

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

    def test_systemd_manager_can_start_stop_restart_and_report_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            fake_systemctl, systemctl_log = self.write_fake_systemctl(root)
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])
            env["PULLWISE_SYSTEMCTL_BIN"] = shell_path(fake_systemctl)
            env["PULLWISE_SYSTEMCTL_LOG"] = shell_path(systemctl_log)
            env["PULLWISE_RESTART_WAIT_HEALTH"] = "false"

            start = self.run_launcher(["start"], env)
            stop = self.run_launcher(["stop"], env)
            restart = self.run_launcher(["restart"], env)
            status = self.run_launcher(["status"], env)
            calls = systemctl_log.read_text(encoding="utf-8")

        self.assertEqual(0, start.returncode, start.stderr + start.stdout)
        self.assertEqual(0, stop.returncode, stop.stderr + stop.stdout)
        self.assertEqual(0, restart.returncode, restart.stderr + restart.stdout)
        self.assertEqual(0, status.returncode, status.stderr + status.stdout)
        self.assertIn("start pullwise-server", calls)
        self.assertIn("stop pullwise-server", calls)
        self.assertIn("restart pullwise-server", calls)
        self.assertIn("status pullwise-server --no-pager", calls)

    def test_systemd_restart_waits_for_health_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            fake_systemctl, systemctl_log = self.write_fake_systemctl(root)
            health_count = root / "health-count"
            fake_curl = root / "bin" / "curl-retry"
            write_executable(
                fake_curl,
                f"""
                #!/usr/bin/env sh
                count=0
                if [ -f {shell_path(health_count)} ]; then
                  count=$(cat {shell_path(health_count)})
                fi
                count=$((count + 1))
                printf '%s' "$count" > {shell_path(health_count)}
                if [ "$count" -lt 3 ]; then
                  exit 7
                fi
                printf '{{"ok":true}}'
                exit 0
                """,
            )
            self.write_minimal_service(root, env["PULLWISE_SYSTEM_ENV_FILE"])
            env["PULLWISE_SYSTEMCTL_BIN"] = shell_path(fake_systemctl)
            env["PULLWISE_SYSTEMCTL_LOG"] = shell_path(systemctl_log)
            env["PULLWISE_CURL_BIN"] = shell_path(fake_curl)
            env["PULLWISE_RESTART_HEALTH_RETRIES"] = "3"
            env["PULLWISE_RESTART_HEALTH_RETRY_SECONDS"] = "0"

            restart = self.run_launcher(["restart"], env)
            calls = systemctl_log.read_text(encoding="utf-8")
            count = health_count.read_text(encoding="utf-8")

        self.assertEqual(0, restart.returncode, restart.stderr + restart.stdout)
        self.assertIn("restart pullwise-server", calls)
        self.assertEqual("3", count)
        self.assertIn("health endpoint responded after restart", restart.stdout)

    def test_start_dry_run_can_still_print_direct_server_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self.base_launcher_env(Path(tmp))
            env["PULLWISE_MANAGER"] = "direct"

            result = self.run_launcher(["start", "--dry-run"], env)

        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn("-m pullwise_server --host 0.0.0.0 --port 3010", result.stdout)
        self.assertIn("dry-run", result.stdout)

    @unittest.skipIf(os.name == "nt", "direct manager process lifecycle tests require POSIX process semantics")
    def test_direct_manager_can_start_report_status_and_stop_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            venv_python = self.write_fake_long_running_python(root)
            env.update(
                {
                    "PULLWISE_APP_DIR": shell_path(root),
                    "PULLWISE_MANAGER": "direct",
                    "PULLWISE_VENV_DIR": shell_path(venv_python.parents[1]),
                    "PULLWISE_RUN_DIR": shell_path(root / "run"),
                    "PULLWISE_LOG_DIR": shell_path(root / "logs"),
                    "PULLWISE_CHECKOUT_ROOT": shell_path(root / "checkouts"),
                    "PULLWISE_DB_PATH": shell_path(root / "data" / "pullwise.sqlite3"),
                    "PULLWISE_FAKE_SERVER_LOG": shell_path(root / "fake-server.log"),
                    "PULLWISE_STOP_TIMEOUT_SECONDS": "3",
                }
            )

            try:
                start = self.run_launcher(["start"], env)
                self.assertEqual(0, start.returncode, start.stderr + start.stdout)
                self.assertIn("started with pid", start.stdout)

                status = self.run_launcher(["status"], env)
                self.assertEqual(0, status.returncode, status.stderr + status.stdout)
                self.assertIn("pullwise-server: running", status.stdout)
                self.assertIn("-m pullwise_server", status.stdout)

                stop = self.run_launcher(["stop"], env)
                self.assertEqual(0, stop.returncode, stop.stderr + stop.stdout)
                self.assertIn("stopped", stop.stdout)

                stopped = self.run_launcher(["status"], env)
                self.assertEqual(0, stopped.returncode, stopped.stderr + stopped.stdout)
                self.assertIn("pullwise-server: stopped", stopped.stdout)
            finally:
                self.run_launcher(["stop", "--force"], env)

    @unittest.skipIf(os.name == "nt", "direct manager process lifecycle tests require POSIX process semantics")
    def test_direct_manager_restart_replaces_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            venv_python = self.write_fake_long_running_python(root)
            env.update(
                {
                    "PULLWISE_APP_DIR": shell_path(root),
                    "PULLWISE_MANAGER": "direct",
                    "PULLWISE_VENV_DIR": shell_path(venv_python.parents[1]),
                    "PULLWISE_RUN_DIR": shell_path(root / "run"),
                    "PULLWISE_LOG_DIR": shell_path(root / "logs"),
                    "PULLWISE_CHECKOUT_ROOT": shell_path(root / "checkouts"),
                    "PULLWISE_DB_PATH": shell_path(root / "data" / "pullwise.sqlite3"),
                    "PULLWISE_FAKE_SERVER_LOG": shell_path(root / "fake-server.log"),
                    "PULLWISE_STOP_TIMEOUT_SECONDS": "3",
                }
            )

            try:
                start = self.run_launcher(["start"], env)
                self.assertEqual(0, start.returncode, start.stderr + start.stdout)
                first_pid = (root / "run" / "pullwise-server.pid").read_text(encoding="utf-8").strip()

                restart = self.run_launcher(["restart"], env)
                self.assertEqual(0, restart.returncode, restart.stderr + restart.stdout)
                second_pid = (root / "run" / "pullwise-server.pid").read_text(encoding="utf-8").strip()

                self.assertNotEqual(first_pid, second_pid)
                status = self.run_launcher(["status"], env)
                self.assertEqual(0, status.returncode, status.stderr + status.stdout)
                self.assertIn(f"pid: {second_pid}", status.stdout)
            finally:
                self.run_launcher(["stop", "--force"], env)

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

    def test_logs_can_tail_direct_server_and_app_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_MANAGER"] = "direct"
            env["PULLWISE_RUN_DIR"] = shell_path(root / "run")
            env["PULLWISE_LOG_DIR"] = shell_path(root / "logs")
            (root / "run").mkdir()
            (root / "logs").mkdir()
            (root / "run" / "server.out.log").write_text("server output\n", encoding="utf-8")
            (root / "run" / "server.err.log").write_text("server error\n", encoding="utf-8")
            (root / "logs" / "pullwise-2026-05-25.log").write_text("app log\n", encoding="utf-8")

            server = self.run_launcher(["logs", "server"], env)
            error = self.run_launcher(["logs", "error"], env)
            app = self.run_launcher(["logs", "app"], env)

        self.assertEqual(0, server.returncode, server.stderr + server.stdout)
        self.assertEqual(0, error.returncode, error.stderr + error.stdout)
        self.assertEqual(0, app.returncode, app.stderr + app.stdout)
        self.assertIn("server output", server.stdout)
        self.assertIn("server error", error.stdout)
        self.assertIn("app log", app.stdout)

    def test_export_defaults_to_non_secret_runtime_package(self) -> None:
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
            (root / "secrets").mkdir(exist_ok=True)
            (root / "secrets" / "github-app-private-key.pem").write_text("pem", encoding="utf-8")
            self.write_state_encryption_key(root)
            (root / ".pullwise" / "extra").mkdir(parents=True)
            (root / ".pullwise" / "extra" / "artifact.txt").write_text("artifact", encoding="utf-8")
            archive = root / "pullwise-export.tar.gz"

            result = self.run_launcher(["export", shell_path(archive)], env)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(archive.exists())
            contents = tar_contents(archive)
            for expected in [
                "manifest.env",
                "data/pullwise.sqlite3",
                "logs/pullwise-2026-05-23.log",
                "checkouts/usr/scan/repo/README.md",
                "pullwise-state/extra/artifact.txt",
            ]:
                self.assertIn(expected, contents)
            self.assertNotIn("config/server.env", contents)
            self.assertNotIn("secrets/github-app-private-key.pem", contents)
            self.assertNotIn("secrets/state-encryption-key", contents)

    def test_export_include_secrets_packages_env_and_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.base_launcher_env(root)
            env["PULLWISE_APP_DIR"] = shell_path(root)
            env["PULLWISE_SYSTEM_ENV_FILE"] = shell_path(self.write_relative_migration_env(root))
            (root / "data").mkdir()
            (root / "data" / "pullwise.sqlite3").write_text("sqlite", encoding="utf-8")
            (root / "secrets").mkdir(exist_ok=True)
            (root / "secrets" / "github-app-private-key.pem").write_text("pem", encoding="utf-8")
            self.write_state_encryption_key(root)
            archive = root / "pullwise-export.tar.gz"

            result = self.run_launcher(["export", "--include-secrets", shell_path(archive)], env)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            contents = tar_contents(archive)
            self.assertIn("manifest.env", contents)
            self.assertIn("config/server.env", contents)
            self.assertIn("data/pullwise.sqlite3", contents)
            self.assertIn("secrets/github-app-private-key.pem", contents)
            self.assertNotIn("secrets/state-encryption-key", contents)

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
            (source / "secrets").mkdir(exist_ok=True)
            (source / "secrets" / "github-app-private-key.pem").write_text("pem", encoding="utf-8")
            self.write_state_encryption_key(source)
            archive = workspace / "pullwise-export.tar.gz"

            export_result = self.run_launcher(["export", "--include-secrets", shell_path(archive)], source_env)
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
