"""Copy exact Server-owned modules into the ignored local Worker package."""
from __future__ import annotations

import argparse
from pathlib import Path


MODULES = (
    "account_cycle_rules",
    "billing_account_rules",
    "cloudflare_account_adapter",
    "cloudflare_creem_handler",
    "cloudflare_d1_batch",
    "cloudflare_d1_mapping",
    "cloudflare_http_contract",
    "cloudflare_product_read",
    "cloudflare_webhook_receipts",
    "creem_event_rules",
    "creem_signature",
    "product_entitlement_rules",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    server_root = Path(__file__).resolve().parents[2]
    source = server_root / "pullwise_server"
    target = Path(__file__).resolve().parent / "src" / "pullwise_server"
    if not args.check:
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").write_bytes(b"")
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
