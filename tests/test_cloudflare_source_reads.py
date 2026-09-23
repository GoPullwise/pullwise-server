"""D1 Source reads must keep the SQLite REST publication fences."""
import asyncio

from pullwise_server.cloudflare_source_read import D1SourceReads
from pullwise_server.cloudflare_item_read import D1ItemReads
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


def test_shared_watch_parent_disable_hides_source_and_item(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    fixture.store.put_repository_service(repository_id="repo", installation_id="inst-1",
        billing_owner_id="owner", expected_revision=0, enabled=True,
        modules={"pr": True, "ci": False}, analysis_enabled={"pr": False, "ci": False},
        allow_member_sync=False, default_assignee_id=None, priority_order=0)
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id="repo",
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    source_reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    item_reader = D1ItemReads(D1ShapedSQLite(fixture.store))
    assert asyncio.run(source_reader.list_sources_for_billing_owner(
        owner_id="owner", source_id="1", now=fixture.now))
    visible_items = asyncio.run(item_reader.list_items_for_billing_owner(
        owner_id="owner", now=fixture.now))
    assert visible_items
    with fixture.store._immediate() as db:
        db.execute("UPDATE repository_services SET enabled=0,status='paused' WHERE repository_id='repo'")
    assert asyncio.run(source_reader.list_sources_for_billing_owner(
        owner_id="owner", source_id="1", now=fixture.now)) == []
    assert fixture.store.list_sources_for_billing_owner("owner", source_id="1") == []
    assert asyncio.run(item_reader.list_items_for_billing_owner(
        owner_id="owner", now=fixture.now)) == []
    assert fixture.store.list_items_for_billing_owner("owner") == []
    try:
        fixture.store.patch_item_handling(item_id=publication["item"]["id"],
            item_version=visible_items[0]["itemVersion"],
            expected_revision=visible_items[0]["revision"],
            actor_id="owner", disposition="done")
    except ValueError as error:
        assert str(error) == "STALE_ITEM"
    else:
        raise AssertionError("revoked shared-watch Item accepted handling")


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


def test_d1_source_read_retains_saved_result_after_analysis_config_change(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET configuration_revision=configuration_revision+1 WHERE source_id='2'")
    result = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="1", include_content=True, now=fixture.now))
    assert result[0]["contexts"][0]["assessments"]
    assert result == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)


def test_source_read_hides_publication_with_missing_dependency_fences(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_assessment_publications SET fences_json='[]' WHERE source_id='1'")
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    actual = asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="1", include_content=True, now=fixture.now))
    assert actual[0]["contexts"][0]["assessments"] == []
    assert actual == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)


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


def test_archived_watch_hides_its_release_context_even_before_auth_lease_expires(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    source = fixture.store.upsert_source_snapshot(source_id="release-archived", source_type="release",
        external_key="release:archived", repository_id="upstream",
        content={"body": "Private note"}, source_facts={"releaseId": "archived"},
        source_url="https://github.com/a/b/releases/tag/v1", processing_mode="model",
        completeness="complete", lifecycle="active", observed_at=fixture.now)
    fixture.store.set_source_context(source_id="release-archived", context_id="watch:archived",
        context_version=1, configuration_revision=1, authorization_revision=1,
        authorization_valid_until=fixture.now + 300, accessible=True,
        billing_owner_id="owner", watch_id=watch["id"], analysis_enabled=False)
    item = fixture.store.create_item(context_id="watch:archived",
        unit_type="update_release", unit_key="release-archived")
    fixture.store.publish_item_snapshot(item_id=item["id"],
        expected_item_revision=item["revision"],
        sources=[{"sourceId": "release-archived", "sourceVersion": source["sourceVersion"],
                  "sourceRevision": source["sourceRevision"]}],
        context_fences=[{"sourceId": "release-archived", "contextId": "watch:archived",
                         "contextVersion": 1, "configurationRevision": 1,
                         "authorizationRevision": 1}],
        snapshot={"module": "updates", "watchId": watch["id"]},
        observed_at=fixture.now)
    fixture.store.archive_watch(watch["id"], expected_revision=watch["revision"])
    reader = D1SourceReads(D1ShapedSQLite(fixture.store))
    assert fixture.store.list_sources_for_billing_owner("owner",
        source_id="release-archived") == []
    assert asyncio.run(reader.list_sources_for_billing_owner(owner_id="owner",
        source_id="release-archived", now=fixture.now)) == []
    assert fixture.store.list_items_for_billing_owner("owner", item_id=item["id"]) == []
    assert asyncio.run(D1ItemReads(D1ShapedSQLite(fixture.store)).list_items_for_billing_owner(
        owner_id="owner", item_id=item["id"], now=fixture.now)) == []


def test_archived_secondary_watch_hides_primary_saved_assessment(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='2'", (watch["id"],))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    fixture.store.archive_watch(watch["id"], expected_revision=watch["revision"])
    local = fixture.store.list_sources_for_billing_owner("owner", source_id="1", include_content=True)
    d1 = asyncio.run(D1SourceReads(D1ShapedSQLite(fixture.store)).list_sources_for_billing_owner(
        owner_id="owner", source_id="1", include_content=True, now=fixture.now))
    assert local[0]["contexts"][0]["assessments"] == []
    assert d1 == local
