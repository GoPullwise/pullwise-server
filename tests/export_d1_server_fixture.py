"""Generate synthetic actual-schema data for local workerd, never export user DBs."""
import tempfile
from contextlib import closing
from pathlib import Path

from test_cloudflare_server_mapping import seed, claim_args, publication_args


def main():
    with tempfile.TemporaryDirectory() as directory:
        f, job, frozen = seed(Path(directory) / "synthetic.db")
        with f.store._immediate() as db:
            db.execute("""CREATE TABLE billing_public_catalog(
                id INTEGER PRIMARY KEY CHECK(id=1),payload_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL,source_revision INTEGER NOT NULL,
                updated_at INTEGER NOT NULL)""")
        publish = publication_args(f, job, frozen)
        claim = claim_args(f, job, frozen)
        f.source("3", "Queued source")
        names = ["watch_controls", "update_watches", "processing_controls",
                 "discovery_targets", "billing_public_catalog",
                 "source_records", "source_versions", "source_contexts", "assessments",
                 "source_assessment_publications", "items", "item_versions", "provider_attempts",
                 "processing_usage_buckets", "processing_usage_ledger", "background_jobs",
                 "analysis_claim_owners", "app_state", "account_entitlement_authority",
                 "d1_claim_authority", "billing_webhook_receipts", "d1_enqueue_decision"]
        schemas, inserts = [], []
        with closing(f.store.connect()) as db:
            for name in names:
                schemas.append(db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
                               .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
                rows = db.execute(f"SELECT * FROM {name}")
                columns = [column[0] for column in rows.description]
                for row in rows:
                    inserts.append((f"INSERT INTO {name}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(row)))
            indexes = db.execute("SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()
            schemas += [row[1].replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
                        .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1) for row in indexes if row[0] in names]
        output = Path(__file__).parents[1] / "cloudflare/probe/src/server_fixture.py"
        output.write_text("# Generated synthetic fixture; regenerate with tests/export_d1_server_fixture.py\nDATA = "
            + repr(dict(schemas=schemas, names=names, inserts=inserts, claim=claim,
                        publication=publish)) + "\n", encoding="utf-8")
        package = output.parent / "pullwise_server"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        for name in ("account_cycle_rules", "product_entitlement_rules", "billing_account_rules", "cloudflare_d1_batch",
                     "cloudflare_d1_mapping", "cloudflare_account_adapter", "cloudflare_analysis_adapter",
                     "creem_signature", "creem_event_rules", "cloudflare_webhook_receipts",
                     "cloudflare_creem_handler", "cloudflare_source_read",
                     "cloudflare_watch_adapter",
                     "cloudflare_session_adapter",
                     "cloudflare_oauth_state_adapter",
                     "cloudflare_billing_catalog_write", "product_public_catalog_rules",
                     "creem_public_catalog_rules",
                     "product_dto_rules", "update_filter", "product_domain"):
            source = Path(__file__).parents[1] / "pullwise_server" / f"{name}.py"
            (package / f"{name}.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print("Generated synthetic Server schema fixture")


if __name__ == "__main__":
    main()
