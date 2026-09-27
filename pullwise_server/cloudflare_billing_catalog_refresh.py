"""Refresh the public price catalog from configured Creem product IDs only."""
from __future__ import annotations

from .cloudflare_billing_catalog import read_public_plan
from .cloudflare_billing_catalog_write import D1BillingCatalogTransactions
from .cloudflare_creem_gateway import webhook_product_ids


async def read_or_refresh_catalog(*, binding, gateway, headers, products: dict, now: int):
    row = await binding.prepare("SELECT expires_at,source_revision FROM billing_public_catalog WHERE id=1").first()
    if row and int(row["expires_at"]) > now:
        return await read_public_plan(binding=binding, headers=headers, now=now)
    ids = webhook_product_ids(products)
    if set(ids) != {"pro", "max"} or not any(ids.values()):
        return 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
    fetched = {}
    for product_id in {item for values in ids.values() for item in values}:
        fetched[product_id] = await gateway.product(product_id)
    revision = max(now, int(row["source_revision"]) + 1) if row else max(1, now)
    await D1BillingCatalogTransactions(binding).stage_from_products(
        configured_ids=ids, fetched_products=fetched, source_revision=revision,
        now=now, expires_at=now + 3600)
    return await read_public_plan(binding=binding, headers=headers, now=now)
