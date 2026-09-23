"""Trusted D1 enqueue for fact-only manual sync, currently unmounted."""
from __future__ import annotations

import json
import hashlib
from typing import Any

from .product_dto_rules import job_dto
from .product_repository_access import account_can_read_repository_service
from .product_job_filters import job_resource_allowed
from .cloudflare_watch_adapter import _credential_guard


class D1ManualSyncTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def request(self, *, resource_kind: str, resource_id: str,
                      owner_id: str, job_id: str, now: int,
                      proof: dict | None = None,
                      idempotency_key: str | None = None,
                      request_id: str | None = None) -> dict:
        if (resource_kind not in {"watch", "repository"} or any(not isinstance(value, str) or not value
                for value in (resource_id, owner_id, job_id))
                or type(now) is not int or now < 0):
            raise ValueError("invalid trusted manual sync request")
        if idempotency_key is not None and (
                not isinstance(idempotency_key, str) or not idempotency_key
                or len(idempotency_key) > 128
                or not isinstance(request_id, str) or not request_id):
            raise ValueError("INVALID_CONFIGURATION")
        job_type = "sync_watch" if resource_kind == "watch" else "sync_repository"
        logical_key = f"{job_type}:{resource_id}"
        path = f"/api/v1/{'watches' if resource_kind == 'watch' else 'repositories'}/{resource_id}/sync"
        body_hash = hashlib.sha256(b"{}").hexdigest()
        idem_key = (owner_id, "POST", path, idempotency_key)

        def response(job: dict) -> dict:
            return {"id": job["id"], "operation": job_type,
                    "status": job["state"],
                    "links": {"self": f"/api/v1/jobs/{job['id']}"},
                    "requestId": request_id}
        if resource_kind == "watch":
            resource_query = """SELECT * FROM update_watches w
                WHERE w.id=? AND w.owner_id=? AND w.billing_owner_id=?
                  AND w.archived_at IS NULL AND w.enabled=1
                  AND w.target_repository_id IS NULL"""
            resource_params = (resource_id, owner_id, owner_id)
            resource_guard = "EXISTS(" + resource_query + ")"
        else:
            resource_query = """SELECT s.* FROM repository_services s
                WHERE s.repository_id=? AND s.billing_owner_id=?
                  AND s.enabled=1 AND s.status='active'
                  AND EXISTS(SELECT 1 FROM discovery_targets d
                    WHERE d.resource_kind='repository' AND d.resource_id=s.repository_id
                      AND d.repository_id=s.repository_id AND d.billing_owner_id=s.billing_owner_id
                      AND d.module IN ('pr','ci') AND d.installation_id=s.installation_id
                      AND d.accessible=1 AND d.valid_until>=?)"""
            resource_params = (resource_id, owner_id, now)
            resource_guard = "EXISTS(" + resource_query + ")"
        if proof is not None and proof.get("token"):
            try:
                scopes = json.loads(proof["key"]["scopes"])
                restrictions = json.loads(proof["key"]["restrictions"])
                expires_at = proof["key"]["expires_at"]
            except (KeyError, TypeError, ValueError):
                raise ValueError("RESOURCE_NOT_AUTHORIZED") from None
            required_read = "watches:read" if resource_kind == "watch" else "repositories:read"
            if (not isinstance(scopes, list) or not {required_read, "sync:write"}.issubset(scopes)
                    or not isinstance(restrictions, dict)
                    or expires_at is not None and (type(expires_at) is not int or expires_at < now)
                    or not job_resource_allowed(job_type=job_type,
                        resource_id=resource_id, target_repository_id=None,
                        restrictions=restrictions)):
                raise ValueError("RESOURCE_NOT_AUTHORIZED")
        elif proof is not None:
            try:
                session = json.loads(proof["sessions"])[proof["session_id"]]
                expires_at = session["expiresAt"]
            except (KeyError, TypeError, ValueError):
                raise ValueError("RESOURCE_NOT_AUTHORIZED") from None
            if (session.get("userId") != owner_id or type(expires_at) is not int
                    or expires_at < now):
                raise ValueError("RESOURCE_NOT_AUTHORIZED")
        credential_sql, credential_params = _credential_guard(owner_id, proof)
        existing = await self.binding.batch([
            self.binding.prepare("""SELECT u.value AS snapshot FROM app_state a,json_each(a.payload) u
                WHERE a.name='users' AND u.key=? AND """ + credential_sql).bind(
                    owner_id, *credential_params),
            self.binding.prepare(resource_query).bind(*resource_params),
            self.binding.prepare("SELECT * FROM background_jobs WHERE logical_key=? AND state IN ('queued','running','retry_wait')").bind(logical_key),
        ])
        if not existing[0].results or not existing[1].results:
            raise ValueError("RESOURCE_NOT_AUTHORIZED")
        account_snapshot = existing[0].results[0]["snapshot"]
        account = json.loads(account_snapshot)
        if (not isinstance(account, dict) or account.get("id") != owner_id
                or resource_kind == "repository" and not account_can_read_repository_service(
                    account, existing[1].results[0])):
            raise ValueError("RESOURCE_NOT_AUTHORIZED")
        if idempotency_key is not None:
            idem_snapshot = await self.binding.batch([
                self.binding.prepare("SELECT 1 WHERE " + credential_sql + " AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u WHERE a.name='users' AND u.key=? AND u.value=?)").bind(
                    *credential_params, owner_id, account_snapshot),
                self.binding.prepare(resource_query).bind(*resource_params),
                self.binding.prepare("""SELECT * FROM request_idempotency
                    WHERE subject_id=? AND method=? AND path=? AND idempotency_key=?""").bind(*idem_key),
            ])
            if not idem_snapshot[0].results or not idem_snapshot[1].results:
                raise ValueError("RESOURCE_NOT_AUTHORIZED")
            rows = idem_snapshot[2].results
            idem = rows[0] if rows else None
            if idem is not None and int(idem["expires_at"]) > now:
                if idem["body_hash"] != body_hash:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if idem["state"] != "completed":
                    raise ValueError("IDEMPOTENCY_IN_PROGRESS")
                return json.loads(idem["response_json"])
        idem_available = """NOT EXISTS(SELECT 1 FROM request_idempotency
            WHERE subject_id=? AND method=? AND path=? AND idempotency_key=?
              AND expires_at>?)"""
        expiry_key = (*idem_key, now)
        def idem_commands(payload: dict) -> list:
            return [
                self.binding.prepare("""DELETE FROM request_idempotency
                    WHERE subject_id=? AND method=? AND path=? AND idempotency_key=?
                      AND expires_at<=?""").bind(*expiry_key),
                self.binding.prepare("""INSERT INTO request_idempotency(
                    subject_id,method,path,idempotency_key,body_hash,state,
                    status_code,response_json,created_at,completed_at,expires_at)
                    VALUES(?,?,?,?,?,'completed',202,?,?,?,?)""").bind(
                        *idem_key, body_hash,
                        json.dumps(payload, separators=(",", ":"), sort_keys=True),
                        now, now, now + 86400),
            ]
        if existing[2].results:
            if existing[2].results[0]["requester_id"] != owner_id:
                raise ValueError("RESOURCE_NOT_AUTHORIZED")
            if idempotency_key is not None:
                active = existing[2].results[0]
                payload = response(active)
                await self.binding.batch([
                    self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                        VALUES(CASE WHEN """ + credential_sql + """
                          AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                            WHERE a.name='users' AND u.key=? AND u.value=?)
                          AND """ + resource_guard + """
                          AND EXISTS(SELECT 1 FROM background_jobs
                            WHERE id=? AND logical_key=? AND requester_id=?
                              AND state IN ('queued','running','retry_wait'))
                          AND """ + idem_available + """ THEN 1 ELSE 0 END)""").bind(
                              *credential_params, owner_id, account_snapshot,
                              *resource_params, active["id"], logical_key, owner_id,
                              *expiry_key),
                    *idem_commands(payload),
                    self.binding.prepare("DELETE FROM d1_command_guard"),
                ])
                return payload
            return job_dto(existing[2].results[0], reused=True)
        statements = [
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN """ + credential_sql + """
                    AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                        WHERE a.name='users' AND u.key=? AND u.value=?)
                    AND """ + resource_guard + """
                    AND NOT EXISTS(SELECT 1 FROM background_jobs
                        WHERE logical_key=? AND state IN ('queued','running','retry_wait'))
                    """ + ("AND " + idem_available if idempotency_key is not None else "") + """
                    THEN 1 ELSE 0 END)""").bind(*credential_params,
                        owner_id, account_snapshot, *resource_params, logical_key,
                        *(expiry_key if idempotency_key is not None else ())),
            self.binding.prepare("""INSERT INTO background_jobs(
                id,job_type,logical_key,generation,trusted_trigger,requester_id,
                state,attempt,created_at,updated_at)
                SELECT ?,?,?,COALESCE(MAX(generation),0)+1,'manual_sync',?,
                    'queued',0,?,? FROM background_jobs WHERE logical_key=?""").bind(
                        job_id, job_type, logical_key, owner_id, now, now, logical_key),
            self.binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        ]
        if idempotency_key is not None:
            statements.extend(idem_commands(response({"id": job_id, "state": "queued"})))
        statements.append(self.binding.prepare("DELETE FROM d1_command_guard"))
        await self.binding.batch(statements)
        row = await self.binding.prepare("SELECT * FROM background_jobs WHERE id=?").bind(job_id).first()
        if row is None:
            raise RuntimeError("committed manual sync missing")
        return (response(row) if idempotency_key is not None
                else job_dto(row, reused=False))

    async def request_idempotent(self, *, resource_kind: str, resource_id: str,
                                 owner_id: str, job_id: str, now: int,
                                 idempotency_key: str, request_id: str,
                                 proof: dict | None = None) -> dict:
        return await self.request(resource_kind=resource_kind,
            resource_id=resource_id, owner_id=owner_id, job_id=job_id,
            now=now, proof=proof, idempotency_key=idempotency_key,
            request_id=request_id)
