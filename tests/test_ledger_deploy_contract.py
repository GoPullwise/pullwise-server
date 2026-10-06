"""S01: static deployment guard and ledger contract checks; no Wrangler invocation."""

import json
import asyncio
import sqlite3
import subprocess
import os
import sys
import shutil
import shlex
import importlib.util
from unittest.mock import patch
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from test_cloudflare_github_identity_http import D1ShapedSQLite, Store
from pullwise_server.cloudflare_http_contract import handle_http_request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-cloudflare.sh"
BASH = shutil.which("bash") or "bash"


class LedgerDeployContractTests(unittest.TestCase):
    def test_active_preview_retains_budget_and_database_isolation(self):
        spec = importlib.util.spec_from_file_location("preview_checker", ROOT / "scripts/check-ledger-s01.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        config = json.loads((ROOT / "cloudflare/server/wrangler.preview.jsonc").read_text())
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "src").mkdir()
            (folder / "src/entry.py").touch()
            path = folder / "wrangler.preview.jsonc"
            path.write_text(json.dumps(config))
            with patch.object(checker, "SERVER", folder):
                checker.validate_config("preview", allow_placeholders=False)
                config["d1_databases"][0]["database_id"] = "80a29a0d-5699-449f-9541-a01dc461ca9d"
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    checker.validate_config("preview", allow_placeholders=False)
    def test_cloudflare_build_static_checks_use_managed_python_with_sqlite(self):
        plan = json.loads((ROOT / "cloudflare/server/build-trigger-plan.json").read_text())
        command = next(command for command in plan["build_command"].split(" && ")
                       if "scripts/check-ledger-s01.py" in command)
        args = shlex.split(command)
        self.assertEqual(args[:2], ["uv", "run"])
        self.assertIn("--managed-python", args)
        self.assertEqual(args[args.index("--python") + 1], "3.10.12")
        self.assertEqual(args[args.index("--with") + 1], "PyYAML==6.0.3")
        self.assertIn("--no-project", args)

    def test_remote_config_rejects_invalid_d1_switch_and_cron(self):
        spec = importlib.util.spec_from_file_location("ledger_config_checker", ROOT / "scripts/check-ledger-s01.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        original = json.loads((ROOT / "cloudflare/server/wrangler.production.jsonc").read_text())
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "src").mkdir()
            (folder / "src/entry.py").touch()
            for enabled, crons in [("invalid", []), (None, []), ("0", ["* * * * *"])]:
                config = json.loads(json.dumps(original))
                config["vars"]["PULLWISE_D1_ACCESS_ENABLED"] = enabled
                config["triggers"] = {"crons": crons}
                (folder / "wrangler.production.jsonc").write_text(json.dumps(config))
                with patch.object(checker, "SERVER", folder), self.assertRaises(ValueError):
                    checker.validate_config("production", allow_placeholders=True)

    def test_production_activation_config_accepts_only_its_isolated_runtime(self):
        spec = importlib.util.spec_from_file_location("activation_checker", ROOT / "scripts/check-ledger-s01.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        original = json.loads((ROOT / "cloudflare/server/wrangler.production.jsonc").read_text())
        self.assertEqual(original["vars"]["PULLWISE_D1_ACCESS_ENABLED"], "0")
        original["vars"]["PULLWISE_D1_ACCESS_ENABLED"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "src").mkdir()
            (folder / "src/entry.py").touch()
            path = folder / "wrangler.production.jsonc"
            path.write_text(json.dumps(original))
            with patch.object(checker, "SERVER", folder):
                checker.validate_config("production", allow_placeholders=False)
                for contamination in ("database", "host", "app", "callback", "provider", "coordinator", "preview_flag"):
                    config = json.loads(json.dumps(original))
                    if contamination == "database":
                        config["d1_databases"][0]["database_id"] = "e9dc3b89-f81f-4fce-87ef-d8797d879fb4"
                    elif contamination == "host":
                        config["routes"] = [{"pattern": "preview-api.pull-wise.com", "custom_domain": True}]
                    elif contamination == "app":
                        config["vars"]["PULLWISE_APP_URL"] = config["vars"]["PULLWISE_ALLOWED_ORIGINS"] = "https://preview.pull-wise.com"
                    elif contamination == "callback":
                        config["vars"]["PULLWISE_GITHUB_CALLBACK_URL"] = "https://preview.pull-wise.com/api/auth/github/callback"
                    elif contamination == "provider":
                        config["vars"]["PULLWISE_CREEM_API_BASE_URL"] = "https://test-api.creem.io"
                    elif contamination == "coordinator":
                        config["durable_objects"] = {"bindings": [{"name": "VALIDATION_BUDGET", "class_name": "ValidationBudget"}]}
                    else:
                        config["vars"]["PULLWISE_PREVIEW_PRODUCT_ENABLED"] = "1"
                    path.write_text(json.dumps(config))
                    with self.subTest(contamination=contamination), self.assertRaises(ValueError):
                        checker.validate_config("production", allow_placeholders=False)

    def test_reviewed_zone_route_preserves_existing_dns(self):
        spec = importlib.util.spec_from_file_location("ledger_config_checker", ROOT / "scripts/check-ledger-s01.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        config = json.loads((ROOT / "cloudflare/server/wrangler.production.jsonc").read_text())
        config["routes"] = [{"pattern": "api.pull-wise.com/*", "zone_name": "pull-wise.com"}]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "src").mkdir()
            (folder / "src/entry.py").touch()
            (folder / "wrangler.production.jsonc").write_text(json.dumps(config))
            with patch.object(checker, "SERVER", folder):
                checker.validate_config("production", allow_placeholders=True)
                config["routes"] = [{"pattern": "*.pull-wise.com/*", "zone_name": "pull-wise.com"}]
                (folder / "wrangler.production.jsonc").write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    checker.validate_config("production", allow_placeholders=True)

    def test_default_dry_run_does_not_run_or_propose_remote_migrations(self):
        result = subprocess.run(
            [BASH, SCRIPT.relative_to(ROOT).as_posix(), "--environment", "preview"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PULLWISE_PYTHON": Path(sys.executable).as_posix()},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertNotIn("d1 migrations apply", result.stdout)
        self.assertNotIn("d1 migrations apply", SCRIPT.read_text())

    def test_execute_requires_explicit_local_verification(self):
        result = subprocess.run(
            [BASH, SCRIPT.relative_to(ROOT).as_posix(), "--environment", "production", "--execute"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PULLWISE_PYTHON": Path(sys.executable).as_posix()},
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local checks", result.stderr.lower())

    def test_release_uses_pinned_python_package_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            repo = folder / "repo"
            scripts = repo / "scripts"
            worker = repo / "cloudflare/server"
            scripts.mkdir(parents=True)
            worker.mkdir(parents=True)
            shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
            (scripts / "check-ledger-s01.py").symlink_to(ROOT / "scripts/check-ledger-s01.py")
            (worker / "sync_server_modules.py").write_text("print('Fixture source sync')\n")
            cli = worker / "node_modules/wrangler/wrangler-dist/cli.js"
            cli.parent.mkdir(parents=True)
            cli.touch()
            fake_uv = folder / "uv"
            fake_uv.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$PWD\" \"$@\"\n")
            fake_uv.chmod(0o755)
            for environment, activate in (("preview", False), ("production", False), ("production", True)):
                args = [BASH, "scripts/deploy-cloudflare.sh", "--environment", environment,
                        "--execute", "--local-checks-passed"]
                if activate:
                    args.append("--activate-production")
                with self.subTest(environment=environment, activate=activate):
                    result = subprocess.run(args, cwd=repo,
                        text=True, capture_output=True, check=False,
                        env={**os.environ, "PULLWISE_PYTHON": Path(sys.executable).as_posix(),
                             "PATH": str(folder) + os.pathsep + os.environ["PATH"]})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(worker.as_posix(), result.stdout)
                    self.assertIn("run\n--frozen\n--python\n3.14.2\npywrangler\ndeploy\n--config\nwrangler."+environment+".jsonc", result.stdout)
                    self.assertEqual("--var\nPULLWISE_D1_ACCESS_ENABLED:1" in result.stdout, activate)
                    self.assertNotIn("d1 migrations apply", result.stdout)
            paused = json.loads((ROOT / "cloudflare/server/wrangler.production.jsonc").read_text())
            self.assertEqual(paused["vars"]["PULLWISE_D1_ACCESS_ENABLED"], "0")

    def test_production_activation_option_cannot_target_preview(self):
        result = subprocess.run([BASH, SCRIPT.relative_to(ROOT).as_posix(),
            "--environment", "preview", "--activate-production"], cwd=ROOT,
            text=True, capture_output=True, check=False,
            env={**os.environ, "PULLWISE_PYTHON": Path(sys.executable).as_posix()})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --environment production", result.stderr)

    def test_preview_and_production_are_separate_and_unconfigured(self):
        configs = []
        for environment in ("preview", "production"):
            path = ROOT / "cloudflare" / "server" / f"wrangler.{environment}.jsonc"
            config = json.loads(path.read_text())
            self.assertEqual(config["d1_databases"][0]["binding"], "DB")
            self.assertEqual(config["d1_databases"][0]["migrations_dir"], "migrations")
            self.assertFalse(config["workers_dev"])
            configs.append(config)
        self.assertNotEqual(configs[0]["name"], configs[1]["name"])
        self.assertNotEqual(configs[0]["d1_databases"][0]["database_name"], configs[1]["d1_databases"][0]["database_name"])

    def test_fresh_migrations_support_worker_health(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.db"
            with closing(sqlite3.connect(path)) as database:
                for migration in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
                    database.executescript(migration.read_text())
                tables = {row[0] for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertFalse(tables.intersection({"processing_usage_buckets",
                    "processing_usage_ledger", "provider_attempts"}))
            store = Store.__new__(Store)
            store.path = path
            async def read_body():
                return b""
            status, payload = asyncio.run(handle_http_request(
                method="GET", path="/health", headers={}, read_body=read_body,
                binding=D1ShapedSQLite(store), creem_secret="",
                configured_products={}, now=1_800_000_000))
            self.assertEqual(status, 200)
            self.assertTrue(payload["database"]["configured"])


if __name__ == "__main__":
    unittest.main()
