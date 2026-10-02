"""Copy exact Server-owned modules into the ignored local Worker package."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


MODULES = (
    "account_cycle_rules",
    "api_key_dto_rules",
    "billing_account_rules",
    "billing_catalog_rules",
    "billing_projection",
    "cloudflare_account_adapter",
    "cloudflare_api_key_read",
    "cloudflare_api_key_write",
    "cloudflare_billing_catalog",
    "cloudflare_billing_catalog_refresh",
    "cloudflare_billing_catalog_write",
    "cloudflare_billing_mutations",
    "cloudflare_billing_read",
    "cloudflare_creem_gateway",
    "cloudflare_creem_handler",
    "cloudflare_d1_batch",
    "cloudflare_d1_mapping",
    "cloudflare_github_gateway",
    "cloudflare_github_identity_http",
    "cloudflare_http_contract",
    "cloudflare_jev_gateway",
    "cloudflare_ledger_api",
    "cloudflare_ledger_auth",
    "cloudflare_ledger_expenses",
    "cloudflare_ledger_profile",
    "cloudflare_ledger_reports",
    "cloudflare_ledger_suggestions",
    "cloudflare_native_d1",
    "cloudflare_oauth_state_adapter",
    "cloudflare_principal",
    "cloudflare_session_adapter",
    "cloudflare_webhook_receipts",
    "cloudflare_validation_budget",
    "cloudflare_preview_budget",
    "cloudflare_preview_schema",
    "cloudflare_plan_limits",
    "ledger_plan_policy",
    "ledger_money_totals",
    "json_input",
    "creem_event_rules",
    "creem_public_catalog_rules",
    "creem_signature",
    "typesafe_client",
)


def check_package_imports(source: Path) -> None:
    available = set(MODULES) | {"__init__"}
    for module in MODULES:
        tree = ast.parse((source / f"{module}.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            imported = {node.module.split(".")[0]} if node.module else {
                alias.name for alias in node.names}
            missing = imported - available
            if missing:
                raise SystemExit(f"{module} imports unpackaged modules: {', '.join(sorted(missing))}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    server_root = Path(__file__).resolve().parents[2]
    source = server_root / "pullwise_server"
    check_package_imports(source)
    target = Path(__file__).resolve().parent / "src" / "pullwise_server"
    if not args.check:
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").write_bytes(b"")
    stale = [item for item in target.glob("*.py")
             if item.stem not in MODULES and item.name != "__init__.py"]
    if args.check and stale:
        raise SystemExit("obsolete Worker modules: " + ", ".join(item.name for item in stale))
    if not args.check:
        for item in stale:
            item.unlink()
    for module in MODULES:
        source_bytes = (source / f"{module}.py").read_bytes()
        destination = target / f"{module}.py"
        if args.check:
            if not destination.is_file() or destination.read_bytes() != source_bytes:
                raise SystemExit(f"stale local Server module: {module}")
        else:
            destination.write_bytes(source_bytes)
    print("Local Server Worker modules match Server source" if args.check
          else "Copied Server-owned modules for local Worker runtime")


if __name__ == "__main__":
    main()
