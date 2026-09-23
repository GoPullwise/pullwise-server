"""Successful processing history is owner-scoped and paginated without model work."""
from contextlib import closing
import pytest

from test_cloudflare_server_mapping import seed


def test_usage_events_exclude_reserved_and_other_owners_with_stable_pages(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("""UPDATE processing_usage_ledger SET state='consumed',finished_at=?
            WHERE charge_key='charge'""", (fixture.now,))
        db.execute("UPDATE processing_usage_buckets SET reserved=0,used=1")
        db.execute("""INSERT INTO processing_usage_ledger(
            charge_key,reservation_id,billing_owner_id,period,module,state,
            reserved_at,finished_at) VALUES('other','res-other','other','period','ci',
            'consumed',?,?)""", (fixture.now, fixture.now))
        db.execute("""INSERT INTO processing_usage_ledger(
            charge_key,reservation_id,billing_owner_id,period,module,state,
            reserved_at,finished_at) VALUES('owner-ci','res-ci','owner','period','ci',
            'consumed',?,?)""", (fixture.now, fixture.now))
    first = fixture.store.list_processing_usage_events("owner", limit=1)
    assert len(first["items"]) == 1
    assert first["items"][0]["id"] != "res-other"
    assert first["hasMore"] is True and first["nextCursor"]
    second = fixture.store.list_processing_usage_events("owner",
        cursor=first["nextCursor"], limit=1)
    assert second["hasMore"] is False and second["nextCursor"] is None
    assert {row["module"] for row in first["items"] + second["items"]} == {"pr", "ci"}
    assert [row["module"] for row in fixture.store.list_processing_usage_events(
        "owner", module="ci")["items"]] == ["ci"]
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        fixture.store.list_processing_usage_events("owner", module="ci",
            cursor=first["nextCursor"], limit=1)
    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        fixture.store.list_processing_usage_events("other",
            cursor=first["nextCursor"], limit=1)
