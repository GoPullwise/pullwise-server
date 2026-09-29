"""Product-wide preview admission with per-batch, durable row reservations.

The application owns SQL; clients cannot submit SQL. Writes additionally need
scalar INSERTs or unique-key UPDATE/DELETE fences. Index effects and temporary
guard cleanup are reserved before dispatch. There is no application retry.
"""
from __future__ import annotations

import json
import re
import time

from .cloudflare_preview_schema import INDEX_COUNTS, PRIMARY_KEYS, SCHEMA_SQL
from .cloudflare_validation_budget import (
    BudgetError, MeteredD1, OperationBound, RequestPlan, _Statement, _field,
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


def _input_bound(params):
    size = 0
    for value in params:
        if value is None or type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
            continue
        if type(value) is not str or len(value.encode("utf-8")) > 8192:
            raise ValueError("preview parameter exceeds input bound")
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
    OperationBound(SCHEMA_SQL, 2000, 128),
))


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
        journal.finish(ticket, now=clock())
    except BaseException:
        journal.stop("INITIALIZATION_OUTCOME_UNKNOWN")
        raise


class ProductMeteredD1(MeteredD1):
    def __init__(self, binding, journal, ticket, *, clock=time.time):
        self._binding, self.journal, self.ticket, self.clock = binding, journal, ticket, clock
        self.data = journal.snapshot()["product_data"]
        self.calls = 0

    async def _operation(self, statements, reads, writes):
        self.journal.check(self.ticket, now=self.clock())
        if self.calls >= 128:
            self.journal._reject("OPERATION_LIMIT")
        self.calls += 1
        self.journal.reserve_operation(self.ticket, reads=reads, writes=writes, now=self.clock())
        bound = OperationBound(tuple(s.sql for s in statements), reads, writes)
        results = await self._dispatch(statements, self.calls, bound)
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
        self.journal.save_product_state(self.ticket, self.data, now=self.clock())

    async def batch(self, statements):
        statements = list(statements)
        if not statements or len(statements) > 64 or any(
                not isinstance(s, _Statement) or s.meter is not self for s in statements):
            self.journal._reject("UNREVIEWED_SQL")
        reads = writes = guards = 0
        upper = dict(self.data["rows"])
        try:
            for s in statements:
                array_size = _input_bound(s.params)
                tokens = _tokens(s.sql)
                cost = sql_write_bound(s.sql, guards)
                references = re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", s.sql, re.I)
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
        result = await self._operation(statements, reads, writes)
        if writes:
            await self.refresh(upper)
        return result
