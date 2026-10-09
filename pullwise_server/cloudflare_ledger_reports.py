"""Shared filters for ledger detail and reports."""
from __future__ import annotations

import re
from datetime import date
from typing import Mapping
import csv
import io


EXPORT_COLUMNS = ("id", "target_kind", "project_id", "occurred_on", "amount_minor",
                  "currency", "category_id", "purpose", "note", "quantity_decimal", "unit")
EXPORT_HEADER = ("id", "target", "projectId", "occurredOn", "amountMinor", "currency",
                 "categoryId", "purpose", "note", "quantity", "unit")
EXPORT_PAGE_SIZE = 250

# Archived projects retain ordinary financial history. Removed projects retain
# their records only in the background and must not contribute to any public
# detail, report or CSV page, including requests without an explicit project.
VISIBLE_EXPENSE_TARGET_SQL = """AND (expenses.target_kind='shared' OR
    (expenses.target_kind='project' AND EXISTS(SELECT 1 FROM ledger_projects AS visible_project
        WHERE visible_project.owner_id=expenses.owner_id AND visible_project.id=expenses.project_id
            AND visible_project.deleted_at IS NULL)))"""


class CsvExport:
    """A page-at-a-time CSV body. The Worker turns chunks into a ReadableStream."""

    def __init__(self, first_rows, next_page):
        self.first_rows = first_rows
        self.next_page = next_page

    async def chunks(self):
        output = io.StringIO(newline="")
        csv.writer(output).writerow(EXPORT_HEADER)
        yield output.getvalue()
        rows = self.first_rows
        while rows:
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            for row in rows:
                writer.writerow([_csv_cell(row[column]) for column in EXPORT_COLUMNS])
            yield output.getvalue()
            if len(rows) < EXPORT_PAGE_SIZE:
                break
            last = rows[-1]
            rows = await self.next_page(last["occurred_on"], last["id"])

from .cloudflare_ledger_api import _error, _page_inputs, _param, _valid_resource_id
from .cloudflare_ledger_auth import ledger_principal, target_allowed
from .cloudflare_principal import PrincipalAuthError
from .ledger_money_totals import AGGREGATE_SQL, aggregate_minor, public_minor


def expense_filter(params: Mapping[str, object], *, paged: bool = False):
    target = _param(params, "target") or "all"
    project_id = _param(params, "projectId")
    category_id = _param(params, "categoryId")
    currency = _param(params, "currency")
    start, end = _param(params, "from"), _param(params, "to")
    if target not in {"all", "project", "shared"} or (project_id and target == "shared"):
        raise ValueError("target")
    if any(value and not _valid_resource_id(value) for value in (project_id, category_id)):
        raise ValueError("resource")
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency")
    for value in (start, end):
        if value and (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
                      or date.fromisoformat(value).isoformat() != value):
            raise ValueError("date")
    if start and end and start >= end:
        raise ValueError("range")
    clauses, values = [VISIBLE_EXPENSE_TARGET_SQL], []
    if target != "all":
        clauses.append("AND target_kind=?")
        values.append(target)
    if project_id:
        clauses.append("AND project_id=?")
        values.append(project_id)
    if category_id:
        clauses.append("AND category_id=?")
        values.append(category_id)
    if start:
        clauses.append("AND occurred_on>=?")
        values.append(start)
    if end:
        clauses.append("AND occurred_on<?")
        values.append(end)
    if currency:
        clauses.append("AND currency=?")
        values.append(currency)
    if paged:
        limit, cursor = _page_inputs(params)
        return " ".join(clauses), values, limit, cursor
    return " ".join(clauses), values


def restriction_filter(restrictions: dict):
    project_ids = restrictions.get("projectIds")
    shared = restrictions.get("shared") is True
    if project_ids is None:
        return ("", []) if shared else ("AND target_kind='project'", [])
    if not project_ids:
        return ("AND target_kind='shared'", []) if shared else ("AND 0", [])
    slots = ",".join("?" for _ in project_ids)
    return (f"AND (project_id IN ({slots}) OR target_kind='shared')", project_ids) if shared else (
        f"AND project_id IN ({slots})", project_ids)


def _csv_cell(value):
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n")
    return "'" + text if stripped.startswith(("=", "+", "-", "@")) else text


async def handle_report_request(*, binding, method, path, headers, params, now):
    is_export = path == "/api/v1/expenses/export"
    name = path.removeprefix("/api/v1/reports/")
    if not is_export and name not in {"summary", "timeseries", "categories"}:
        return None
    if method != "GET":
        return _error(405, "METHOD_NOT_ALLOWED")
    try:
        where, values = expense_filter(params)
        bucket = _param(params, "bucket") or "day"
        if name == "timeseries" and bucket not in {"day", "month"}:
            raise ValueError("bucket")
    except ValueError:
        return _error(422, "INVALID_INPUT")
    scope = "expenses:read" if is_export else "reports:read"
    try:
        user, restrictions, auth, validate = await ledger_principal(
            binding=binding, headers=headers, scope=scope, now=now)
        if (_param(params, "target") == "shared" and not restrictions.get("shared")) or (
                _param(params, "projectId") and not target_allowed(
                    restrictions, "project", _param(params, "projectId"))):
            return _error(403, "TARGET_FORBIDDEN")
        restricted, restricted_values = restriction_filter(restrictions)
        if is_export:
            base_sql = ("SELECT * FROM expenses WHERE owner_id=? AND deleted_at IS NULL " + where +
                        " " + restricted)
            sql = base_sql + f" ORDER BY occurred_on,id LIMIT {EXPORT_PAGE_SIZE}"
        else:
            bucket_sql = "substr(occurred_on,1,7)" if bucket == "month" else "occurred_on"
            if name == "summary":
                dimensions = "target_kind,project_id,currency"
                projection = "target_kind,project_id,NULL AS category_id,NULL AS bucket,currency"
            elif name == "timeseries":
                dimensions = f"target_kind,project_id,{bucket_sql},currency"
                projection = f"target_kind,project_id,NULL AS category_id,{bucket_sql} AS bucket,currency"
            else:
                dimensions = "target_kind,project_id,category_id,currency"
                projection = "target_kind,project_id,category_id,NULL AS bucket,currency"
            sql = (f"SELECT {projection},{AGGREGATE_SQL} FROM expenses "
                   "WHERE owner_id=? AND deleted_at IS NULL " + where + " " + restricted +
                   f" GROUP BY {dimensions}")
        parts = await binding.batch([*auth, binding.prepare(sql).bind(user["id"], *values, *restricted_values)])
        validate([part.results for part in parts[:len(auth)]])
        rows = parts[-1].results
    except PrincipalAuthError as exc:
        return _error(exc.status, exc.code)
    if is_export:
        async def next_page(after_date, after_id):
            page_sql = (base_sql + " AND (occurred_on>? OR (occurred_on=? AND id>?))" +
                        f" ORDER BY occurred_on,id LIMIT {EXPORT_PAGE_SIZE}")
            commands = [*auth, binding.prepare(page_sql).bind(user["id"], *values,
                *restricted_values, after_date, after_date, after_id)]
            page = await binding.batch(commands)
            validate([part.results for part in page[:len(auth)]])
            return page[-1].results
        return 200, CsvExport(rows, next_page)
    groups = {}
    for row in rows:
        target = row["target_kind"]
        project_id = row["project_id"] if target == "project" else None
        if name == "summary":
            keys = [(target, project_id, None, None, row["currency"]),
                    ("account", None, None, None, row["currency"])]
            if target == "project":
                keys.append(("project", None, None, None, row["currency"]))
        elif name == "timeseries":
            time_bucket = row["bucket"]
            keys = [(target, project_id, None, time_bucket, row["currency"])]
        else:
            keys = [(target, project_id, row["category_id"], None, row["currency"])]
        for key in keys:
            groups[key] = groups.get(key, 0) + aggregate_minor(row)
    return 200, {"groups": [{"target": target, "projectId": project_id,
        "categoryId": category_id, "bucket": time_bucket, "currency": currency,
        "amountMinor": public_minor(amount)} for (target, project_id, category_id, time_bucket, currency), amount
        in sorted(groups.items(), key=lambda item: tuple(part or "" for part in item[0]))]}
