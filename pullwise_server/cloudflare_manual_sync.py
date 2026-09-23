"""Trusted D1 enqueue for fact-only manual sync, currently unmounted."""
from __future__ import annotations

from typing import Any

from .product_dto_rules import job_dto


class D1ManualSyncTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def request(self, *, resource_kind: str, resource_id: str,
                      owner_id: str, job_id: str, now: int) -> dict:
        if (resource_kind != "watch" or any(not isinstance(value, str) or not value
                for value in (resource_id, owner_id, job_id))
                or type(now) is not int or now < 0):
            raise ValueError("invalid trusted manual sync request")
        job_type = "sync_watch"
        logical_key = f"{job_type}:{resource_id}"
        resource_guard = """EXISTS(SELECT 1 FROM update_watches w
            WHERE w.id=? AND w.owner_id=? AND w.billing_owner_id=?
              AND w.archived_at IS NULL AND w.enabled=1
              AND w.target_repository_id IS NULL)"""
        existing = await self.binding.batch([
            self.binding.prepare("SELECT 1 FROM app_state a,json_each(a.payload) u WHERE a.name='users' AND u.key=?").bind(owner_id),
            self.binding.prepare("SELECT 1 FROM update_watches w WHERE w.id=? AND w.owner_id=? AND w.billing_owner_id=? AND w.archived_at IS NULL AND w.enabled=1 AND w.target_repository_id IS NULL").bind(
                resource_id, owner_id, owner_id),
            self.binding.prepare("SELECT * FROM background_jobs WHERE logical_key=? AND state IN ('queued','running','retry_wait')").bind(logical_key),
        ])
        if not existing[0].results or not existing[1].results:
            raise ValueError("RESOURCE_NOT_AUTHORIZED")
        if existing[2].results:
            if existing[2].results[0]["requester_id"] != owner_id:
                raise ValueError("RESOURCE_NOT_AUTHORIZED")
            return job_dto(existing[2].results[0], reused=True)
        await self.binding.batch([
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                    WHERE a.name='users' AND u.key=?) AND """ + resource_guard + """
                    AND NOT EXISTS(SELECT 1 FROM background_jobs
                        WHERE logical_key=? AND state IN ('queued','running','retry_wait'))
                    THEN 1 ELSE 0 END)""").bind(owner_id, resource_id, owner_id, owner_id, logical_key),
            self.binding.prepare("""INSERT INTO background_jobs(
                id,job_type,logical_key,generation,trusted_trigger,requester_id,
                state,attempt,created_at,updated_at)
                SELECT ?,?,?,COALESCE(MAX(generation),0)+1,'manual_sync',?,
                    'queued',0,?,? FROM background_jobs WHERE logical_key=?""").bind(
                        job_id, job_type, logical_key, owner_id, now, now, logical_key),
            self.binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
        row = await self.binding.prepare("SELECT * FROM background_jobs WHERE id=?").bind(job_id).first()
        if row is None:
            raise RuntimeError("committed manual sync missing")
        return job_dto(row, reused=False)
