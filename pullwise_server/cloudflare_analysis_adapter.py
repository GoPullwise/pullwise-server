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

    async def reserve_processing_unit(self, *, owner_id: str, charge_key: str,
                                      reservation_id: str, module: str, now: int) -> dict:
        existing = await self.binding.prepare("""SELECT reservation_id,billing_owner_id,
            period,module,state FROM processing_usage_ledger WHERE charge_key=?""").bind(charge_key).first()
        account = await self.binding.prepare("""SELECT u.value AS snapshot,
            authority.revision AS account_revision,authority.period AS period
            FROM account_entitlement_authority authority
            JOIN app_state a ON a.name='users'
            JOIN json_each(a.payload) u ON u.key=authority.owner_id
            WHERE authority.owner_id=?""").bind(owner_id).first()
        if not account:
            raise ValueError("persisted reservation account is missing")
        if existing:
            if (existing["billing_owner_id"] != owner_id or existing["module"] != module
                    or (existing["state"] in {"reserved", "consumed"}
                        and existing["period"] != account["period"])):
                raise ValueError("CHARGE_KEY_CONFLICT")
            if existing["state"] in {"reserved", "consumed"}:
                await execute_d1_batch(self.binding, mapping.confirm_existing_reservation(
                    charge_key=charge_key, reservation_id=existing["reservation_id"],
                    owner_id=owner_id, period=existing["period"], module=module,
                    state=existing["state"]))
                return {"chargeKey": charge_key, "reservationId": existing["reservation_id"],
                        "state": existing["state"], "reused": True}
            commands = mapping.reserve_released_processing_unit(owner_id=owner_id,
                account_snapshot=account["snapshot"], account_revision=account["account_revision"],
                charge_key=charge_key, reservation_id=reservation_id, module=module, now=now)
        else:
            commands = mapping.reserve_first_processing_unit(owner_id=owner_id,
                account_snapshot=account["snapshot"], account_revision=account["account_revision"],
                charge_key=charge_key, reservation_id=reservation_id, module=module, now=now)
        await execute_d1_batch(self.binding, commands)
        return {"chargeKey": charge_key, "reservationId": reservation_id,
                "state": "reserved", "reused": False}

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

    async def enqueue_first_analysis_job(self, *, job_id: str, logical_key: str,
                                         source_id: str, context_id: str,
                                         reservation_id: str, trigger: str, now: int,
                                         global_active_limit: int,
                                         owner_active_limit: int) -> dict:
        commands = mapping.enqueue_first_analysis_job(
            job_id=job_id, logical_key=logical_key, source_id=source_id,
            context_id=context_id, reservation_id=reservation_id,
            trigger=trigger, now=now, global_active_limit=global_active_limit,
            owner_active_limit=owner_active_limit)
        await execute_d1_batch(self.binding, commands)
        row = await self.binding.prepare("SELECT id FROM background_jobs WHERE id=?").bind(job_id).first()
        return {"rejected": row is None}

    async def claim_due_analysis(self, *, now: int, token: str,
                                 global_monthly_limit: int,
                                 owner_rolling_limit: int,
                                 global_rolling_limit: int) -> dict | None:
        if not isinstance(token, str) or not token:
            raise ValueError("trusted claim token is required")
        due = await self.binding.prepare("""SELECT job.id,
            EXISTS(SELECT 1 FROM source_records source
                JOIN source_contexts context ON context.source_id=source.source_id
                    AND context.context_id=job.context_id
                JOIN processing_usage_ledger ledger ON ledger.reservation_id=job.reservation_id
                WHERE source.source_id=job.source_id
                AND source.processing_mode='model' AND source.lifecycle='active'
                AND source.latest_version=job.source_version_id
                AND source.source_revision=job.source_revision
                AND context.accessible=1 AND context.analysis_enabled=1 AND context.context_stale=0
                AND context.authorization_valid_until>=?
                AND context.authorization_revision=job.authorization_revision
                AND context.configuration_revision=job.configuration_revision
                AND context.context_version=job.context_version
                AND context.billing_owner_id=job.billing_owner_id
                AND ledger.billing_owner_id=job.billing_owner_id AND ledger.state='reserved') AS valid_binding,
            EXISTS(SELECT 1 FROM account_entitlement_authority authority
                JOIN processing_usage_ledger ledger ON ledger.reservation_id=job.reservation_id
                WHERE authority.owner_id=job.billing_owner_id AND authority.dirty=0
                AND authority.period_start<=? AND authority.valid_until>?
                AND ledger.period=authority.period) AS account_ready
            FROM background_jobs job
            LEFT JOIN analysis_claim_owners fairness ON fairness.billing_owner_id=job.billing_owner_id
            WHERE job.job_type='analyze_source' AND job.attempt<3
            AND ((job.state IN ('queued','retry_wait') AND COALESCE(job.next_attempt_at,0)<=?)
                OR (job.state='running' AND COALESCE(job.claimed_until,0)<=?))
            ORDER BY COALESCE(fairness.last_claim_order,0),job.rowid LIMIT 16""").bind(
                now, now, now, now, now).all()
        for row in due.results:
            job_id = row["id"]
            if not row["valid_binding"]:
                await execute_d1_batch(self.binding,
                    mapping.terminate_invalid_due_job(job_id=job_id, now=now))
                continue
            if not row["account_ready"]:
                continue
            await self.claim(job_id=job_id, token=token, now=now,
                global_monthly_limit=global_monthly_limit,
                owner_rolling_limit=owner_rolling_limit,
                global_rolling_limit=global_rolling_limit)
            return {"jobId": job_id, "token": token}
        return None

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
