"""Trusted monotonic publication of a verified public Creem price catalog."""
from __future__ import annotations

import json
from typing import Any

from .product_public_catalog_rules import catalog_payload as _catalog_payload
from .creem_public_catalog_rules import verified_public_catalog


class D1BillingCatalogTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def stage_from_products(self, *, configured_ids: dict,
                                  fetched_products: dict, source_revision: int,
                                  now: int, expires_at: int) -> bool:
        return await self.stage_verified_catalog(
            payload=verified_public_catalog(configured_ids, fetched_products),
            source_revision=source_revision, now=now,
            expires_at=expires_at)

    async def stage_verified_catalog(self, *, payload: dict,
                                     source_revision: int, now: int,
                                     expires_at: int) -> bool:
        if (not isinstance(payload, dict) or type(source_revision) is not int
                or source_revision < 1 or type(now) is not int
                or type(expires_at) is not int
                or not now < expires_at <= now + 86400
                or payload.get("provider") not in {"disabled", "creem"}
                or type(payload.get("enabled")) is not bool):
            raise ValueError("invalid trusted catalog")
        checked = _catalog_payload([{"payload_json": json.dumps(payload),
            "expires_at": expires_at, "source_revision": source_revision}], now)
        if checked is None:
            raise ValueError("invalid trusted catalog")
        checked.pop("page", None)
        catalog_json = json.dumps(checked, separators=(",", ":"), ensure_ascii=False)
        current = await self.binding.prepare("""SELECT payload_json,source_revision,
            expires_at FROM billing_public_catalog WHERE id=1""").first()
        if current is not None and source_revision <= int(current["source_revision"]):
            if (source_revision == int(current["source_revision"])
                    and catalog_json == current["payload_json"]
                    and expires_at == current["expires_at"]):
                return False
            raise ValueError("STALE_CATALOG")
        if current is None:
            command = self.binding.prepare("""INSERT INTO billing_public_catalog(
                id,payload_json,expires_at,source_revision,updated_at)
                SELECT 1,?,?,?,? WHERE NOT EXISTS(
                    SELECT 1 FROM billing_public_catalog WHERE id=1)""").bind(
                        catalog_json, expires_at, source_revision, now)
        else:
            command = self.binding.prepare("""UPDATE billing_public_catalog SET
                payload_json=?,expires_at=?,source_revision=?,updated_at=?
                WHERE id=1 AND source_revision=? AND payload_json=?""").bind(
                    catalog_json, expires_at, source_revision, now,
                    current["source_revision"], current["payload_json"])
        await self.binding.batch([command,
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("DELETE FROM d1_command_guard")])
        return True
