"""Export synthetic Server-owned account tables for local D1 HTTP validation."""
from __future__ import annotations

import os
import hashlib
import json
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
    "provider_attempts",
    "watch_controls",
    "update_watches",
    "d1_command_guard",
    "api_keys",
    "source_records",
    "source_versions",
    "source_contexts",
    "source_assessment_publications",
    "items",
    "item_versions",
    "item_handling_events",
    "background_jobs",
    "repository_services",
    "processing_controls",
    "discovery_targets",
)


def main() -> None:
    server_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(server_root))
    sys.path.insert(0, str(server_root / "tests"))
    from test_cloudflare_server_mapping import seed
    from pullwise_server.product_jobs import ProductJobScheduler

    output = Path(__file__).resolve().parent / ".wrangler" / "local-seed.sql"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as directory:
        fixture, _, _ = seed(Path(directory) / "synthetic.db")
        first_watch = fixture.store.create_watch(owner_id="owner",
            target_repository_id=None, upstream_repository_id="github:101",
            billing_owner_id="owner", interests=["OAuth"], enabled=True,
            analysis_enabled=False)
        fixture.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:102", billing_owner_id="owner",
            interests=["database"], enabled=True, analysis_enabled=False)
        with fixture.store._immediate() as db:
            db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'",
                (first_watch["id"],))
        item = fixture.store.create_item(context_id="repo:repo:pr",
            unit_type="pr_thread", unit_key="thread-local-http")
        sources = [dict(sourceId=source_id, sourceVersion=record["sourceVersion"],
                        sourceRevision=record["sourceRevision"])
                   for source_id, record in fixture.records.items()]
        fences = [dict(sourceId=source["sourceId"], contextId="repo:repo:pr",
                       contextVersion=1, configurationRevision=1,
                       authorizationRevision=1) for source in sources]
        fixture.store.publish_item_snapshot(item_id=item["id"],
            expected_item_revision=item["revision"], sources=sources,
            context_fences=fences, snapshot={"module": "pr", "title": "Synthetic follow-up"},
            observed_at=fixture.now)
        sync_job = ProductJobScheduler(fixture.store).request_manual_sync(
            resource_kind="watch", resource_id=first_watch["id"],
            requester_id="owner")
        with fixture.store._immediate() as db:
            db.execute("UPDATE background_jobs SET id='sync-local' WHERE id=?",
                (sync_job["id"],))
        with fixture.store._immediate() as db:
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES('sessions',?,?)",
                (json.dumps({"session-local": {"userId": "owner",
                    "expiresAt": fixture.now + 86400}}), fixture.now))
            db.execute("""CREATE TABLE IF NOT EXISTS api_keys(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,key_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL,expires_at INTEGER,restrictions TEXT NOT NULL,
                created_at INTEGER NOT NULL,last_used_at INTEGER,revoked_at INTEGER)""")
            token = "pwk_local_http_test"
            db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("key-local", "owner", "Synthetic", token[:16],
                 hashlib.sha256(token.encode()).hexdigest(),
                 '["profile:read","usage:read","watches:read","items:read"]', fixture.now + 86400,
                 json.dumps({"watchIds": [first_watch["id"]]}),
                 fixture.now, None, None))
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
