"""A retained missed occurrence is also its bounded, durable notification.

Email is attempted once only, after a compare-and-set claim. If no current
verified email is usable, delivery fails, or execution stops after that claim,
the saved occurrence remains visible in the recipient's Pullwise inbox. No
provider errors, email addresses or private grants appear in public responses.
"""
from __future__ import annotations

import json

from .cloudflare_email_gateway import EmailDeliveryError
from .cloudflare_ledger_expenses import _decimal_amount
from .cloudflare_principal import (
    PrincipalAuthError, _cookie_sessions, _header, _principal,
    _resource_auth_snapshot, has_verified_email_identity,
)
from .cloudflare_state_records import record_name

RESOURCE = "/api/v1/recurring-expense-notifications"
MAX_NOTIFICATIONS = 100
_READABLE = """(n.recipient_user_id=n.owner_id OR
    (m.removed_at IS NULL AND m.role IN ('admin','editor','viewer')))"""
_VISIBLE_TARGET = """(r.target_kind='shared' OR EXISTS(
    SELECT 1 FROM ledger_projects p WHERE p.owner_id=n.owner_id
        AND p.id=r.project_id AND p.deleted_at IS NULL))"""
_SOURCE = """FROM expense_recurring_pending n
    JOIN expense_recurring_rules r ON r.owner_id=n.owner_id AND r.id=n.rule_id
    JOIN app_state o ON o.name='record:users:'||n.owner_id
    LEFT JOIN workspace_members m ON m.workspace_id=n.owner_id
        AND m.user_id=n.recipient_user_id"""


def _user(payload, identifier):
    saved = json.loads(payload)
    return saved if isinstance(saved, dict) and saved.get("id") == identifier else None


async def notify_recurring_failure(*, binding, row, period_key, now, email_gateway=None):
    """Notify only a newly retained failure; overflow occurrences never call us.

    The recipient belongs to the original failure, not an actor adopted by a
    later plan edit. Recheck that account and its current ledger access before
    any delivery. Pending/sending states are inbox-visible, so an interruption
    neither loses the operation nor triggers a duplicate provider attempt.
    """
    saved = await binding.prepare("""SELECT n.*,r.target_kind,r.project_id,
        o.payload AS owner_snapshot,a.payload AS recipient_snapshot,
        m.role AS recipient_role,m.revision AS recipient_revision
        """ + _SOURCE + """
        JOIN app_state a ON a.name='record:users:'||n.recipient_user_id
        WHERE n.rule_id=? AND n.owner_id=? AND n.period_key=?
            AND n.notification_state='pending' AND r.status!='canceled'
            AND """ + _READABLE + " AND " + _VISIBLE_TARGET).bind(
                row["id"], row["owner_id"], period_key).first()
    if saved is None:
        return None
    recipient = _user(saved["recipient_snapshot"], saved["recipient_user_id"])
    owner = _user(saved["owner_snapshot"], saved["owner_id"])
    if recipient is None or owner is None:
        return None
    checks = [
        "EXISTS(SELECT 1 FROM app_state a WHERE a.name=? AND a.payload=?)",
        "EXISTS(SELECT 1 FROM app_state o WHERE o.name=? AND o.payload=?)",
        """EXISTS(SELECT 1 FROM expense_recurring_rules r WHERE r.id=?
            AND r.owner_id=? AND r.status!='canceled' AND
            (r.target_kind='shared' OR EXISTS(SELECT 1 FROM ledger_projects p
                WHERE p.owner_id=r.owner_id AND p.id=r.project_id
                    AND p.deleted_at IS NULL)))""",
    ]
    values = [record_name("users", saved["recipient_user_id"]), saved["recipient_snapshot"],
        record_name("users", saved["owner_id"]), saved["owner_snapshot"],
        saved["rule_id"], saved["owner_id"]]
    if saved["recipient_user_id"] != saved["owner_id"]:
        checks.append("""EXISTS(SELECT 1 FROM workspace_members m WHERE m.workspace_id=?
            AND m.user_id=? AND m.role=? AND m.revision=? AND m.removed_at IS NULL)""")
        values.extend([saved["owner_id"], saved["recipient_user_id"],
            saved["recipient_role"], saved["recipient_revision"]])
    claimed = await binding.batch([binding.prepare("""UPDATE expense_recurring_pending
        SET notification_state='sending' WHERE rule_id=? AND owner_id=? AND period_key=?
            AND recipient_user_id=? AND notification_state='pending' AND """ +
        " AND ".join(checks) + " RETURNING rule_id").bind(
            saved["rule_id"], saved["owner_id"], saved["period_key"], saved["recipient_user_id"], *values)])
    if not claimed[0].results:
        return None
    email_gateway = email_gateway or getattr(binding, "email_gateway", None)
    delivery = "inbox"
    if (email_gateway is not None and getattr(email_gateway, "configured", False)
            and has_verified_email_identity(recipient)):
        template = json.loads(saved["template_json"])
        try:
            await email_gateway.send_recurring_failure(recipient["email"],
                scheduled_on=saved["scheduled_on"],
                amount=_decimal_amount(template["amount_minor"], template["currency"]),
                currency=template["currency"])
        except EmailDeliveryError:
            # A failed/unknown send is not retried. The durable inbox is the
            # fallback channel and the manual action remains available.
            pass
        else:
            delivery = "email"
    await binding.batch([binding.prepare("""UPDATE expense_recurring_pending
        SET notification_state=? WHERE rule_id=? AND owner_id=? AND period_key=?
            AND notification_state='sending'""").bind(delivery,
                saved["rule_id"], saved["owner_id"], saved["period_key"])])
    return delivery


async def handle_recurring_notification_request(*, binding, method, path, headers, now):
    if path != RESOURCE:
        return None
    if method != "GET":
        return 405, {"error": {"code": "METHOD_NOT_ALLOWED"}}
    try:
        if (_header(headers, "Authorization") or _header(headers, "X-Pullwise-Api-Key")
                or not _cookie_sessions(headers)):
            return 403, {"error": {"code": "COOKIE_SESSION_REQUIRED"}}
        actor, restrictions = await _principal(binding, headers, scope="expenses:read", now=now)
        proof = {}
        auth, validate = _resource_auth_snapshot(binding, headers, actor, restrictions,
            now, "expenses:read", proof)
        parts = await binding.batch([*auth, binding.prepare("""SELECT n.*,r.target_kind,
            r.project_id,o.payload AS owner_snapshot """ + _SOURCE + """
            WHERE n.recipient_user_id=?
                AND n.notification_state IN ('pending','sending','inbox')
                AND r.status!='canceled' AND """ + _READABLE + " AND " + _VISIBLE_TARGET + """
            ORDER BY n.created_at,n.rule_id,n.period_key LIMIT 101""").bind(actor["id"])])
        validate([part.results for part in parts[:len(auth)]])
    except PrincipalAuthError as error:
        return error.status, {"error": {"code": error.code}}
    items = []
    for saved in parts[-1].results[:MAX_NOTIFICATIONS]:
        owner = _user(saved["owner_snapshot"], saved["owner_id"])
        if owner is None:
            continue
        template = json.loads(saved["template_json"])
        target = {"kind": saved["target_kind"]}
        if saved["target_kind"] == "project":
            target["projectId"] = saved["project_id"]
        items.append({"id": saved["rule_id"] + ":" + saved["period_key"],
            "ruleId": saved["rule_id"], "periodKey": saved["period_key"],
            "scheduledOn": saved["scheduled_on"], "workspaceId": saved["owner_id"],
            "workspaceName": str(owner.get("name") or owner.get("githubLogin") or "Personal") + " ledger",
            "target": target, "amount": _decimal_amount(template["amount_minor"], template["currency"]),
            "currency": template["currency"], "purpose": template["purpose"],
            "failedCode": saved["failed_code"], "createdAt": saved["created_at"]})
    return 200, {"items": items, "hasMore": len(parts[-1].results) > MAX_NOTIFICATIONS}
