"""Finite native state-record/cutover acceptance with synthetic local accounts.

The generated Worker has no remote binding or provider gateway. It packages
canonical source, executes real native D1/SQLite-DO SQL and records native meta.
Bulk fixture insertion is a fixed number of scalar statements, never a load
test. The previous Projects acceptance remains evidence for its earlier source.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
SECRET = "local-only-capacity-synthetic-webhook-secret"
PRODUCTS = {"pro":{"month":"prod_capacity_pro_month","year":"prod_capacity_pro_year"},
            "max":{"month":"prod_capacity_max_month","year":"prod_capacity_max_year"}}

ENTRY = r'''
import json
import time
from workers import WorkerEntrypoint, DurableObject, Response
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import (
    BudgetJournal, BudgetError, MeteredD1, OperationBound, RequestPlan, ParameterBound, _field,
)
from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, initialize_product, migrate_product_state_records, initial_data,
    SCHEMA_VERSION, SCHEMA_FINGERPRINT,
)
from pullwise_server.cloudflare_state_records import (
    record_name, encode_record, read_record, read_record_json, read_records, USER_RECORD_BYTES,
)
from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
from pullwise_server.cloudflare_session_adapter import D1SessionTransactions
from pullwise_server.cloudflare_oauth_state_adapter import D1OAuthStates
from pullwise_server.cloudflare_ledger_auth import ledger_principal
from pullwise_server.cloudflare_principal import PrincipalAuthError
from pullwise_server.cloudflare_d1_mapping import record_billing_webhook_receipt
from pullwise_server.cloudflare_d1_batch import execute_d1_batch
from pullwise_server.account_cycle_rules import effective_user_plan
from pullwise_server.cloudflare_github_identity_http import _repo_items

OWNER, MEMBER = "usr_github_720001", "usr_github_720002"


class FixtureGitHub:
    def __init__(self, env):
        pass

    async def unseal(self, value):
        if value != "sealed:local-fixture":
            raise ValueError("unknown synthetic fixture token")
        return "local-synthetic"

    async def installations(self, token):
        return [{"id":9001,"account":{"id":720001,"login":"local-team","type":"Organization"}}]

    async def repositories(self, token, installation_id):
        assert token == "local-synthetic" and installation_id == 9001
        return [{"id":index,"full_name":"local-team/repository-"+str(index)}
                for index in range(100001,101001)]


canonical.WorkerGitHubGateway = FixtureGitHub


class ObservedD1(NativeD1):
    def __init__(self, binding):
        super().__init__(binding)
        self.metadata, self.groups, self.dispatches = [], [], 0

    async def batch(self, statements):
        self.dispatches += 1
        results = await super().batch(statements)
        values = []
        for result in results:
            meta = _field(result, "meta")
            values.append({"rowsRead": _field(meta, "rows_read"),
                           "rowsWritten": _field(meta, "rows_written"),
                           "attempts": _field(meta, "total_attempts")})
        self.metadata.extend(values)
        self.groups.append({"statements":len(values),"rowsRead":sum(item["rowsRead"] for item in values),
                            "rowsWritten":sum(item["rowsWritten"] for item in values)})
        print(json.dumps({"nativeStateCapacityMeta": values}))
        return results


def profile(identity, now):
    return {"id": identity, "githubId": identity.removeprefix("usr_github_"),
            "githubLogin": "local-" + identity.removeprefix("usr_github_"),
            "name": "Synthetic local capacity account", "createdAt": now - 1000,
            "githubAccessToken": "sealed:local-fixture", "providers": ["github"],
            "billing": {"plan": "free"},
            "retainedFixtureField": {"memo": "原样保留🙂", "flag": True}}


def owner_profile(now):
    result = profile(OWNER, now)
    result["billing"] = {"provider": "creem", "plan": "pro", "status": "active",
        "subscriptionId": "sub_capacity_local", "customerId": "cus_capacity_local",
        "interval": "month", "currentPeriodStart": now - 1000,
        "currentPeriodEnd": now + 86400}
    return result


def legacy_fixture(now):
    sessions = {}
    for identity, owner in (("ses-old-owner", OWNER), ("ses-old-member", MEMBER),
                             ("ses-old-owner-other", OWNER), ("ses-old-member-other", MEMBER)):
        sessions[identity] = {"id": identity, "userId": owner,
                              "createdAt": now - 60, "expiresAt": now + 3600}
    event = {"eventType": "subscription.paid", "eventCreated": now - 100,
             "processedAt": now - 90, "applied": True, "stale": False}
    pending = {"eventId": "evt_capacity_pending", "eventType": "subscription.paid",
               "userId": "usr_unmatched_capacity", "plan": "pro", "status": "active",
               "eventCreated": now - 50, "subscriptionId": "sub_capacity_unmatched"}
    return {"users": {OWNER: owner_profile(now), MEMBER: profile(MEMBER, now)},
            "sessions": sessions,
            "githubStates": {"state-old-login": {"kind": "login", "expiresAt": now + 600,
                "redirectTo": "/projects", "codeVerifier": "synthetic-local-verifier"},
                "state-old-install": {"kind": "install", "expiresAt": now + 600,
                    "userId": OWNER, "sessionId": "ses-old-owner"}},
            "billingEvents": {"evt_capacity_applied": event},
            "billingPendingUpdates": [pending]}


async def authenticated(binding, session, scope, now, workspace=None):
    headers = {"Cookie": "pw_session=" + session}
    if workspace:
        headers["X-Pullwise-Workspace"] = workspace
    proof = {}
    user, restrictions, statements, validate = await ledger_principal(
        binding=binding, headers=headers, scope=scope, now=now, proof=proof)
    results = await binding.batch(statements)
    validate([part.results for part in results])
    return user, proof


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if getattr(self.env, "PULLWISE_MODE", "") != "local":
            return Response.json({"error": "LOCAL_ONLY"}, status=503)
        # This synthetic config cannot target the actual preview namespace.
        fixture_name = ("local-unsafe-state-capacity-fixture" if str(request.url).endswith(
            "/_fixture/unsafe-first-cutover") else "local-state-capacity-fixture")
        return await self.env.FIXTURE_JOURNAL.get(
            self.env.FIXTURE_JOURNAL.idFromName(fixture_name)).fetch(request)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env

    async def fetch(self, request):
        path = str(request.url).split("?", 1)[0].split("://", 1)[-1].partition("/")[2]
        path = "/" + path
        now = int(time.time())
        journal = BudgetJournal(self.ctx.storage.sql, preview_product=True, product_operations=True)
        native = ObservedD1(self.env.DB)
        if path == "/_fixture/unsafe-first-cutover":
            # Separate local-only DO represents an unstarted legacy cutover
            # whose prior native outcome was unknown. No D1 fixture mutation.
            modeled = journal.snapshot()
            modeled.update(schema_ready=True,schema_version=SCHEMA_VERSION,
                           schema_fingerprint=SCHEMA_FINGERPRINT,product_data=initial_data())
            journal._save(modeled)
            journal.stop("D1_OUTCOME_UNKNOWN")
            before = journal.snapshot()
            try:
                await migrate_product_state_records(native,journal)
            except BudgetError:
                pass
            assert native.dispatches == 0
            assert journal.snapshot() == before and journal.snapshot()["stopped"] == "D1_OUTCOME_UNKNOWN"
            restored = BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            assert restored.snapshot() == before
            try:
                restored.begin_product(now=time.time())
            except BudgetError as error:
                assert str(error) == "D1_OUTCOME_UNKNOWN"
            else:
                raise AssertionError("Unknown-outcome stop was reset by state migration")
            return Response.json({"passed":True,"unknownOutcomeStopRetained":True,
                "restartRetainsUnsafeStop":True,"nativeStatements":0,"separateLocalSafetyFixture":True})
        if path == "/_fixture/first-cutover":
            await initialize_product(native, journal)
            # Model the existing legacy journal on its unchanged compiled
            # schema. Fresh initialization correctly marks empty record
            # storage ready; only fixture version markers are removed here.
            historical = journal.snapshot()
            historical.pop("state_storage_version",None)
            historical.pop("state_record_migration",None)
            journal._save(historical)
            fixture = legacy_fixture(now)
            commands = []
            for kind, value in fixture.items():
                payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                assert len(payload.encode()) <= 8192
                if kind == "githubStates":
                    command = ("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",(kind,payload,now))
                else:
                    command = ("UPDATE app_state SET payload=?,updated_at=? WHERE name=?",(payload,now,kind))
                commands.append(command)
            commands.extend([("""INSERT INTO account_entitlement_authority
                (owner_id,revision,plan,period,period_start,valid_until,dirty)
                VALUES(?,?,?,?,?,?,?)""",(owner,revision,plan,"2026-10",now-1000,now+86400,0))
                for owner,revision,plan in ((OWNER,9,"pro"),(MEMBER,4,"free"))])
            commands.append(("""INSERT INTO workspace_members
                (workspace_id,user_id,role,revision,joined_at,updated_at,invited_by_user_id)
                VALUES(?,?,?,?,?,?,?)""",(OWNER,MEMBER,"editor",7,"local","local",OWNER)))
            for event_id, state in (("evt_capacity_applied","applied"),("evt_capacity_pending","pending")):
                update = fixture["billingPendingUpdates"][0] if state == "pending" else {
                    "eventId":event_id,"eventType":"subscription.paid","userId":OWNER}
                commands.append(("""INSERT INTO billing_webhook_receipts
                    (event_id,raw_sha256,update_json,received_at,state) VALUES(?,?,?,?,?)""",
                    (event_id,"a"*64,json.dumps(update,separators=(",",":")),now,state)))
            unknown_empty = getattr(self.env,"FIXTURE_UNKNOWN_EMPTY_LEGACY","") == "1"
            if unknown_empty:
                commands.append(("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                    ("retained-empty-legacy","{}",now)))
            parameters = tuple(tuple(ParameterBound("integer",minimum=value,maximum=value)
                if type(value) is int else ParameterBound("text",max_bytes=max(1,len(value.encode())))
                for value in values) for _,values in commands)
            seed_plan = RequestPlan("local-legacy-capacity-seed","POST","/_internal/local-fixture",1,
                (OperationBound(tuple(sql for sql,_ in commands),2048,256,parameters=parameters),))
            ticket = journal.begin(seed_plan,now=time.time())
            meter = MeteredD1(native,journal,ticket,seed_plan)
            await meter.batch([meter.prepare(sql).bind(*values) for sql,values in commands])
            journal.finish(ticket,now=time.time())
            # This explicit local fixture models the old committed cardinality
            # snapshot. It changes only product data/version markers; every
            # metered setup reservation, actual counter and evidence survives.
            historical = journal.snapshot()
            modeled_data = initial_data()
            modeled_data["rows"].update(app_state=6 if unknown_empty else 5,account_entitlement_authority=2,
                workspace_members=1,billing_webhook_receipts=2)
            modeled_data.update(json={kind:len(value) for kind,value in fixture.items()},arrays=1)
            historical.update(product_data=modeled_data,product_data_verified=True)
            journal._save(historical)
            before = journal.snapshot()
            prior_groups = len(native.groups)
            await migrate_product_state_records(native,journal)
            migrated = journal.snapshot()
            cutover_groups = native.groups[prior_groups:]
            assert sum(group["rowsWritten"] > 0 for group in cutover_groups) == 1
            assert migrated["schema_version"] == SCHEMA_VERSION == 5
            assert migrated["schema_fingerprint"] == SCHEMA_FINGERPRINT
            assert migrated["state_storage_version"] == 1
            for field in ("requests","reserved_read","reserved_written","actual_read","actual_written"):
                assert migrated[field] >= before[field]
            assert migrated["actual_written"] > before["actual_written"]
            assert migrated["evidence"][:len(before["evidence"])] == before["evidence"]
            assert migrated["stopped"] is None and migrated["active"] is None
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            for kind, value in fixture.items():
                items = [(item["eventId"],item) for item in value] if isinstance(value,list) else value.items()
                for identity, record in items:
                    assert await read_record(meter,kind,identity) == record
            leftovers = await meter.prepare("SELECT name,payload FROM app_state WHERE name NOT GLOB 'record:*' ORDER BY name").all()
            assert all(json.loads(row["payload"]) in ({},[]) for row in leftovers.results)
            if unknown_empty:
                assert any(row["name"] == "retained-empty-legacy" and row["payload"] == "{}" for row in leftovers.results)
            owner, _ = await authenticated(meter,"ses-old-owner","projects:write",now)
            member, _ = await authenticated(meter,"ses-old-member","expenses:write",now,OWNER)
            assert owner["id"] == OWNER and owner["_workspace"]["role"] == "owner"
            assert member["_workspace"]["role"] == "editor" and member["_workspace"]["revision"] == 7
            revision = await meter.prepare("SELECT owner_id,revision FROM account_entitlement_authority ORDER BY owner_id").all()
            assert {row["owner_id"]:row["revision"] for row in revision.results} == {OWNER:9,MEMBER:4}
            journal.finish(ticket,now=time.time())
            return Response.json({"passed":True,"migratedRecords":10,"legacyMapsEmpty":True,
                "oldSessionsPreserved":True,"oauthStatesPreserved":True,"billingEventsPendingPreserved":True,
                "rolesAndAccountRevisionsPreserved":True,"schemaVersion":5,"schemaFingerprint":SCHEMA_FINGERPRINT,
                "atomicCutoverMutationBatches":1,"cutoverNativeBatches":cutover_groups,
                "explicitLocalSeedNativeStatements":len(commands),"explicitLocalSeedMetered":True,
                "unknownEmptyLegacyRetainedAndSnapshotCAS":unknown_empty,
                "counterBefore":{key:before[key] for key in ("requests","reserved_read","reserved_written","actual_read","actual_written")},
                "counterAfter":{key:journal.snapshot()[key] for key in ("requests","reserved_read","reserved_written","actual_read","actual_written")}})
        if path == "/_fixture/restart-no-replay":
            before = journal.snapshot()
            await migrate_product_state_records(native,journal)
            assert native.dispatches == 0 and journal.snapshot() == before
            return Response.json({"passed":True,"reconstructedJournalObject":True,"nativeStatements":0,
                "cutoverNotReplayed":True,"journalUnchanged":True})
        if path == "/_fixture/capacity":
            assert journal.snapshot()["state_storage_version"] == 1
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            rows = []
            for index in range(498):
                identity = "usr_github_" + str(730000+index)
                rows.append((record_name("users",identity),encode_record("users",identity,profile(identity,now))))
            for index in range(996):
                identity = "ses-capacity-" + str(index).zfill(4)
                owner = OWNER if index%2 == 0 else MEMBER
                rows.append((record_name("sessions",identity),encode_record("sessions",identity,
                    {"id":identity,"userId":owner,"createdAt":now,"expiresAt":now+3600})))
            for offset in range(0,len(rows),64):
                await meter.batch([meter.prepare("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)").bind(
                    name,payload,now) for name,payload in rows[offset:offset+64]])
            account = await read_record(meter,"users",OWNER)
            gateway = FixtureGitHub(self.env)
            repositories = _repo_items(await gateway.repositories("local-synthetic",9001),9001,
                {"id":720001,"login":"local-team","type":"Organization"})
            account["githubRepositoryAccess"] = {"mode":"github-app","status":"authorized",
                "authorizedUserId":OWNER,"authorizedGithubId":"720001","installationId":"9001",
                "repositoryItems":repositories,"repositoriesNeedSync":False,"authorizedAt":now}
            account["billingSubscriptionEvents"] = [{"provider":"creem","eventId":"evt_history_"+str(index),
                "eventType":"subscription.paid","eventCreated":now-index,"processedAt":now-index,
                "subscriptionId":"sub_capacity_local","customerId":"cus_capacity_local",
                "status":"active","plan":"pro","interval":"month"} for index in range(100)]
            payload = encode_record("users",OWNER,account)
            payload_bytes = len(payload.encode())
            assert 8192 < payload_bytes < USER_RECORD_BYTES
            await D1AccountTransactions(meter).stage_account_write(owner_id=OWNER,expected_revision=9,
                next_account_json=payload,now=now)
            capacity_refresh = dict(native.groups[-1])
            capacity_refresh["statementMeta"] = native.metadata[-capacity_refresh["statements"]:]
            capacity_refresh["physicalAppStateRows"] = meter.data["rows"]["app_state"]
            assert capacity_refresh["statements"] == 2 and capacity_refresh["rowsWritten"] == 0
            stored = await read_record(meter,"users",OWNER)
            assert stored == account and len(stored["githubRepositoryAccess"]["repositoryItems"]) == 1000
            assert len(stored["billingSubscriptionEvents"]) == 100
            principal, _ = await authenticated(meter,"ses-old-owner","projects:read",now)
            assert principal["githubRepositoryAccess"] == account["githubRepositoryAccess"]
            member, _ = await authenticated(meter,"ses-old-member","expenses:write",now,OWNER)
            assert member["_workspace"]["role"] == "editor"
            try:
                await authenticated(meter,"ses-old-member","projects:write",now,OWNER)
            except PrincipalAuthError as error:
                assert error.code == "ROLE_FORBIDDEN"
            else:
                raise AssertionError("Editor project management unexpectedly allowed")
            counts = await meter.prepare("""SELECT
                (SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:users:*') AS users,
                (SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:sessions:*') AS sessions,
                (SELECT COUNT(*) FROM billing_webhook_receipts) AS receipts,
                (SELECT COUNT(*) FROM d1_command_guard) AS guards""").first()
            assert {key:counts[key] for key in ("users","sessions","receipts","guards")} == {
                "users":500,"sessions":1000,"receipts":2,"guards":0}
            page = await read_records(meter,"users",limit=17)
            next_page = await read_records(meter,"users",limit=17,cursor=page[-1]["id"])
            assert len(page) == len(next_page) == 16
            assert set(item["id"] for item in page).isdisjoint(item["id"] for item in next_page)
            assert all(item["id"]>page[-1]["id"] for item in next_page)
            journal.finish(ticket,now=time.time())
            return Response.json({"passed":True,"users":500,"sessions":1000,"repositories":1000,
                "billingHistory":100,"ownerRecordBytes":payload_bytes,"bulkScalarStatements":len(rows),
                "bulkBatches":(len(rows)+63)//64,"largeAccountCASReadWrite":True,
                "postWriteNumericCardinalityRefresh":capacity_refresh,
                "oldSessionAfterCapacity":True,"editorWriteScopeAuthorizedProjectManageDenied":True,
                "keysetRequestedPageSize":17,"keysetPageSize":16,"keysetDistinctSecondPage":True,
                "receiptRowsUnchanged":True,"guardRows":0,"stopped":journal.snapshot()["stopped"]})
        if path == "/_fixture/normal-record-commands":
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            session = await D1SessionTransactions(meter).issue_session(owner_id=OWNER,
                session_id="ses-new-capacity-owner",now=now,expires_at=now+3600)
            assert session["userId"] == OWNER
            assert not await D1SessionTransactions(meter).revoke_session(owner_id=MEMBER,
                session_id="ses-new-capacity-owner",now=now)
            await authenticated(meter,"ses-new-capacity-owner","projects:read",now)
            assert await D1SessionTransactions(meter).revoke_session(owner_id=OWNER,
                session_id="ses-new-capacity-owner",now=now)
            assert await read_record(meter,"sessions","ses-new-capacity-owner") is None
            old = await D1OAuthStates(meter).consume(state_id="state-old-login",expected_kind="login",now=now)
            assert old["codeVerifier"] == "synthetic-local-verifier"
            try:
                await D1OAuthStates(meter).consume(state_id="state-old-login",expected_kind="login",now=now)
            except ValueError as error:
                assert str(error) == "OAUTH_STATE_INVALID"
            else:
                raise AssertionError("OAuth state replay unexpectedly accepted")
            await D1OAuthStates(meter).issue(state_id="state-new-capacity",record={
                "kind":"login","expiresAt":now+300,"codeVerifier":"synthetic-new"},now=now)
            assert (await D1OAuthStates(meter).consume(state_id="state-new-capacity",expected_kind="login",now=now))["codeVerifier"] == "synthetic-new"
            pending = await read_record(meter,"billingPendingUpdates","evt_capacity_pending")
            await execute_d1_batch(meter,record_billing_webhook_receipt(event_id="evt_capacity_pending",
                raw_sha256="a"*64,update_json=json.dumps(pending,separators=(",",":")),now=now))
            receipt_count = await meter.prepare("SELECT COUNT(*) AS n FROM billing_webhook_receipts").first()
            assert receipt_count["n"] == 2
            before = native.dispatches
            oversized = json.dumps({"id":OWNER,"memo":"x"*USER_RECORD_BYTES},separators=(",",":"))
            try:
                await meter.batch([meter.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=?").bind(
                    oversized,now,record_name("users",OWNER))])
            except ValueError:
                pass
            else:
                raise AssertionError("Oversized stored account unexpectedly admitted")
            assert native.dispatches == before and meter.accounted_outcome()
            await authenticated(meter,"ses-old-owner","projects:read",now)
            journal.finish(ticket,now=time.time())
            assert journal.snapshot()["stopped"] is None
            return Response.json({"passed":True,"sessionIssueRevoke":True,"wrongOwnerCannotRevoke":True,
                "preservedOAuthSingleUse":True,"newOAuthSingleUse":True,"receiptReplayNoDuplicate":True,
                "oversizeRecordRejectedBeforeNativeDispatch":True,"healthyFollowingRead":True})
        if path in {"/_fixture/billing-paid","/_fixture/billing-unpaid","/_fixture/billing-replay"}:
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            account = await read_record(meter,"users",OWNER)
            authority = await meter.prepare("SELECT revision FROM account_entitlement_authority WHERE owner_id=?").bind(OWNER).first()
            expected_status = "active" if path.endswith("billing-paid") else "unpaid"
            expected_plan = "pro" if expected_status == "active" else "free"
            # Settlement dirties account authority; the trusted entitlement
            # refresh advances its revision once more before webhook ACK.
            expected_revision = 12 if expected_status == "active" else 14
            assert account["billing"]["status"] == expected_status
            assert effective_user_plan(account,timestamp=now) == expected_plan
            assert authority["revision"] == expected_revision
            assert len(account["billingSubscriptionEvents"]) == 100
            assert len(account["githubRepositoryAccess"]["repositoryItems"]) == 1000
            assert account["retainedFixtureField"] == {"memo":"原样保留🙂","flag":True}
            guards = await meter.prepare("SELECT COUNT(*) AS n FROM d1_command_guard").first()
            assert guards["n"] == 0
            journal.finish(ticket,now=time.time())
            return Response.json({"passed":True,"storedStatus":expected_status,"effectivePlan":expected_plan,
                "accountRevision":expected_revision,"retainedRepositories":1000,"retainedBillingHistory":100,
                "opaqueRetainedFieldPreserved":True,"guardRows":0})
        if path == "/_fixture/account-recheck":
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            account = await read_record(meter,"users",OWNER)
            authority = await meter.prepare("SELECT revision FROM account_entitlement_authority WHERE owner_id=?").bind(OWNER).first()
            revision_before = authority["revision"]
            payload = encode_record("users",OWNER,account)
            assert 8192 < len(payload.encode()) < USER_RECORD_BYTES
            await D1AccountTransactions(meter).stage_account_write(owner_id=OWNER,
                expected_revision=revision_before,next_account_json=payload,now=now)
            await D1AccountTransactions(meter).refresh_account_entitlement(owner_id=OWNER,
                expected_revision=revision_before+1,now=now)
            preserved = await read_record(meter,"users",OWNER)
            assert preserved == account
            authority = await meter.prepare("SELECT revision,dirty FROM account_entitlement_authority WHERE owner_id=?").bind(OWNER).first()
            assert authority["revision"] == revision_before+2 and authority["dirty"] == 0
            owner,_ = await authenticated(meter,"ses-old-owner","projects:read",now)
            member,_ = await authenticated(meter,"ses-old-member","expenses:write",now,OWNER)
            assert owner["id"] == OWNER and member["_workspace"]["role"] == "editor"
            assert len(preserved["githubRepositoryAccess"]["repositoryItems"]) == 1000
            assert len(preserved["billingSubscriptionEvents"]) == 100
            assert effective_user_plan(preserved,timestamp=now) == "free"
            journal.finish(ticket,now=time.time())
            return Response.json({"passed":True,"ownerRecordBytes":len(payload.encode()),
                "canonicalAccountCAS":True,"byteEnvelopeAndIdentityHardeningCurrent":True,
                "oldOwnerAndMemberSessionsPreserved":True,"repositoriesRetained":1000,"historyRetained":100,
                "authorityRevisionBefore":revision_before,"authorityRevisionAfter":revision_before+2,
                "effectivePlan":"free","retainedAccountJsonUnchanged":True})
        if path == "/_fixture/page-only":
            ticket = journal.begin_product(now=time.time())
            meter = ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            first = await read_records(meter,"users",limit=17)
            second = await read_records(meter,"users",limit=17,cursor=first[-1]["id"])
            assert len(first) == len(second) == 16
            assert set(item["id"] for item in first).isdisjoint(item["id"] for item in second)
            assert all(item["id"]>first[-1]["id"] for item in second)
            assert all(sum(len(item["snapshot"].encode()) for item in page) <= 8*1024*1024 for page in (first,second))
            assert len(native.metadata) == 2 and all(item["rowsWritten"] == 0 for item in native.metadata)
            journal.finish(ticket,now=time.time())
            return Response.json({"passed":True,"requestedLimit":17,"firstPageRecords":16,"secondPageRecords":16,
                "disjointNextCursor":True,"nativeStatements":2,"nativeRowsRead":sum(item["rowsRead"] for item in native.metadata),
                "nativeRowsWritten":0,"pageByteCounts":[sum(len(item["snapshot"].encode()) for item in page) for page in (first,second)]})
        if path == "/_fixture/final-status":
            state = journal.snapshot()
            assert state["stopped"] is None and state["active"] is None
            return Response.json({"passed":True,"schemaVersion":state["schema_version"],
                "stateStorageVersion":state["state_storage_version"],"requests":state["requests"],
                "actualRead":state["actual_read"],"actualWritten":state["actual_written"],
                "reservedRead":state["reserved_read"],"reservedWritten":state["reserved_written"],
                "evidenceRows":state.get("product_evidence_rows"),"stopped":state["stopped"]})
        ticket = journal.begin_product(now=time.time())
        meter = ProductMeteredD1(native,journal,ticket)
        await meter.ensure_cardinality()
        response = await canonical._Application(self.env,meter).fetch(request)
        assert meter.accounted_outcome()
        journal.finish(ticket,now=time.time())
        return response
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--resume-billing",action="store_true",help="Continue signed billing only in preserved local fixture")
    parser.add_argument("--prior-evidence",type=Path)
    parser.add_argument("--resume-paid-event-time",type=int,help="Original synthetic signed event time from bounded offline recovery SELECT")
    parser.add_argument("--cutover-only",action="store_true",help="Final-source four-request migration/restart proof with sixth unknown-empty row; no capacity reseed")
    parser.add_argument("--verify-existing",action="store_true",help="Continue restart/status/unsafe checks on preserved successful cutover")
    parser.add_argument("--page-only",action="store_true",help="Two HTTP final pagination proof on preserved capacity fixture; no reseed")
    parser.add_argument("--account-recheck",action="store_true",help="Two HTTP final-source large account CAS/auth proof on preserved capacity fixture")
    args = parser.parse_args()
    if sum((args.cutover_only,args.resume_billing,args.verify_existing,args.page_only,args.account_recheck)) > 1:
        raise SystemExit("Select only one fresh or preserved fixture mode")
    resumed = args.resume_billing or args.verify_existing or args.page_only or args.account_recheck
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")):
        raise SystemExit("Run directory must be in /workspace")
    prior = None
    if resumed:
        if args.prior_evidence is None or args.resume_billing and args.resume_paid_event_time is None:
            raise SystemExit("Resume requires prior evidence and exact original synthetic event time")
        prior = json.loads(args.prior_evidence.read_text())
        assert prior["localOnly"] and prior["realAccounts"] == 0 and prior["runtimeStopped"]
        if args.resume_billing:
            assert prior["cases"][-1]["path"] == "/webhooks/creem" and prior["cases"][-1]["result"] == {"received":True}
    directory.mkdir(parents=True, exist_ok=resumed)
    source = directory / "src"
    source.mkdir(exist_ok=resumed)
    (source / "entry.py").write_text(ENTRY, encoding="utf-8")
    if not resumed:
        shutil.copy2(WORKER / "src/entry.py", source / "application_entry.py")
        shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(WORKER / "python_modules", directory / "python_modules",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    elif args.verify_existing or args.page_only or args.account_recheck:
        shutil.copy2(WORKER / "src/entry.py",source / "application_entry.py")
        shutil.copytree(ROOT / "pullwise_server",source / "pullwise_server",dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    config = {"name":"pullwise-state-capacity-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"local","PULLWISE_D1_ACCESS_ENABLED":"1","PULLWISE_APP_URL":"https://local.example.test",
            "PULLWISE_ALLOWED_ORIGINS":f"http://127.0.0.1:{args.port}",
            "PULLWISE_CREEM_PRODUCT_IDS_JSON":json.dumps(PRODUCTS,separators=(",",":")),
            "PULLWISE_CREEM_WEBHOOK_SECRET":SECRET,
            "FIXTURE_UNKNOWN_EMPTY_LEGACY":"1" if args.cutover_only else "0",
            "PULLWISE_JEV_ENABLED":"0","PULLWISE_JEV_QUALITY_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"state-capacity-local-only",
            "database_id":"00000000-0000-0000-0000-000000000004","remote":False}],
        "durable_objects":{"bindings":[{"name":"FIXTURE_JOURNAL","class_name":"FixtureJournal"}]},
        "migrations":[{"tag":"local-fixture-v1","new_sqlite_classes":["FixtureJournal"]}]}
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config,indent=2)+"\n")
    bundle = Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or hashlib.sha256(bundle.read_bytes()).hexdigest() != BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache is missing or invalid")
    binary = WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec "+shlex.quote(str(binary))+' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env = {**os.environ,"XDG_CACHE_HOME":"/workspace/.cache","XDG_CONFIG_HOME":"/workspace/.config",
        "UV_CACHE_DIR":"/workspace/.cache/uv","UV_PYTHON_INSTALL_DIR":"/workspace/.python",
        "WRANGLER_SEND_METRICS":"false","MINIFLARE_WORKERD_PATH":str(wrapper),
        "WRANGLER_LOG_PATH":str(directory / "wrangler-debug.log")}
    source_files = sorted((source / "pullwise_server").glob("*.py")) + [source / "application_entry.py"]
    source_manifest = [(str(path.relative_to(source)),hashlib.sha256(path.read_bytes()).hexdigest())
                       for path in source_files]
    evidence = {"passed":False,"localOnly":True,"syntheticAccountsOnly":True,"realAccounts":0,
        "nativePythonFFI":True,"nativeDurableObjectSQLite":True,"remoteRequests":0,
        "remoteD1Operations":0,"realProviderRequests":0,"realPayments":0,"clientRetries":0,
        "localHttpCap":4 if args.cutover_only else 16,"cutoverOnly":args.cutover_only,
        "sourceSha256":{str(path.relative_to(source)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_files if "state_record" in path.name or "preview_budget" in path.name
            or "session_adapter" in path.name or "principal" in path.name or "account_adapter" in path.name
            or "d1_mapping" in path.name or path.name == "application_entry.py"},
        "runtimeBundleSha256":BUNDLE_SHA256,"workerdSha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
        "canonicalSourceTreeSha256":hashlib.sha256(json.dumps(source_manifest,separators=(",",":")).encode()).hexdigest(),
        "canonicalSourceFileCount":len(source_manifest),
        "previousProjectsBaseline":"docs/validation/projects-subscriptions-native-2026-10-06.json",
        "previousReleaseBaseline":"docs/validation/projects-preview-follow-up-release-2026-10-06.json",
        "previousPartialRealAccountBaseline":"docs/validation/projects-two-real-users-baseline-2026-10-06.json",
        "previousRealAccountBaselineIsFinalRecordAcceptance":False}
    if prior:
        if args.resume_billing:
            assert prior["canonicalSourceTreeSha256"] == evidence["canonicalSourceTreeSha256"]
        evidence.update(resumedPreservedLocalFixture=True,firstSignedPaidAcknowledgedInPriorPhase=args.resume_billing,
            priorEvidence=str(args.prior_evidence),priorFailure=prior.get("failure"),
            offlineSyntheticRecoverySelects=2 if args.resume_billing else 0,fixtureSourceUpdatedOnly=args.resume_billing,
            priorCanonicalSourceTreeSha256=prior["canonicalSourceTreeSha256"])
        if args.verify_existing or args.page_only or args.account_recheck:
            evidence["localHttpCap"] = prior["localHttpRequestsAttempted"] + (3 if args.verify_existing else 2)
            evidence["continuationOnly"] = "cutover-verification" if args.verify_existing else "pagination" if args.page_only else "large-account-cas"
    results = list(prior["cases"]) if prior else []
    process, attempted, processes_started = None, prior["localHttpRequestsAttempted"] if prior else 0, prior["runtimeProcessesStarted"] if prior else 0
    log_path = directory / "runtime.log"
    try:
        with log_path.open("a" if resumed else "w") as log:
            command = [str(WORKER / ".venv/bin/pywrangler"),"dev","--local",
                "--config",str(config_path),"--ip","127.0.0.1","--port",str(args.port),
                "--inspector-port",str(args.port+1000),"--persist-to",str(directory / "state")]
            def start_runtime():
                nonlocal process, processes_started
                startup_log_offset = log_path.stat().st_size
                process = subprocess.Popen(command,cwd=WORKER,env=env,stdout=log,
                    stderr=subprocess.STDOUT,start_new_session=True)
                processes_started += 1
                deadline = time.monotonic()+120
                while time.monotonic()<deadline:
                    if process.poll() is not None:
                        raise AssertionError("Native capacity Worker startup failed")
                    try:
                        # TCP alone can see a still-closing previous listener.
                        # Require this process's Wrangler readiness log first.
                        if "Ready on http://127.0.0.1:"+str(args.port) not in log_path.read_bytes()[startup_log_offset:].decode(errors="replace"):
                            time.sleep(.2)
                            continue
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2):
                            return
                    except OSError:
                        time.sleep(.2)
                raise AssertionError("Native capacity startup timed out")
            start_runtime()
            opener = build_opener(ProxyHandler({}),NoRedirect())
            def call(path,method="POST",body=None,expected=200,session=None,signature=None):
                nonlocal attempted
                attempted += 1
                assert attempted <= evidence["localHttpCap"]
                headers = {"Content-Type":"application/json","Origin":f"http://127.0.0.1:{args.port}"}
                if session:
                    headers["Cookie"] = "pw_session="+session
                if signature:
                    headers["creem-signature"] = signature
                request = Request(f"http://127.0.0.1:{args.port}"+path,method=method,data=body,headers=headers)
                try:
                    response = opener.open(request,timeout=90)
                except HTTPError as error:
                    response = error
                with response:
                    payload = json.loads(response.read())
                    assert response.status == expected,(path,response.status,payload)
                    results.append({"path":path,"status":response.status,"result":payload})
                    return payload
            if not resumed:
                assert call("/_fixture/first-cutover")["passed"]
                os.killpg(process.pid,signal.SIGTERM)
                process.wait(timeout=15)
                # A new native workerd process opens the same local SQLite files.
                # No source recopy, reseed, journal reset or provider call occurs.
                start_runtime()
                followup = ("restart-no-replay",) if args.cutover_only else ("restart-no-replay","capacity","normal-record-commands","restart-no-replay")
                for path in followup:
                    assert call("/_fixture/"+path)["passed"]
            if args.verify_existing:
                assert call("/_fixture/restart-no-replay")["passed"]
            if args.page_only:
                assert call("/_fixture/page-only")["passed"]
            if args.account_recheck:
                assert call("/_fixture/account-recheck")["passed"]
            if not args.cutover_only and not args.verify_existing and not args.page_only and not args.account_recheck:
                event_time = args.resume_paid_event_time if args.resume_billing else int(time.time())
                def webhook(identifier,event_type,created):
                    iso = lambda seconds: datetime.fromtimestamp(seconds,timezone.utc).isoformat().replace("+00:00","Z")
                    return json.dumps({"id":identifier,"eventType":event_type,"created_at":created,
                        "object":{"id":"sub_capacity_local","status":"unpaid" if event_type.endswith("unpaid") else "active",
                            "customer":{"id":"cus_capacity_local"},"product":{"id":PRODUCTS["pro"]["month"],"billing_period":"every-month"},
                            "current_period_start_date":iso(event_time-60),"current_period_end_date":iso(event_time+3600),
                            "metadata":{"userId":"usr_github_720001"}}},separators=(",",":")).encode()
                paid = webhook("evt_capacity_new_paid","subscription.paid",event_time)
                unpaid = webhook("evt_capacity_new_unpaid","subscription.unpaid",event_time+1)
                for raw,status_path,expected_state in ((paid,"billing-paid","applied"),
                        (unpaid,"billing-unpaid","applied"),(paid,"billing-replay","duplicate")):
                    signature = hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
                    if not (args.resume_billing and status_path == "billing-paid"):
                        # Public ACK intentionally omits internal settlement state.
                        assert call("/webhooks/creem",body=raw,signature=signature) == {"received":True}
                    assert call("/_fixture/"+status_path)["passed"]
                call("/api/v1/projects",method="GET",session="ses-old-owner")
                call("/billing/checkout-sessions",body=b" "*8193,expected=413,session="ses-old-owner")
                call("/api/v1/projects",method="GET",session="ses-old-owner")
            assert call("/_fixture/final-status")["passed"]
            if not args.page_only and not args.account_recheck:
                assert call("/_fixture/unsafe-first-cutover")["passed"]
            evidence.update(passed=True,cases=results)
    except Exception as error:
        evidence["failure"] = str(error)
        raise
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait(timeout=5)
        metadata = []
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeStateCapacityMeta":'):
                metadata.extend(json.loads(line)["nativeStateCapacityMeta"])
        complete = bool(metadata) and all(type(item.get(name)) is int and item[name] >= 0
            for item in metadata for name in ("rowsRead","rowsWritten"))
        final = next((case["result"] for case in reversed(results) if case["path"] == "/_fixture/final-status"),None)
        matches = complete and final is not None and sum(item["rowsRead"] for item in metadata) == final["actualRead"] and sum(
            item["rowsWritten"] for item in metadata) == final["actualWritten"]
        if evidence["passed"] and not matches:
            evidence.update(passed=False,failure="Native metadata missing, invalid or different from cumulative journal actual totals")
        evidence.update(localHttpRequestsAttempted=attempted,localHttpRequestsCompleted=len(results),
            nativeStatements=len(metadata),nativeRowsRead=sum(item["rowsRead"] for item in metadata),
            nativeRowsWritten=sum(item["rowsWritten"] for item in metadata),
            nativeAttemptsObserved=sorted(set(item["attempts"] for item in metadata),key=str),
            nativeMetadataComplete=complete,nativeMetaMatchesCumulativeJournal=matches,
            runtimeProcessesStarted=processes_started,actualProcessRestarts=max(0,processes_started-1),
            cases=results,runtimeStopped=process is None or process.poll() is not None)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted",
            "nativeStatements","nativeRowsRead","nativeRowsWritten","actualProcessRestarts",
            "nativeMetaMatchesCumulativeJournal","runtimeStopped")},ensure_ascii=False))
    if not evidence["passed"]:
        raise SystemExit("Native capacity acceptance failed; evidence saved")


if __name__ == "__main__":
    main()
