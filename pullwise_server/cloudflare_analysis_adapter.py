"""Async D1 boundary for the finite claim and first-result publication slice.

Trusted scheduling supplies job, token, time and global/rolling limits. Account
snapshot, revision and owner monthly limit always come from durable D1 state.
"""
from __future__ import annotations

from typing import Any

from . import cloudflare_d1_mapping as mapping
from .cloudflare_d1_batch import execute_d1_batch


class D1AnalysisTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def reserve_first_processing_unit(self, *, owner_id: str, charge_key: str,
                                            reservation_id: str, module: str, now: int) -> Any:
        account = await self.binding.prepare("""SELECT u.value AS snapshot,
            authority.revision AS account_revision
            FROM account_entitlement_authority authority
            JOIN app_state a ON a.name='users'
            JOIN json_each(a.payload) u ON u.key=authority.owner_id
            WHERE authority.owner_id=?""").bind(owner_id).first()
        if not account:
            raise ValueError("persisted reservation account is missing")
        commands = mapping.reserve_first_processing_unit(owner_id=owner_id,
            account_snapshot=account["snapshot"], account_revision=account["account_revision"],
            charge_key=charge_key, reservation_id=reservation_id, module=module, now=now)
        return await execute_d1_batch(self.binding, commands)

    async def claim(self, *, job_id: str, token: str, now: int,
                    global_monthly_limit: int, owner_rolling_limit: int,
                    global_rolling_limit: int) -> Any:
        account = await self.binding.prepare("""SELECT u.value AS snapshot,
            authority.revision AS account_revision,
            authority.monthly_processing_limit AS monthly_processing_limit
            FROM background_jobs j
            JOIN account_entitlement_authority authority ON authority.owner_id=j.billing_owner_id
            JOIN app_state a ON a.name='users'
            JOIN json_each(a.payload) u ON u.key=j.billing_owner_id
            WHERE j.id=?""").bind(job_id).first()
        if not account:
            raise ValueError("persisted claim account is missing")
        commands = mapping.claim(job_id=job_id, token=token, now=now,
            account_snapshot=account["snapshot"], account_revision=account["account_revision"],
            owner_monthly_limit=account["monthly_processing_limit"] * 3,
            global_monthly_limit=global_monthly_limit,
            owner_rolling_limit=owner_rolling_limit,
            global_rolling_limit=global_rolling_limit)
        return await execute_d1_batch(self.binding, commands)

    async def publication(self, *, job_id: str, **payload: Any) -> Any:
        account = await self.binding.prepare("""SELECT u.value AS snapshot,
            claim.account_revision AS account_revision
            FROM background_jobs j
            JOIN d1_claim_authority claim ON claim.job_id=j.id
            JOIN app_state a ON a.name='users'
            JOIN json_each(a.payload) u ON u.key=j.billing_owner_id
            WHERE j.id=?""").bind(job_id).first()
        if not account:
            raise ValueError("persisted publication account is missing")
        commands = mapping.publication(job_id=job_id,
            account_snapshot=account["snapshot"],
            account_revision=account["account_revision"], **payload)
        return await execute_d1_batch(self.binding, commands)

    async def record_claim_failure(self, *, job_id: str, token: str, now: int,
                                   retryable: bool, next_attempt_at: int | None) -> Any:
        return await execute_d1_batch(self.binding, mapping.record_claim_failure(
            job_id=job_id, token=token, now=now,
            retryable=retryable, next_attempt_at=next_attempt_at))
