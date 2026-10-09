"""Product-wide preview admission with per-batch, durable row reservations.

The application owns SQL; clients cannot submit SQL. Writes additionally need
scalar INSERTs or unique-key UPDATE/DELETE fences. Index effects and temporary
guard cleanup are reserved before dispatch. There is no application retry.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from .cloudflare_state_records import (
    STATE_KINDS, STATE_STORAGE_VERSION, USER_RECORD_BYTES, SMALL_RECORD_BYTES,
    MAX_RECORD_ID_BYTES, record_parameter_limits, legacy_record_rows,
)

from .cloudflare_preview_schema import (
    INDEX_COUNTS, PRIMARY_KEYS, SCHEMA_SQL, SCHEMA_VERSION, SCHEMA_FINGERPRINT,
    SCHEMA_OBJECTS, LEGACY_INDEX_COUNTS, LEGACY_SCHEMA_OBJECTS,
    LEGACY_SCHEMA_FINGERPRINT, UPGRADE_SQL,
    V5_SCHEMA_OBJECTS, V5_SCHEMA_FINGERPRINT, V5_INDEX_COUNTS, UPGRADE_V6_SQL,
    V6_SCHEMA_OBJECTS, V6_SCHEMA_FINGERPRINT, V6_INDEX_COUNTS, UPGRADE_V7_SQL,
    V7_SCHEMA_OBJECTS, V7_SCHEMA_FINGERPRINT, V7_INDEX_COUNTS, UPGRADE_V8_SQL,
    V8_SCHEMA_OBJECTS, V8_SCHEMA_FINGERPRINT, V8_INDEX_COUNTS, UPGRADE_V9_SQL,
)
from .cloudflare_validation_budget import (
    BudgetError, MeteredD1, OperationBound, RequestPlan, _Statement, _field,
    READ_CEILING, WRITE_CEILING, ParameterBound,
)

_TOKEN = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][\w]*|\d+|\?|\S")


def _tokens(sql):
    result = []
    for match in _TOKEN.finditer(sql):
        token = match.group()
        if token.startswith(("--", "/*")):
            continue
        result.append("<VALUE>" if token.startswith(("'", '"')) or token.isdigit() else token.upper())
    if ";" in result[:-1]:
        raise ValueError("multiple SQL statements")
    return result[:-1] if result and result[-1] == ";" else result


def _top(tokens):
    depth = 0
    for index, token in enumerate(tokens):
        if token == ")":
            depth -= 1
        if depth == 0:
            yield index, token
        if token == "(":
            depth += 1


def _target(tokens):
    if tokens[0] == "UPDATE":
        return tokens[1].lower()
    marker = "INTO" if tokens[0] == "INSERT" else "FROM"
    return tokens[tokens.index(marker) + 1].lower()


def _where_keys(tokens):
    where = next((i for i, t in _top(tokens) if t == "WHERE"), None)
    if where is None:
        return set()
    tail = tokens[where + 1:]
    end = next((i for i, t in _top(tail) if t in {"RETURNING", "ORDER", "GROUP", "LIMIT"}), len(tail))
    tail = tail[:end]
    if any(t == "OR" for _, t in _top(tail)):
        return set()
    cuts = [-1] + [i for i, t in _top(tail) if t == "AND"] + [len(tail)]
    keys = set()
    for start, finish in zip(cuts, cuts[1:]):
        term = tail[start + 1:finish]
        if len(term) == 5 and term[1] == ".":
            term = term[2:]
        if len(term) == 3 and term[1] == "=" and term[2] in {"?", "<VALUE>"}:
            keys.add(term[0].lower())
    return keys


def sql_write_bound(sql, guard_rows=0):
    tokens = _tokens(sql)
    if not tokens:
        raise ValueError("empty SQL")
    verb = tokens[0]
    if verb == "SELECT":
        return 0
    if verb not in {"INSERT", "UPDATE", "DELETE"}:
        raise ValueError("unreviewed SQL operation")
    table = _target(tokens)
    if table not in INDEX_COUNTS:
        raise ValueError("unknown table")
    if table == "d1_command_guard" and verb == "DELETE":
        if tokens != ["DELETE", "FROM", "D1_COMMAND_GUARD"]:
            raise ValueError("unreviewed guard cleanup")
        return guard_rows
    top = list(_top(tokens))
    if verb == "INSERT":
        if "REPLACE" in tokens or any(t in {"UNION", "INTERSECT", "EXCEPT"} for _, t in top):
            raise ValueError("non-scalar or replacing INSERT")
        values = next((i for i, t in top if t == "VALUES"), None)
        if values is not None:
            if tokens[values + 1] != "(":
                raise ValueError("non-scalar INSERT")
            close = next((i for i, t in top if i > values and t == ")"), None)
            if close is None or close + 1 < len(tokens) and tokens[close + 1] == ",":
                raise ValueError("multi-row INSERT")
        elif not any(t == "SELECT" for _, t in top) or any(t == "FROM" for _, t in top):
            raise ValueError("non-scalar INSERT SELECT")
        # A scalar UPSERT can replace old index entries as well as add new ones.
        factor = 2 if any(t == "CONFLICT" for _, t in top) else 1
        return 1 + factor * INDEX_COUNTS[table]
    where = next((i for i, t in top if t == "WHERE"), None)
    if where is None:
        raise ValueError("mutation lacks unique key")
    tail = tokens[where + 1:]
    end = next((i for i, t in _top(tail) if t == "RETURNING"), len(tail))
    tail = tail[:end]
    if any(t == "OR" for _, t in _top(tail)):
        raise ValueError("disjunctive mutation fence")
    cuts = [-1] + [i for i, t in _top(tail) if t == "AND"] + [len(tail)]
    keys = set()
    for start, finish in zip(cuts, cuts[1:]):
        term = tail[start + 1:finish]
        if len(term) == 3 and term[1] == "=" and term[2] in {"?", "<VALUE>"}:
            keys.add(term[0].lower())
    if not set(PRIMARY_KEYS[table]) <= keys or not PRIMARY_KEYS[table]:
        raise ValueError("mutation lacks unique key")
    return 1 + (2 if verb == "UPDATE" else 1) * INDEX_COUNTS[table]


def _json_size(value):
    if isinstance(value, list):
        return max([len(value)] + [_json_size(v) for v in value])
    if isinstance(value, dict):
        nested = [_json_size(v) for v in value.values()]
        return max([0] + nested)
    return 0


def _expense_json_parameters(sql, *, actors=False):
    """Locate reviewed expense/rule replay JSON in simple scalar INSERTs."""
    tokens = _tokens(sql) if sql else []
    columns_by_table = {"EXPENSE_EVENTS": {"BEFORE_JSON", "AFTER_JSON"},
                        "EXPENSE_CREATE_IDEMPOTENCY": {"RESPONSE_JSON"},
                        "EXPENSE_RECURRING_RULES": {"CREATE_RESPONSE_JSON"},
                        "LEDGER_ACTIVITY_EVENTS": {"BEFORE_JSON", "AFTER_JSON"}}
    if actors:
        columns_by_table = {"LEDGER_ACTIVITY_EVENTS": {"ACTOR_JSON"}}
    if (len(tokens) < 8 or tokens[:2] != ["INSERT", "INTO"]
            or tokens[2] not in columns_by_table or tokens[3] != "("):
        return set()
    close = tokens.index(")", 4)
    columns = tokens[4:close]
    if tokens[close + 1:close + 3] != ["VALUES", "("] or tokens[-1] != ")":
        return set()
    values = tokens[close + 3:-1]
    if (not columns or len(columns) != len(values)
            or columns[1::2] != [","] * (len(columns) // 2)
            or values[1::2] != [","] * (len(values) // 2)
            or any(value not in {"?", "NULL", "<VALUE>"} for value in values[::2])):
        return set()
    index, expanded = 0, set()
    for column, value in zip(columns[::2], values[::2]):
        if value == "?":
            if column in columns_by_table[tokens[2]]:
                expanded.add(index)
            index += 1
    return expanded


def _input_bound(params, *, sql=""):
    # An 8 KiB ingress can gain <1 KiB DTO metadata and <4 KiB assistance
    # (30 categories + uncertain, safe generated IDs, finite float probabilities).
    # Stored expense JSON has a 16 KiB envelope. A typed exact-key user record
    # (including retained repo/billing history) has its separate 512 KiB bound.
    # Plain text, ephemeral/event records and all HTTP ingress retain 8 KiB.
    expanded = _expense_json_parameters(sql)
    actors = _expense_json_parameters(sql, actors=True)
    record_limits = record_parameter_limits(sql, params)
    size = 0
    total_bytes = 0
    for index, value in enumerate(params):
        if value is None or type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
            continue
        limit = record_limits.get(index, 2048 if index in actors else 16384 if index in expanded else 8192)
        if type(value) is not str or len(value.encode("utf-8")) > limit:
            raise ValueError("preview parameter exceeds input bound")
        total_bytes += len(value.encode("utf-8"))
        if total_bytes > 8 * 1024 * 1024:
            raise ValueError("preview operation parameters exceed input bound")
        if index in expanded or index in actors:
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                raise ValueError("expense snapshot must be a JSON object")
            json.dumps(decoded, allow_nan=False, ensure_ascii=False).encode("utf-8")
            size = max(size, _json_size(decoded))
            continue
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                # Notes and purposes are plain text even when they start '{'.
                continue
            json.dumps(decoded, allow_nan=False, ensure_ascii=False).encode("utf-8")
            size = max(size, _json_size(decoded))
    return size


def _count_sql(tables):
    return "SELECT " + ",".join(
        f'(SELECT COUNT(*) FROM {table}) AS {table}' for table in tables)


_COUNT_SQL = _count_sql(INDEX_COUNTS)
_V5_COUNT_SQL = _count_sql(V5_INDEX_COUNTS)
_V6_COUNT_SQL = _count_sql(V6_INDEX_COUNTS)
_V7_COUNT_SQL = _count_sql(V7_INDEX_COUNTS)
_V8_COUNT_SQL = _count_sql(V8_INDEX_COUNTS)
_STATE_SQL = "SELECT name,payload FROM app_state LIMIT 7"
_RECORD_COUNT_SQL = "SELECT " + ",".join(
    f"COALESCE(SUM(CASE WHEN name GLOB 'record:{kind}:*' THEN 1 ELSE 0 END),0) AS {kind}"
    for kind in STATE_KINDS) + " FROM app_state"


def _auth_record_invalid_sql(kind, suffix):
    def text(field, maximum):
        value = f"json_extract(payload,'$.{field}')"
        return (f"json_type(payload,'$.{field}') IS NOT 'text' OR "
                f"length(CAST({value} AS BLOB)) NOT BETWEEN 1 AND {maximum} OR "
                f"instr({value},char(0))>0 OR "
                f"{value} GLOB ('*['||char(1)||'-'||char(31)||char(127)||']*')")

    def clock(field):
        return (f"json_type(payload,'$.{field}') IS NOT 'integer' OR "
                f"json_extract(payload,'$.{field}') NOT BETWEEN 0 AND 9007199254740991")

    if kind == "githubIdentities":
        return (f"length({suffix}) NOT BETWEEN 1 AND 16 OR {suffix} GLOB '*[^0-9]*' OR "
                f"CAST(CAST({suffix} AS INTEGER) AS TEXT) IS NOT {suffix} OR "
                f"CAST({suffix} AS INTEGER) NOT BETWEEN 1 AND 9007199254740991 OR "
                f"json_extract(payload,'$.githubId') IS NOT {suffix} OR "
                f"({text('userId', MAX_RECORD_ID_BYTES)}) OR ({clock('createdAt')}) OR "
                "json_extract(payload,'$.createdAt')=0")
    if kind not in {"emailIdentities", "emailChallenges"}:
        return "0"
    email = "json_extract(payload,'$.email')"
    base = (f"length({suffix})!=64 OR {suffix} GLOB '*[^0-9a-f]*' OR "
            f"({text('email', 254)}) OR {email}!=lower({email}) OR "
            f"{email} GLOB '*[^!-~]*' OR instr({email},'@')<2 OR "
            f"instr(substr({email},instr({email},'@')+1),'@')>0 OR "
            f"length({email})<=instr({email},'@') OR ({clock('createdAt')})")
    if kind == "emailIdentities":
        return (base + f" OR ({text('userId', MAX_RECORD_ID_BYTES)}) OR ({clock('verifiedAt')}) OR "
                "json_extract(payload,'$.verifiedAt')<json_extract(payload,'$.createdAt')")
    hashes = " OR ".join(
        f"json_type(payload,'$.{field}') IS NOT 'text' OR length(json_extract(payload,'$.{field}'))!=64 OR "
        f"json_extract(payload,'$.{field}') GLOB '*[^0-9a-f]*'"
        for field in ("codeHash", "browserHash"))
    return (base + f" OR {hashes} OR ({clock('expiresAt')}) OR "
            "json_extract(payload,'$.expiresAt')<=json_extract(payload,'$.createdAt') OR "
            "json_extract(payload,'$.expiresAt')>json_extract(payload,'$.createdAt')+600 OR "
            "json_type(payload,'$.attempts') IS NOT 'integer' OR "
            "json_extract(payload,'$.attempts') NOT BETWEEN 0 AND 5 OR "
            "json_type(payload,'$.challengeId') IS NOT 'text' OR "
            "length(json_extract(payload,'$.challengeId'))!=43 OR "
            "json_extract(payload,'$.challengeId') GLOB '*[^A-Za-z0-9_-]*' OR "
            "json_extract(payload,'$.purpose') IS NOT 'login' AND json_extract(payload,'$.purpose') IS NOT 'link' OR "
            "json_extract(payload,'$.purpose')='login' AND (json_type(payload,'$.userId') IS NOT NULL OR "
            "json_type(payload,'$.sessionId') IS NOT NULL) OR "
            f"json_extract(payload,'$.purpose')='link' AND (({text('userId', MAX_RECORD_ID_BYTES)}) OR "
            f"({text('sessionId', MAX_RECORD_ID_BYTES)}))")


def _strict_record_sql():
    # Structural aggregate only. Semantic validity is closed over empty init,
    # strictly decoded legacy cutover, and strictly decoded typed mutations.
    # Direct imports/console writes bypassing those paths are unsupported.
    cases = []
    for kind in STATE_KINDS:
        prefix = f"record:{kind}:"
        suffix = f"substr(name,{len(prefix) + 1})"
        identity = (f"json_extract(payload,'$.id') IS NOT {suffix}" if kind == "users"
                    else f"(json_type(payload,'$.id') IS NOT NULL AND json_extract(payload,'$.id') IS NOT {suffix})"
                    if kind == "sessions" else f"json_extract(payload,'$.eventId') IS NOT {suffix}"
                    if kind == "billingPendingUpdates" else _auth_record_invalid_sql(kind, suffix))
        cases.append(f"""WHEN name GLOB '{prefix}*' THEN CASE
            WHEN instr(name,char(0))>0 OR name GLOB ('*['||char(1)||'-'||char(31)||char(127)||']*') THEN 1
            WHEN json_valid(payload)=0 THEN 1
            WHEN json_type(payload)!='object' THEN 1
            WHEN length(CAST(payload AS BLOB))>{USER_RECORD_BYTES if kind == 'users' else SMALL_RECORD_BYTES} THEN 1
            WHEN length(CAST({suffix} AS BLOB)) NOT BETWEEN 1 AND {MAX_RECORD_ID_BYTES} THEN 1
            WHEN {identity} THEN 1 ELSE 0 END""")
    return ("SELECT COALESCE(SUM(CASE WHEN name NOT GLOB 'record:*' THEN "
            "CASE WHEN payload IN ('{}','[]') THEN 0 ELSE 1 END " + " ".join(cases) +
            " ELSE 1 END),0) AS invalidRecords FROM app_state")


_STRICT_RECORD_SQL = _strict_record_sql()
_STATE_RECORD_CASE = "product-state-record-v1"
# This proves the expanded typed-record vocabulary without rerunning the
# completed storage-v1 cutover or changing the canonical D1 schema.
STATE_RECORD_INTEGRITY_VERSION = 2


def _record_product_data(count_result, state_result, upper, strict_result=None, *, tables=INDEX_COUNTS):
    rows, kinds = list(_field(count_result, "results", [])), list(_field(state_result, "results", []))
    if len(rows) != 1 or len(kinds) != 1:
        raise BudgetError("PREVIEW_DATA_BOUND")
    counts = {table: _field(rows[0], table) for table in tables}
    if any(type(value) is not int or not 0 <= value <= upper[table] for table, value in counts.items()):
        raise BudgetError("PREVIEW_DATA_BOUND")
    if counts["d1_command_guard"]:
        raise BudgetError("NONEMPTY_GUARD")
    totals = {kind: _field(kinds[0], kind) for kind in STATE_KINDS}
    if (any(type(value) is not int or not 0 <= value <= counts["app_state"] for value in totals.values())
            or not 0 <= counts["app_state"] - sum(totals.values()) <= 6):
        raise BudgetError("PREVIEW_DATA_BOUND")
    if strict_result is not None:
        invalid = list(_field(strict_result, "results", []))
        if len(invalid) != 1 or _field(invalid[0], "invalidRecords") != 0:
            raise BudgetError("STATE_RECORD_INVALID")
    # No active product SQL iterates a global JSON map. Byte envelopes prove
    # any future per-record virtual traversal's finite worst case separately.
    return {"rows": counts, "json": {}, "arrays": USER_RECORD_BYTES // 2,
            "records": totals}
_EMPTY_SQL = "SELECT name FROM sqlite_master LIMIT 65"
_INITIAL = RequestPlan("product-schema", "POST", "/_internal/initialize", 1, (
    OperationBound((_EMPTY_SQL,), 384, 0),
    OperationBound(SCHEMA_SQL, 4000, 256),
))
_SCHEMA_QUERY = ("SELECT type,name,tbl_name,sql FROM sqlite_schema "
    "WHERE name NOT GLOB '_cf_*' AND (name NOT GLOB 'sqlite_*' "
    "OR name GLOB 'sqlite_autoindex_*') ORDER BY type,name LIMIT 129")
_LEGACY_COUNT_SQL = "SELECT " + ",".join(
    f'(SELECT COUNT(*) FROM {table}) AS {table}' for table in LEGACY_INDEX_COUNTS)
_UPGRADE_CASE = "product-schema-v4-to-v5"


def _current_schema(state):
    return (state.get("schema_version") == SCHEMA_VERSION
            and state.get("schema_fingerprint") == SCHEMA_FINGERPRINT)


def _schema_objects(result):
    rows = list(_field(result, "results", []))
    if len(rows) >= 129:
        raise BudgetError("PREVIEW_SCHEMA_MISMATCH")
    objects = []
    for row in rows:
        kind, name, table, sql = (_field(row, key) for key in ("type", "name", "tbl_name", "sql"))
        if not all(type(value) is str for value in (kind, name, table)) or not (
                sql is None or type(sql) is str):
            raise BudgetError("PREVIEW_SCHEMA_MISMATCH")
        objects.append((kind, name, table, " ".join(sql.split()) if sql else None))
    return tuple(objects)


def _read_bound(rows):
    return 3 * (sum(rows.values()) + rows["app_state"] + 16)


def _upgrade_plan(state):
    """Compile one finite plan from already persisted cardinality ceilings."""
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if (not isinstance(rows, dict) or set(rows) != set(LEGACY_INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= WRITE_CEILING for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(data.get("json"), dict)
            or any(type(n) is not int or n < 0 for n in data["json"].values())
            or type(data.get("arrays")) is not int or data["arrays"] < 0):
        raise BudgetError("PREVIEW_DATA_BOUND")
    projects = rows["ledger_projects"]
    upper = {**rows, "workspace_members": 0, "workspace_invites": 0,
             "workspace_events": 0, "ledger_project_repositories": projects}
    # DDL catalog/index writes have a fixed margin. Conservatively include a
    # full indexed project rewrite for each ADD COLUMN, as well as each scalar
    # repository backfill and its two indexes. Native proof may tighten this
    # compiled bound, never reclaim any already reserved writes.
    writes = 128 + projects * (1 + V5_INDEX_COUNTS["ledger_project_repositories"]
                              + 2 * (1 + 2 * LEGACY_INDEX_COUNTS["ledger_projects"]))
    operations = (
        OperationBound((_SCHEMA_QUERY,), 384, 0),
        OperationBound((_LEGACY_COUNT_SQL, _STATE_SQL), _read_bound(rows), 0),
        OperationBound(UPGRADE_SQL, 384 * len(UPGRADE_SQL) + 32 * projects, writes),
        OperationBound((_SCHEMA_QUERY, _V5_COUNT_SQL, _STATE_SQL), 384 + _read_bound(upper), 0),
    )
    if (sum(op.rows_read for op in operations) > READ_CEILING
            or sum(op.rows_written for op in operations) > WRITE_CEILING):
        raise BudgetError("BUDGET_EXHAUSTED")
    return RequestPlan(_UPGRADE_CASE, "POST", "/_internal/schema-upgrade", 1, operations), upper


def begin_product_schema_upgrade(journal, plan, *, now):
    """Persist one compiled upgrade reservation using existing product admission.

    Generic finite plans retain their request cap. No caller SQL, budget reset,
    retry, new journal name or namespace is introduced by this path.
    """
    state = journal.snapshot()
    if (plan.name != _UPGRADE_CASE or plan.max_requests != 1
            or state.get("schema_ready") is not True
            or state.get("schema_version") not in (None, 4)
            or state.get("schema_fingerprint") not in (None, LEGACY_SCHEMA_FINGERPRINT)
            or state["cases"].get("product-schema") != 1
            or state["cases"].get(_UPGRADE_CASE, 0) != 0
            or state.get("schema_upgrade") is not None):
        raise BudgetError("SCHEMA_UPGRADE_UNREVIEWED")
    # Compare against our compiled plan before consuming the one case.
    reviewed, _ = _upgrade_plan(state)
    if plan != reviewed:
        raise BudgetError("SCHEMA_UPGRADE_UNREVIEWED")
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=now)
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=now)
    state = journal.check(ticket, now=now)
    state["cases"][_UPGRADE_CASE] = 1
    state["schema_upgrade"] = {"from": 4, "to": 5,
                               "request": ticket, "complete": False}
    journal._save(state)
    return ticket


def _validated_product_data(count_result, state_result, upper, tables):
    count_rows = list(_field(count_result, "results", []))
    states = list(_field(state_result, "results", []))
    if len(count_rows) != 1 or len(states) > 6:
        raise BudgetError("PREVIEW_DATA_BOUND")
    counts = {table: _field(count_rows[0], table) for table in tables}
    if any(type(n) is not int or not 0 <= n <= upper[table] for table, n in counts.items()):
        raise BudgetError("PREVIEW_DATA_BOUND")
    if counts["d1_command_guard"]:
        raise BudgetError("NONEMPTY_GUARD")
    sizes, arrays = {}, 0
    for row in states:
        payload = _field(row, "payload")
        if type(payload) is not str or len(payload.encode("utf-8")) > 8192:
            raise BudgetError("PREVIEW_DATA_BOUND")
        data = json.loads(payload)
        sizes[str(_field(row, "name"))] = len(data) if isinstance(data, (dict, list)) else 0
        arrays = max(arrays, _json_size(data))
    return {"rows": counts, "json": sizes, "arrays": arrays}


async def upgrade_product_schema(binding, journal, *, clock=time.time):
    """One binding-only v4-to-v5 upgrade; caller holds the existing DO lock.

    The native D1 batch is atomic. D1 and DO storage cannot share a transaction;
    an interrupted/unknown result stops with its entire reservation retained.
    The explicit, versioned preview upgrade flag gates this path at request
    ingress; ordinary product SQL cannot submit DDL or choose another migration.
    """
    if (_current_schema(journal.snapshot()) or
            (journal.snapshot().get("schema_version"), journal.snapshot().get("schema_fingerprint"))
                in {(5, V5_SCHEMA_FINGERPRINT), (6, V6_SCHEMA_FINGERPRINT), (7, V7_SCHEMA_FINGERPRINT), (8, V8_SCHEMA_FINGERPRINT)}):
        return
    plan, upper = _upgrade_plan(journal.snapshot())
    ticket = begin_product_schema_upgrade(journal, plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)

    async def execute(sql):
        results = await meter.batch([meter.prepare(statement) for statement in sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        write_contract = sql == UPGRADE_SQL
        # D1 only automatically retries read-only queries. Every statement in
        # this exact frozen group contains a write keyword (CREATE/ALTER/INSERT),
        # so an absent attempts field is covered by that documented contract.
        # https://developers.cloudflare.com/d1/observability/debug-d1/#automatic-retries
        # No other SQL group or caller-supplied operation receives this rule.
        if any(not (write_contract and value is None)
               and (type(value) is not int or value != 1) for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        if write_contract:
            state = journal.check(ticket, now=clock())
            state["schema_upgrade"]["write_execution"] = {
                "native_attempts": attempts,
                "provenance": "d1-nonretryable-write-contract-v1",
            }
            journal._save(state)
        return results

    try:
        schema = await execute(plan.operations[0].sql)
        if _schema_objects(schema[0]) != LEGACY_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        legacy = await execute(plan.operations[1].sql)
        _validated_product_data(legacy[0], legacy[1],
            journal.snapshot()["product_data"]["rows"], LEGACY_INDEX_COUNTS)
        await execute(plan.operations[2].sql)
        verified = await execute(plan.operations[3].sql)
        if _schema_objects(verified[0]) != V5_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = _validated_product_data(verified[1], verified[2], upper, V5_INDEX_COUNTS)
        state = journal.check(ticket, now=clock())
        state.update(product_data=data, schema_version=5,
                     schema_fingerprint=V5_SCHEMA_FINGERPRINT, product_data_verified=True)
        state["schema_upgrade"]["complete"] = True
        journal._save(state)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_UPGRADE_OUTCOME_UNKNOWN")
        raise


_UPGRADE_V6_CASE = "product-schema-v5-to-v6"
_FK_COUNT_SQL = "SELECT COUNT(*) AS violations FROM pragma_foreign_key_check"


@dataclass(frozen=True)
class _SchemaUpgradePlan:
    """Closed product-only plan; generic finite admission retains its ceilings."""
    operations: tuple
    records_mode: bool

    @property
    def rows_read(self):
        return sum(operation.rows_read for operation in self.operations)

    @property
    def rows_written(self):
        return sum(operation.rows_written for operation in self.operations)


def _upgrade_v6_plan(state):
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if (not isinstance(rows, dict) or set(rows) != set(V6_INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(data.get("json"), dict)
            or type(data.get("arrays")) is not int or data["arrays"] < 0):
        raise BudgetError("PREVIEW_DATA_BOUND")
    records_mode = state.get("state_storage_version") == STATE_STORAGE_VERSION
    state_queries = (_RECORD_COUNT_SQL, _STRICT_RECORD_SQL) if records_mode else (_STATE_SQL,)
    # COUNT and structural state/FK traversals include up to three native read
    # attempts. DROP's incoming RESTRICT checks may scan child expenses for
    # every project, including deleted rows that partial list indexes omit.
    # The rebuild copies only projects, not their financial/audit child rows.
    scans = 3 * (16 * (sum(rows.values()) + 4 * rows["app_state"] + 128))
    projects = rows["ledger_projects"]
    migration_reads = (384 * len(UPGRADE_V6_SQL) + 16 * sum(rows.values())
        + 32 * (projects + 1) * (1 + rows["expenses"] + rows["ledger_project_repositories"]))
    # Both old/new project tables, all three unique indexes, named-index copy,
    # DROP/index catalog effects and the final guard insert/delete are retained.
    migration_writes = 256 + 2 * projects * (1 + V6_INDEX_COUNTS["ledger_projects"])
    operations = (
        OperationBound((_SCHEMA_QUERY,), 3 * 384, 0),
        OperationBound((_V6_COUNT_SQL, *state_queries, _FK_COUNT_SQL), scans, 0),
        OperationBound(UPGRADE_V6_SQL, migration_reads, migration_writes),
        OperationBound((_SCHEMA_QUERY, _V6_COUNT_SQL, *state_queries, _FK_COUNT_SQL), 3 * 384 + scans, 0),
    )
    plan = _SchemaUpgradePlan(operations, records_mode)
    if plan.rows_read > 9007199254740991 or plan.rows_written > 9007199254740991:
        raise BudgetError("PREVIEW_DATA_BOUND")
    return plan


def begin_product_schema_upgrade_v6(journal, plan, *, now):
    """Reserve the whole independent v5-to-v6 case before any native query."""
    state = journal.snapshot()
    if (state.get("schema_ready") is not True or state.get("schema_version") != 5
            or state.get("schema_fingerprint") != V5_SCHEMA_FINGERPRINT
            or state["cases"].get("product-schema") != 1
            or state["cases"].get(_UPGRADE_V6_CASE, 0) != 0
            or state.get("schema_upgrade_v6") is not None
            or (state.get("schema_upgrade") or {}).get("complete", True) is not True
            or (state.get("state_record_migration") or {}).get("complete", True) is not True
            or plan != _upgrade_v6_plan(state)):
        raise BudgetError("SCHEMA_V6_UPGRADE_UNREVIEWED")
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=now)
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=now)
    state = journal.check(ticket, now=now)
    state["cases"][_UPGRADE_V6_CASE] = 1
    state["schema_upgrade_v6"] = {"from": 5, "to": 6, "request": ticket, "complete": False,
        "reserved_read": plan.rows_read, "reserved_written": plan.rows_written}
    state["product_data_verified"] = False
    journal._save(state)
    return ticket


async def upgrade_product_schema_v6(binding, journal, *, clock=time.time):
    """One flagged atomic nullable-project upgrade under the existing DO lock.

    Numeric native accounting is mandatory. Read-only groups reserve three
    scans; absent attempts or explicit integer 1..3 retain the full reservation.
    The exact mutation group includes only DDL/DML and the two state-changing
    FK PRAGMA assignments. D1's documented retry eligibility is limited to
    SELECT/EXPLAIN/WITH, so this closed group accepts absent attempts without
    inventing a native value; any explicit write attempt other than integer one
    stops. No other PRAGMA/SQL receives that rule. The in-batch guard checks all
    FKs before deferral is disabled; child rows and IDs never need rewriting.
    https://developers.cloudflare.com/d1/observability/debug-d1/#automatic-retries
    """
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if (_current_schema(state) or
            (state.get("schema_version"), state.get("schema_fingerprint"))
                in {(6, V6_SCHEMA_FINGERPRINT), (7, V7_SCHEMA_FINGERPRINT), (8, V8_SCHEMA_FINGERPRINT)}):
        marker = state.get("schema_upgrade_v6")
        if marker is not None and marker.get("complete") is not True:
            raise BudgetError("SCHEMA_V6_UPGRADE_UNREVIEWED")
        return
    plan = _upgrade_v6_plan(state)
    ticket = begin_product_schema_upgrade_v6(journal, plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)

    async def execute(index):
        operation = plan.operations[index]
        results = await meter.batch([meter.prepare(sql) for sql in operation.sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        write_group = index == 2
        if any(value is not None and
               (type(value) is not int or not 1 <= value <= (1 if write_group else 3))
               for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        if write_group:
            saved = journal.check(ticket, now=clock())
            saved["schema_upgrade_v6"]["write_execution"] = {
                "native_attempts": attempts,
                "provenance": "d1-atomic-write-batch-with-fk-pragma-v1"}
            journal._save(saved)
        return results

    def validate(results, offset):
        if plan.records_mode:
            data = _record_product_data(results[offset], results[offset + 1],
                state["product_data"]["rows"], results[offset + 2], tables=V6_INDEX_COUNTS)
        else:
            data = _validated_product_data(results[offset], results[offset + 1],
                state["product_data"]["rows"], V6_INDEX_COUNTS)
        if data["rows"] != state["product_data"]["rows"]:
            raise BudgetError("PREVIEW_DATA_BOUND")
        fk = list(_field(results[-1], "results", []))
        if len(fk) != 1 or _field(fk[0], "violations") != 0:
            raise BudgetError("PREVIEW_FOREIGN_KEY_MISMATCH")
        return data

    try:
        schema = await execute(0)
        if _schema_objects(schema[0]) != V5_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        validate(await execute(1), 0)
        await execute(2)
        verified = await execute(3)
        if _schema_objects(verified[0]) != V6_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = validate(verified, 1)
        saved = journal.check(ticket, now=clock())
        saved.update(product_data=data, schema_version=6,
            schema_fingerprint=V6_SCHEMA_FINGERPRINT, product_data_verified=True)
        saved["schema_upgrade_v6"]["complete"] = True
        journal._save(saved)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_V6_UPGRADE_OUTCOME_UNKNOWN")
        raise


_UPGRADE_V7_CASE = "product-schema-v6-to-v7"


def _upgrade_v7_plan(state):
    """Compile the closed link/rule/audit extension from verified v6 counts."""
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if (not isinstance(rows, dict) or set(rows) != set(V6_INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(data.get("json"), dict)
            or type(data.get("arrays")) is not int or data["arrays"] < 0):
        raise BudgetError("PREVIEW_DATA_BOUND")
    records_mode = state.get("state_storage_version") == STATE_STORAGE_VERSION
    state_queries = (_RECORD_COUNT_SQL, _STRICT_RECORD_SQL) if records_mode else (_STATE_SQL,)
    # Read-only groups include all three possible native attempts, complete
    # count/state/FK traversal and schema catalog reads. No data is exported.
    scans = 3 * (16 * (sum(rows.values()) + 4 * rows["app_state"] + 128))
    projects, events = rows["ledger_projects"], rows["expense_events"]
    migration_reads = (384 * len(UPGRADE_V7_SQL) + 32 * sum(rows.values())
                       + 64 * projects + 32 * events)
    # Conservatively reserve both ADD COLUMN rewrites, all existing project
    # indexes, old/new event rows/indexes, new catalogs and temporary FK guard.
    migration_writes = (384 + 4 * projects * (1 + 2 * V6_INDEX_COUNTS["ledger_projects"])
                        + 2 * events * (1 + V6_INDEX_COUNTS["expense_events"]))
    operations = (
        OperationBound((_SCHEMA_QUERY,), 3 * 384, 0),
        OperationBound((_V6_COUNT_SQL, *state_queries, _FK_COUNT_SQL), scans, 0),
        OperationBound(UPGRADE_V7_SQL, migration_reads, migration_writes),
        OperationBound((_SCHEMA_QUERY, _V7_COUNT_SQL, *state_queries, _FK_COUNT_SQL),
                       3 * 384 + scans, 0),
    )
    plan = _SchemaUpgradePlan(operations, records_mode)
    if plan.rows_read > 9007199254740991 or plan.rows_written > 9007199254740991:
        raise BudgetError("PREVIEW_DATA_BOUND")
    return plan


def begin_product_schema_upgrade_v7(journal, plan, *, now):
    """One append-only case in the same singleton journal and database."""
    state = journal.snapshot()
    if (state.get("schema_ready") is not True or state.get("schema_version") != 6
            or state.get("schema_fingerprint") != V6_SCHEMA_FINGERPRINT
            or state.get("product_data_verified") is not True
            or state["cases"].get("product-schema") != 1
            or state["cases"].get(_UPGRADE_V7_CASE, 0) != 0
            or state.get("schema_upgrade_v7") is not None
            or any((state.get(marker) or {}).get("complete", True) is not True
                   for marker in ("schema_upgrade", "schema_upgrade_v6", "state_record_migration"))
            or plan != _upgrade_v7_plan(state)):
        raise BudgetError("SCHEMA_V7_UPGRADE_UNREVIEWED")
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=now)
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=now)
    state = journal.check(ticket, now=now)
    state["cases"][_UPGRADE_V7_CASE] = 1
    state["schema_upgrade_v7"] = {"from": 6, "to": 7, "request": ticket, "complete": False,
        "reserved_read": plan.rows_read, "reserved_written": plan.rows_written}
    state["product_data_verified"] = False
    journal._save(state)
    return ticket


async def upgrade_product_schema_v7(binding, journal, *, clock=time.time):
    """Flagged atomic v6-to-v7 extension; the caller holds the existing DO lock.

    All prior markers and accounting are retained. Unknown outcomes cannot be
    retried, resumed or reset. Read groups reserve all three possible attempts
    even when native attempt metadata is absent; the exact compiled write
    group instead uses D1's nonretryable write contract. Every result still
    requires complete numeric rows-read/written metadata.
    """
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if (_current_schema(state) or
            (state.get("schema_version"), state.get("schema_fingerprint"))
                in {(7, V7_SCHEMA_FINGERPRINT), (8, V8_SCHEMA_FINGERPRINT)}):
        marker = state.get("schema_upgrade_v7")
        if marker is not None and marker.get("complete") is not True:
            raise BudgetError("SCHEMA_V7_UPGRADE_UNREVIEWED")
        return
    plan = _upgrade_v7_plan(state)
    ticket = begin_product_schema_upgrade_v7(journal, plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)
    upper = {**state["product_data"]["rows"], "expense_recurring_rules": 0,
             "expense_recurring_occurrences": 0}

    async def execute(index):
        results = await meter.batch([meter.prepare(sql) for sql in plan.operations[index].sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        if any(value is not None and
               (type(value) is not int or not 1 <= value <= (1 if index == 2 else 3))
               for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        if index == 2:
            saved = journal.check(ticket, now=clock())
            saved["schema_upgrade_v7"]["write_execution"] = {
                "native_attempts": attempts, "provenance": "d1-nonretryable-write-contract-v1"}
            journal._save(saved)
        return results

    def validate(results, offset, *, old=False):
        tables = V6_INDEX_COUNTS if old else V7_INDEX_COUNTS
        ceiling = state["product_data"]["rows"] if old else upper
        if plan.records_mode:
            data = _record_product_data(results[offset], results[offset + 1], ceiling,
                                        results[offset + 2], tables=tables)
        else:
            data = _validated_product_data(results[offset], results[offset + 1], ceiling, tables)
        if data["rows"] != ceiling:
            raise BudgetError("PREVIEW_DATA_BOUND")
        fk = list(_field(results[-1], "results", []))
        if len(fk) != 1 or _field(fk[0], "violations") != 0:
            raise BudgetError("PREVIEW_FOREIGN_KEY_MISMATCH")
        return data

    try:
        schema = await execute(0)
        if _schema_objects(schema[0]) != V6_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        validate(await execute(1), 0, old=True)
        await execute(2)
        verified = await execute(3)
        if _schema_objects(verified[0]) != V7_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = validate(verified, 1)
        saved = journal.check(ticket, now=clock())
        saved.update(product_data=data, schema_version=7,
                     schema_fingerprint=V7_SCHEMA_FINGERPRINT, product_data_verified=True)
        saved["schema_upgrade_v7"]["complete"] = True
        journal._save(saved)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_V7_UPGRADE_OUTCOME_UNKNOWN")
        raise


_UPGRADE_V8_CASE = "product-schema-v7-to-v8"


def _upgrade_v8_plan(state):
    """Compile the closed invitation/request/audit extension from verified v7 counts."""
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if (not isinstance(rows, dict) or set(rows) != set(V7_INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(data.get("json"), dict)
            or type(data.get("arrays")) is not int or data["arrays"] < 0):
        raise BudgetError("PREVIEW_DATA_BOUND")
    records_mode = state.get("state_storage_version") == STATE_STORAGE_VERSION
    state_queries = (_RECORD_COUNT_SQL, _STRICT_RECORD_SQL) if records_mode else (_STATE_SQL,)
    # Read-only groups include all three possible native attempts, complete
    # count/state/FK traversal and schema catalog reads. No data is exported.
    scans = 3 * (16 * (sum(rows.values()) + 4 * rows["app_state"] + 128))
    invites, events = rows["workspace_invites"], rows["workspace_events"]
    migration_reads = (384 * len(UPGRADE_V8_SQL) + 32 * sum(rows.values())
                       + 64 * invites + 32 * events)
    # Preserve old/new rows, all old/new indexes, new catalog writes and the
    # final FK guard. No existing identity, finance or recurring rows change.
    migration_writes = (384 + invites * (2 + V7_INDEX_COUNTS["workspace_invites"]
                                         + V8_INDEX_COUNTS["workspace_invites"])
                        + 2 * events * (1 + V7_INDEX_COUNTS["workspace_events"]))
    operations = (
        OperationBound((_SCHEMA_QUERY,), 3 * 384, 0),
        OperationBound((_V7_COUNT_SQL, *state_queries, _FK_COUNT_SQL), scans, 0),
        OperationBound(UPGRADE_V8_SQL, migration_reads, migration_writes),
        OperationBound((_SCHEMA_QUERY, _V8_COUNT_SQL, *state_queries, _FK_COUNT_SQL),
                       3 * 384 + scans, 0),
    )
    plan = _SchemaUpgradePlan(operations, records_mode)
    if plan.rows_read > 9007199254740991 or plan.rows_written > 9007199254740991:
        raise BudgetError("PREVIEW_DATA_BOUND")
    return plan


def begin_product_schema_upgrade_v8(journal, plan, *, now):
    """One append-only case in the same singleton journal and database."""
    state = journal.snapshot()
    if (state.get("schema_ready") is not True or state.get("schema_version") != 7
            or state.get("schema_fingerprint") != V7_SCHEMA_FINGERPRINT
            or state.get("product_data_verified") is not True
            or state["cases"].get("product-schema") != 1
            or state["cases"].get(_UPGRADE_V8_CASE, 0) != 0
            or state.get("schema_upgrade_v8") is not None
            or any((state.get(marker) or {}).get("complete", True) is not True
                   for marker in ("schema_upgrade", "schema_upgrade_v6", "schema_upgrade_v7", "state_record_migration"))
            or plan != _upgrade_v8_plan(state)):
        raise BudgetError("SCHEMA_V8_UPGRADE_UNREVIEWED")
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=now)
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=now)
    state = journal.check(ticket, now=now)
    state["cases"][_UPGRADE_V8_CASE] = 1
    state["schema_upgrade_v8"] = {"from": 7, "to": 8, "request": ticket, "complete": False,
        "reserved_read": plan.rows_read, "reserved_written": plan.rows_written}
    state["product_data_verified"] = False
    journal._save(state)
    return ticket


async def upgrade_product_schema_v8(binding, journal, *, clock=time.time):
    """Flagged atomic v7-to-v8 extension; the caller holds the existing DO lock.

    All prior markers and accounting are retained. Unknown outcomes cannot be
    retried, resumed or reset. Read groups reserve all three possible attempts
    even when native attempt metadata is absent; the exact compiled write
    group instead uses D1's nonretryable write contract. Every result still
    requires complete numeric rows-read/written metadata.
    """
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if (_current_schema(state) or
            (state.get("schema_version") == 8 and state.get("schema_fingerprint") == V8_SCHEMA_FINGERPRINT)):
        marker = state.get("schema_upgrade_v8")
        if marker is not None and marker.get("complete") is not True:
            raise BudgetError("SCHEMA_V8_UPGRADE_UNREVIEWED")
        return
    plan = _upgrade_v8_plan(state)
    ticket = begin_product_schema_upgrade_v8(journal, plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)
    upper = {**state["product_data"]["rows"], "workspace_join_requests": 0}

    async def execute(index):
        results = await meter.batch([meter.prepare(sql) for sql in plan.operations[index].sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        if any(value is not None and
               (type(value) is not int or not 1 <= value <= (1 if index == 2 else 3))
               for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        if index == 2:
            saved = journal.check(ticket, now=clock())
            saved["schema_upgrade_v8"]["write_execution"] = {
                "native_attempts": attempts, "provenance": "d1-nonretryable-write-contract-v1"}
            journal._save(saved)
        return results

    def validate(results, offset, *, old=False):
        tables = V7_INDEX_COUNTS if old else V8_INDEX_COUNTS
        ceiling = state["product_data"]["rows"] if old else upper
        if plan.records_mode:
            data = _record_product_data(results[offset], results[offset + 1], ceiling,
                                        results[offset + 2], tables=tables)
        else:
            data = _validated_product_data(results[offset], results[offset + 1], ceiling, tables)
        if data["rows"] != ceiling:
            raise BudgetError("PREVIEW_DATA_BOUND")
        fk = list(_field(results[-1], "results", []))
        if len(fk) != 1 or _field(fk[0], "violations") != 0:
            raise BudgetError("PREVIEW_FOREIGN_KEY_MISMATCH")
        return data

    try:
        schema = await execute(0)
        if _schema_objects(schema[0]) != V7_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        validate(await execute(1), 0, old=True)
        await execute(2)
        verified = await execute(3)
        if _schema_objects(verified[0]) != V8_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = validate(verified, 1)
        saved = journal.check(ticket, now=clock())
        saved.update(product_data=data, schema_version=8,
                     schema_fingerprint=V8_SCHEMA_FINGERPRINT, product_data_verified=True)
        if (plan.records_mode and
                saved.get("state_record_integrity_version") != STATE_RECORD_INTEGRITY_VERSION):
            # The upgraded snapshot already passed the expanded strict record
            # proof. Reuse it on the next healthy read without a second scan;
            # retain an existing proof's original ticket across this upgrade.
            saved["state_record_integrity_version"] = STATE_RECORD_INTEGRITY_VERSION
            saved["state_record_integrity_request"] = ticket
        saved["schema_upgrade_v8"]["complete"] = True
        journal._save(saved)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_V8_UPGRADE_OUTCOME_UNKNOWN")
        raise


_UPGRADE_V9_CASE = "product-schema-v8-to-v9"


def _upgrade_v9_plan(state):
    """Compile the closed rolling-activity extension from verified v8 counts."""
    data = state.get("product_data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if (not isinstance(rows, dict) or set(rows) != set(V8_INDEX_COUNTS)
            or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in rows.values())
            or rows["d1_command_guard"] != 0
            or not isinstance(data.get("json"), dict)
            or type(data.get("arrays")) is not int or data["arrays"] < 0):
        raise BudgetError("PREVIEW_DATA_BOUND")
    records_mode = state.get("state_storage_version") == STATE_STORAGE_VERSION
    state_queries = (_RECORD_COUNT_SQL, _STRICT_RECORD_SQL) if records_mode else (_STATE_SQL,)
    # Read-only groups include all three possible native attempts, complete
    # count/state/FK traversal and schema catalog reads. No data is exported.
    scans = 3 * (16 * (sum(rows.values()) + 4 * rows["app_state"] + 128))
    # This creates one empty projection and two empty indexes. No historical
    # rows are copied, rebuilt or deleted. Catalog writes retain a fixed margin.
    migration_reads = 384 * len(UPGRADE_V9_SQL)
    migration_writes = 128
    operations = (
        OperationBound((_SCHEMA_QUERY,), 3 * 384, 0),
        OperationBound((_V8_COUNT_SQL, *state_queries, _FK_COUNT_SQL), scans, 0),
        OperationBound(UPGRADE_V9_SQL, migration_reads, migration_writes),
        OperationBound((_SCHEMA_QUERY, _COUNT_SQL, *state_queries, _FK_COUNT_SQL),
                       3 * 384 + scans, 0),
    )
    plan = _SchemaUpgradePlan(operations, records_mode)
    if plan.rows_read > 9007199254740991 or plan.rows_written > 9007199254740991:
        raise BudgetError("PREVIEW_DATA_BOUND")
    return plan


def begin_product_schema_upgrade_v9(journal, plan, *, now):
    """One append-only case in the same singleton journal and database."""
    state = journal.snapshot()
    if (state.get("schema_ready") is not True or state.get("schema_version") != 8
            or state.get("schema_fingerprint") != V8_SCHEMA_FINGERPRINT
            or state.get("product_data_verified") is not True
            or state["cases"].get("product-schema") != 1
            or state["cases"].get(_UPGRADE_V9_CASE, 0) != 0
            or state.get("schema_upgrade_v9") is not None
            or any((state.get(marker) or {}).get("complete", True) is not True
                   for marker in ("schema_upgrade", "schema_upgrade_v6", "schema_upgrade_v7",
                                  "schema_upgrade_v8", "state_record_migration"))
            or plan != _upgrade_v9_plan(state)):
        raise BudgetError("SCHEMA_V9_UPGRADE_UNREVIEWED")
    if (not journal.product_operations and
            (state["reserved_read"] + plan.rows_read > journal.read_ceiling
             or state["reserved_written"] + plan.rows_written > WRITE_CEILING)):
        journal._reject("BUDGET_EXHAUSTED")
    ticket = journal.begin_product(now=now)
    journal.reserve_operation(ticket, reads=plan.rows_read, writes=plan.rows_written, now=now)
    state = journal.check(ticket, now=now)
    state["cases"][_UPGRADE_V9_CASE] = 1
    state["schema_upgrade_v9"] = {"from": 8, "to": 9, "request": ticket, "complete": False,
        "reserved_read": plan.rows_read, "reserved_written": plan.rows_written}
    state["product_data_verified"] = False
    journal._save(state)
    return ticket


async def upgrade_product_schema_v9(binding, journal, *, clock=time.time):
    """Flagged atomic v8-to-v9 extension; the caller holds the existing DO lock.

    All prior markers and accounting are retained. Unknown outcomes cannot be
    retried, resumed or reset. Read groups reserve all three possible attempts
    even when native attempt metadata is absent; the exact compiled write
    group instead uses D1's nonretryable write contract. Every result still
    requires complete numeric rows-read/written metadata.
    """
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if (_current_schema(state) or
            (state.get("schema_version") == 9 and state.get("schema_fingerprint") == SCHEMA_FINGERPRINT)):
        marker = state.get("schema_upgrade_v9")
        if marker is not None and marker.get("complete") is not True:
            raise BudgetError("SCHEMA_V9_UPGRADE_UNREVIEWED")
        return
    plan = _upgrade_v9_plan(state)
    ticket = begin_product_schema_upgrade_v9(journal, plan, now=clock())
    meter = MeteredD1(binding, journal, ticket, plan, clock=clock)
    upper = {**state["product_data"]["rows"], "ledger_activity_events": 0}

    async def execute(index):
        results = await meter.batch([meter.prepare(sql) for sql in plan.operations[index].sql])
        attempts = [_field(_field(result, "meta"), "total_attempts") for result in results]
        if any(value is not None and
               (type(value) is not int or not 1 <= value <= (1 if index == 2 else 3))
               for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        if index == 2:
            saved = journal.check(ticket, now=clock())
            saved["schema_upgrade_v9"]["write_execution"] = {
                "native_attempts": attempts, "provenance": "d1-nonretryable-write-contract-v1"}
            journal._save(saved)
        return results

    def validate(results, offset, *, old=False):
        tables = V8_INDEX_COUNTS if old else INDEX_COUNTS
        ceiling = state["product_data"]["rows"] if old else upper
        if plan.records_mode:
            data = _record_product_data(results[offset], results[offset + 1], ceiling,
                                        results[offset + 2], tables=tables)
        else:
            data = _validated_product_data(results[offset], results[offset + 1], ceiling, tables)
        if data["rows"] != ceiling:
            raise BudgetError("PREVIEW_DATA_BOUND")
        fk = list(_field(results[-1], "results", []))
        if len(fk) != 1 or _field(fk[0], "violations") != 0:
            raise BudgetError("PREVIEW_FOREIGN_KEY_MISMATCH")
        return data

    try:
        schema = await execute(0)
        if _schema_objects(schema[0]) != V8_SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        validate(await execute(1), 0, old=True)
        await execute(2)
        verified = await execute(3)
        if _schema_objects(verified[0]) != SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = validate(verified, 1)
        saved = journal.check(ticket, now=clock())
        saved.update(product_data=data, schema_version=9,
                     schema_fingerprint=SCHEMA_FINGERPRINT, product_data_verified=True)
        if (plan.records_mode and
                saved.get("state_record_integrity_version") != STATE_RECORD_INTEGRITY_VERSION):
            # The upgraded snapshot already passed the expanded strict record
            # proof. Reuse it on the next healthy read without a second scan;
            # retain an existing proof's original ticket across this upgrade.
            saved["state_record_integrity_version"] = STATE_RECORD_INTEGRITY_VERSION
            saved["state_record_integrity_request"] = ticket
        saved["schema_upgrade_v9"]["complete"] = True
        journal._save(saved)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_V9_UPGRADE_OUTCOME_UNKNOWN")
        raise


def initial_data():
    return {"rows": {t: 4 if t == "app_state" else 0 for t in INDEX_COUNTS},
            "json": {}, "arrays": 0}


def reconcile_schema_reads(journal):
    """One audited release of the successful, non-retried schema batch margin.

    Keep every legacy product/read-retry reservation and every write reservation.
    No D1 dispatch, counter reset, or recovery from unknown outcomes is allowed.
    """
    state = journal.snapshot()
    if (state.get("schema_read_reconciled") or not state.get("schema_ready")
            or state.get("schema_version") not in (None, 4)
            or state["stopped"] != "BUDGET_EXHAUSTED"
            or state["cases"].get("product-schema") != 1):
        return
    evidence = state["evidence"]
    if (len(evidence) < 2 or any(e.get("complete", True) is not True for e in evidence)
            or any(type(e.get(k)) is not int or e[k] < 0
                   for e in evidence for k in ("rows_read", "rows_written"))
            or sum(e["rows_read"] for e in evidence) != state["actual_read"]
            or sum(e["rows_written"] for e in evidence) != state["actual_written"]):
        return
    first, ddl = evidence[:2]
    if (first.get("request") != 1 or first.get("operation") != 0
            or first["rows_written"] != 0 or first["rows_read"] > 384
            or ddl.get("request") != 1 or ddl.get("operation") != 1
            or not 0 < ddl["rows_written"] <= 128 or ddl["rows_read"] > 2000
            or any(e.get("request") == 1 for e in evidence[2:])):
        return
    margin = 2000 - ddl["rows_read"]
    if not margin or state["reserved_read"] < 2384 or state["reserved_read"] - margin < state["actual_read"]:
        return
    state["reserved_read"] -= margin
    state["read_margin_released"] = state.get("read_margin_released", 0) + margin
    state["schema_read_reconciled"] = {"request": 1, "released": margin}
    state["stopped"] = state["active"] = state["deadline"] = None
    journal._save(state)


async def initialize_product(binding, journal, *, clock=time.time):
    if journal.snapshot().get("schema_ready"):
        if not _current_schema(journal.snapshot()):
            raise BudgetError("SCHEMA_UPGRADE_REQUIRED")
        return
    ticket = journal.begin(_INITIAL, now=clock())
    meter = MeteredD1(binding, journal, ticket, _INITIAL, clock=clock)
    try:
        first = await meter.batch([meter.prepare(_EMPTY_SQL)])
        rows = list(_field(first[0], "results", []))
        if len(rows) >= 65 or any(not str(_field(r, "name", "")).startswith(
                ("_cf_", "sqlite_")) for r in rows):
            journal._reject("PREVIEW_DATABASE_NOT_EMPTY")
        await meter.batch([meter.prepare(sql) for sql in SCHEMA_SQL])
        journal.save_product_state(ticket, initial_data(), now=clock(), initialized=True)
        state = journal.check(ticket, now=clock())
        state.update(schema_version=SCHEMA_VERSION, schema_fingerprint=SCHEMA_FINGERPRINT,
                     state_storage_version=STATE_STORAGE_VERSION)
        state["state_record_migration"] = {"version": STATE_STORAGE_VERSION,
            "request": ticket, "complete": True, "fresh": True, "copied_records": 0}
        journal._save(state)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("INITIALIZATION_OUTCOME_UNKNOWN")
        raise


@dataclass(frozen=True)
class _StateRecordPlan:
    operations: tuple
    copied_records: int


def _compile_state_record_cutover(rows, physical_rows, now):
    """One closed, atomic copy/clear batch derived only from bounded old state."""
    records, sources = legacy_record_rows(rows)
    checks = ["(SELECT COUNT(*) FROM app_state)=?",
              "NOT EXISTS(SELECT 1 FROM app_state WHERE name GLOB 'record:*')",
              "NOT EXISTS(SELECT 1 FROM d1_command_guard)"]
    guard_params = [physical_rows]
    # Guard every observed source, including preserved unknown-empty rows.
    # Otherwise an unrelated row could grow after observation and only be
    # detected by the postcommit structural check.
    for row in rows:
        name, payload = row["name"], row["payload"]
        checks.append("EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)")
        guard_params.extend((name, payload))
    commands = [("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN " +
                 " AND ".join(checks) + " THEN 1 ELSE 0 END)", tuple(guard_params))]
    for name, payload, empty in sources:
        identity = "json_extract(j.value,'$.eventId')" if name == "billingPendingUpdates" else "j.key"
        commands.append((f"""INSERT INTO app_state(name,payload,updated_at)
            SELECT ?||{identity},j.value,? FROM app_state a,json_each(a.payload) j
            WHERE a.name=? AND a.payload=?""", (f"record:{name}:", now, name, payload)))
    for name, payload, empty in sources:
        commands.extend([
            ("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?",
             (empty, now, name, payload)),
            ("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)", ())])
    commands.append(("DELETE FROM d1_command_guard", ()))
    if len(commands) > 64:
        raise ValueError("state cutover batch exceeds reviewed bound")
    # PK insert entries, worst-case old-row index replacements and all guard
    # insert/deletes. Preserve every write reservation after dispatch.
    writes = 2 * len(records) + 3 * len(sources) + 2 * (len(sources) + 1)
    reads = 8 * (physical_rows + len(records) + 16) * (len(records) + len(sources) + 16)
    parameters = tuple(tuple(ParameterBound("integer", minimum=value, maximum=value)
                            if type(value) is int else ParameterBound("text", max_bytes=max(1, len(value.encode("utf-8"))))
                            for value in params) for _, params in commands)
    bound = OperationBound(tuple(sql for sql, _ in commands), reads, writes, parameters=parameters)
    return _StateRecordPlan((bound,), len(records)), commands


async def migrate_product_state_records(binding, journal, *, clock=time.time):
    """Explicit one-shot state cutover, same D1 schema and same DO journal.

    The caller holds the preview lock. No route/helper GET migrates implicitly.
    An incomplete or unknown outcome cannot replay, reset or recover this case.
    """
    state = journal.snapshot()
    if state["stopped"]:
        raise BudgetError(state["stopped"])
    if state.get("state_storage_version") == STATE_STORAGE_VERSION:
        marker = state.get("state_record_migration")
        if marker is not None and marker.get("complete") is not True:
            raise BudgetError("STATE_STORAGE_MIGRATION_UNAVAILABLE")
        return
    if not _current_schema(state):
        raise BudgetError("SCHEMA_UPGRADE_REQUIRED")
    ticket = journal.begin_state_record_migration(now=clock())
    source_meter = ProductMeteredD1(binding, journal, ticket, clock=clock)
    try:
        data = state["product_data"]
        read_bound = 3 * (sum(data["rows"].values()) + data["rows"]["app_state"] + 128)
        observed = await source_meter._operation(
            [source_meter.prepare(_COUNT_SQL), source_meter.prepare(_STATE_SQL)], read_bound, 0)
        _validated_product_data(observed[0], observed[1], data["rows"], INDEX_COUNTS)
        rows = [{"name": _field(row, "name"), "payload": _field(row, "payload")}
                for row in _field(observed[1], "results", [])]
        plan, commands = _compile_state_record_cutover(rows, data["rows"]["app_state"], int(clock()))
        journal.reserve_operation(ticket, reads=plan.operations[0].rows_read,
                                  writes=plan.operations[0].rows_written, now=clock())
        current = journal.check(ticket, now=clock())
        current["state_record_migration"].update(copied_records=plan.copied_records,
            source_rows=len(rows), reserved_read=plan.operations[0].rows_read,
            reserved_written=plan.operations[0].rows_written)
        journal._save(current)
        meter = MeteredD1(binding, journal, ticket, plan, clock=clock)
        result = await meter.batch([meter.prepare(sql).bind(*params) for sql, params in commands])
        attempts = [_field(_field(item, "meta"), "total_attempts") for item in result]
        if any(value is not None and (type(value) is not int or value != 1) for value in attempts):
            journal._reject("MIGRATION_ATTEMPTS_UNPROVEN")
        upper = dict(data["rows"])
        upper["app_state"] += plan.copied_records
        await source_meter._refresh_records(upper, strict=True)
        if sum(source_meter.data["records"].values()) != plan.copied_records:
            journal._reject("STATE_STORAGE_COPY_MISMATCH")
        current = journal.check(ticket, now=clock())
        current["state_storage_version"] = STATE_STORAGE_VERSION
        current["state_record_migration"].update(complete=True, write_execution={
            "native_attempts": attempts, "provenance": "d1-nonretryable-write-contract-v1"})
        journal._save(current)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("STATE_STORAGE_OUTCOME_UNKNOWN")
        raise


class ProductMeteredD1(MeteredD1):
    def __init__(self, binding, journal, ticket, *, clock=time.time, rate_limiter=None, rate_channel="read"):
        self._binding, self.journal, self.ticket, self.clock = binding, journal, ticket, clock
        self.data = journal.snapshot()["product_data"]
        if (set(self.data["rows"]) != set(INDEX_COUNTS)
                or journal.snapshot().get("schema_ready") and not _current_schema(journal.snapshot())):
            raise BudgetError("SCHEMA_UPGRADE_REQUIRED")
        self.calls = 0
        self.inflight = False
        self.cardinality_verified = journal.snapshot().get("product_data_verified") is True
        self.records_mode = journal.snapshot().get("state_storage_version") == STATE_STORAGE_VERSION
        self.records_integrity_verified = (self.records_mode and self.cardinality_verified
            and journal.snapshot().get("state_record_integrity_version") == STATE_RECORD_INTEGRITY_VERSION)
        self.rate_limiter, self.rate_channel = rate_limiter, rate_channel
        self.rate_rejection = None
        self._actor_admitted = False

    def observe_authenticated_actor(self, actor_id):
        if self.rate_limiter is not None and not self._actor_admitted:
            from .cloudflare_preview_rate import PreviewRateLimit
            try:
                self.rate_limiter.actor(actor_id, channel=self.rate_channel, now=self.clock())
            except PreviewRateLimit as error:
                self.rate_rejection = error
                raise
            self._actor_admitted = True

    def accounted_outcome(self):
        """Safe only after cancellation has awaited every native dispatch."""
        return (not self.inflight and self.cardinality_verified
                and self.journal.snapshot()["stopped"] is None)

    async def ensure_cardinality(self):
        # The preview database is written only through the existing singleton
        # journal. A completed, persisted refresh remains authoritative until
        # the next mutation, which always refreshes before closing its ticket.
        # Avoid scanning every table for each healthy read-only request.
        if not self.cardinality_verified or (self.records_mode and not self.records_integrity_verified):
            await self.refresh()

    async def _operation(self, statements, reads, writes):
        self.journal.check(self.ticket, now=self.clock())
        if self.calls >= 128:
            self.journal._reject("OPERATION_LIMIT")
        self.calls += 1
        self.journal.reserve_operation(self.ticket, reads=reads, writes=writes, now=self.clock())
        bound = OperationBound(tuple(s.sql for s in statements), reads, writes)
        self.inflight = True
        try:
            results = await self._dispatch(statements, self.calls, bound)
        finally:
            self.inflight = False
        # A complete single-attempt result proves this group's unused read
        # margin. Missing attempts or any retry retains the entire reservation.
        # Write reservations always remain charged, including index effects.
        if all(type(_field(_field(r, "meta"), "total_attempts")) is int
               and _field(_field(r, "meta"), "total_attempts") == 1 for r in results):
            self.journal.settle_product_reads(self.ticket, self.calls, reads, now=self.clock())
        return results

    async def refresh(self, upper=None):
        if self.records_mode:
            return await self._refresh_records(upper, strict=not self.records_integrity_verified)
        rows = upper or self.data["rows"]
        # COUNT traversals plus the bounded app_state scan, including at most
        # three native attempts for this read-only group. No application retry.
        read_bound = 3 * (sum(rows.values()) + rows["app_state"] + 16)
        result = await self._operation([self.prepare(_COUNT_SQL), self.prepare(_STATE_SQL)], read_bound, 0)
        count_rows = list(_field(result[0], "results", []))
        states = list(_field(result[1], "results", []))
        if len(count_rows) != 1 or len(states) > 6:
            self.journal._reject("PREVIEW_DATA_BOUND")
        counts = {t: _field(count_rows[0], t) for t in INDEX_COUNTS}
        if any(type(n) is not int or n < 0 or n > rows[t] for t, n in counts.items()):
            self.journal._reject("PREVIEW_DATA_BOUND")
        if counts["d1_command_guard"]:
            self.journal._reject("NONEMPTY_GUARD")
        sizes, arrays = {}, 0
        for row in states:
            payload = _field(row, "payload")
            if not isinstance(payload, str) or len(payload.encode("utf-8")) > 8192:
                self.journal._reject("PREVIEW_DATA_BOUND")
            data = json.loads(payload)
            sizes[str(_field(row, "name"))] = len(data) if isinstance(data, (dict, list)) else 0
            arrays = max(arrays, _json_size(data))
        self.data = {"rows": counts, "json": sizes, "arrays": arrays}
        self.journal.save_product_state(self.ticket, self.data, now=self.clock(), verified=True)
        self.cardinality_verified = True

    async def _refresh_records(self, upper=None, *, strict=False):
        rows = upper or self.data["rows"]
        sql = (_COUNT_SQL, _RECORD_COUNT_SQL, _STRICT_RECORD_SQL) if strict else (_COUNT_SQL, _RECORD_COUNT_SQL)
        read_bound = 3 * (sum(rows.values()) + 4 * rows["app_state"] + 128)
        result = await self._operation([self.prepare(query) for query in sql], read_bound, 0)
        try:
            self.data = _record_product_data(result[0], result[1], rows, result[2] if strict else None)
        except BudgetError as error:
            self.journal._reject(str(error))
        self.journal.save_product_state(self.ticket, self.data, now=self.clock(), verified=True)
        if strict:
            state = self.journal.check(self.ticket, now=self.clock())
            state["state_record_integrity_version"] = STATE_RECORD_INTEGRITY_VERSION
            state["state_record_integrity_request"] = self.ticket
            self.journal._save(state)
        self.cardinality_verified = True
        self.records_mode = True
        if strict:
            self.records_integrity_verified = True

    async def batch(self, statements):
        statements = list(statements)
        if not statements or len(statements) > 64 or any(
                not isinstance(s, _Statement) or s.meter is not self for s in statements):
            self.journal._reject("UNREVIEWED_SQL")
        reads = writes = guards = 0
        upper = dict(self.data["rows"])
        # Input-envelope rejection happens before this group's dispatch. It
        # must not poison the shared accounting journal for every other user.
        # Routes already map ValueError to their ordinary invalid-input DTO.
        # SQL/cardinality failures below remain accounting integrity stops.
        arrays = [_input_bound(s.params, sql=s.sql) for s in statements]
        if sum(len(value.encode("utf-8")) for statement in statements for value in statement.params
               if isinstance(value, str)) > 8 * 1024 * 1024:
            raise ValueError("preview operation parameters exceed input bound")
        try:
            for s, array_size in zip(statements, arrays):
                tokens = _tokens(s.sql)
                cost = sql_write_bound(s.sql, guards)
                references = re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", s.sql, re.I)
                if any(table.lower() not in {*INDEX_COUNTS, "json_each", "sqlite_master", "sqlite_schema"}
                       for table in references):
                    raise ValueError("unknown referenced table")
                physical = [self.data["rows"].get(t.lower(), 64 if t.lower() in {
                    "sqlite_master", "sqlite_schema"} else 0) for t in references if t.lower() != "json_each"]
                names = {t.lower() for t in references if t.lower() != "json_each"}
                if len(names) == 1:
                    table = next(iter(names))
                    keys = _where_keys(tokens)
                    primary = PRIMARY_KEYS.get(table, [])
                    if (primary and set(primary) <= keys) or (table == "api_keys" and "key_hash" in keys):
                        physical = [1 for _ in physical]
                loops = s.sql.lower().count("json_each(")
                virtual = max([1, array_size, self.data["arrays"]] + list(self.data["json"].values())) ** loops if loops else 1
                # SELECTs have only traversal costs. DML additionally reserves
                # eight indexed uniqueness/foreign-key probes for scalar rows.
                per_read = (0 if tokens[0] == "SELECT" else 8) + 3 * max([1] + physical) * max(1, len(physical)) * virtual + 2 * loops * virtual
                reads += per_read
                writes += cost
                if cost or tokens[0] in {"INSERT", "UPDATE", "DELETE"}:
                    table = _target(tokens)
                    if table == "d1_command_guard":
                        guards = guards + 1 if tokens[0] == "INSERT" else 0
                    elif tokens[0] == "INSERT":
                        upper[table] += 1
            if guards:
                raise ValueError("guard must be cleared in its atomic batch")
        except (ValueError, UnicodeError, RecursionError, IndexError):
            self.journal._reject("UNREVIEWED_SQL_BOUND")
        # D1 itself can retry read-only queries twice. Include all attempts.
        if writes == 0:
            reads *= 3
        if writes:
            self.cardinality_verified = False
        result = await self._operation(statements, reads, writes)
        if writes:
            await self.refresh(upper)
        return result
