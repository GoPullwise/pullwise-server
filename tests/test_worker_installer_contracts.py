from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import unittest

from pullwise_server import app

def contract_test_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    seen: set[str] = set()
    fallback = shutil.which("bash")
    candidates: list[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, "bash.exe")
        if not os.path.exists(candidate):
            continue
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        lowered = normalized.lower()
        if "\\windows\\system32\\bash.exe" in lowered or "\\windowsapps\\bash.exe" in lowered:
            continue
        candidates.append(candidate)
    return candidates[0] if candidates else fallback


def service_user_helper(script: str) -> str:
    start = script.index("safe_worker_id() {")
    end = script.index('SAFE_WORKER_ID="$(safe_worker_id "$WORKER_ID")"')
    return script[start:end] + '\nservice_user_name "$(safe_worker_id "$1")"\n'


def instance_metadata_helper(script: str) -> str:
    start = script.index("safe_worker_id() {")
    end = script.index('LOGROTATE_FILE="/etc/logrotate.d/$SERVICE_NAME"')
    return (
        'WORKER_ID="$1"\n'
        + script[start:end]
        + '\nprintf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$SAFE_WORKER_ID" "$SERVICE_USER" "$SERVICE_NAME" "$WATCHER_SERVICE_NAME" "$WATCHER_SERVICE_FILE" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"\n'
    )


def instance_metadata(script: str, worker_id: str) -> dict[str, str]:
    shell = shutil.which("sh") or shutil.which("bash")
    if not shell:
        raise unittest.SkipTest("No POSIX shell is available for installer contract tests.")
    if not shutil.which("sha256sum"):
        raise unittest.SkipTest("sha256sum is required for installer contract tests.")

    result = subprocess.run(
        [shell, "-c", instance_metadata_helper(script), "_", worker_id],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr + result.stdout)
    safe_id, service_user, service_name, watcher_service_name, watcher_service_file, config_dir, data_dir, log_dir = result.stdout.strip().split("\t")
    return {
        "safe_id": safe_id,
        "service_user": service_user,
        "service_name": service_name,
        "watcher_service_name": watcher_service_name,
        "watcher_service_file": watcher_service_file,
        "config_dir": config_dir,
        "data_dir": data_dir,
        "log_dir": log_dir,
    }


def ubuntu_dependency_support_helper(script: str) -> str:
    start = script.index("read_os_value() {")
    end = script.index("auto_install_enabled() {")
    return (
        script[start:end]
        + "\nos_file=\"$(mktemp)\"\n"
        + "trap 'rm -f \"$os_file\"' EXIT\n"
        + "printf '%s' \"$PULLWISE_TEST_OS_RELEASE\" > \"$os_file\"\n"
        + "PULLWISE_WORKER_OS_RELEASE_FILE=\"$os_file\"\n"
        + "if is_ubuntu_2204_or_newer; then printf supported; else printf unsupported; fi\n"
    )


def ubuntu_dependency_support(script: str, os_release: str) -> str:
    bash = contract_test_bash()
    if not bash:
        raise unittest.SkipTest("bash is required for installer contract tests.")

    result = subprocess.run(
        [bash, "-c", ubuntu_dependency_support_helper(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PULLWISE_TEST_OS_RELEASE": os_release},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr + result.stdout)
    return result.stdout.strip()


class WorkerInstallerContractsTest(unittest.TestCase):
    def test_systemd_write_paths_are_instance_scoped(self) -> None:
        script = app.worker_install_script()

        self.assertIn("ReadWritePaths=$DATA_DIR $LOG_DIR", script)
        self.assertNotIn("ReadWritePaths=$BASE_DATA_DIR", script)
        self.assertNotIn("SupplementaryGroups=", script)
        self.assertIn('install -d -m 0755 -o root -g root "$BASE_DATA_DIR" "$BASE_LOG_DIR"', script)
        self.assertNotIn('install -d -m 1770 -o root -g "$SERVICE_GROUP" "$BASE_DATA_DIR" "$BASE_LOG_DIR"', script)

    def test_installer_scopes_codex_sqlite_state_and_config(self) -> None:
        script = app.worker_install_script()

        self.assertIn('WORKER_RUNTIME_ROOT="$DATA_DIR/workers/$WORKER_ID"', script)
        self.assertIn('WORKER_VENV="$WORKER_RUNTIME_ROOT/.venv"', script)
        self.assertIn('CODEX_HOME="$WORKER_RUNTIME_ROOT/codex-home"', script)
        self.assertIn('CODEX_SQLITE_HOME="$WORKER_RUNTIME_ROOT/codex-sqlite"', script)
        self.assertIn('SERVICE_TOOL_PATH="$WORKER_VENV/bin:$WORKER_RUNTIME_ROOT/.local/bin:$WORKER_RUNTIME_ROOT/.codex/bin:$CODEX_HOME/bin:$SERVICE_PATH"', script)
        self.assertIn('codex_cli_override_enabled() {', script)
        self.assertIn('if provider_chain_has codex && codex_cli_override_enabled; then', script)
        self.assertIn('useradd --system --home "$WORKER_RUNTIME_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"', script)
        self.assertIn('WorkingDirectory=$WORKER_RUNTIME_ROOT', script)
        self.assertIn('CODEX_SQLITE_HOME="$CODEX_SQLITE_HOME"', script)
        self.assertIn('CODEX_SQLITE_HOME=%q', script)
        self.assertIn('"$CODEX_SQLITE_HOME"', script)
        self.assertIn('install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$CODEX_HOME/config.toml"', script)
        self.assertIn('write_env_value PULLWISE_CODEX_HOME "$CODEX_HOME"', script)
        self.assertIn('write_env_value PULLWISE_CODEX_SQLITE_HOME "$CODEX_SQLITE_HOME"', script)
        self.assertIn('write_env_value PULLWISE_WORKER_ROOT "$WORKER_RUNTIME_ROOT"', script)
        self.assertIn('write_env_value PULLWISE_WORKER_VENV "$WORKER_VENV"', script)
        self.assertIn('write_env_value PULLWISE_PYTHON_BIN "$PYTHON_BIN"', script)
        self.assertIn('write_optional_env_value PULLWISE_CODEX_COMMAND "$CODEX_COMMAND"', script)
        self.assertIn('export CODEX_HOME="\\${PULLWISE_CODEX_HOME:-\\$WORKER_ROOT/codex-home}"', script)
        self.assertIn('export CODEX_SQLITE_HOME="\\${PULLWISE_CODEX_SQLITE_HOME:-\\$WORKER_ROOT/codex-sqlite}"', script)
        self.assertIn("Environment=CODEX_HOME=$CODEX_HOME", script)
        self.assertIn("Environment=CODEX_SQLITE_HOME=$CODEX_SQLITE_HOME", script)

    def test_installer_creates_one_watcher_service_per_worker_instance(self) -> None:
        script = app.worker_install_script()

        self.assertIn('WATCHER_SERVICE_NAME="$SERVICE_NAME-watcher"', script)
        self.assertIn('WATCHER_SERVICE_FILE="/etc/systemd/system/$WATCHER_SERVICE_NAME.service"', script)
        self.assertIn('write_env_value PULLWISE_LIFECYCLE_WATCHER_ENABLED "1"', script)
        self.assertIn('write_env_value PULLWISE_WATCHER_SERVICE_NAME "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('write_env_value PULLWISE_WATCHER_SERVICE_FILE "$WATCHER_SERVICE_FILE"', script)
        self.assertIn('cat > "$WATCHER_SERVICE_FILE" <<EOF', script)
        self.assertIn("Before=$SERVICE_NAME.service", script)
        self.assertIn("ExecStart=$BIN_PATH watch", script)
        self.assertEqual(script.count("StartLimitIntervalSec=300"), 2)
        self.assertEqual(script.count("StartLimitBurst=5"), 2)
        self.assertIn('systemctl enable "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('systemctl restart "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('WATCHER_STARTED=0', script)
        self.assertIn('if [ "$WATCHER_STARTED" = "1" ]; then', script)
        self.assertLess(
            script.index('if [ "$WATCHER_STARTED" = "1" ]; then'),
            script.index('systemctl stop "$WATCHER_SERVICE_NAME"'),
        )
        self.assertLess(
            script.index('\nsystemctl restart "$WATCHER_SERVICE_NAME"\n'),
            script.index('\nWATCHER_STARTED=1\n'),
        )
        self.assertLess(
            script.index('\nWATCHER_STARTED=1\n'),
            script.index('\nsystemctl restart "$SERVICE_NAME"\n'),
        )
        self.assertIn('rollback_file "$WATCHER_SERVICE_FILE" "/etc/systemd/system" "$HAD_WATCHER_SERVICE_FILE"', script)
        self.assertNotIn("ExecStopPost=+$BIN_PATH finalize-uninstall", script)

    def test_installer_runs_codex_device_auth_by_default_after_printing_command(self) -> None:
        script = app.worker_install_script()

        self.assertIn("codex_device_auth_command() {", script)
        self.assertIn('service_user_auth_command "$BIN_PATH" codex-login', script)
        self.assertIn("run_default_auth_commands() {", script)
        self.assertIn('auth_command="$(codex_device_auth_command)"', script)
        self.assertIn('if ! eval "$auth_command"; then', script)
        self.assertIn(
            "print_auth_commands\nrun_default_auth_commands\nif ! run_as_service_user env PULLWISE_DOCTOR_REQUIRE_SYSTEMD_ACTIVE=0 \"$BIN_PATH\" doctor; then",
            script,
        )

    def test_installer_runs_doctor_preflight_before_starting_worker(self) -> None:
        script = app.worker_install_script()

        doctor_call = script.index('\nif ! run_as_service_user env PULLWISE_DOCTOR_REQUIRE_SYSTEMD_ACTIVE=0 "$BIN_PATH" doctor; then\n')
        service_start = script.index('\nsystemctl restart "$SERVICE_NAME"\n')
        self.assertLess(doctor_call, service_start)
        self.assertNotIn("CODEGRAPH_INSTALL_URL", script)
        self.assertNotIn("CODEGRAPH_INSTALL_DIR", script)
        self.assertNotIn("CODEGRAPH_BIN_DIR", script)
        self.assertNotIn("ensure_codegraph_cli", script)
        self.assertNotIn("configure_codegraph_codex_mcp", script)
        self.assertNotIn("codegraph install", script)
        self.assertIn('if ! run_as_service_user env PULLWISE_DOCTOR_REQUIRE_SYSTEMD_ACTIVE=0 "$BIN_PATH" doctor; then', script)
        self.assertNotIn('run_as_service_user "$BIN_PATH" doctor || true', script)

    def test_service_user_name_uses_digest_to_avoid_prefix_collisions(self) -> None:
        shell = shutil.which("sh") or shutil.which("bash")
        if not shell:
            self.skipTest("No POSIX shell is available for installer contract tests.")
        if not shutil.which("sha256sum"):
            self.skipTest("sha256sum is required for installer contract tests.")

        helper = service_user_helper(app.worker_install_script())
        worker_ids = ["wk_abcdefghijklmnop_A", "wk_abcdefghijklmnop_B"]
        names = []
        for worker_id in worker_ids:
            result = subprocess.run(
                [shell, "-c", helper, "_", worker_id],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            names.append(result.stdout.strip())

        self.assertNotEqual(names[0], names[1])
        for worker_id, name in zip(worker_ids, names, strict=True):
            safe_id = app.worker_safe_service_id(worker_id)
            digest = hashlib.sha256(safe_id.encode("utf-8")).hexdigest()[:10]
            self.assertEqual(f"pw-worker-wk-abcdefg-{digest}", name)
            self.assertLessEqual(len(name), 32)

    def test_long_worker_ids_do_not_reuse_instance_names_after_truncation(self) -> None:
        script = app.worker_install_script()
        prefix = "wk_" + ("a" * 45)
        worker_ids = [f"{prefix}_X", f"{prefix}_Y"]
        instances = [instance_metadata(script, worker_id) for worker_id in worker_ids]

        for worker_id, metadata in zip(worker_ids, instances, strict=True):
            digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:10]
            self.assertEqual(app.worker_safe_service_id(worker_id), metadata["safe_id"])
            self.assertEqual(48, len(metadata["safe_id"]))
            self.assertTrue(metadata["safe_id"].endswith(f"-{digest}"))
            self.assertLessEqual(len(metadata["service_user"]), 32)

        for field in (
            "safe_id",
            "service_user",
            "service_name",
            "watcher_service_name",
            "watcher_service_file",
            "config_dir",
            "data_dir",
            "log_dir",
        ):
            self.assertNotEqual(instances[0][field], instances[1][field])
            self.assertNotIn(instances[0][field], instances[1][field])

    def test_existing_service_user_must_belong_to_current_worker_home(self) -> None:
        script = app.worker_install_script()

        self.assertIn('if id "$SERVICE_USER" >/dev/null 2>&1; then', script)
        self.assertIn('existing_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"', script)
        self.assertIn('if [ "$existing_home" != "$WORKER_RUNTIME_ROOT" ] && [ "$existing_home" != "$DATA_DIR" ]; then', script)
        self.assertIn("already exists with home", script)
        self.assertIn('useradd --system --home "$WORKER_RUNTIME_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"', script)

    def test_installer_bootstraps_python_and_uses_sdk_pinned_codex_runtime_by_default(self) -> None:
        script = app.worker_install_script()

        self.assertIn("install_ubuntu_packages python3.10 python3.10-venv python3-pip", script)
        self.assertIn("is_ubuntu_2204_or_newer", script)
        self.assertIn("Ubuntu 22.04 or newer Linux hosts", script)
        self.assertNotIn("Automatic installation is supported on Ubuntu 22.04 hosts", script)
        self.assertIn('ensure_command_available "bwrap" bwrap bubblewrap', script)
        self.assertIn("Pullwise worker requires Python 3.10 or newer.", script)
        self.assertNotIn("Pullwise worker requires Python 3.9", script)
        self.assertIn('PYTHON_BIN="$(python3.10 -c', script)
        self.assertIn(r'PYTHON_BIN="\${PULLWISE_PYTHON_BIN:-\$WORKER_ROOT/.venv/bin/python}"', script)
        self.assertIn('run_as_service_user "$PYTHON_BIN" -m venv "$WORKER_VENV"', script)
        self.assertIn('PYTHON_BIN="$WORKER_VENV/bin/python"', script)
        self.assertIn('run_as_service_user "$PYTHON_BIN" -m pip install --upgrade --force-reinstall --no-cache-dir "$WORKER_PACKAGE"', script)
        self.assertIn('export PATH="\$WORKER_ROOT/.venv/bin:\$WORKER_ROOT/.local/bin:\$WORKER_ROOT/.codex/bin:\$CODEX_HOME/bin:\$SERVICE_PATH"', script)
        self.assertNotIn('\n"$PYTHON_BIN" -m pip install --upgrade --force-reinstall --no-cache-dir "$WORKER_PACKAGE"', script)
        self.assertIn('ensure_command_available "tar" tar tar', script)
        self.assertIn('ensure_command_available "curl" curl curl', script)
        self.assertIn('CODEX_RELEASE="${PULLWISE_CODEX_RELEASE:-}"', script)
        self.assertIn('CODEX_INSTALLER_URL="${PULLWISE_CODEX_INSTALLER_URL:-https://chatgpt.com/codex/install.sh}"', script)
        self.assertIn('curl -fsSL "$CODEX_INSTALLER_URL" -o "$installer_path"', script)
        self.assertIn('CODEX_INSTALL_DIR="$codex_install_dir" CODEX_NON_INTERACTIVE=1 "$installer_path" --release "$release"', script)
        self.assertIn('Codex CLI standalone installer failed for release $release.', script)
        self.assertNotIn('latest release metadata and platform assets are still propagating', script)
        self.assertIn('write_optional_env_value PULLWISE_CODEX_RELEASE "$CODEX_RELEASE"', script)
        self.assertNotIn("https://deb.nodesource.com/node_22.x", script)
        self.assertNotIn("ensure_nodesource_nodejs", script)
        self.assertNotIn("@openai/codex", script)
        self.assertNotRegex(script, r"\|\s*(?:sh|bash)\b")

    def test_dependency_auto_install_supports_ubuntu_2204_or_newer(self) -> None:
        script = app.worker_install_script()

        self.assertEqual(
            "unsupported",
            ubuntu_dependency_support(script, 'ID="ubuntu"\nVERSION_ID="20.04"\n'),
        )
        self.assertEqual(
            "supported",
            ubuntu_dependency_support(script, 'ID="ubuntu"\nVERSION_ID="22.04"\n'),
        )
        self.assertEqual(
            "supported",
            ubuntu_dependency_support(script, 'ID="ubuntu"\nVERSION_ID="24.04"\n'),
        )

    def test_worker_install_command_verifies_bootstrap_script_before_execution(self) -> None:
        command = app.worker_install_command(
            install_url="https://api.pull-wise.com/install-worker.sh",
            server_url="https://api.pull-wise.com",
            worker_id="wk_1",
            worker_name="Worker One",
            worker_package="https://github.com/GoPullwise/pullwise-worker/releases/download/v1.2.3/pullwise_worker-1.2.3-py3-none-any.whl",
            provider_chain="codex",
        )
        expected_hash = hashlib.sha256(app.worker_install_script().encode("utf-8")).hexdigest()

        self.assertIn(expected_hash, command)
        self.assertIn('curl -fsSL', command)
        self.assertIn('-o "$install_script"', command)
        self.assertIn("sha256sum -c -", command)
        self.assertIn('bash "$install_script"', command)
        self.assertNotIn("--codex-release", command)
        self.assertNotIn("curl -fsSL 'https://api.pull-wise.com/install-worker.sh' | bash", command)
        self.assertNotIn("| bash -s --", command)


if __name__ == "__main__":
    unittest.main()


