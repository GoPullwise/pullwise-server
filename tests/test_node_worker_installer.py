from __future__ import annotations

import unittest
import shutil
import subprocess

from pullwise_server import app


class NodeWorkerInstallerTest(unittest.TestCase):
    def test_release_package_and_active_installer_are_node_only(self) -> None:
        self.assertEqual(
            app.worker_release_package("1.2.3"),
            "https://github.com/GoPullwise/pullwise-worker/releases/download/v1.2.3/pullwise-worker-1.2.3.tgz",
        )
        script = app.worker_install_script()
        for required in (
            'NODE_VERSION="22.23.1"',
            'install --prefix "$APP_ROOT" --omit=dev --ignore-scripts',
            "pullwise-worker-watcher-",
            "ExecStart=__BIN_PATH__ watch",
            "ExecStart=__BIN_PATH__ serve",
            "After=network-online.target __WATCHER_SERVICE__.service",
            "PULLWISE_PI_PROFILE_ROOT=",
            "PULLWISE_WORKER_STATE_ROOT=",
            "apt-get install -y git",
            "safe_worker_id() {",
            'getent passwd "$SERVICE_USER"',
            "Existing service user has a different home",
            "Profiles are managed by Pullwise Model Gateway",
            "PULLWISE_WORKER_BOOTSTRAP_TOKEN",
            '"$NODE_ROOT/bin/node" "$APP_ROOT/node_modules/pullwise-worker/src/main.ts" bootstrap',
        ):
            self.assertIn(required, script)
        for forbidden in (
            "python",
            "pip ",
            ".venv",
            "CODEX_HOME",
            "PULLWISE_CODEX",
            "openai-codex",
            "profile add",
            "pi auth login",
            "Add another provider account or API key?",
            "--bootstrap-token",
        ):
            self.assertNotIn(forbidden, script)
        bash = shutil.which("bash")
        if bash:
            probe = subprocess.run([bash, "--version"], capture_output=True, check=False)
            if probe.returncode != 0:
                self.skipTest("bash is present but unavailable in this environment")
            checked = subprocess.run(
                [bash, "-n"],
                input=script.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
