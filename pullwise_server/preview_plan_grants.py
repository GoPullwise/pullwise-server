"""Bounded preview operator entitlements, separate from payment provider facts.

Only the preview coordinator invokes this command through its existing metered
singleton ticket. Public HTTP handlers never accept grant instructions.
"""
from __future__ import annotations

import json
import re

from .cloudflare_d1_batch import execute_d1_batch
from .cloudflare_state_records import decode_record, encode_record, read_record_json
from . import cloudflare_d1_mapping as mapping

MAX_GRANT_SECONDS = 31 * 24 * 60 * 60
MAX_SAFE_INTEGER = 9007199254740991
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class PreviewPlanGrantError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def preview_grants_enabled(env):
    return (getattr(env, "PULLWISE_MODE", "") == "preview"
        and str(getattr(env, "PULLWISE_D1_ACCESS_ENABLED", "0")) == "1"
        and str(getattr(env, "PULLWISE_PREVIEW_PRODUCT_ENABLED", "0")) == "1"
        and getattr(env, "PULLWISE_APP_URL", "") == "https://preview.pull-wise.com"
        and getattr(env, "PULLWISE_CREEM_API_BASE_URL", "") == "https://test-api.creem.io")


def _bounded_text(value, maximum):
    return (isinstance(value, str) and bool(value) and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and len(value.encode("utf-8")) <= maximum)


def preview_plan_grant(user):
    grant = user.get("previewPlanGrant") if isinstance(user, dict) else None
    if not isinstance(grant, dict):
        return None
    if (grant.get("source") != "operator" or grant.get("environment") != "preview"
            or grant.get("plan") != "max"
            or not isinstance(grant.get("grantId"), str)
            or not _TOKEN.fullmatch(grant["grantId"])
            or not _bounded_text(grant.get("reason"), 500)
            or any(type(grant.get(key)) is not int or not 0 <= grant[key] <= MAX_SAFE_INTEGER
                   for key in ("startsAt", "expiresAt", "issuedAt"))
            or not 0 < grant["expiresAt"] - grant["startsAt"] <= MAX_GRANT_SECONDS):
        return None
    return grant


def active_preview_plan_grant(user, *, now):
    grant = preview_plan_grant(user)
    return grant if grant and grant["startsAt"] <= now < grant["expiresAt"] else None


def parse_preview_plan_grant_request(request_json, *, now):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate grant field")
            result[key] = value
        return result

    try:
        if not isinstance(request_json, str) or len(request_json.encode("utf-8")) > 8192:
            raise ValueError("grant request bound")
        request = json.loads(request_json, object_pairs_hook=unique)
        if not isinstance(request, dict) or set(request) != {
                "grantId", "ownerId", "githubLogin", "email", "plan", "startsAt", "expiresAt", "reason"}:
            raise ValueError("grant request fields")
        if (request["plan"] != "max"
                or any(not isinstance(request[key], str) or not _TOKEN.fullmatch(request[key])
                       for key in ("grantId", "ownerId"))
                or not _bounded_text(request["githubLogin"], 100)
                or not _bounded_text(request["email"], 320) or "@" not in request["email"]
                or not _bounded_text(request["reason"], 500)
                or any(type(request[key]) is not int or not 0 <= request[key] <= MAX_SAFE_INTEGER
                       for key in ("startsAt", "expiresAt"))
                or not 0 < request["expiresAt"] - request["startsAt"] <= MAX_GRANT_SECONDS
                or request["startsAt"] > now + MAX_GRANT_SECONDS):
            raise ValueError("invalid grant request")
        return request
    except (ValueError, TypeError, UnicodeError):
        raise PreviewPlanGrantError("INVALID_PREVIEW_PLAN_GRANT") from None


def _grant_fields(request):
    return {key: request[key] for key in ("grantId", "plan", "startsAt", "expiresAt", "reason")}


async def apply_preview_plan_grant(*, binding, request_json, now):
    """One exact identity, atomic account/audit/projection, read-only replay."""
    request = parse_preview_plan_grant_request(request_json, now=now)
    owner = request["ownerId"]
    snapshot = await read_record_json(binding, "users", owner)
    if snapshot is None:
        raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_IDENTITY_MISMATCH")
    user = decode_record("users", owner, snapshot)
    if user.get("githubLogin") != request["githubLogin"] or user.get("email") != request["email"]:
        raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_IDENTITY_MISMATCH")
    event_id = "preview-plan-grant:" + request["grantId"]
    audit_snapshot = await read_record_json(binding, "billingEvents", event_id)
    existing = preview_plan_grant(user)
    fields = _grant_fields(request)
    identity = {"githubLogin": request["githubLogin"], "email": request["email"]}
    if audit_snapshot is not None:
        audit = decode_record("billingEvents", event_id, audit_snapshot)
        if (audit.get("eventType") != "operator.preview_plan_granted"
                or audit.get("ownerId") != owner or audit.get("identity") != identity
                or audit.get("grant") != existing or existing is None
                or any(existing.get(key) != value for key, value in fields.items())):
            raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_CONFLICT")
        return {"applied": False, "replayed": True, "grantId": request["grantId"],
                "plan": "max", "expiresAt": request["expiresAt"]}
    if request["expiresAt"] <= now:
        raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_EXPIRED")
    if "previewPlanGrant" in user and (existing is None or existing["expiresAt"] > now):
        raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_CONFLICT")
    authority = await binding.prepare("SELECT revision FROM account_entitlement_authority WHERE owner_id=?").bind(owner).first()
    if authority is None or type(authority["revision"]) is not int or authority["revision"] < 1:
        raise PreviewPlanGrantError("PREVIEW_PLAN_GRANT_AUTHORITY_MISSING")
    revision = authority["revision"]
    grant = {**fields, "issuedAt": now, "source": "operator", "environment": "preview"}
    next_json = encode_record("users", owner, {**user, "previewPlanGrant": grant})
    audit_json = encode_record("billingEvents", event_id, {
        "eventType": "operator.preview_plan_granted", "eventCreated": now,
        "processedAt": now, "applied": True, "grant": grant, "identity": identity})
    # The standard staged command dirties/increments authority. Refresh it in
    # this same atomic batch, leaving no committed partially projected grant.
    commands = mapping.stage_account_event(owner_id=owner, expected_revision=revision,
        account_snapshot=snapshot, next_account_json=next_json, event_id=event_id,
        event_record_json=audit_json, now=now)
    commands = commands[:-1] + mapping.refresh_account_entitlement(owner_id=owner,
        expected_revision=revision + 1, account_snapshot=next_json, now=now)
    await execute_d1_batch(binding, commands)
    return {"applied": True, "replayed": False, "grantId": request["grantId"],
            "plan": "max", "expiresAt": request["expiresAt"]}
