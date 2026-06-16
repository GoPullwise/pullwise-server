from __future__ import annotations

import hashlib
import shutil
import subprocess
import unittest

from pullwise_server import app


def service_user_helper(script: str) -> str:
    start = script.index("safe_worker_id() {")
    end = script.index('SAFE_WORKER_ID="$(safe_worker_id "$WORKER_ID")"')
    return script[start:end] + '\nservice_user_name "$(safe_worker_id "$1")"\n'


def instance_metadata_helper(script: str) -> str:
    start = script.index("safe_worker_id() {")
    end = script.index('SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"')
    return (
        'WORKER_ID="$1"\n'
        + script[start:end]
        + '\nprintf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$SAFE_WORKER_ID" "$SERVICE_USER" "$SERVICE_NAME" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"\n'
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
    safe_id, service_user, service_name, config_dir, data_dir, log_dir = result.stdout.strip().split("\t")
    return {
        "safe_id": safe_id,
        "service_user": service_user,
        "service_name": service_name,
        "config_dir": config_dir,
        "data_dir": data_dir,
        "log_dir": log_dir,
    }


class WorkerInstallerContractsTest(unittest.TestCase):
    def test_systemd_write_paths_are_instance_scoped(self) -> None:
        script = app.worker_install_script()

        self.assertIn("ReadWritePaths=$DATA_DIR $LOG_DIR", script)
        self.assertNotIn("ReadWritePaths=$BASE_DATA_DIR", script)
        self.assertNotIn("SupplementaryGroups=", script)
        self.assertIn('install -d -m 0755 -o root -g root "$BASE_DATA_DIR" "$BASE_LOG_DIR"', script)
        self.assertNotIn('install -d -m 1770 -o root -g "$SERVICE_GROUP" "$BASE_DATA_DIR" "$BASE_LOG_DIR"', script)

    def test_installer_creates_one_watcher_service_per_worker_instance(self) -> None:
        script = app.worker_install_script()

        self.assertIn('WATCHER_SERVICE_NAME="$SERVICE_NAME-watcher"', script)
        self.assertIn('WATCHER_SERVICE_FILE="/etc/systemd/system/$WATCHER_SERVICE_NAME.service"', script)
        self.assertIn('write_env_value PULLWISE_LIFECYCLE_WATCHER_ENABLED "1"', script)
        self.assertIn('write_env_value PULLWISE_WATCHER_SERVICE_NAME "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('write_env_value PULLWISE_WATCHER_SERVICE_FILE "$WATCHER_SERVICE_FILE"', script)
        self.assertIn('cat > "$WATCHER_SERVICE_FILE" <<EOF', script)
        self.assertIn("ExecStart=$BIN_PATH watch", script)
        self.assertIn('systemctl enable "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('systemctl restart "$WATCHER_SERVICE_NAME"', script)
        self.assertIn('rollback_file "$WATCHER_SERVICE_FILE" "/etc/systemd/system" "$HAD_WATCHER_SERVICE_FILE"', script)
        self.assertNotIn("ExecStopPost=+$BIN_PATH finalize-uninstall", script)

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

        for field in ("safe_id", "service_user", "service_name", "config_dir", "data_dir", "log_dir"):
            self.assertNotEqual(instances[0][field], instances[1][field])

    def test_existing_service_user_must_belong_to_current_worker_home(self) -> None:
        script = app.worker_install_script()

        self.assertIn('if id "$SERVICE_USER" >/dev/null 2>&1; then', script)
        self.assertIn('existing_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"', script)
        self.assertIn('if [ "$existing_home" != "$DATA_DIR" ]; then', script)
        self.assertIn("already exists with home", script)
        self.assertIn('useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"', script)


if __name__ == "__main__":
    unittest.main()
