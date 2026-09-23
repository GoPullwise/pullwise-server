"""Candidate D1 Item reads retain the local REST visibility and handling DTO."""
import asyncio

from pullwise_server.cloudflare_item_read import D1ItemReads
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed, mapping, execute, claim_args, publication_args


def test_d1_item_list_detail_and_revoked_secondary_context_match_store(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    reader = D1ItemReads(D1ShapedSQLite(fixture.store))
    owner = "owner"
    listed = asyncio.run(reader.list_items_for_billing_owner(owner_id=owner, now=fixture.now))
    assert listed == fixture.store.list_items_for_billing_owner(owner)
    item_id = publication["item"]["id"]
    detail = asyncio.run(reader.list_items_for_billing_owner(owner_id=owner,
        now=fixture.now, item_id=item_id, include_history=True))
    assert detail == fixture.store.list_items_for_billing_owner(owner,
        item_id=item_id, include_history=True)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET accessible=0 WHERE source_id='2'")
    assert asyncio.run(reader.list_items_for_billing_owner(owner_id=owner,
        now=fixture.now)) == []


def test_item_read_withdraws_stale_secondary_source_and_authorization_fence(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    reader = D1ItemReads(D1ShapedSQLite(fixture.store))
    assert asyncio.run(reader.list_items_for_billing_owner(owner_id="owner",
        now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET authorization_revision=authorization_revision+1 WHERE source_id='2'")
    assert asyncio.run(reader.list_items_for_billing_owner(owner_id="owner",
        now=fixture.now)) == []
    assert fixture.store.list_items_for_billing_owner("owner") == []


def test_analysis_switch_keeps_saved_item_readable_without_new_publication(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    with fixture.store._immediate() as db:
        db.execute("""UPDATE source_contexts SET analysis_enabled=0,
            configuration_revision=configuration_revision+1
            WHERE source_id='2'""")
    reader = D1ItemReads(D1ShapedSQLite(fixture.store))
    assert fixture.store.list_items_for_billing_owner("owner")
    assert asyncio.run(reader.list_items_for_billing_owner(owner_id="owner",
        now=fixture.now)) == fixture.store.list_items_for_billing_owner("owner")
