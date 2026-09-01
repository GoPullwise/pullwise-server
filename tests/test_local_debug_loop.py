from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "local_debug_loop.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("pullwise_local_debug_loop", MODULE_PATH)
assert SPEC and SPEC.loader
local_debug = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local_debug
SPEC.loader.exec_module(local_debug)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def visit(self, path: str) -> None:
        self.calls.append(("VISIT", path, None))

    def request(self, method: str, path: str, body: object = None) -> dict:
        self.calls.append((method, path, body))
        if path == "/auth/session":
            return {"authenticated": True, "admin": True}
        if path.startswith("/integrations/github/authorize"):
            return {"url": "/integrations/github/callback?scope=all"}
        if path == "/repositories/sync":
            return {"ok": True, "items": [{"id": "repo_1", "fullName": "acme/api", "defaultBranch": "main"}]}
        if path == "/repositories":
            return {"items": [{"id": "repo_1", "fullName": "acme/api", "defaultBranch": "main"}]}
        if path == "/scans/preflight":
            return {"allowedCount": 1, "repositories": [{"available": True}]}
        if path == "/scans":
            return {"id": "scan_1", "status": "queued"}
        if path == "/scans/scan_1/cancel":
            return {"id": "scan_1", "status": "cancelled"}
        if path == "/scans/scan_1":
            return {"id": "scan_1", "status": "cancelled"}
        if path == "/admin/workers":
            return {"items": [{"worker_id": "worker_1", "status": "degraded", "doctor_status": "not_ready"}]}
        if path == "/admin/status":
            return {"ok": True}
        raise AssertionError(f"unexpected request: {method} {path}")


class LocalDebugLoopTest(unittest.TestCase):
    def test_browser_entries_bootstrap_fake_login_for_web_and_admin(self) -> None:
        config = local_debug.RuntimeConfig(
            workspace=Path("F:/Pullwise"),
            run_root=Path("F:/Pullwise/pullwise-server/.pullwise/local-debug/run"),
            server_port=18080,
            web_port=15173,
            admin_port=15174,
        )

        entries = local_debug.local_browser_entry_urls(config)
        opened: list[str] = []
        result = local_debug.open_local_browser_entries(
            entries,
            lambda url: opened.append(url) or True,
        )

        self.assertEqual(opened, [entries["web"], entries["admin"]])
        self.assertEqual(result, {"web": True, "admin": True})
        expected_redirects = {
            "web": "http://127.0.0.1:15173/dashboard/overview",
            "admin": "http://127.0.0.1:15174/workers",
        }
        for name, entry_url in entries.items():
            parsed = urllib.parse.urlparse(entry_url)
            self.assertEqual(parsed.scheme, "http")
            self.assertEqual(parsed.netloc, "127.0.0.1:18080")
            self.assertEqual(parsed.path, "/auth/github/callback")
            self.assertEqual(urllib.parse.parse_qs(parsed.query), {"redirectTo": [expected_redirects[name]]})

    def test_browser_open_failure_keeps_the_local_environment_usable(self) -> None:
        entries = {"web": "http://example.test/web", "admin": "http://example.test/admin"}
        attempted: list[str] = []

        def unavailable(url: str) -> bool:
            attempted.append(url)
            raise OSError("no desktop browser")

        result = local_debug.open_local_browser_entries(entries, unavailable)

        self.assertEqual(attempted, [entries["web"], entries["admin"]])
        self.assertEqual(result, {"web": False, "admin": False})

    def test_process_plan_starts_distinct_loopback_services_and_both_worker_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for project in ("pullwise-server", "pullwise-worker", "pullwise-admin", "pullwise-web"):
                (workspace / project).mkdir()
            run_root = workspace / "pullwise-server" / ".pullwise" / "local-debug" / "run"
            config = local_debug.RuntimeConfig(
                workspace=workspace,
                run_root=run_root,
                server_port=18080,
                web_port=15173,
                admin_port=15174,
                admin_email="local-admin@pullwise.test",
                profile_root=run_root / "worker-profiles",
            )

            initial = local_debug.initial_process_specs(config, "python", "npm")
            worker = local_debug.worker_process_specs(config, "node", "worker_1", "secret-token")

            self.assertEqual([item.name for item in initial], ["server", "web", "admin"])
            self.assertEqual([item.name for item in worker], ["worker-watcher", "worker-service"])
            self.assertIn("15173", initial[1].argv)
            self.assertIn("15174", initial[2].argv)
            self.assertEqual(initial[0].env["PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS"], "true")
            self.assertEqual(
                initial[0].env["PULLWISE_ALLOWED_ORIGINS"],
                "http://127.0.0.1:15173,http://127.0.0.1:15174",
            )
            self.assertEqual(worker[0].env["PULLWISE_WORKER_TOKEN"], "secret-token")

            public = local_debug.public_runtime_state(initial + worker, "worker_1")
            self.assertNotIn("secret-token", str(public))
            self.assertNotIn("PULLWISE_WORKER_TOKEN", str(public))

    def test_core_smoke_covers_user_admin_and_scan_create_cancel_flows(self) -> None:
        api = FakeApi()

        report = local_debug.run_core_smoke(api, "http://127.0.0.1:15173", "worker_1")

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["repository"]["fullName"], "acme/api")
        self.assertEqual(report["scan"]["status"], "cancelled")
        called_paths = [path for _method, path, _body in api.calls]
        self.assertIn("/auth/session", called_paths)
        self.assertIn("/repositories/sync", called_paths)
        self.assertIn("/scans/preflight", called_paths)
        self.assertIn("/scans", called_paths)
        self.assertIn("/scans/scan_1/cancel", called_paths)
        self.assertIn("/admin/workers", called_paths)
        self.assertIn("/admin/status", called_paths)


if __name__ == "__main__":
    unittest.main()
