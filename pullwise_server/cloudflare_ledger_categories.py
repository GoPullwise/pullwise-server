"""Category removal withdraws future selection while preserving stored labels.

The account/workspace principal and credential snapshot remain the ordinary
ledger authority. No financial record, replay, model reservation or recurring
rule is rewritten by these category operations.
"""
from __future__ import annotations

from .cloudflare_ledger_api import MAX_REVISION, _error, _revision, _timestamp, _write_guard
from .cloudflare_ledger_auth import ledger_principal
from .cloudflare_plan_limits import PlanLimitError
from .cloudflare_principal import PrincipalAuthError


def category_dto(row):
    result = {"id": row["id"], "name": row["name"], "color": row["color"],
              "revision": row["revision"], "archivedAt": row["archived_at"]}
    if row.get("deleted_at") is not None:
        result["removedAt"] = row["deleted_at"]
    return result


def _include_removed(params):
    value = params.get("includeRemoved")
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("category metadata parameter")
        value = value[0]
    if value is None:
        return False
    if value not in ("true", "false"):
        raise ValueError("category metadata parameter")
    return value == "true"


async def list_categories(*, binding, headers, params, now):
    """Normal lists hide removed rows; explicit metadata preserves history labels."""
    try:
        include_removed = _include_removed(params)
    except ValueError:
        return _error(422, "INVALID_INPUT")
    try:
        user, _, auth, validate = await ledger_principal(
            binding=binding, headers=headers, scope="categories:read", now=now)
        visible = "" if include_removed else " AND deleted_at IS NULL"
        parts = await binding.batch([*auth, binding.prepare(
            "SELECT * FROM expense_categories WHERE owner_id=?" + visible +
            " ORDER BY name,id").bind(user["id"])])
        validate([part.results for part in parts[:len(auth)]])
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    return 200, [category_dto(row) for row in parts[-1].results]


async def _snapshot(binding, item_id, headers, now):
    proof = {}
    user, _, auth, validate = await ledger_principal(
        binding=binding, headers=headers, scope="categories:write", now=now, proof=proof)
    parts = await binding.batch([*auth, binding.prepare(
        "SELECT * FROM expense_categories WHERE id=? AND owner_id=?").bind(item_id, user["id"])])
    validate([part.results for part in parts[:len(auth)]])
    found = parts[-1].results
    return user, proof, found[0] if found else None


async def remove_category(*, binding, item_id, headers, body, now):
    """Tombstone one current category using its existing authority and revision."""
    if not isinstance(body, dict) or body:
        return _error(422, "INVALID_INPUT")
    expected = _revision(headers)
    if expected is None:
        return _error(428, "PRECONDITION_REQUIRED")
    if expected < 0:
        return _error(422, "INVALID_INPUT")
    try:
        user, proof, existing = await _snapshot(binding, item_id, headers, now)
    except PrincipalAuthError as error:
        return _error(error.status, error.code)
    if existing is None:
        return _error(404, "NOT_FOUND")
    if expected != existing["revision"]:
        return _error(412, "PRECONDITION_FAILED")
    if existing["deleted_at"] is not None:
        return 204, None
    if expected >= MAX_REVISION:
        return _error(409, "REVISION_LIMIT")
    stamp = _timestamp(now)
    commands = [_write_guard(binding, proof, user["id"], now),
        binding.prepare("""UPDATE expense_categories SET deleted_at=?,
            archived_at=COALESCE(archived_at,?),revision=revision+1,updated_at=?
            WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL""").bind(
                stamp, stamp, stamp, item_id, user["id"], expected),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")]
    try:
        await binding.batch(commands)
    except PlanLimitError as error:
        return error.response()
    except Exception as error:
        # This is only a known rolled-back SQL fence. The native preview meter
        # raises a separate BudgetError for unknown outcomes; it propagates.
        if "CHECK constraint failed: ok=1" not in str(error):
            raise
        try:
            await _snapshot(binding, item_id, headers, now)
        except PrincipalAuthError as denial:
            return _error(denial.status, denial.code)
        return _error(412, "PRECONDITION_FAILED")
    return 204, None
