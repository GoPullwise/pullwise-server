"""Export synthetic Server-owned account tables for local D1 HTTP validation."""
from __future__ import annotations

import argparse
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
    "request_idempotency",
    "repository_services",
    "repository_directory",
    "public_upstream_proofs",
    "processing_controls",
    "discovery_targets",
    "billing_public_catalog",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-read", action="store_true")
    parser.add_argument("--repository-list", action="store_true")
    options = parser.parse_args()
    server_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(server_root))
    sys.path.insert(0, str(server_root / "tests"))
    from test_cloudflare_server_mapping import seed
    from pullwise_server.product_jobs import ProductJobScheduler
    from pullwise_server.product_entitlement_rules import PLAN_ENTITLEMENTS

    output = Path(__file__).resolve().parent / ".wrangler" / (
        "local-repository-list-seed.sql" if options.repository_list else
        "local-repository-seed.sql" if options.repository_read else "local-seed.sql")
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
            db.execute("""CREATE TABLE billing_public_catalog(
                id INTEGER PRIMARY KEY CHECK(id=1),payload_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL,source_revision INTEGER NOT NULL,
                updated_at INTEGER NOT NULL)""")
            catalog = {"provider": "disabled", "enabled": False,
                "currency": "USD", "plans": [
                    {"id": plan, "name": plan.title(),
                     "entitlements": dict(PLAN_ENTITLEMENTS[plan]),
                     "prices": {"month": {"amount": "0" if plan == "free" else None,
                                          "currency": "USD", "interval": "month",
                                          "configured": plan == "free"}}}
                    for plan in ("free", "pro", "max")]}
            db.execute("INSERT INTO billing_public_catalog VALUES(1,?,?,1,?)",
                (json.dumps(catalog), fixture.now + 3600, fixture.now))
            db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'",
                (first_watch["id"],))
            db.execute("""INSERT INTO processing_usage_buckets(
                billing_owner_id,period,metric,used,reserved,limit_value,updated_at)
                VALUES('owner','historical','intelligent_processing',2,0,5000,?)""",
                (fixture.now - 1000,))
            db.execute("""INSERT INTO processing_usage_ledger(
                charge_key,reservation_id,billing_owner_id,period,module,state,
                reserved_at,finished_at)
                VALUES('historical-unit','res-historical','owner','historical',
                       'updates','consumed',?,?)""",
                (fixture.now - 1200, fixture.now - 1000))
            db.execute("""INSERT INTO processing_usage_ledger(
                charge_key,reservation_id,billing_owner_id,period,module,state,
                reserved_at,finished_at)
                VALUES('historical-unit-2','res-historical-2','owner','historical',
                       'updates','consumed',?,?)""",
                (fixture.now - 1100, fixture.now - 900))
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
        if options.repository_read or options.repository_list:
            fixture.store.put_repository_service(repository_id="repo",
                installation_id="inst-1", billing_owner_id="owner", expected_revision=0,
                enabled=True, modules={"pr": True, "ci": False},
                analysis_enabled={"pr": False, "ci": False},
                allow_member_sync=False, default_assignee_id=None, priority_order=0)
            fixture.store.set_discovery_authorization(resource_kind="repository",
                resource_id="repo", module="pr", github_repository_id="101",
                installation_id="inst-1", app_id="synthetic-app",
                authorization_revision=1, accessible=True,
                valid_until=fixture.now + 300, observed_at=fixture.now)
        with fixture.store._immediate() as db:
            if options.repository_read or options.repository_list:
                users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
                users["owner"]["githubRepositoryAccess"] = {
                    "mode": "github-app", "authorizedUserId": "owner",
                    "authorizedGithubId": "author", "repositoriesNeedSync": False,
                    "repositoryItems": [{"id": "repo", "installationId": "inst-1"}]
                        + ([{"id": "repo-two", "installationId": "inst-1"}]
                           if options.repository_list else []),
                }
                db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))
                if options.repository_list:
                    account = db.execute("SELECT json_extract(payload,'$.owner') FROM app_state WHERE name='users'").fetchone()[0]
                    items = [{"id": "repo", "githubRepoId": "101", "fullName": "synthetic/repo",
                              "defaultBranch": "main", "private": True,
                              "installationId": "inst-1", "appId": "synthetic-app",
                              "appAccessible": True},
                             {"id": "repo-two", "githubRepoId": "102",
                              "fullName": "synthetic/other", "defaultBranch": "main",
                              "private": False, "installationId": "inst-1",
                              "appId": "synthetic-app", "appAccessible": True}]
                    db.execute("""INSERT INTO repository_directory VALUES(?,?,?,?,?,?,?)""",
                        ("owner", account, 1, fixture.now, fixture.now + 300, 2,
                         json.dumps(items)))
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
                 json.dumps(["profile:read", "usage:read", "watches:read", "items:read"]
                            + (["repositories:read"] if options.repository_read or options.repository_list else [])),
                 fixture.now + 86400,
                 json.dumps({"watchIds": [first_watch["id"]],
                             **({"repositoryIds": ["repo-two"]} if options.repository_list else
                                {"repositoryIds": ["repo"]} if options.repository_read else {})}),
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
