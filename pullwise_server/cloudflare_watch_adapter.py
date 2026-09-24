"""Finite D1 creation command for a trusted, resolved public upstream watch."""
from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any

from .product_domain import context_hash, validate_watch_interests, watch_scope_key
from .product_dto_rules import watch_dto
from .product_entitlement_rules import entitlements_for_user


def _credential_guard(owner_id: str, proof: dict | None) -> tuple[str, tuple]:
    if proof is None:
        return ("EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u "
                "WHERE a.name='users' AND u.key=?)", (owner_id,))
    predicates = ["""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
        WHERE a.name='users' AND u.key=? AND u.value=?)"""]
    values: list = [owner_id, proof["user"]]
    if proof["token"]:
        key = proof["key"]
        predicates.append("""EXISTS(SELECT 1 FROM api_keys WHERE key_hash=?
            AND user_id=? AND scopes=? AND restrictions=?
            AND expires_at IS ? AND revoked_at IS NULL)""")
        values.extend((hashlib.sha256(proof["token"].encode()).hexdigest(),
                       owner_id, key["scopes"], key["restrictions"],
                       key["expires_at"]))
    else:
        predicates.append("""EXISTS(SELECT 1 FROM app_state
            WHERE name='sessions' AND payload=?)""")
        values.append(proof["sessions"])
    return " AND ".join(predicates), tuple(values)


class D1WatchTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def create_public_watch(self, *, owner_id: str,
                                  resolved_public_repository_id: str,
                                  interests: list[str], enabled: bool,
                                  analysis_enabled: bool, now: int,
                                  include_prerelease: bool = False,
                                  priority_order: int = 0,
                                  proof: dict | None = None,
                                  public_resolution: dict | None = None,
                                  idempotency: dict | None = None) -> dict:
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(resolved_public_repository_id, str)
                or not resolved_public_repository_id or type(now) is not int
                or type(enabled) is not bool or type(analysis_enabled) is not bool
                or type(include_prerelease) is not bool
                or type(priority_order) is not int or priority_order < 0):
            raise ValueError("invalid watch command")
        normalized = validate_watch_interests(interests)
        semantic_hash = context_hash(normalized)
        scope_key = watch_scope_key(owner_id=owner_id,
            target_repository_id=None,
            upstream_repository_id=resolved_public_repository_id)
        account = await self.binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
        if not account:
            raise ValueError("UNAUTHENTICATED")
        user = json.loads(account["snapshot"])
        entitlement = entitlements_for_user(user, timestamp=now)
        limit = entitlement["entitlements"]["activeWatchLimit"]
        existing = await self.binding.prepare("""SELECT id FROM update_watches
            WHERE watch_scope_key=? AND archived_at IS NULL""").bind(scope_key).first()
        if existing:
            raise ValueError("WATCH_ALREADY_EXISTS")
        if enabled:
            capacity = await self.binding.prepare("""SELECT COUNT(*) AS active_count
                FROM update_watches WHERE billing_owner_id=? AND enabled=1
                  AND archived_at IS NULL""").bind(owner_id).first()
            if capacity and int(capacity["active_count"]) >= limit:
                raise ValueError("WATCH_LIMIT_REACHED")
        control = await self.binding.prepare("""SELECT context_version,context_hash
            FROM watch_controls WHERE watch_scope_key=?""").bind(scope_key).first()
        version = (int(control["context_version"]) if control else 1)
        if control and control["context_hash"] != semantic_hash:
            version += 1
        guard_conditions = ["""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
            WHERE a.name='users' AND u.key=? AND u.value=?)""",
            "NOT EXISTS(SELECT 1 FROM update_watches WHERE watch_scope_key=? AND archived_at IS NULL)"]
        guard_values: list = [owner_id, account["snapshot"], scope_key]
        if proof is not None:
            credential_sql, credential_params = _credential_guard(owner_id, proof)
            guard_conditions.append(credential_sql)
            guard_values.extend(credential_params)
        if public_resolution is not None:
            guard_conditions.append("""EXISTS(SELECT 1 FROM public_upstream_proofs
                WHERE lookup_key=? AND github_repo_id=? AND full_name=?
                AND source_revision=? AND public_visible=1 AND private=0
                AND observed_at<=? AND valid_until>=?)""")
            guard_values.extend((public_resolution["lookup_key"],
                resolved_public_repository_id, public_resolution["full_name"],
                public_resolution["source_revision"], now, now))
        if idempotency is not None:
            guard_conditions.append("""NOT EXISTS(SELECT 1 FROM request_idempotency
                WHERE subject_id=? AND method='POST' AND path='/api/v1/watches'
                AND idempotency_key=? AND expires_at>?)""")
            guard_values.extend((owner_id, idempotency["key"], now))
        if enabled:
            guard_conditions.append("""(SELECT COUNT(*) FROM update_watches
                WHERE billing_owner_id=? AND enabled=1 AND archived_at IS NULL)<?""")
            guard_values.extend((owner_id, limit))
        if control is None:
            guard_conditions.append("NOT EXISTS(SELECT 1 FROM watch_controls WHERE watch_scope_key=?)")
            guard_values.append(scope_key)
        else:
            guard_conditions.append("""EXISTS(SELECT 1 FROM watch_controls
                WHERE watch_scope_key=? AND context_version=? AND context_hash=?)""")
            guard_values.extend((scope_key, control["context_version"], control["context_hash"]))
        statements = [self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN """ + " AND ".join(guard_conditions)
            + " THEN 1 ELSE 0 END)").bind(*guard_values)]
        interests_json = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
        if control is None:
            statements.append(self.binding.prepare("""INSERT INTO watch_controls(
                watch_scope_key,owner_id,target_repository_id,upstream_repository_id,
                context_version,context_hash,interests_json,created_at,updated_at)
                VALUES(?,?,NULL,?,?,?,?,?,?)""").bind(
                    scope_key, owner_id, resolved_public_repository_id,
                    version, semantic_hash, interests_json, now, now))
        elif control["context_hash"] != semantic_hash:
            statements.append(self.binding.prepare("""UPDATE watch_controls SET
                context_version=?,context_hash=?,interests_json=?,updated_at=?
                WHERE watch_scope_key=? AND context_version=? AND context_hash=?""").bind(
                    version, semantic_hash, interests_json, now, scope_key,
                    control["context_version"], control["context_hash"]))
            statements.append(self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""))
        statements.append(self.binding.prepare("""INSERT OR IGNORE INTO processing_controls(
            control_key,initial_backfill_state,updated_at)
            VALUES(?,'not_started',?)""").bind(scope_key, now))
        watch_id = f"watch_{uuid.uuid4().hex}"
        statements.append(self.binding.prepare("""INSERT INTO update_watches(
            id,watch_scope_key,owner_id,target_repository_id,upstream_repository_id,
            billing_owner_id,context_version,context_hash,interests_json,
            include_prerelease,priority_order,enabled,analysis_enabled,revision,
            created_at,updated_at)
            VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?,?,1,?,?)""").bind(
                watch_id, scope_key, owner_id, resolved_public_repository_id,
                owner_id, version, semantic_hash, interests_json,
                int(include_prerelease), priority_order, int(enabled),
                int(analysis_enabled), now, now))
        if idempotency is not None:
            watch = {"id": watch_id, "watchScopeKey": scope_key,
                "ownerId": owner_id, "targetRepositoryId": None,
                "upstreamRepositoryId": resolved_public_repository_id,
                "billingOwnerId": owner_id, "contextVersion": version,
                "contextHash": semantic_hash, "interests": list(normalized),
                "includePrerelease": include_prerelease,
                "priorityOrder": priority_order, "enabled": enabled,
                "analysisEnabled": analysis_enabled, "revision": 1}
            response = {**watch, "upstream": public_resolution["full_name"],
                "status": "active" if enabled else "paused", "lastSyncedAt": None,
                "links": {"self": f"/api/v1/watches/{watch_id}"},
                "requestId": idempotency["request_id"]}
            statements.extend([
                self.binding.prepare("""DELETE FROM request_idempotency
                    WHERE subject_id=? AND method='POST' AND path='/api/v1/watches'
                    AND idempotency_key=? AND expires_at<=?""").bind(
                        owner_id, idempotency["key"], now),
                self.binding.prepare("""INSERT INTO request_idempotency(
                    subject_id,method,path,idempotency_key,body_hash,state,
                    status_code,response_json,created_at,completed_at,expires_at)
                    VALUES(?,'POST','/api/v1/watches',?,?,'completed',201,?,?,?,?)""").bind(
                        owner_id, idempotency["key"], idempotency["body_hash"],
                        json.dumps(response, separators=(",", ":"), sort_keys=True),
                        now, now, now + 86400),
            ])
        statements.append(self.binding.prepare("DELETE FROM d1_command_guard"))
        await self.binding.batch(statements)
        if idempotency is not None:
            return response
        row = await self.binding.prepare("SELECT * FROM update_watches WHERE id=?").bind(watch_id).first()
        if row is None:
            raise RuntimeError("committed watch missing")
        return watch_dto(row)

    async def update_public_watch(self, *, owner_id: str, watch_id: str,
                                  expected_revision: int, changes: dict,
                                  now: int, proof: dict | None = None) -> dict:
        allowed = {"interests", "enabled", "analysisEnabled",
                   "includePrerelease", "priorityOrder"}
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(watch_id, str) or not watch_id
                or type(expected_revision) is not int or expected_revision < 1
                or type(now) is not int or not isinstance(changes, dict)
                or not changes or set(changes) - allowed):
            raise ValueError("invalid watch update")
        for name in ("enabled", "analysisEnabled", "includePrerelease"):
            if name in changes and type(changes[name]) is not bool:
                raise ValueError(f"{name} must be boolean")
        if ("priorityOrder" in changes and
                (type(changes["priorityOrder"]) is not int or changes["priorityOrder"] < 0)):
            raise ValueError("priorityOrder must be non-negative")
        watch = await self.binding.prepare("""SELECT * FROM update_watches
            WHERE id=? AND billing_owner_id=? AND archived_at IS NULL""").bind(
                watch_id, owner_id).first()
        if watch is None:
            raise ValueError("NOT_FOUND")
        if int(watch["revision"]) != expected_revision:
            raise ValueError("REVISION_MISMATCH")
        if watch["target_repository_id"] is not None:
            raise ValueError("private/shared watch update requires target authority")
        control = await self.binding.prepare("""SELECT context_version,context_hash
            FROM watch_controls WHERE watch_scope_key=?""").bind(
                watch["watch_scope_key"]).first()
        if control is None:
            raise ValueError("WATCH_CONTROL_NOT_FOUND")
        account = await self.binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
        if account is None:
            raise ValueError("UNAUTHENTICATED")
        entitlement = entitlements_for_user(json.loads(account["snapshot"]), timestamp=now)
        limit = entitlement["entitlements"]["activeWatchLimit"]
        interests = (validate_watch_interests(changes["interests"])
                     if "interests" in changes else tuple(json.loads(watch["interests_json"])))
        semantic_hash = context_hash(interests)
        changed_semantics = semantic_hash != watch["context_hash"]
        version = int(control["context_version"]) + int(changed_semantics)
        enabled = int(changes.get("enabled", bool(watch["enabled"])))
        analysis = int(changes.get("analysisEnabled", bool(watch["analysis_enabled"])))
        prerelease = int(changes.get("includePrerelease", bool(watch["include_prerelease"])))
        priority = changes.get("priorityOrder", int(watch["priority_order"]))
        interests_json = json.dumps(interests, separators=(",", ":"), ensure_ascii=False)
        predicates = ["""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
            WHERE a.name='users' AND u.key=? AND u.value=?)""",
            """EXISTS(SELECT 1 FROM update_watches
            WHERE id=? AND billing_owner_id=? AND revision=? AND archived_at IS NULL)""",
            """EXISTS(SELECT 1 FROM watch_controls
            WHERE watch_scope_key=? AND context_version=? AND context_hash=?)"""]
        params: list = [owner_id, account["snapshot"], watch_id, owner_id,
            expected_revision, watch["watch_scope_key"], control["context_version"],
            control["context_hash"]]
        if proof is not None:
            credential_sql, credential_params = _credential_guard(owner_id, proof)
            predicates.append(credential_sql)
            params.extend(credential_params)
        if enabled and not watch["enabled"]:
            predicates.append("""(SELECT COUNT(*) FROM update_watches
                WHERE billing_owner_id=? AND enabled=1 AND archived_at IS NULL
                  AND id!=?)<?""")
            params.extend((owner_id, watch_id, limit))
        statements = [self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN """ + " AND ".join(predicates)
            + " THEN 1 ELSE 0 END)").bind(*params)]
        job_condition = """j.job_type='analyze_source'
            AND j.state IN ('queued','retry_wait')
            AND EXISTS(SELECT 1 FROM source_contexts sc WHERE sc.watch_id=?
                AND sc.source_id=j.source_id AND sc.context_id=j.context_id)"""
        statements.append(self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM background_jobs j
                LEFT JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
                WHERE """ + job_condition + """ AND j.reservation_id IS NOT NULL
                  AND (l.reservation_id IS NULL OR l.state!='reserved'))
              AND NOT EXISTS(SELECT 1 FROM (
                SELECT l.billing_owner_id AS owner_id,l.period AS period,
                       COUNT(*) AS needed
                FROM processing_usage_ledger l JOIN background_jobs j
                  ON j.reservation_id=l.reservation_id
                WHERE l.state='reserved' AND """ + job_condition + """
                GROUP BY l.billing_owner_id,l.period) needed
                LEFT JOIN processing_usage_buckets b
                  ON b.billing_owner_id=needed.owner_id AND b.period=needed.period
                  AND b.metric='intelligent_processing'
                WHERE b.billing_owner_id IS NULL OR b.reserved<needed.needed)
              THEN 1 ELSE 0 END)""").bind(watch_id, watch_id))
        if changed_semantics:
            statements.extend([
                self.binding.prepare("""UPDATE watch_controls SET
                    context_version=?,context_hash=?,interests_json=?,updated_at=?
                    WHERE watch_scope_key=? AND context_version=? AND context_hash=?""").bind(
                        version, semantic_hash, interests_json, now,
                        watch["watch_scope_key"], control["context_version"],
                        control["context_hash"]),
                self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                    VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
                self.binding.prepare("""UPDATE processing_controls
                    SET config_stable_at=?,updated_at=? WHERE control_key=?""").bind(
                        now, now, watch["watch_scope_key"]),
            ])
        statements.extend([
            self.binding.prepare("""UPDATE update_watches SET
                context_version=?,context_hash=?,interests_json=?,enabled=?,
                analysis_enabled=?,include_prerelease=?,priority_order=?,
                revision=revision+1,updated_at=?
                WHERE id=? AND billing_owner_id=? AND revision=? AND archived_at IS NULL""").bind(
                    version, semantic_hash, interests_json, enabled, analysis,
                    prerelease, priority, now, watch_id, owner_id, expected_revision),
            self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
            self.binding.prepare("""UPDATE processing_usage_buckets AS b
                SET reserved=reserved-(SELECT COUNT(*) FROM processing_usage_ledger l
                    JOIN background_jobs j ON j.reservation_id=l.reservation_id
                    WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                      AND l.state='reserved' AND """ + job_condition + """),updated_at=?
                WHERE b.metric='intelligent_processing' AND EXISTS(
                    SELECT 1 FROM processing_usage_ledger l JOIN background_jobs j
                      ON j.reservation_id=l.reservation_id
                    WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                      AND l.state='reserved' AND """ + job_condition + ")").bind(
                          watch_id, now, watch_id),
            self.binding.prepare("""UPDATE processing_usage_ledger
                SET state='released',finished_at=? WHERE state='reserved'
                  AND reservation_id IN (SELECT j.reservation_id FROM background_jobs j
                      WHERE """ + job_condition + ")").bind(now, watch_id),
            self.binding.prepare("""UPDATE background_jobs AS j
                SET state='cancelled',claim_token=NULL,claimed_until=NULL,updated_at=?
                WHERE """ + job_condition).bind(now, watch_id),
            self.binding.prepare("""UPDATE discovery_targets
                SET configuration_epoch=MAX(configuration_epoch+1,?),
                    configuration_stamp=json_array(resource_id,?)
                WHERE resource_kind='watch' AND resource_id=?
                  AND billing_owner_id=?""").bind(
                    expected_revision + 1, expected_revision + 1, watch_id, owner_id),
            self.binding.prepare("""UPDATE source_contexts SET
                configuration_revision=COALESCE((SELECT configuration_epoch
                    FROM discovery_targets WHERE resource_kind='watch'
                      AND resource_id=? AND billing_owner_id=?),?),
                analysis_enabled=?,
                context_stale=CASE WHEN context_version!=? THEN 1 ELSE context_stale END,
                context_version=?,
                processing_status=CASE WHEN ?=0 THEN 'analysis_disabled'
                                       ELSE processing_status END,
                updated_at=? WHERE watch_id=? AND billing_owner_id=?""").bind(
                    watch_id, owner_id, expected_revision + 1,
                    int(bool(enabled and analysis)), version, version,
                    int(bool(enabled and analysis)), now, watch_id, owner_id),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
        await self.binding.batch(statements)
        row = await self.binding.prepare("SELECT * FROM update_watches WHERE id=?").bind(watch_id).first()
        if row is None:
            raise RuntimeError("committed watch missing")
        return watch_dto(row)

    async def archive_watch(self, *, owner_id: str, watch_id: str,
                            expected_revision: int, now: int,
                            proof: dict | None = None) -> None:
        if (not isinstance(owner_id, str) or not owner_id
                or not isinstance(watch_id, str) or not watch_id
                or type(expected_revision) is not int or expected_revision < 1
                or type(now) is not int):
            raise ValueError("invalid watch archive command")
        current = await self.binding.prepare("""SELECT id FROM update_watches
            WHERE id=? AND billing_owner_id=? AND revision=? AND archived_at IS NULL""").bind(
                watch_id, owner_id, expected_revision).first()
        if current is None:
            raise ValueError("REVISION_MISMATCH")
        job_condition = """j.job_type='analyze_source'
            AND j.state IN ('queued','running','retry_wait')
            AND EXISTS(SELECT 1 FROM source_contexts sc WHERE sc.watch_id=?
                AND sc.source_id=j.source_id AND sc.context_id=j.context_id)"""
        credential_sql, credential_params = _credential_guard(owner_id, proof)
        watch_guard = self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN EXISTS(SELECT 1 FROM update_watches w
                WHERE w.id=? AND w.billing_owner_id=? AND w.revision=?
                  AND w.archived_at IS NULL)
              AND """ + credential_sql + """
              THEN 1 ELSE 0 END)""").bind(
                  watch_id, owner_id, expected_revision, *credential_params)
        reservation_guard = self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM background_jobs j
                LEFT JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
                WHERE """ + job_condition + """ AND j.reservation_id IS NOT NULL
                  AND (l.reservation_id IS NULL OR l.state!='reserved'))
              AND NOT EXISTS(SELECT 1 FROM (
                SELECT l.billing_owner_id AS owner_id,l.period AS period,
                       COUNT(*) AS needed
                FROM processing_usage_ledger l JOIN background_jobs j
                  ON j.reservation_id=l.reservation_id
                WHERE l.state='reserved' AND """ + job_condition + """
                GROUP BY l.billing_owner_id,l.period) needed
                LEFT JOIN processing_usage_buckets b
                  ON b.billing_owner_id=needed.owner_id AND b.period=needed.period
                  AND b.metric='intelligent_processing'
                WHERE b.billing_owner_id IS NULL OR b.reserved<needed.needed)
              THEN 1 ELSE 0 END)""").bind(watch_id, watch_id)
        archive = self.binding.prepare("""UPDATE update_watches
            SET archived_at=?,enabled=0,analysis_enabled=0,
                revision=revision+1,updated_at=?
            WHERE id=? AND billing_owner_id=? AND revision=?
              AND archived_at IS NULL""").bind(
                now, now, watch_id, owner_id, expected_revision)
        changed_guard = self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)""")
        release_buckets = self.binding.prepare("""UPDATE processing_usage_buckets AS b
            SET reserved=reserved-(SELECT COUNT(*) FROM processing_usage_ledger l
                JOIN background_jobs j ON j.reservation_id=l.reservation_id
                WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                  AND l.state='reserved' AND """ + job_condition + """),updated_at=?
            WHERE b.metric='intelligent_processing' AND EXISTS(
                SELECT 1 FROM processing_usage_ledger l JOIN background_jobs j
                  ON j.reservation_id=l.reservation_id
                WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                  AND l.state='reserved' AND """ + job_condition + ")").bind(
                      watch_id, now, watch_id)
        release_ledger = self.binding.prepare("""UPDATE processing_usage_ledger
            SET state='released',finished_at=? WHERE state='reserved'
              AND reservation_id IN (SELECT j.reservation_id FROM background_jobs j
                  WHERE """ + job_condition + ")").bind(now, watch_id)
        cancel_jobs = self.binding.prepare("""UPDATE background_jobs AS j
            SET state='cancelled',claim_token=NULL,claimed_until=NULL,updated_at=?
            WHERE (""" + job_condition + """ OR
                (j.job_type='sync_watch' AND j.logical_key=?
                 AND j.state IN ('queued','running','retry_wait')))""").bind(
                     now, watch_id, f"sync_watch:{watch_id}")
        revoke_contexts = self.binding.prepare("""UPDATE source_contexts
            SET accessible=0,authorization_revision=authorization_revision+1,
                authorization_valid_until=?,analysis_enabled=0,context_stale=1,
                processing_status='analysis_disabled',updated_at=?
            WHERE watch_id=? AND billing_owner_id=?""").bind(
                now, now, watch_id, owner_id)
        revoke_target = self.binding.prepare("""UPDATE discovery_targets
            SET accessible=0,authorization_revision=authorization_revision+1,
                valid_until=?,configuration_epoch=configuration_epoch+1,
                configuration_stamp=NULL
            WHERE resource_kind='watch' AND resource_id=?
              AND billing_owner_id=?""").bind(now, watch_id, owner_id)
        clear_guard = self.binding.prepare("DELETE FROM d1_command_guard")
        await self.binding.batch([watch_guard, reservation_guard, archive,
            changed_guard, release_buckets, release_ledger, cancel_jobs,
            revoke_contexts, revoke_target, clear_guard])
