"""Product-wide preview admission with per-batch, durable row reservations.

The application owns SQL; clients cannot submit SQL. Writes additionally need
scalar INSERTs or unique-key UPDATE/DELETE fences. Index effects and temporary
guard cleanup are reserved before dispatch. There is no application retry.
"""
from __future__ import annotations

import json
import re
import time

from .cloudflare_preview_schema import (
    INDEX_COUNTS, PRIMARY_KEYS, SCHEMA_SQL, SCHEMA_VERSION, SCHEMA_FINGERPRINT,
    SCHEMA_OBJECTS, LEGACY_INDEX_COUNTS, LEGACY_SCHEMA_OBJECTS,
    LEGACY_SCHEMA_FINGERPRINT, UPGRADE_SQL,
)
from .cloudflare_validation_budget import (
    BudgetError, MeteredD1, OperationBound, RequestPlan, _Statement, _field,
    READ_CEILING, WRITE_CEILING,
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


def _expense_json_parameters(sql):
    """Locate only reviewed JSON columns in simple scalar expense INSERTs."""
    tokens = _tokens(sql) if sql else []
    columns_by_table = {"EXPENSE_EVENTS": {"BEFORE_JSON", "AFTER_JSON"},
                        "EXPENSE_CREATE_IDEMPOTENCY": {"RESPONSE_JSON"}}
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
    # Only stored expense JSON gets this 16 KiB envelope; plain text/app_state
    # and every other SQL parameter retain the original 8 KiB ceiling.
    expanded = _expense_json_parameters(sql)
    size = 0
    for index, value in enumerate(params):
        if value is None or type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
            continue
        if type(value) is not str or len(value.encode("utf-8")) > (16384 if index in expanded else 8192):
            raise ValueError("preview parameter exceeds input bound")
        if index in expanded:
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


_COUNT_SQL = "SELECT " + ",".join(
    f'(SELECT COUNT(*) FROM {table}) AS {table}' for table in INDEX_COUNTS)
_STATE_SQL = "SELECT name,payload FROM app_state LIMIT 7"
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
    writes = 128 + projects * (1 + INDEX_COUNTS["ledger_project_repositories"]
                              + 2 * (1 + 2 * LEGACY_INDEX_COUNTS["ledger_projects"]))
    operations = (
        OperationBound((_SCHEMA_QUERY,), 384, 0),
        OperationBound((_LEGACY_COUNT_SQL, _STATE_SQL), _read_bound(rows), 0),
        OperationBound(UPGRADE_SQL, 384 * len(UPGRADE_SQL) + 32 * projects, writes),
        OperationBound((_SCHEMA_QUERY, _COUNT_SQL, _STATE_SQL), 384 + _read_bound(upper), 0),
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
    state["schema_upgrade"] = {"from": 4, "to": SCHEMA_VERSION,
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
    if _current_schema(journal.snapshot()):
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
        if _schema_objects(verified[0]) != SCHEMA_OBJECTS:
            journal._reject("PREVIEW_SCHEMA_MISMATCH")
        data = _validated_product_data(verified[1], verified[2], upper, INDEX_COUNTS)
        state = journal.check(ticket, now=clock())
        state.update(product_data=data, schema_version=SCHEMA_VERSION,
                     schema_fingerprint=SCHEMA_FINGERPRINT, product_data_verified=True)
        state["schema_upgrade"]["complete"] = True
        journal._save(state)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("SCHEMA_UPGRADE_OUTCOME_UNKNOWN")
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
        state.update(schema_version=SCHEMA_VERSION, schema_fingerprint=SCHEMA_FINGERPRINT)
        journal._save(state)
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("INITIALIZATION_OUTCOME_UNKNOWN")
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
        if not self.cardinality_verified:
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
