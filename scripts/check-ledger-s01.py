#!/usr/bin/env python3
"""Static S01 checks. Never opens D1 or invokes Wrangler."""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "cloudflare" / "server"
PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"


def validate_config(environment: str, allow_placeholders: bool) -> None:
    config_path = SERVER / f"wrangler.{environment}.jsonc"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("name") != f"pullwise-server-{environment}":
        raise ValueError("Worker name does not match environment")
    if config.get("main") != "src/entry.py" or not (SERVER / "src" / "entry.py").is_file():
        raise ValueError("Server Worker entry is missing")
    if config.get("workers_dev") is not False or config.get("preview_urls") is not False:
        raise ValueError("unreviewed public Worker URL")
    routes = config.get("routes", [])
    if len(routes) != 1:
        raise ValueError("one reviewed hostname is required")
    route = routes[0]
    pattern = route.get("pattern", "")
    host = pattern if route.get("custom_domain") is True else pattern.removesuffix("/*")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("route must target one exact hostname")
    if route.get("custom_domain") is not True:
        zone = route.get("zone_name", "")
        if not pattern.endswith("/*") or not zone or not (host == zone or host.endswith("." + zone)):
            raise ValueError("reviewed zone route is required")
    vars_ = config.get("vars", {})
    if vars_.get("PULLWISE_D1_ACCESS_ENABLED") != "0":
        raise ValueError("remote D1 access must remain paused")
    if config.get("triggers", {}).get("crons"):
        raise ValueError("cron is forbidden during bounded validation")
    app_url = vars_.get("PULLWISE_APP_URL", "")
    allowed = vars_.get("PULLWISE_ALLOWED_ORIGINS", "")
    parsed_app = urlparse(app_url)
    if not host or parsed_app.scheme != "https" or not parsed_app.hostname or allowed != app_url:
        raise ValueError("HTTPS app URL and matching allowed origin are required")
    if vars_.get("PULLWISE_MODE") != environment or vars_.get("PULLWISE_COOKIE_SAME_SITE") != "None":
        raise ValueError("environment or Cookie origin policy is missing")
    databases = config.get("d1_databases", [])
    if len(databases) != 1 or databases[0].get("binding") != "DB":
        raise ValueError("one DB binding is required")
    database = databases[0]
    if database.get("migrations_dir") != "migrations" or not database.get("database_name"):
        raise ValueError("D1 migration layout is missing")
    database_id = database.get("database_id", "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", database_id):
        raise ValueError("D1 database ID must be a UUID")
    if not allow_placeholders and (
        database_id == PLACEHOLDER_ID
        or ".invalid" in host
        or ".invalid" in parsed_app.hostname
    ):
        raise ValueError("placeholder database ID or domain remains")
    if any(key for key in vars_ if "SECRET" in key or "TOKEN" in key or "PRIVATE_KEY" in key):
        raise ValueError("secrets must not appear in Wrangler vars")


def validate_contract() -> None:
    import yaml

    contract = yaml.safe_load((ROOT / "openapi" / "ledger-v1.yaml").read_text(encoding="utf-8"))
    if contract.get("openapi") != "3.1.0":
        raise ValueError("ledger OpenAPI version missing")
    operations = [operation for path in contract["paths"].values() for method, operation in path.items()
                  if method in {"get", "post", "patch", "delete"}]
    ids = [operation["operationId"] for operation in operations]
    if len(ids) != len(set(ids)) or any("x-pullwise-scope" not in op for op in operations):
        raise ValueError("operation IDs or scope declarations invalid")
    if not contract["components"]["securitySchemes"]["bearerKey"]["scheme"] == "bearer":
        raise ValueError("Bearer API key contract missing")

    def visit(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "$ref" and nested.startswith("#/components/"):
                    target = contract
                    for segment in nested[2:].split("/"):
                        target = target[segment]
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(contract)
    migrations = list((SERVER / "migrations").glob("*.sql"))
    if not migrations or any(not sqlite3.complete_statement(path.read_text(encoding="utf-8"))
                             for path in migrations):
        raise ValueError("ledger migration is absent or incomplete")
    with sqlite3.connect(":memory:") as database:
        for path in sorted(migrations):
            database.executescript(path.read_text(encoding="utf-8"))
        required = {"ledger_projects", "expenses", "expense_events", "app_state",
                    "api_keys", "billing_webhook_receipts", "billing_public_catalog"}
        actual = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= actual:
            raise ValueError("ledger runtime migration tables are missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=("preview", "production"))
    parser.add_argument("--allow-placeholders", action="store_true")
    args = parser.parse_args()
    try:
        for environment in ((args.environment,) if args.environment else ("preview", "production")):
            validate_config(environment, args.allow_placeholders)
        validate_contract()
    except (ValueError, KeyError, OSError, ImportError) as exc:
        print(f"S01 check failed: {exc}", file=sys.stderr)
        return 1
    print("S01 static checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
