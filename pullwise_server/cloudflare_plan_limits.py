"""Atomic commercial limits inside the existing credential/mutation batch.

This does not replace the independent global S17/S18 validation budget. The
counter write rolls back with a failed mutation, audit or credential guard.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .account_cycle_rules import PAID_PLAN_IDS, effective_user_plan
from .ledger_plan_policy import default_policy, entitlements, usd_micros, JEV_RESERVATION_MICROUSD
from .cloudflare_state_records import record_name


class PlanLimitError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)

    def response(self):
        return self.status, {"error": {"code": self.code}}


ACTIVE_EXPENSE_SQL = """expenses.deleted_at IS NULL AND (expenses.target_kind='shared'
    OR EXISTS(SELECT 1 FROM ledger_projects active_project
        WHERE active_project.owner_id=expenses.owner_id AND active_project.id=expenses.project_id
            AND active_project.deleted_at IS NULL))"""


def active_expense_count_statement(binding, owner_id):
    return binding.prepare("SELECT COUNT(*) AS records FROM expenses WHERE owner_id=? AND " +
        ACTIVE_EXPENSE_SQL).bind(owner_id)


def capacity_usage_statement(binding, owner_id):
    """Projects retain cumulative capacity; expenses count current visible rows.

    Before the first metered mutation, the bounded owner-indexed fallback has
    exactly the same stored-row semantics as the quota initialization itself.
    Reads never rewrite legacy cumulative expense counters. The next ordinary
    mutation refreshes that counter from the same active predicate atomically.
    """
    return binding.prepare("""SELECT
        COALESCE((SELECT projects FROM ledger_plan_usage WHERE owner_id=?),
            (SELECT COUNT(*) FROM ledger_projects WHERE owner_id=?)) AS projects,
        (SELECT COUNT(*) FROM expenses WHERE owner_id=? AND """ + ACTIVE_EXPENSE_SQL + """ ) AS records,
        (SELECT month FROM ledger_plan_usage WHERE owner_id=?) AS jev_month,
        (SELECT jev_reserved_microusd FROM ledger_plan_usage WHERE owner_id=?) AS jev_reserved_microusd""").bind(
                owner_id, owner_id, owner_id, owner_id, owner_id)


def capacity_usage_payload(user, rows, *, now, policy=None):
    if len(rows) != 1:
        raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
    entitlement = entitlements(user, now=now, policy=policy)
    limits = entitlement["limits"]
    payload = {"workspaceId": user["id"], "jev": None}
    for field, column in (("projects", "projects"), ("expenseRecords", "records")):
        used = rows[0][column]
        if type(used) is not int or not 0 <= used <= 1000000:
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        limit = limits[field]
        payload[field] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}
    if entitlement["jev"]["eligible"]:
        # The existing reservation counter is a conservative allowance meter,
        # not a provider invoice. A GET projects the current UTC month without
        # initializing/resetting usage or carrying a previous month's balance.
        month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        saved_month = rows[0]["jev_month"]
        reserved = rows[0]["jev_reserved_microusd"]
        if saved_month is None and reserved is None:
            used = 0
        else:
            if (not isinstance(saved_month, str)
                    or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", saved_month)
                    or saved_month > month or type(reserved) is not int
                    or not 0 <= reserved <= 9007199254740991):
                raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
            used = reserved if saved_month == month else 0
        payload["jev"] = {"month": month, "currency": "USD", "usedMicrousd": used,
                          "limitMicrousd": usd_micros(entitlement["jev"]["monthlyBudgetUsd"])}
    return payload


_ERRORS = {"plan_project_limit": (403, "PROJECT_LIMIT"),
           "plan_clock_fence": (409, "QUOTA_WINDOW_CHANGED"),
           "plan_record_limit": (403, "RECORD_LIMIT"),
           "plan_write_rate": (429, "WRITE_RATE_LIMIT"),
           "plan_monthly_write_limit": (429, "MONTHLY_WRITE_LIMIT"),
           "plan_max_required": (403, "JEV_PLAN_REQUIRED"),
           "plan_jev_budget_limit": (429, "JEV_BUDGET_LIMIT")}
_MUTATION = re.compile(r"^\s*(?:INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM)\s+"
    r"(ledger_projects|expense_categories|expenses|api_keys|expense_suggestion_budget|expense_suggestion_events|ledger_plan_usage|workspace_members|workspace_invites|workspace_events|workspace_join_requests|ledger_project_repositories|expense_recurring_rules|expense_recurring_occurrences|expense_recurring_pending|ledger_activity_events)\b", re.I)
_USER_FENCE = re.compile(r"\bu\.name\s*=\s*\?\s+AND\s+u\.payload\s*=\s*\?", re.I)


def _fenced_user(statements):
    """Only an exact stored-user CAS can supply the commercial owner/plan."""
    for item in statements:
        if not re.search(r"\bapp_state\s+u\b", item.sql, re.I):
            continue
        for match in _USER_FENCE.finditer(item.sql):
            index = item.sql[:match.start()].count("?")
            try:
                key, snapshot = item.params[index:index + 2]
                saved = json.loads(snapshot)
                if (isinstance(saved, dict) and isinstance(saved.get("id"), str)
                        and key == record_name("users", saved["id"])):
                    return saved["id"], saved
            except (TypeError, ValueError, IndexError):
                continue
    return None, None

_USAGE_SQL = """INSERT INTO ledger_plan_usage(owner_id,projects,records,month,writes,minute,
    minute_writes,jev_reserved_microusd,project_cap,record_cap,minute_cap,month_cap,jev_cap,
    project_delta,record_delta,jev_delta,previous_month,previous_minute)
    VALUES(?,
      CASE WHEN EXISTS(SELECT 1 FROM ledger_plan_usage WHERE owner_id=?) THEN 0
        ELSE (SELECT COUNT(*) FROM ledger_projects WHERE owner_id=?) END,
      (SELECT COUNT(*) FROM expenses WHERE owner_id=? AND """ + ACTIVE_EXPENSE_SQL + """),
      ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(owner_id) DO UPDATE SET
      projects=ledger_plan_usage.projects+excluded.project_delta,
      records=excluded.records,
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
        return await self._batch(statements, reserve_jev=False)

    async def reserve_jev(self, statements):
        """Reserve one model call atomically with its current authority guards."""
        return await self._batch(statements, reserve_jev=True)

    async def _batch(self, statements, *, reserve_jev):
        statements = list(statements)
        if reserve_jev and any(_MUTATION.match(item.sql) for item in statements):
            # Reservations are a separate guarded admission step before the
            # provider call, never a business mutation or an event append.
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        # A closed Owner erasure carries its own exact capacity/one-write
        # counter in the guarded batch. Validate the entire canonical recipe;
        # never infer negative capacity from an arbitrary DELETE statement.
        from .cloudflare_project_erasure import erasure_candidate, recipe_identity
        if recipe_identity(statements, policy=self.plan_policy, now=self.now) is not None:
            return await self.binding.batch([
                self.binding.prepare(item.sql).bind(*item.params) for item in statements])
        if erasure_candidate(statements):
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        mutations = [(item, _MUTATION.match(item.sql)) for item in statements]
        mutations = [(item, match.group(1).lower()) for item, match in mutations if match]
        # Rolling activity is bookkeeping within the same fenced business batch.
        # Its append/expiry companions do not add a commercial write or disable
        # the original emergency pause/cancel exemptions. The native global
        # meter still accounts every projection row and index entry.
        mutations = [(item, table) for item, table in mutations if table not in {"ledger_activity_events", "expense_recurring_pending"}]
        if any(table == "ledger_plan_usage" for _, table in mutations):
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        raw = [self.binding.prepare(item.sql).bind(*item.params) for item in statements]
        # Revoking a compromised key must remain possible even when its owner
        # exhausted commercial quota. The original credential fence still runs.
        if mutations and all(table == "api_keys" and re.match(
                r"^\s*UPDATE\s+api_keys\s+SET\s+revoked_at\s*=", item.sql, re.I)
                for item, table in mutations):
            return await self.binding.batch(raw)
        # Stopping an automation remains possible after an account allowance
        # is exhausted. Only a literal pause/cancel update gets this exemption;
        # active edits, occurrences and generated expenses retain normal quota.
        # The original cookie/member/rule CAS guards and global meter remain.
        if mutations and all(table == "expense_recurring_rules" and re.match(
                r"^\s*UPDATE\s+expense_recurring_rules\s+SET\s+status\s*=\s*'(?:paused|canceled)'\s*,",
                item.sql, re.I) for item, table in mutations):
            return await self.binding.batch(raw)
        # Emergency membership/token revocation remains available after a
        # commercial allowance is exhausted. It still needs the original
        # current-authority fences and the independent global D1 budget.
        revocations = {
            "workspace_members": r"^\s*UPDATE\s+workspace_members\s+SET\s+removed_at\s*=",
            "workspace_invites": r"^\s*UPDATE\s+workspace_invites\s+SET\s+status\s*=\s*'revoked'",
        }
        if mutations and any(table in revocations for _, table in mutations) and all(
                table in revocations and re.match(revocations[table], item.sql, re.I)
                or table == "workspace_events" and len(item.params) >= 4
                   and item.params[3] in {"remove_member", "revoke_invite"}
                for item, table in mutations):
            return await self.binding.batch(raw)
        # Model events are internal bookkeeping. Their original credential
        # fence remains atomic and the underlying global D1 meter still runs,
        # but neither a usage UPSERT nor a business write charge is needed.
        if mutations and all(table == "expense_suggestion_events" for _, table in mutations):
            return await self.binding.batch(raw)
        if not mutations and not reserve_jev:
            return await self.binding.batch(raw)
        # Trusted fences contain the account ID and exact persisted user JSON.
        # Never derive the owner or paid plan from an HTTP input or API-key scope.
        owner, user = _fenced_user(statements)
        if user is None:
            raise PlanLimitError(503, "USAGE_GUARD_UNAVAILABLE")
        plan = effective_user_plan(user, timestamp=self.now)
        limits = self.plan_policy[plan]
        project_delta = sum(table == "ledger_projects" and item.sql.lstrip().upper().startswith("INSERT")
                            for item, table in mutations)
        record_insertions = sum(table == "expenses" and item.sql.lstrip().upper().startswith("INSERT")
                                for item, table in mutations)
        record_removals = sum(table == "expenses" and re.match(
            r"^\s*UPDATE\s+expenses\s+SET\s+deleted_at\s*=\s*\?", item.sql, re.I) is not None
            for item, table in mutations)
        record_delta = record_insertions - record_removals
        jev_delta = JEV_RESERVATION_MICROUSD if reserve_jev else 0
        write_delta = int(any(table not in {"expense_suggestion_budget", "expense_suggestion_events"}
                              for _, table in mutations))
        if jev_delta and plan not in PAID_PLAN_IDS:
            raise PlanLimitError(403, "JEV_PLAN_REQUIRED")
        period = datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y-%m")
        minute = self.now // 60
        usage = await self.binding.prepare("""SELECT projects,records,month,writes,minute,
            minute_writes,jev_reserved_microusd FROM ledger_plan_usage WHERE owner_id=?""").bind(owner).first()
        if usage and (period < usage["month"] or minute < usage["minute"]):
            raise PlanLimitError(409, "QUOTA_WINDOW_CHANGED")
        if usage:
            projects = usage["projects"]
        elif project_delta:
            # Accounts created before the usage table retain their capacity
            # history, including archived projects and removed expenses.
            counts = await self.binding.prepare("SELECT COUNT(*) AS projects FROM ledger_projects WHERE owner_id=?").bind(owner).first()
            projects = counts["projects"]
        else:
            projects = 0
        records = (await active_expense_count_statement(self.binding, owner).first())["records"]
        month_writes = usage["writes"] if usage and usage["month"] == period else 0
        minute_writes = usage["minute_writes"] if usage and usage["minute"] == minute else 0
        # Expected business denials must not dispatch a failing native batch:
        # its missing accounting would permanently stop the preview journal.
        # The original UPSERT remains the authority for concurrent requests.
        if project_delta and projects + project_delta > limits["projects"]:
            raise PlanLimitError(403, "PROJECT_LIMIT")
        if record_insertions and records + record_delta > limits["records"]:
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
        counter = self.binding.prepare(_USAGE_SQL).bind(owner, owner, owner, owner,
            period, write_delta, minute, write_delta, jev_delta,
            limits["projects"], limits["records"],
            limits["writesPerMinute"] if write_delta else max(limits["writesPerMinute"], minute_writes),
            limits["writesPerMonth"] if write_delta else max(limits["writesPerMonth"], month_writes),
            usd_micros(limits["jevMonthlyBudgetUsd"]), project_delta, record_insertions, jev_delta, period, minute)
        try:
            # Keep original result indexes; domain callers rely on batch offsets.
            # Recount after all domain mutations in the same transaction.
            # A later quota failure still rolls back every original guard and
            # mutation. Appending never interrupts an adjacent changes() CAS.
            result = await self.binding.batch([*raw, counter])
            return result[:-1]
        except Exception as error:
            for constraint, (status, code) in _ERRORS.items():
                if constraint in str(error):
                    raise PlanLimitError(status, code) from None
            raise


async def reserve_jev_batch(binding, statements, *, now):
    """Use the monthly allowance even for callers without a quota wrapper.

    Runtime routes already use PlanLimitedD1. Local/native callers that supply
    a raw D1 adapter still receive the same fenced monthly reservation; none
    may fall back to an unmetered provider call.
    """
    limited = binding if isinstance(binding, PlanLimitedD1) else PlanLimitedD1(binding, now=now)
    return await limited.reserve_jev(statements)


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
