"""Atomic commercial limits inside the existing credential/mutation batch.

This does not replace the independent global S17/S18 validation budget. The
counter write rolls back with a failed mutation, audit or credential guard.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .account_cycle_rules import effective_user_plan
from .ledger_plan_policy import default_policy, usd_micros, JEV_RESERVATION_MICROUSD


class PlanLimitError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)

    def response(self):
        return self.status, {"error": {"code": self.code}}


_ERRORS = {"plan_project_limit": (403, "PROJECT_LIMIT"),
           "plan_clock_fence": (409, "QUOTA_WINDOW_CHANGED"),
           "plan_record_limit": (403, "RECORD_LIMIT"),
           "plan_write_rate": (429, "WRITE_RATE_LIMIT"),
           "plan_monthly_write_limit": (429, "MONTHLY_WRITE_LIMIT"),
           "plan_max_required": (403, "MAX_REQUIRED"),
           "plan_jev_budget_limit": (429, "JEV_BUDGET_LIMIT")}
_MUTATION = re.compile(r"^\s*(?:INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM)\s+"
    r"(ledger_projects|expense_categories|expenses|api_keys|expense_suggestion_budget|expense_suggestion_events|ledger_plan_usage)\b", re.I)

_USAGE_SQL = """INSERT INTO ledger_plan_usage(owner_id,projects,records,month,writes,minute,
    minute_writes,jev_reserved_microusd,project_cap,record_cap,minute_cap,month_cap,jev_cap,
    project_delta,record_delta,jev_delta,previous_month,previous_minute)
    VALUES(?,
      CASE WHEN EXISTS(SELECT 1 FROM ledger_plan_usage WHERE owner_id=?) THEN 0
        ELSE (SELECT COUNT(*) FROM ledger_projects WHERE owner_id=?) END + ?,
      CASE WHEN EXISTS(SELECT 1 FROM ledger_plan_usage WHERE owner_id=?) THEN 0
        ELSE (SELECT COUNT(*) FROM expenses WHERE owner_id=?) END + ?,
      ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(owner_id) DO UPDATE SET
      projects=ledger_plan_usage.projects+excluded.project_delta,
      records=ledger_plan_usage.records+excluded.record_delta,
      writes=CASE WHEN ledger_plan_usage.month=excluded.month
        THEN ledger_plan_usage.writes+excluded.writes ELSE excluded.writes END,
      minute_writes=CASE WHEN ledger_plan_usage.minute=excluded.minute
        THEN ledger_plan_usage.minute_writes+excluded.minute_writes ELSE excluded.minute_writes END,
      jev_reserved_microusd=CASE WHEN ledger_plan_usage.month=excluded.month
        THEN ledger_plan_usage.jev_reserved_microusd+excluded.jev_delta ELSE excluded.jev_delta END,
      previous_month=ledger_plan_usage.month,previous_minute=ledger_plan_usage.minute,
      month=excluded.month,minute=excluded.minute,
      project_cap=excluded.project_cap,record_cap=excluded.record_cap,
      minute_cap=excluded.minute_cap,month_cap=excluded.month_cap,jev_cap=excluded.jev_cap,
      project_delta=excluded.project_delta,record_delta=excluded.record_delta,jev_delta=excluded.jev_delta"""


class PlanLimitedD1:
    def __init__(self, binding, *, policy=None, now):
        self.binding, self.plan_policy, self.now = binding, policy or default_policy(), now

    def prepare(self, sql):
        return _Prepared(self, sql)

    async def batch(self, statements):
        statements = list(statements)
        mutations = [(item, _MUTATION.match(item.sql)) for item in statements]
        mutations = [(item, match.group(1).lower()) for item, match in mutations if match]
        if any(table == "ledger_plan_usage" for _, table in mutations):
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        raw = [self.binding.prepare(item.sql).bind(*item.params) for item in statements]
        # Revoking a compromised key must remain possible even when its owner
        # exhausted commercial quota. The original credential fence still runs.
        if mutations and all(table == "api_keys" and re.match(
                r"^\s*UPDATE\s+api_keys\s+SET\s+revoked_at\s*=", item.sql, re.I)
                for item, table in mutations):
            return await self.binding.batch(raw)
        # Model events are internal bookkeeping. Their original credential
        # fence remains atomic and the underlying global D1 meter still runs,
        # but neither a usage UPSERT nor a business write charge is needed.
        if mutations and all(table == "expense_suggestion_events" for _, table in mutations):
            return await self.binding.batch(raw)
        if not mutations:
            return await self.binding.batch(raw)
        # Trusted fences contain the account ID and exact persisted user JSON.
        # Never derive the owner or paid plan from an HTTP input or API-key scope.
        owner = user = None
        for item in statements:
            if "u.value=?" in item.sql and len(item.params) >= 2:
                index = 4 if item.sql.lstrip().upper().startswith("UPDATE API_KEYS") else 1
                try:
                    saved = json.loads(item.params[index])
                except (IndexError, TypeError, ValueError):
                    continue
                if isinstance(saved, dict) and isinstance(saved.get("id"), str):
                    owner, user = saved["id"], saved
                    break
        if user is None:
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        plan = effective_user_plan(user, timestamp=self.now)
        limits = self.plan_policy[plan]
        project_delta = sum(table == "ledger_projects" and item.sql.lstrip().upper().startswith("INSERT")
                            for item, table in mutations)
        record_delta = sum(table == "expenses" and item.sql.lstrip().upper().startswith("INSERT")
                           for item, table in mutations)
        jev_delta = JEV_RESERVATION_MICROUSD if any(table == "expense_suggestion_budget" for _, table in mutations) else 0
        write_delta = int(any(table not in {"expense_suggestion_budget", "expense_suggestion_events"}
                              for _, table in mutations))
        if jev_delta and plan != "max":
            raise PlanLimitError(403, "MAX_REQUIRED")
        period = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y-%m")
        minute = self.now // 60
        usage = await self.binding.prepare("""SELECT projects,records,month,writes,minute,
            minute_writes,jev_reserved_microusd FROM ledger_plan_usage WHERE owner_id=?""").bind(owner).first()
        if usage and (period < usage["month"] or minute < usage["minute"]):
            raise PlanLimitError(409, "QUOTA_WINDOW_CHANGED")
        if usage:
            projects, records = usage["projects"], usage["records"]
        elif project_delta or record_delta:
            # Accounts created before the usage table retain their capacity
            # history, including archived projects and removed expenses.
            counts = await self.binding.prepare("""SELECT
                (SELECT COUNT(*) FROM ledger_projects WHERE owner_id=?) AS projects,
                (SELECT COUNT(*) FROM expenses WHERE owner_id=?) AS records""").bind(owner, owner).first()
            projects, records = counts["projects"], counts["records"]
        else:
            projects = records = 0
        month_writes = usage["writes"] if usage and usage["month"] == period else 0
        minute_writes = usage["minute_writes"] if usage and usage["minute"] == minute else 0
        # Expected business denials must not dispatch a failing native batch:
        # its missing accounting would permanently stop the preview journal.
        # The original UPSERT remains the authority for concurrent requests.
        if project_delta and projects + project_delta > limits["projects"]:
            raise PlanLimitError(403, "PROJECT_LIMIT")
        if record_delta and records + record_delta > limits["records"]:
            raise PlanLimitError(403, "RECORD_LIMIT")
        if write_delta and minute_writes + write_delta > limits["writesPerMinute"]:
            raise PlanLimitError(429, "WRITE_RATE_LIMIT")
        if write_delta and month_writes + write_delta > limits["writesPerMonth"]:
            raise PlanLimitError(429, "MONTHLY_WRITE_LIMIT")
        if jev_delta:
            cap = usd_micros(limits["jevMonthlyBudgetUsd"])
            if jev_delta > cap:
                raise PlanLimitError(429, "JEV_BUDGET_LIMIT")
            if usage and usage["month"] == period and usage["jev_reserved_microusd"] + jev_delta > cap:
                # Deterministic optional exhaustion must not dispatch a failing
                # D1 batch. The atomic UPSERT still fences concurrent admission.
                raise PlanLimitError(429, "JEV_BUDGET_LIMIT")
        counter = self.binding.prepare(_USAGE_SQL).bind(owner, owner, owner, project_delta,
            owner, owner, record_delta, period, write_delta, minute, write_delta, jev_delta,
            limits["projects"], limits["records"],
            limits["writesPerMinute"] if write_delta else max(limits["writesPerMinute"], minute_writes),
            limits["writesPerMonth"] if write_delta else max(limits["writesPerMonth"], month_writes),
            usd_micros(limits["jevMonthlyBudgetUsd"]), project_delta, record_delta, jev_delta, period, minute)
        try:
            # Keep original result indexes; domain callers rely on batch offsets.
            result = await self.binding.batch([raw[0], counter, *raw[1:]])
            return [result[0], *result[2:]]
        except Exception as error:
            for constraint, (status, code) in _ERRORS.items():
                if constraint in str(error):
                    raise PlanLimitError(status, code) from None
            raise


@dataclass(frozen=True)
class _Prepared:
    binding: PlanLimitedD1
    sql: str
    params: tuple = ()

    def bind(self, *params):
        return _Prepared(self.binding, self.sql, params)

    async def first(self):
        if _MUTATION.match(self.sql):
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        return await self.binding.binding.prepare(self.sql).bind(*self.params).first()

    async def all(self):
        if _MUTATION.match(self.sql):
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        return await self.binding.binding.prepare(self.sql).bind(*self.params).all()
