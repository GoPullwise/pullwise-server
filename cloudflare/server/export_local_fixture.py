"""Export synthetic Server-owned account tables for local D1 HTTP validation."""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile


TABLES = (
    "app_state",
    "account_entitlement_authority",
    "billing_webhook_receipts",
    "processing_usage_buckets",
    "processing_usage_ledger",
    "d1_command_guard",
)


def main() -> None:
    server_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(server_root))
    sys.path.insert(0, str(server_root / "tests"))
    from test_cloudflare_server_mapping import seed

    output = Path(__file__).resolve().parent / ".wrangler" / "local-seed.sql"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as directory:
        fixture, _, _ = seed(Path(directory) / "synthetic.db")
        with closing(fixture.store.connect()) as db:
            statements = []
            for table in TABLES:
                schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                                    (table,)).fetchone()
                if schema is None:
                    raise ValueError(f"synthetic table missing: {table}")
                statements.append(schema[0] + ";")
                cursor = db.execute(f'SELECT * FROM "{table}"')
                columns = [column[0] for column in cursor.description]
                names = ",".join(f'"{column}"' for column in columns)
                for row in cursor:
                    values = ",".join(db.execute("SELECT quote(?)", (value,)).fetchone()[0]
                                      for value in row)
                    statements.append(f'INSERT INTO "{table}"({names}) VALUES({values});')
    output.write_text("\n".join(statements) + "\n", encoding="utf-8")
    print("Exported synthetic account tables for local D1 only")


if __name__ == "__main__":
    main()
