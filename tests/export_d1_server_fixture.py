"""Generate synthetic actual-schema data for local workerd, never export user DBs."""
import tempfile
from contextlib import closing
from pathlib import Path

from test_cloudflare_server_mapping import seed, claim_args, publication_args


def main():
    with tempfile.TemporaryDirectory() as directory:
        f, job, frozen = seed(Path(directory) / "synthetic.db")
        publish = publication_args(f, job, frozen)
        claim = claim_args(f, job, frozen)
        names = ["source_records", "source_versions", "source_contexts", "assessments",
                 "source_assessment_publications", "items", "item_versions", "provider_attempts",
                 "processing_usage_buckets", "processing_usage_ledger", "background_jobs",
                 "analysis_claim_owners", "app_state"]
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
            + repr(dict(schemas=schemas, names=names, inserts=inserts, claim=claim, publication=publish)) + "\n", encoding="utf-8")
        print("Generated synthetic Server schema fixture")


if __name__ == "__main__":
    main()
