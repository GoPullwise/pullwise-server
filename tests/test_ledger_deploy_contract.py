"""S01: static deployment guard and ledger contract checks; no Wrangler invocation."""

import json
import asyncio
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_cloudflare_github_identity_http import D1ShapedSQLite, Store
from pullwise_server.cloudflare_http_contract import handle_http_request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-cloudflare.sh"


class LedgerDeployContractTests(unittest.TestCase):
    def test_default_dry_run_rejects_placeholder_configuration(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--environment", "preview"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placeholder", result.stderr.lower())
        self.assertNotIn("migrations apply", result.stdout)

    def test_execute_requires_explicit_local_verification(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--environment", "production", "--execute"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local checks", result.stderr.lower())

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
            with sqlite3.connect(path) as database:
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
