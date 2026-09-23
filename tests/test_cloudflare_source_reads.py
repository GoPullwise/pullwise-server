"""D1 Source reads must keep the SQLite REST publication fences."""
import asyncio

from pullwise_server.cloudflare_source_read import D1SourceReads
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import (
    seed, mapping, execute, claim_args, publication_args,
)


def test_d1_source_list_and_detail_match_product_store(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    listed = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        now=fixture.now))
    assert listed == fixture.store.list_sources_for_billing_owner("owner")
    detail = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="1", include_content=True, now=fixture.now))
    assert detail == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)


def test_d1_source_read_expires_publication_after_secondary_source_change(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    current = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="1", include_content=True, now=fixture.now))
    assert current == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)
    assert current[0]["contexts"][0]["assessments"]
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_records SET source_revision=source_revision+1 WHERE source_id='2'")
    expired = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="1", include_content=True, now=fixture.now))
    assert expired == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)
    assert expired[0]["contexts"][0]["assessments"] == []


def test_d1_source_read_keeps_unclassified_release_without_item_and_fences_access(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    fixture.store.upsert_source_snapshot(source_id="release-1", source_type="release",
        external_key="release:1", repository_id="upstream", content={"body": "New release"},
        source_facts={"releaseId": "1"}, source_url="https://github.com/a/b/releases/tag/v1",
        processing_mode="model", completeness="partial", lifecycle="active",
        observed_at=fixture.now)
    fixture.store.set_source_context(source_id="release-1", context_id="watch:release-1",
        context_version=1, configuration_revision=1, authorization_revision=1,
        authorization_valid_until=fixture.now + 300, accessible=True,
        billing_owner_id="owner", watch_id=watch["id"], analysis_enabled=False,
        coverage={"coverage": "partial"})
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    found = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="release-1", now=fixture.now))
    assert found == fixture.store.list_sources_for_billing_owner("owner", source_id="release-1")
    assert found[0]["contexts"][0]["itemId"] is None
    assert found[0]["contexts"][0]["relevance"] is None
    assert found[0]["contexts"][0]["updateSignals"] == {}
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET accessible=0 WHERE source_id='release-1'")
    assert asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="release-1", now=fixture.now)) == []
