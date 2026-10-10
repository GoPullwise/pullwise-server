"""Closed Owner-only project business erasure, never arbitrary bulk SQL.

These fixed templates are shared by the application, quota wrapper and native
meter. Complete sequence and binding agreement are required; ordinary bulk
mutations remain unadmitted. All children and current capacity change together.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .account_cycle_rules import effective_user_plan
from .cloudflare_plan_limits import ACTIVE_EXPENSE_SQL, PlanLimitError
from .ledger_plan_policy import default_policy, usd_micros
from .cloudflare_state_records import record_name

D = "SELECT id FROM expenses WHERE owner_id=:owner AND target_kind='project' AND project_id=:project"
R = "SELECT id FROM expense_recurring_rules WHERE owner_id=:owner AND target_kind='project' AND project_id=:project"


def mentions(column):
    return (f"(json_extract({column},'$.target.kind')='project' AND "
            f"json_extract({column},'$.target.projectId')=:project)")


EVENT_SELECTED = f"(expense_id IN ({D}) OR {mentions('before_json')} OR {mentions('after_json')})"
REPLAY_SELECTED = f"(expense_id IN ({D}) OR {mentions('response_json')})"

PROJECT_MUTATIONS = (
    ("expense_suggestion_events", f"""DELETE FROM expense_suggestion_events WHERE owner_id=:owner AND (
        draft_project_id=:project OR project_id=:project OR recorded_expense_id IN ({D}) OR id IN (
          SELECT json_extract(before_json,'$.assistance.suggestionId') FROM expense_events
            WHERE owner_id=:owner AND {EVENT_SELECTED}
          UNION SELECT json_extract(after_json,'$.assistance.suggestionId') FROM expense_events
            WHERE owner_id=:owner AND {EVENT_SELECTED}
          UNION SELECT json_extract(response_json,'$.assistance.suggestionId') FROM expense_create_idempotency
            WHERE owner_id=:owner AND {REPLAY_SELECTED}))"""),
    ("ledger_activity_events", f"""DELETE FROM ledger_activity_events WHERE owner_id=:owner AND (
        project_id=:project OR {mentions('before_json')} OR {mentions('after_json')}
        OR (resource_kind='expense' AND resource_id IN ({D}))
        OR (resource_kind='recurring_rule' AND resource_id IN ({R})))"""),
    ("expense_events", f"DELETE FROM expense_events WHERE owner_id=:owner AND {EVENT_SELECTED}"),
    ("expense_create_idempotency", f"DELETE FROM expense_create_idempotency WHERE owner_id=:owner AND {REPLAY_SELECTED}"),
    ("expense_recurring_occurrences", f"""UPDATE expense_recurring_occurrences SET expense_id=NULL WHERE owner_id=:owner
        AND expense_id IN ({D}) AND rule_id NOT IN ({R})"""),
    ("expense_recurring_occurrences", f"""DELETE FROM expense_recurring_occurrences WHERE owner_id=:owner
        AND rule_id IN ({R})"""),
    ("expense_recurring_pending", f"DELETE FROM expense_recurring_pending WHERE owner_id=:owner AND rule_id IN ({R})"),
    ("expense_recurring_rules", "DELETE FROM expense_recurring_rules WHERE owner_id=:owner AND target_kind='project' AND project_id=:project"),
    ("expenses", "DELETE FROM expenses WHERE owner_id=:owner AND target_kind='project' AND project_id=:project"),
    ("ledger_project_repositories", "DELETE FROM ledger_project_repositories WHERE owner_id=:owner AND project_id=:project"),
    ("ledger_projects", "DELETE FROM ledger_projects WHERE owner_id=:owner AND id=:project AND revision=:revision"),
)

PROJECT_GUARD = """INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN EXISTS(
    SELECT 1 FROM ledger_projects WHERE owner_id=:owner AND id=:project AND revision=:revision)
    THEN 1 ELSE 0 END)"""
CHANGED_GUARD = "INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"
CLEANUP = "DELETE FROM d1_command_guard"
COUNTS = "SELECT COUNT(*) AS projects FROM ledger_projects WHERE owner_id=?"
USAGE = f"""INSERT INTO ledger_plan_usage(owner_id,projects,records,month,writes,minute,
    minute_writes,jev_reserved_microusd,project_cap,record_cap,minute_cap,month_cap,jev_cap,
    project_delta,record_delta,jev_delta,previous_month,previous_minute)
    VALUES(:owner,
      (SELECT COUNT(*) FROM ledger_projects WHERE owner_id=:owner),
      (SELECT COUNT(*) FROM expenses WHERE owner_id=:owner AND {ACTIVE_EXPENSE_SQL})-
      (SELECT COUNT(*) FROM expenses WHERE owner_id=:owner AND target_kind='project'
          AND project_id=:project AND {ACTIVE_EXPENSE_SQL}),
      :month,1,:minute,1,0,:project_cap,:record_cap,:minute_cap,:month_cap,:jev_cap,0,0,0,:month,:minute)
    ON CONFLICT(owner_id) DO UPDATE SET
      projects=ledger_plan_usage.projects,
      records=excluded.records,
      writes=CASE WHEN ledger_plan_usage.month=excluded.month THEN ledger_plan_usage.writes+1 ELSE 1 END,
      minute_writes=CASE WHEN ledger_plan_usage.minute=excluded.minute THEN ledger_plan_usage.minute_writes+1 ELSE 1 END,
      jev_reserved_microusd=CASE WHEN ledger_plan_usage.month=excluded.month
          THEN ledger_plan_usage.jev_reserved_microusd ELSE 0 END,
      previous_month=ledger_plan_usage.month,previous_minute=ledger_plan_usage.minute,
      month=excluded.month,minute=excluded.minute,project_cap=excluded.project_cap,record_cap=excluded.record_cap,
      minute_cap=excluded.minute_cap,month_cap=excluded.month_cap,jev_cap=excluded.jev_cap,
      project_delta=0,record_delta=0,jev_delta=0
    WHERE ledger_plan_usage.projects>=(SELECT COUNT(*) FROM ledger_projects WHERE owner_id=:owner)"""
FINAL_GUARD = f"""INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN
    NOT EXISTS(SELECT 1 FROM ledger_projects WHERE owner_id=:owner AND id=:project)
    AND NOT EXISTS(SELECT 1 FROM expenses WHERE owner_id=:owner AND project_id=:project)
    AND NOT EXISTS(SELECT 1 FROM expense_recurring_rules WHERE owner_id=:owner AND project_id=:project)
    AND NOT EXISTS(SELECT 1 FROM ledger_project_repositories WHERE owner_id=:owner AND project_id=:project)
    AND NOT EXISTS(SELECT 1 FROM expense_suggestion_events WHERE owner_id=:owner
        AND (draft_project_id=:project OR project_id=:project))
    AND NOT EXISTS(SELECT 1 FROM expense_events WHERE owner_id=:owner
        AND ({mentions('before_json')} OR {mentions('after_json')}))
    AND NOT EXISTS(SELECT 1 FROM expense_create_idempotency WHERE owner_id=:owner AND {mentions('response_json')})
    AND NOT EXISTS(SELECT 1 FROM ledger_activity_events WHERE owner_id=:owner
        AND (project_id=:project OR {mentions('before_json')} OR {mentions('after_json')}))
    AND EXISTS(SELECT 1 FROM ledger_plan_usage WHERE owner_id=:owner
        AND projects>=(SELECT COUNT(*) FROM ledger_projects WHERE owner_id=:owner)
        AND records=(SELECT COUNT(*) FROM expenses WHERE owner_id=:owner AND {ACTIVE_EXPENSE_SQL}))
    THEN 1 ELSE 0 END)"""


def compile_named(sql, values):
    keys = re.findall(r':([a-z_]+)\b', sql)
    return re.sub(r':[a-z_]+\b', '?', sql), tuple(values[key] for key in keys)


def usage_values(owner, project, user, now, policy):
    limits = policy[effective_user_plan(user, timestamp=now)]
    return {'owner': owner, 'project': project,
        'month': datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m'), 'minute': now // 60,
        'project_cap': limits['projects'], 'record_cap': limits['records'],
        'minute_cap': limits['writesPerMinute'], 'month_cap': limits['writesPerMonth'],
        'jev_cap': usd_micros(limits['jevMonthlyBudgetUsd'])}


def recipe_tail(owner, project, revision, user, now, policy):
    values = {**usage_values(owner, project, user, now, policy), 'revision': revision}
    templates = (PROJECT_GUARD, USAGE, CHANGED_GUARD,
        *(sql for _, sql in PROJECT_MUTATIONS), CHANGED_GUARD, FINAL_GUARD, CLEANUP)
    return [compile_named(sql, values) for sql in templates]


def owner_cookie_guard_sql():
    # Reuse the exact application guard generator; no alternative authority SQL.
    from .cloudflare_ledger_api import _write_guard
    class Template:
        def prepare(self, sql):
            self.sql = sql
            return self
        def bind(self, *params):
            return self
    return _write_guard(Template(), {'user': '{}', 'sessions': '{}', 'session_id': 's'}, 'owner', 0).sql


def owner_key_guard_sql():
    """The existing raw Owner/key guard, not a second authorization path."""
    from .cloudflare_ledger_api import _write_guard
    class Template:
        def prepare(self, sql):
            self.sql = sql
            return self
        def bind(self, *params):
            return self
    return _write_guard(Template(), {'user': '{}', 'token': 'pwk_template',
        'key': {'scopes': '[]', 'restrictions': '{}'}}, 'owner', 0).sql


def erasure_candidate(statements):
    """Recognize the private family so an incomplete recipe cannot fall back.

    Membership here never authorizes dispatch. Only recipe_identity does that.
    Ordinary project edits and scalar expense/rule deletes use other templates.
    """
    templates = {compile_named(sql, dict.fromkeys(re.findall(r':([a-z_]+)\b', sql)))[0]
                 for _, sql in PROJECT_MUTATIONS}
    return any(item.sql in templates or re.match(
        r'^\s*DELETE\s+FROM\s+ledger_projects\b', item.sql, re.I)
        for item in statements)


def recipe_identity(statements, *, policy=None, now=None):
    """Recognize only the complete closed sequence and its repeated bindings.

    Native metering uses the exact supplied clock/limits; PlanLimitedD1 also
    reconstructs them from its current effective Owner plan and trusted clock.
    """
    if len(statements) != len(PROJECT_MUTATIONS) + 7 or statements[0].sql not in {
            owner_cookie_guard_sql(), owner_key_guard_sql()}:
        return None
    try:
        first = statements[0].params
        user = json.loads(first[1])
        owner = user['id']
        if first[0] != record_name('users', owner) or type(first[-1]) is not int:
            return None
        restrictions = None
        if statements[0].sql == owner_cookie_guard_sql():
            if len(first) != 6:
                return None
            session = json.loads(first[3])
            if (not isinstance(session, dict) or first[4] != owner or session.get('userId') != owner
                    or not str(first[2]).startswith('record:sessions:')
                    or type(session.get('expiresAt')) is not int
                    or session['expiresAt'] < first[-1]):
                return None
        else:
            from .api_key_dto_rules import ALLOWED_SCOPES, parse_api_key_restrictions
            if len(first) != 7 or first[3] != owner or not isinstance(first[2], str):
                return None
            if not re.fullmatch(r'[0-9a-f]{64}', first[2]):
                return None
            scopes, restrictions = json.loads(first[4]), json.loads(first[5])
            if (not isinstance(scopes, list) or 'projects:write' not in scopes
                    or any(not isinstance(scope, str) or scope not in ALLOWED_SCOPES for scope in scopes)
                    or len(scopes) != len(set(scopes))
                    or not isinstance(restrictions, dict)
                    or parse_api_key_restrictions(restrictions) != restrictions
                    or restrictions.get('workspaceId', owner) != owner):
                return None
        owner2, project, revision = statements[1].params
        if (owner2 != owner or not isinstance(project, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project)
                or type(revision) is not int or not 1 <= revision <= 9007199254740991):
            return None
        if restrictions is not None:
            from .cloudflare_ledger_auth import target_allowed
            if not target_allowed(restrictions, 'project', project):
                return None
        clock = first[-1] if now is None else now
        if clock != first[-1]:
            return None
        if policy is None:
            # Extract the fixed UPSERT's effective-limit bindings. Authorization
            # remains in the original raw-user guard and PlanLimitedD1 path.
            keys = re.findall(r':([a-z_]+)\b', USAGE)
            values = dict(zip(keys, statements[2].params))
            if len(statements[2].params) != len(keys):
                return None
            limits = {'projects': values['project_cap'], 'records': values['record_cap'],
                'writesPerMinute': values['minute_cap'], 'writesPerMonth': values['month_cap'],
                'jevMonthlyBudgetUsd': str(values['jev_cap'] // 1000000) + '.' + str(values['jev_cap'] % 1000000).zfill(6)}
            policy = {effective_user_plan(user, timestamp=clock): limits}
        tail = recipe_tail(owner, project, revision, user, clock, policy)
        if any(s.sql != sql or tuple(s.params) != params for s, (sql, params) in zip(statements[1:], tail)):
            return None
        return owner, project, revision
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return None


def recipe_bounds(statements, rows, indexes):
    """Per-SQL linear ceilings for this exact complete recipe, including FKs.

    Cleanup selectors build finite uncorrelated lists. Each active expense
    predicate additionally performs at most one indexed parent-project lookup
    per expense: project IDs are globally unique, with owner equality checked
    on that same row. These point probes receive a separate linear margin.
    Incoming expense children have owner/expense or globally unique indexes.
    All selected child rows are erased/unlinked before parent deletion; the
    single project parent may traverse each remaining incoming owner prefix.
    """
    if recipe_identity(statements) is None:
        raise ValueError('not the complete project erasure recipe')
    reads = writes = 0
    for index, item in enumerate(statements):
        references = re.findall(r'\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)', item.sql, re.I)
        # Each uncorrelated source scan/list build receives an independent
        # eight-row margin. Fixed index/FK probes get separate margins below.
        read = 32 + 8 * sum(rows[table.lower()] for table in references)
        read += 32 * rows['expenses'] * item.sql.count(ACTIVE_EXPENSE_SQL)
        if 4 <= index < 4 + len(PROJECT_MUTATIONS):
            table, named = PROJECT_MUTATIONS[index - 4]
            outer_memberships = 2 if table == 'expense_suggestion_events' else named.upper().count(' IN (')
            read += 8 * rows[table] * (1 + indexes[table] + outer_memberships)
            if table == 'expense_suggestion_events':
                read += 16 * rows['expense_events'] + 8 * rows['expense_create_idempotency']
            elif table == 'expenses':
                read += 24 * rows[table]
            elif table == 'expense_recurring_rules':
                read += 8 * (rows[table] + rows['expense_recurring_pending'])
            elif table == 'ledger_projects':
                read += 8 * sum(rows[key] for key in ('expenses', 'expense_recurring_rules', 'ledger_project_repositories'))
            size = 1 if table == 'ledger_projects' else rows[table]
            write = size * (1 + (2 if named.startswith('UPDATE') else 1) * indexes[table])
        elif index == 2:
            # One scalar lazy usage insert/update; no commercial second UPSERT.
            read += 8 * (1 + 2 * indexes['ledger_plan_usage'])
            write = 1 + 2 * indexes['ledger_plan_usage']
        else:
            write = 5 if index == len(statements) - 1 else 1
        reads += read
        writes += write
    if max(reads, writes) > 9007199254740991:
        raise ValueError('project erasure bound exceeds safe integer')
    return reads, writes


async def preflight_usage(binding, owner, project, user, now, policy):
    counts = await binding.prepare(COUNTS).bind(owner).first()
    usage = await binding.prepare("SELECT * FROM ledger_plan_usage WHERE owner_id=?").bind(owner).first()
    # Project capacity is cumulative. Legacy expense counters are refreshed
    # from active rows in the same erasure transaction, never on this read.
    if usage and usage['projects'] < counts['projects']:
        raise PlanLimitError(503, 'USAGE_GUARD_UNAVAILABLE')
    values = usage_values(owner, project, user, now, policy)
    if usage and (values['month'] < usage['month'] or values['minute'] < usage['minute']):
        raise PlanLimitError(409, 'QUOTA_WINDOW_CHANGED')
    writes = usage['writes'] if usage and usage['month'] == values['month'] else 0
    minute_writes = usage['minute_writes'] if usage and usage['minute'] == values['minute'] else 0
    if minute_writes + 1 > values['minute_cap']:
        raise PlanLimitError(429, 'WRITE_RATE_LIMIT')
    if writes + 1 > values['month_cap']:
        raise PlanLimitError(429, 'MONTHLY_WRITE_LIMIT')


async def remove_project(*, binding, item_id, headers, now):
    from .cloudflare_ledger_api import _authorized, _error, _revision, _write_guard
    from .cloudflare_ledger_auth import ledger_principal
    from .cloudflare_principal import _header, _cookie_sessions
    user, _, proof, rows = await _authorized(binding, headers, 'projects:write', now,
        [binding.prepare('SELECT * FROM ledger_projects WHERE id=?').bind(item_id)], 'project', item_id)
    if (proof.get('workspace_role') != 'owner' or proof.get('actor_user_id') != user['id']
            or proof.get('workspace_id') != user['id']):
        return _error(403, 'PROJECT_OWNER_REQUIRED')
    if proof.get('key') is None and (_header(headers, 'Authorization')
            or _header(headers, 'X-Pullwise-Api-Key')
            or proof.get('session_id') not in _cookie_sessions(headers)):
        return _error(403, 'PROJECT_OWNER_SESSION_REQUIRED')
    revision = _revision(headers)
    if revision is None:
        return _error(428, 'PRECONDITION_REQUIRED')
    if revision < 0:
        return _error(422, 'INVALID_INPUT')
    if not rows[0]:
        # Empty outcome in the authenticated Owner's scope, no ownership claim.
        return 204, None
    existing = rows[0][0]
    if existing['owner_id'] != user['id']:
        return _error(404, 'NOT_FOUND')
    if existing['revision'] != revision:
        return _error(412, 'PRECONDITION_FAILED')
    raw_user = json.loads(proof.get('owner_user', proof['user']))
    policy = getattr(binding, 'plan_policy', None) or default_policy()
    try:
        await preflight_usage(binding, user['id'], item_id, raw_user, now, policy)
        fresh = {}
        _, _, auth, validate = await ledger_principal(binding=binding, headers=headers,
            scope='projects:write', now=now, target_kind='project', project_id=item_id, proof=fresh)
        parts = await binding.batch([*auth, binding.prepare(
            'SELECT revision FROM ledger_projects WHERE owner_id=? AND id=?').bind(user['id'], item_id)])
        validate([part.results for part in parts[:len(auth)]])
        if fresh != proof:
            return _error(403, 'AUTHORIZATION_CHANGED')
        if not parts[-1].results:
            return 204, None
        if parts[-1].results[0]['revision'] != revision:
            return _error(412, 'PRECONDITION_FAILED')
        commands = [_write_guard(binding, proof, user['id'], now)]
        commands.extend(binding.prepare(sql).bind(*params) for sql, params in
            recipe_tail(user['id'], item_id, revision, raw_user, now, policy))
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception as error:
        # Only a known SQLite/D1 CHECK rollback can be resolved by fresh reads.
        # Metered unknown outcomes raise their accounting error and propagate.
        if 'CHECK constraint failed: ok=1' not in str(error):
            raise
        _, _, current, current_rows = await _authorized(binding, headers, 'projects:write', now,
            [binding.prepare('SELECT revision FROM ledger_projects WHERE owner_id=? AND id=?').bind(user['id'], item_id)],
            'project', item_id)
        if current != proof:
            return _error(403, 'AUTHORIZATION_CHANGED')
        return _error(412, 'PRECONDITION_FAILED')
    # Unknown native outcomes propagate unchanged, preserving journal stop.
    return 204, None
