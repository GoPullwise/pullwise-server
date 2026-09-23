"""Trusted first-generation RepositoryService D1 owner/capacity CAS."""
from __future__ import annotations

import json
from typing import Any

from .product_dto_rules import repository_service_dto
from .product_entitlement_rules import entitlements_for_user


_SOURCE_TYPES = {"pr": ("pr_state", "pr_comment", "pr_review_body", "pr_review_comment"),
                 "ci": ("ci_failure",) * 4}


class D1RepositoryTransactions:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def put_service(self, *, repository_id: str, installation_id: str,
                          owner_id: str, expected_revision: int,
                          enabled: bool, modules: dict,
                          analysis_enabled: dict, allow_member_sync: bool,
                          default_assignee_id: str | None,
                          priority_order: int, now: int) -> dict:
        if (not all(isinstance(value, str) and value for value in
                    (repository_id, installation_id, owner_id))
                or type(expected_revision) is not int or expected_revision < 0
                or type(now) is not int or type(enabled) is not bool
                or type(allow_member_sync) is not bool
                or type(priority_order) is not int or priority_order < 0
                or default_assignee_id is not None and
                    (not isinstance(default_assignee_id, str) or not default_assignee_id)):
            raise ValueError("invalid repository service command")
        if (not isinstance(modules, dict) or not isinstance(analysis_enabled, dict)
                or set(modules) != {"pr", "ci"}
                or set(analysis_enabled) != {"pr", "ci"}
                or any(type(value) is not bool for value in
                    (*modules.values(), *analysis_enabled.values()))
                or any(analysis_enabled[name] and not modules[name]
                       for name in ("pr", "ci"))):
            raise ValueError("invalid repository module switches")
        account = await self.binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
            json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
        if account is None:
            raise ValueError("UNAUTHENTICATED")
        entitlement = entitlements_for_user(json.loads(account["snapshot"]), timestamp=now)
        limit = entitlement["entitlements"]["activeRepositoryLimit"]
        current = await self.binding.prepare("""SELECT * FROM repository_services
            WHERE repository_id=?""").bind(repository_id).first()
        if current is None and expected_revision != 0:
            raise ValueError("REVISION_MISMATCH")
        if current is not None:
            if current["billing_owner_id"] != owner_id:
                raise ValueError("REPOSITORY_ALREADY_MANAGED")
            if int(current["revision"]) != expected_revision:
                raise ValueError("REVISION_MISMATCH")
        modules_json = json.dumps(modules, separators=(",", ":"), sort_keys=True)
        analysis_json = json.dumps(analysis_enabled, separators=(",", ":"), sort_keys=True)
        predicates = ["""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
            WHERE a.name='users' AND u.key=? AND u.value=?)"""]
        params: list = [owner_id, account["snapshot"]]
        if current is None:
            predicates.append("NOT EXISTS(SELECT 1 FROM repository_services WHERE repository_id=?)")
            params.append(repository_id)
        else:
            predicates.append("""EXISTS(SELECT 1 FROM repository_services
                WHERE repository_id=? AND billing_owner_id=? AND revision=?)""")
            params.extend((repository_id, owner_id, expected_revision))
        if enabled and (current is None or not current["enabled"]):
            predicates.append("""(SELECT COUNT(*) FROM repository_services
                WHERE billing_owner_id=? AND enabled=1 AND repository_id!=?)<?""")
            params.extend((owner_id, repository_id, limit))
        statements = [self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN """ + " AND ".join(predicates)
            + " THEN 1 ELSE 0 END)").bind(*params)]
        if current is None:
            statements.append(self.binding.prepare("""INSERT INTO repository_services(
                repository_id,installation_id,billing_owner_id,enabled,modules_json,
                analysis_enabled_json,allow_member_sync,default_assignee_id,
                priority_order,status,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""").bind(
                    repository_id, installation_id, owner_id, int(enabled),
                    modules_json, analysis_json, int(allow_member_sync),
                    default_assignee_id, priority_order,
                    "active" if enabled else "paused", now, now))
        else:
            statements.append(self.binding.prepare("""UPDATE repository_services SET
                installation_id=?,enabled=?,modules_json=?,analysis_enabled_json=?,
                allow_member_sync=?,default_assignee_id=?,priority_order=?,status=?,
                revision=revision+1,updated_at=?
                WHERE repository_id=? AND billing_owner_id=? AND revision=?""").bind(
                    installation_id, int(enabled), modules_json, analysis_json,
                    int(allow_member_sync), default_assignee_id, priority_order,
                    "active" if enabled else "paused", now,
                    repository_id, owner_id, expected_revision))
        statements.append(self.binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""))
        for module, source_types in _SOURCE_TYPES.items():
            context_id = f"repo:{repository_id}:{module}"
            source_condition = """j.job_type='analyze_source' AND j.state IN ('queued','retry_wait')
                AND j.context_id=? AND j.billing_owner_id=?
                AND EXISTS(SELECT 1 FROM source_records sr WHERE sr.source_id=j.source_id
                    AND sr.repository_id=? AND sr.source_type IN (?,?,?,?))"""
            scope = (context_id, owner_id, repository_id, *source_types)
            statements.append(self.binding.prepare("""INSERT INTO d1_command_guard(ok)
                VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM background_jobs j
                    LEFT JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
                    WHERE """ + source_condition + """ AND j.reservation_id IS NOT NULL
                    AND (l.reservation_id IS NULL OR l.state!='reserved'))
                  AND NOT EXISTS(SELECT 1 FROM (
                    SELECT l.billing_owner_id AS owner_id,l.period,COUNT(*) AS needed
                    FROM processing_usage_ledger l JOIN background_jobs j
                      ON j.reservation_id=l.reservation_id
                    WHERE l.state='reserved' AND """ + source_condition + """
                    GROUP BY l.billing_owner_id,l.period) needed
                    LEFT JOIN processing_usage_buckets b ON b.billing_owner_id=needed.owner_id
                      AND b.period=needed.period AND b.metric='intelligent_processing'
                    WHERE b.billing_owner_id IS NULL OR b.reserved<needed.needed)
                  THEN 1 ELSE 0 END)""").bind(*scope, *scope))
            statements.extend([
                self.binding.prepare("""UPDATE processing_usage_buckets AS b SET
                    reserved=reserved-(SELECT COUNT(*) FROM processing_usage_ledger l
                        JOIN background_jobs j ON j.reservation_id=l.reservation_id
                        WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                          AND l.state='reserved' AND """ + source_condition + """),updated_at=?
                    WHERE b.metric='intelligent_processing' AND EXISTS(
                        SELECT 1 FROM processing_usage_ledger l
                        JOIN background_jobs j ON j.reservation_id=l.reservation_id
                        WHERE l.billing_owner_id=b.billing_owner_id AND l.period=b.period
                          AND l.state='reserved' AND """ + source_condition + ")").bind(
                              *scope, now, *scope),
                self.binding.prepare("""UPDATE processing_usage_ledger SET
                    state='released',finished_at=? WHERE state='reserved'
                    AND reservation_id IN (SELECT j.reservation_id FROM background_jobs j
                        WHERE """ + source_condition + ")").bind(now, *scope),
                self.binding.prepare("""UPDATE background_jobs AS j SET state='cancelled',
                    claim_token=NULL,claimed_until=NULL,updated_at=? WHERE """ + source_condition).bind(
                        now, *scope),
                self.binding.prepare("""UPDATE discovery_targets SET
                    configuration_epoch=MAX(configuration_epoch+1,?),
                    configuration_stamp=json_array(resource_id,?)
                    WHERE resource_kind='repository' AND resource_id=?
                      AND billing_owner_id=? AND module=?""").bind(
                        expected_revision + 1, expected_revision + 1,
                        repository_id, owner_id, module),
                self.binding.prepare("""UPDATE source_contexts SET
                    configuration_revision=MAX(configuration_revision+?,?),
                    analysis_enabled=?,
                    processing_status=CASE WHEN ?=0 THEN 'analysis_disabled'
                                           ELSE processing_status END,
                    updated_at=? WHERE context_id=? AND billing_owner_id=?
                    AND source_id IN (SELECT source_id FROM source_records
                        WHERE repository_id=? AND source_type IN (?,?,?,?))""").bind(
                            int(current is not None), expected_revision + 1,
                            int(enabled and modules[module] and analysis_enabled[module]),
                            int(enabled and modules[module] and analysis_enabled[module]),
                            now, context_id, owner_id, repository_id, *source_types),
            ])
        statements.append(self.binding.prepare("DELETE FROM d1_command_guard"))
        await self.binding.batch(statements)
        row = await self.binding.prepare("SELECT * FROM repository_services WHERE repository_id=?").bind(
            repository_id).first()
        if row is None:
            raise RuntimeError("committed repository service missing")
        return repository_service_dto(row)
