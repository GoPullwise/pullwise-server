"""Trusted public-price catalog projection must advance monotonically."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_billing_catalog_write import D1BillingCatalogTransactions
from pullwise_server.product_entitlement_rules import PLAN_ENTITLEMENTS
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


def catalog(amount):
    return {"provider": "creem", "enabled": True, "currency": "USD",
        "plans": [{"id": plan, "name": plan.title(),
            "entitlements": dict(PLAN_ENTITLEMENTS[plan]),
            "prices": {"month": {"amount": "0" if plan == "free" else amount,
                "currency": "USD", "interval": "month", "configured": True}}}
            for plan in ("free", "pro", "max")]}


def test_verified_catalog_stage_advances_revision_and_rejects_stale_price(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("""CREATE TABLE billing_public_catalog(id INTEGER PRIMARY KEY,
            payload_json TEXT,expires_at INTEGER,source_revision INTEGER,updated_at INTEGER)""")
    adapter = D1BillingCatalogTransactions(D1ShapedSQLite(fixture.store))
    assert asyncio.run(adapter.stage_verified_catalog(payload=catalog("29"),
        source_revision=1, now=fixture.now, expires_at=fixture.now + 3600))
    assert asyncio.run(adapter.stage_verified_catalog(payload=catalog("30"),
        source_revision=2, now=fixture.now, expires_at=fixture.now + 3600))
    with pytest.raises(ValueError, match="STALE_CATALOG"):
        asyncio.run(adapter.stage_verified_catalog(payload=catalog("28"),
            source_revision=1, now=fixture.now, expires_at=fixture.now + 3600))
    with closing(fixture.store.connect()) as db:
        row = db.execute("SELECT payload_json,source_revision FROM billing_public_catalog").fetchone()
    assert row[1] == 2 and json.loads(row[0])["plans"][1]["prices"]["month"]["amount"] == "30"


def test_verified_catalog_stage_rejects_concurrent_revision_change(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("""CREATE TABLE billing_public_catalog(id INTEGER PRIMARY KEY,
            payload_json TEXT,expires_at INTEGER,source_revision INTEGER,updated_at INTEGER)""")
        db.execute("INSERT INTO billing_public_catalog VALUES(1,?,?,1,?)",
            (json.dumps(catalog("29")), fixture.now + 3600, fixture.now))
    binding = D1ShapedSQLite(fixture.store)

    def competing_refresh():
        with fixture.store._immediate() as db:
            db.execute("UPDATE billing_public_catalog SET source_revision=3")

    binding.before_batch = competing_refresh
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1BillingCatalogTransactions(binding).stage_verified_catalog(
            payload=catalog("30"), source_revision=2,
            now=fixture.now, expires_at=fixture.now + 3600))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT source_revision FROM billing_public_catalog").fetchone()[0] == 3


def test_catalog_stage_from_injected_verified_products_keeps_product_id(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("""CREATE TABLE billing_public_catalog(id INTEGER PRIMARY KEY,
            payload_json TEXT,expires_at INTEGER,source_revision INTEGER,updated_at INTEGER)""")
    product = {"id": "prod-pro-month", "name": "Pullwise Pro",
        "description": "PR CI Updates", "price": 2900, "currency": "USD",
        "billing_type": "recurring", "billing_period": "every-month",
        "status": "active"}
    adapter = D1BillingCatalogTransactions(D1ShapedSQLite(fixture.store))
    assert asyncio.run(adapter.stage_from_products(
        configured_ids={"pro": ["prod-pro-month"], "max": []},
        fetched_products={"prod-pro-month": product}, source_revision=1,
        now=fixture.now, expires_at=fixture.now + 3600))
    with closing(fixture.store.connect()) as db:
        saved = json.loads(db.execute("SELECT payload_json FROM billing_public_catalog").fetchone()[0])
    assert saved["plans"][1]["prices"]["month"]["productId"] == "prod-pro-month"
